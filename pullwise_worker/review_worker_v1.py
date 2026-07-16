from __future__ import annotations

import ast
import base64
import copy
import fnmatch
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
import random
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import zipfile
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import __version__
from ._main_part_01_bootstrap import (
    PullwiseRequestError,
    REPOSITORY_MIRROR_CACHE_DIR_NAME,
    worker_machine_metrics_payload,
    worker_memory_payload,
)
from .agentic_execution import build_execution_capabilities
from .codex_sdk_runtime import (
    append_text_no_follow,
    CodexRuntimeResources,
    CodexTokenUsage,
    path_has_symlink_component,
    read_text_no_follow,
    TurnEventScope,
    require_identifier,
    run_bounded_call,
)
from .current_run_eta import CurrentRunEstimator

try:
    import fcntl
except ImportError:  # pragma: no cover - runtime is Linux only; import stays testable elsewhere.
    fcntl = None

PROTOCOL_VERSION = "review-worker-protocol/v1"
WORKER_VERSION = __version__
MAX_REVIEWER_CONCURRENCY = 2
MAX_REVIEW_BUNDLES = 64
MAX_REVIEWER_ASSIGNMENTS = 128
MODEL_TURN_WORKSPACES_DIR_NAME = "model-turns"
MAX_MODEL_OUTPUT_FILES = 2048
MAX_MODEL_OUTPUT_FILE_BYTES = 16 * 1024 * 1024
MAX_MODEL_OUTPUT_TOTAL_BYTES = 64 * 1024 * 1024
LOW_MEMORY_REVIEWER_TOTAL_BYTES = 6 * 1024**3
LOW_MEMORY_REVIEWER_AVAILABLE_BYTES = 2 * 1024**3
CODEX_THREAD_ARCHIVE_TIMEOUT_SECONDS = 15
CODEX_ACCOUNT_READ_TIMEOUT_SECONDS = 15
CODEX_CLOSE_TIMEOUT_SECONDS = 15
TERMINAL_STATES = {"completed", "failed", "cancelled", "partial_completed"}
ACTIVE_HEARTBEAT_STATUSES = {"busy", "leased", "cancelling", "finishing", "failure_handling"}
WORKER_COMMAND_ACTIVE_STATUSES = {"pending", "running"}
REFRESH_CODEX_QUOTA_COMMAND = "refresh_codex_quota"
PIPELINE_PHASES = (
    ("prepare_workspace", 3),
    ("start_codex_app_server", 7),
    ("initialize_codex_connection", 10),
    ("check_codex_auth", 12),
    ("bootstrap_helper_scripts", 17),
    ("inventory_repository", 24),
    ("token_budget", 27),
    ("repo_map", 33),
    ("risk_routing", 39),
    ("bundle_planning", 43),
    ("bundle_packing", 47),
    ("reviewer_fanout", 70),
    ("reviewer_json_validation", 73),
    ("location_validation", 76),
    ("clustering_and_voting", 81),
    ("intent_test_validation", 82),
    ("intent_mining", 84),
    ("intent_test_planning", 86),
    ("validation_workspace_prepare", 88),
    ("intent_test_writing", 90),
    ("intent_test_running", 92),
    ("intent_test_failure_analysis", 94),
    ("validator_disproof", 96),
    ("final_report_json", 97),
    ("render_markdown_report", 98),
    ("qa_gate", 99),
    ("hash_artifacts", 99),
    ("upload_artifacts", 100),
    ("submit_result_envelope", 100),
    ("cleanup_active_job", 100),
)
SEMANTIC_PHASES = {
    "repo_map",
    "risk_routing",
    "bundle_planning",
    "reviewer_fanout",
    "clustering_and_voting",
    "intent_mining",
    "intent_test_planning",
    "intent_test_writing",
    "intent_test_failure_analysis",
    "validator_disproof",
    "final_report_json",
}
SEMANTIC_PHASE_MODEL_OUTPUTS: dict[str, tuple[str, ...]] = {
    "repo_map": ("repo-map.json",),
    "risk_routing": ("risk-routing.json",),
    "bundle_planning": ("bundle-grouping.json",),
    "clustering_and_voting": ("clusters.json", "validation-input.json"),
    "intent_mining": ("intent/intent-map.json",),
    "intent_test_planning": ("intent/intent-test-plan.json",),
    "intent_test_writing": (
        "intent/intent-test-source.json",
        "intent/generated-tests",
    ),
    "intent_test_failure_analysis": ("intent/intent-test-results.json",),
    "validator_disproof": ("validated-findings.json",),
    "final_report_json": ("report.agent.json",),
}
INTENT_VALIDATION_CHILD_PHASES = {
    "intent_mining",
    "intent_test_planning",
    "validation_workspace_prepare",
    "intent_test_writing",
    "intent_test_running",
    "intent_test_failure_analysis",
}


def phase_estimate_unit_id(phase: str) -> str:
    return f"phase:{phase}"


def current_run_estimator_for_job(
    job: dict[str, Any],
    *,
    monotonic_clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    started_monotonic: float | None = None,
) -> CurrentRunEstimator:
    policy = review_worker_policy_for_job(job)
    deadline_seconds = max(0, int(policy.get("scanDeadlineSeconds") or 0))
    run_started_monotonic = (
        monotonic_clock() if started_monotonic is None else float(started_monotonic)
    )
    estimator = CurrentRunEstimator(
        monotonic_clock=monotonic_clock,
        wall_clock=wall_clock,
        deadline_monotonic=(
            run_started_monotonic + deadline_seconds if deadline_seconds > 0 else None
        ),
    )
    estimator.set_resource_pool(
        "pipeline",
        configured_concurrency=1,
        effective_concurrency=1,
    )
    reviewer_concurrency = reviewer_concurrency_for_job(job)
    estimator.set_resource_pool(
        "reviewer",
        configured_concurrency=reviewer_concurrency,
        effective_concurrency=reviewer_concurrency,
    )
    previous_unit_id = ""
    for index, (phase, _progress) in enumerate(PIPELINE_PHASES):
        unit_id = phase_estimate_unit_id(phase)
        estimator.add_work_unit(
            unit_id,
            kind=(
                "reviewer_barrier"
                if phase == "reviewer_fanout"
                else "semantic_turn"
                if phase in SEMANTIC_PHASES
                else "mechanical_phase"
            ),
            resource_pool="pipeline",
            dependencies=(previous_unit_id,) if previous_unit_id else (),
            order=index,
        )
        previous_unit_id = unit_id
    return estimator


CORE_EFFORT_PHASES = SEMANTIC_PHASES - {"bundle_planning"}
MECHANICAL_PHASES = {phase for phase, _progress in PIPELINE_PHASES} - SEMANTIC_PHASES
REQUIRED_COMPLETED_ARTIFACTS = {
    "report.human",
    "report.agent",
    "coverage",
    "qa",
    "token_budget",
}
REQUIRED_COMPLETED_ARTIFACT_FILES = {
    "report.md": "report.human",
    "report.agent.json": "report.agent",
    "coverage.json": "coverage",
    "qa.json": "qa",
    "token-budget.json": "token_budget",
}
PHASE_JSON_OUTPUTS: dict[str, tuple[tuple[str, str], ...]] = {
    "bootstrap_helper_scripts": (("bootstrap_helper_scripts.summary.json", "bootstrap-helper-summary/v1"),),
    "inventory_repository": (("inventory.json", "inventory/v1"),),
    "token_budget": (("token-budget.json", "token-budget/v1"),),
    "repo_map": (("repo-map.json", "repo-map/v1"),),
    "risk_routing": (("risk-routing.json", "risk-routing/v1"),),
    "bundle_planning": (
        ("bundle-grouping.json", "bundle-grouping/v1"),
        ("bundle-plan.json", "bundle-plan/v1"),
        ("coverage.json", "coverage/v1"),
    ),
    "reviewer_json_validation": (("json-errors.json", "reviewer-json-validation/v1"),),
    "location_validation": (("location-verification.json", "location-verification/v1"),),
    "clustering_and_voting": (("clusters.json", "cluster-output/v1"), ("validation-input.json", "validation-input/v1")),
    "intent_test_validation": (("intent/intent-test-validation.json", "intent-test-validation/v1"),),
    "intent_mining": (("intent/intent-map.json", "intent-map/v1"),),
    "intent_test_planning": (("intent/intent-test-plan.json", "intent-test-plan/v1"),),
    "validation_workspace_prepare": (("intent/validation-workspace.json", "validation-workspace/v1"),),
    "intent_test_writing": (("intent/intent-test-source.json", "intent-test-source/v1"),),
    "intent_test_running": (("intent/intent-test-results.raw.json", "intent-test-run-results/v1"),),
    "intent_test_failure_analysis": (("intent/intent-test-results.json", "intent-test-result/v1"),),
    "validator_disproof": (("validated-findings.json", "validation-output/v1"),),
    "final_report_json": (("report.agent.json", "v1"),),
    "qa_gate": (("qa.json", "qa/v1"),),
}
PHASE_PATH_OUTPUTS: dict[str, tuple[str, ...]] = {
    "bundle_packing": ("bundles",),
    "reviewer_fanout": ("raw-reviewers",),
    "hash_artifacts": ("artifact:artifact-manifest.json",),
}
REQUIRED_TOOL_FILES = (
    "00_bootstrap_check.py",
    "01_inventory.py",
    "02_estimate_budget.py",
    "03_plan_bundles.py",
    "04_pack_bundle.py",
    "05_validate_reviewer_json.py",
    "06_verify_locations.py",
    "07_prepare_cluster_input.py",
    "08_render_reports.py",
    "09_run_qa_gate.py",
    "10_hash_artifacts.py",
    "11_prepare_validation_workspace.py",
    "12_run_project_test.py",
    "13_collect_test_outputs.py",
    "14_validate_intent_test_json.py",
)
REQUIRED_SCHEMA_FILES = (
    "inventory.schema.json",
    "token-budget.schema.json",
    "repo-map.schema.json",
    "risk-routing.schema.json",
    "bundle-plan.schema.json",
    "bundle-grouping.schema.json",
    "reviewer-output.schema.json",
    "location-verification.schema.json",
    "cluster-output.schema.json",
    "intent-map.schema.json",
    "intent-test-plan.schema.json",
    "intent-test-result.schema.json",
    "validation-output.schema.json",
    "final-report.schema.json",
    "qa.schema.json",
    "artifact-manifest.schema.json",
    "server-result-envelope.schema.json",
    "progress-event.schema.json",
    "heartbeat.schema.json",
    "worker-register.schema.json",
    "lease.schema.json",
)
REQUIRED_PROMPT_FILES = (
    "00_repo_mapper.md",
    "01_risk_router.md",
    "02_bundle_planner.md",
    "reviewers/security.md",
    "reviewers/correctness.md",
    "reviewers/test_gap.md",
    "reviewers/correctness_lite.md",
    "03_clusterer.md",
    "intent/04_intent_miner.md",
    "intent/05_intent_test_planner.md",
    "intent/06_intent_test_writer.md",
    "intent/07_intent_test_failure_analyzer.md",
    "08_validator.md",
    "09_reporter.md",
)
REVIEWER_OUTPUT_SCHEMA_VERSION = "codex-reviewer-output/v1"
REVIEWER_OUTPUT_SCHEMA_ALIASES = {"reviewer-output/v1"}
REVIEWER_CONFIDENCE_PROMPT_CONTRACT = (
    "Every finding's confidence must be a JSON number in [0,1], not a string or label."
)
INTENT_TEST_CLASSIFICATIONS = {
    "confirmed_bug",
    "plausible_bug",
    "test_oracle_wrong",
    "test_harness_error",
    "environment_error",
    "flaky_or_nondeterministic",
    "dependency_missing",
    "unclear_requirement",
    "passed_no_bug_reproduced",
    "skipped_not_runnable",
}
INTENT_TEST_STATUSES = {"passed", "failed", "skipped", "timeout", "error"}
CODEX_ERROR_CODES = {
    "UsageLimitExceeded": "CODEX_QUOTA_EXHAUSTED",
    "usageLimitExceeded": "CODEX_QUOTA_EXHAUSTED",
    "RateLimitReached": "CODEX_QUOTA_EXHAUSTED",
    "rate_limit_reached": "CODEX_QUOTA_EXHAUSTED",
    "workspace_owner_credits_depleted": "CODEX_QUOTA_EXHAUSTED",
    "workspace_member_credits_depleted": "CODEX_QUOTA_EXHAUSTED",
    "workspace_owner_usage_limit_reached": "CODEX_QUOTA_EXHAUSTED",
    "workspace_member_usage_limit_reached": "CODEX_QUOTA_EXHAUSTED",
    "ContextWindowExceeded": "CODEX_CONTEXT_WINDOW_EXCEEDED",
    "contextWindowExceeded": "CODEX_CONTEXT_WINDOW_EXCEEDED",
    "Unauthorized": "CODEX_UNAUTHORIZED",
    "unauthorized": "CODEX_UNAUTHORIZED",
    "SandboxError": "CODEX_SANDBOX_ERROR",
    "sandboxError": "CODEX_SANDBOX_ERROR",
    "HttpConnectionFailed": "CODEX_UPSTREAM_CONNECTION_FAILED",
    "InternalServerError": "CODEX_INTERNAL_SERVER_ERROR",
    "internalServerError": "CODEX_INTERNAL_SERVER_ERROR",
}
CODEX_QUOTA_ERROR_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "quota exceeded",
    "quota exhausted",
    "usage limit",
    "rate limit",
    "rate_limit",
    "too many requests",
    "no credits",
    "credits exhausted",
    "out of credits",
)
CODEX_QUOTA_HTTP_429_RE = re.compile(
    r"\b(?:http(?:\s+status)?|status(?:\s+code)?|response(?:\s+code)?)\s*[:=]?\s*429\b",
    re.IGNORECASE,
)
GLOBAL_AGENTS_TEXT = """# Codex Repo Review Worker Global Instructions

You are running inside an isolated Codex repo review worker.

Rules:
- Full repository scan, not diff review.
- Do not install dependencies.
- Do not call external review or scanning tools.
- Do not modify application source files.
- Write only the declared outputs inside the current writable phase output directory.
- For dynamic tests, write source only under intent/generated-tests/** in the writable phase output directory; the Worker materializes and executes it in the disposable validation workspace.
- Helper scripts must use Python 3 standard library only.
- Helper scripts perform mechanical tasks only.
- Codex performs semantic code review judgment.
- Every main finding must be concrete, located, evidenced, and actionable.
- A generated test failure is evidence, not automatically a confirmed bug.
"""
REVIEW_AGENTS_TEXT = """# Codex Full Repository Review Instructions

This is a full repository scan, not a diff review.

Required outputs:
- report.md
- report.agent.json
- coverage.json
- token-budget.json
- qa.json
- artifact-manifest.json
- codex-events.jsonl
- worker.log.jsonl
- progress.log.jsonl

Rules:
- Do not modify application source files.
- Do not install dependencies.
- Do not call external review/scanning tools.
- Every main finding must have path, line range, evidence, failure_scenario, impact, recommendation, severity, confidence, false_positive_risk, and next_agent_task.
- Weak findings go to appendix.
- Disproven findings do not appear in main findings.
- Coverage and skipped scope must be reported.
- Intent-driven tests may be generated only for selected P0/P1 candidate findings.
- Test failures must be classified as confirmed_bug, plausible_bug, test_oracle_wrong, test_harness_error, environment_error, flaky_or_nondeterministic, dependency_missing, unclear_requirement, passed_no_bug_reproduced, or skipped_not_runnable.
"""


PROGRESS_COUNTER_KEYS = (
    "source_like_files_total",
    "source_like_files_classified",
    "bundles_total",
    "bundles_packed",
    "reviewer_runs_total",
    "reviewer_runs_completed",
    "intent_tests_total",
    "intent_tests_written",
    "intent_tests_run",
    "validator_candidates_total",
    "validator_candidates_completed",
    "artifacts_total",
    "artifacts_uploaded",
)
DEFAULT_PROVIDER_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
LOG_ARTIFACT_NAMES = {"codex-events.jsonl", "worker.log.jsonl", "progress.log.jsonl"}
FINAL_REFRESH_ARTIFACT_NAMES = LOG_ARTIFACT_NAMES | {"debug-bundle.zip"}
DEBUG_BUNDLE_NAME = "debug-bundle.zip"
DEBUG_BUNDLE_ARTIFACT_ID = "art_debug_bundle"
LOCATION_VERIFICATION_ARTIFACT_ID = "art_location_verification"
UPLOADED_ARTIFACT_MANIFEST_NAME = "uploaded-artifact-manifest.json"
MAX_BUNDLE_ESTIMATED_TOKENS = 60000
PROVIDER_ENV_PASSTHROUGH_KEYS = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)
PROTECTED_PROVIDER_ENV_KEYS = {
    "HOME",
    "USERPROFILE",
    "CODEX_HOME",
    "CODEX_SQLITE_HOME",
    "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
}


def default_progress_counters() -> dict[str, int]:
    return {key: 0 for key in PROGRESS_COUNTER_KEYS}


def progress_step_label(phase: str) -> str:
    return str(phase or "").replace("_", " ").strip().capitalize()


def default_progress_steps() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for index, (phase, target_progress) in enumerate(PIPELINE_PHASES, start=1):
        steps.append(
            {
                "id": phase,
                "index": index,
                "label": progress_step_label(phase),
                "description": "",
                "target_percent": target_progress,
            }
        )
    return steps


@dataclass
class ActiveJob:
    job_id: str
    run_id: str
    lease_id: str
    attempt_id: str
    state: str = "leased"
    started_at: float = field(default_factory=time.time)
    current_phase: str = "prepare_workspace"
    cancel_requested: bool = False
    cancel_reason: str = ""
    cancel_requested_reported: bool = False
    terminal_result_in_flight: bool = False
    terminal_result_submitted: bool = False
    run_dir: Path | None = None
    last_event_sequence: int = 0
    overall_percent: float = 0.0
    current_phase_status: str = "pending"
    current_phase_percent: float = 0.0
    message: str = ""
    thread_id: str = ""
    counters: dict[str, int] = field(default_factory=default_progress_counters)
    active_unit: dict[str, Any] = field(default_factory=dict)
    flow_steps: list[dict[str, Any]] = field(default_factory=default_progress_steps)
    current_run_estimator: CurrentRunEstimator | None = field(default=None, repr=False)
    estimate_repair_counts: dict[str, int] = field(default_factory=dict, repr=False)

    def heartbeat_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "state": self.state,
            "started_at": iso_time(self.started_at),
            "current_phase": self.current_phase,
            "cancel_requested": self.cancel_requested,
            "thread_id": self.thread_id or None,
        }

    def progress_snapshot(self) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "overall_percent": self.overall_percent,
            "current_phase": self.current_phase,
            "current_phase_status": self.current_phase_status,
            "current_phase_percent": self.current_phase_percent,
            "message": self.message,
            "steps": self.progress_steps(),
            "counters": dict(self.counters),
            "active_unit": dict(self.active_unit),
            "last_event_sequence": self.last_event_sequence,
            "updated_at": iso_time(time.time()),
        }

        estimate = self.current_run_estimator.snapshot() if self.current_run_estimator else None
        if isinstance(estimate, dict):
            payload["estimate"] = estimate
        return payload

    def apply_progress_data(self, data: dict[str, Any] | None) -> None:
        if not isinstance(data, dict):
            return
        for key in PROGRESS_COUNTER_KEYS:
            if key not in data:
                continue
            try:
                self.counters[key] = max(0, int(data[key]))
            except (TypeError, ValueError):
                continue
        active_unit = data.get("active_unit")
        if isinstance(active_unit, dict):
            self.active_unit = dict(active_unit)

    def progress_steps(self) -> list[dict[str, Any]]:
        phase_index = next(
            (index for index, step in enumerate(self.flow_steps) if step.get("id") == self.current_phase),
            -1,
        )
        steps: list[dict[str, Any]] = []
        for index, step in enumerate(self.flow_steps):
            status = "pending"
            percent = 0.0
            if phase_index >= 0 and index < phase_index:
                status = "completed"
                percent = 100.0
            elif index == phase_index:
                status = self.current_phase_status or "running"
                percent = self.current_phase_percent
            steps.append(
                {
                    "id": str(step.get("id") or ""),
                    "index": int(step.get("index") or index + 1),
                    "label": str(step.get("label") or progress_step_label(str(step.get("id") or ""))),
                    "description": str(step.get("description") or ""),
                    "target_percent": step.get("target_percent"),
                    "status": status,
                    "percent": round(float(percent), 2),
                }
            )
        return steps


class WorkerState:
    def __init__(self) -> None:
        self.state = "starting"
        self.active_job: ActiveJob | None = None
        self.provider_ready = True

    @property
    def local_queue_depth(self) -> int:
        return 0

    @property
    def available_job_slots(self) -> int:
        return 1 if self.state == "idle" and self.active_job is None and self.provider_ready else 0

    def can_lease(self) -> bool:
        return self.active_job is None and self.state == "idle" and self.local_queue_depth == 0 and self.provider_ready

    def set_active(self, job: ActiveJob) -> None:
        if self.active_job is not None:
            raise RuntimeError("worker already has an active job")
        self.active_job = job
        self.state = "leased"

    def clear_active(self, terminal_state: str) -> None:
        if terminal_state not in TERMINAL_STATES:
            raise RuntimeError(f"cannot clear active job from non-terminal state: {terminal_state}")
        self.active_job = None
        self.state = "idle"


class WorkerLock:
    def __init__(self, worker_root: Path, worker_id: str, codex_home: Path | None = None) -> None:
        self.worker_root = worker_root
        self.worker_id = worker_id
        self.path = worker_root / "lock" / "worker.lock"
        self.codex_home = codex_home or worker_root / "codex-home"
        self._handle: Any = None

    def acquire(self) -> None:
        if fcntl is None:
            raise RuntimeError("worker lock requires Linux/POSIX fcntl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"worker lock is already held: {self.path}") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "worker_id": self.worker_id,
                    "pid": os.getpid(),
                    "hostname": socket.gethostname(),
                    "started_at": iso_time(time.time()),
                    "codex_home": str(self.codex_home),
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


def _active_worker_root(config: Any) -> Path:
    service_home = Path(
        str(getattr(config, "service_home", "") or "/var/lib/codex-review")
    ).expanduser()
    configured_root = os.environ.get("PULLWISE_WORKER_ROOT", "").strip()
    if not configured_root:
        configured_root = str(getattr(config, "worker_root", "") or "").strip()
    if configured_root:
        return Path(configured_root).expanduser()
    worker_id = str(getattr(config, "worker_id", "worker") or "worker")
    return service_home / "workers" / worker_id


class Isolation:
    def __init__(self, config: Any) -> None:
        self.worker_id = str(config.worker_id)
        service_home = Path(str(getattr(config, "service_home", "") or "/var/lib/codex-review"))
        self.service_home = service_home
        self.worker_root = _active_worker_root(config)
        self.codex_home = Path(str(getattr(config, "codex_home", "") or self.worker_root / "codex-home"))
        self.codex_sqlite_home = Path(str(getattr(config, "codex_sqlite_home", "") or self.worker_root / "codex-sqlite"))
        self.config_home = self.worker_root / ".config"
        self.cache_home = self.worker_root / ".cache"
        self.data_home = self.worker_root / ".local" / "share"
        self.runtime = self.worker_root / "runtime"
        self.workspaces = self.worker_root / "workspaces"
        self.artifacts = self.worker_root / "artifacts"
        self.logs = self.worker_root / "logs"

    def prepare(self) -> None:
        for path in (
            self.worker_root,
            self.codex_home,
            self.codex_sqlite_home,
            self.config_home,
            self.cache_home,
            self.data_home,
            self.runtime,
            self.workspaces,
            self.artifacts,
            self.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o700)
        config_toml = self.codex_home / "config.toml"
        if not config_toml.exists():
            config_toml.write_text('cli_auth_credentials_store = "file"\n', encoding="utf-8")
            config_toml.chmod(0o600)
        agents = self.codex_home / "AGENTS.md"
        try:
            existing_agents = read_text_no_follow(agents, encoding="utf-8")
        except FileNotFoundError:
            existing_agents = ""
        if not existing_agents or existing_agents.startswith(
            "# Codex Repo Review Worker Global Instructions"
        ):
            _write_worker_owned_bytes(agents, GLOBAL_AGENTS_TEXT.encode("utf-8"))
            agents.chmod(0o600)

    def env(self, config: Any) -> dict[str, str]:
        env = {
            key: os.environ[key]
            for key in PROVIDER_ENV_PASSTHROUGH_KEYS
            if os.environ.get(key)
        }
        extra = getattr(config, "codex_env", None)
        extra_path = ""
        if isinstance(extra, dict):
            for key, value in extra.items():
                text_key = str(key)
                upper_key = text_key.upper()
                if upper_key in PROTECTED_PROVIDER_ENV_KEYS:
                    continue
                if upper_key == "PATH":
                    extra_path = str(value)
                    continue
                env[text_key] = str(value)
        base_path = (
            extra_path
            or str(getattr(config, "service_path", "") or "").strip()
            or os.environ.get("PATH")
            or DEFAULT_PROVIDER_PATH
        )
        path_parts = [
            str(self.worker_root / ".venv" / "bin"),
            str(self.worker_root / ".local" / "bin"),
            str(self.worker_root / ".codex" / "bin"),
            str(self.codex_home / "bin"),
            base_path,
        ]
        env.update(
            {
                "HOME": str(self.worker_root),
                "USERPROFILE": str(self.worker_root),
                "CODEX_HOME": str(self.codex_home),
                "CODEX_SQLITE_HOME": str(self.codex_sqlite_home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_CACHE_HOME": str(self.cache_home),
                "XDG_DATA_HOME": str(self.data_home),
                "PATH": os.pathsep.join(dict.fromkeys(part for part in path_parts if part)),
            }
        )
        return env


def scoped_codex_command(config: Any) -> str:
    worker_root = _active_worker_root(config)
    command = str(getattr(config, "codex_command", "") or "").strip()
    if not command:
        return ""
    command_path = Path(command).expanduser()
    if not command_path.is_absolute():
        raise RuntimeError(f"Codex command must be an absolute path inside worker_root: {command}")
    resolved_command = command_path.resolve(strict=False)
    resolved_worker_root = worker_root.resolve(strict=False)
    try:
        resolved_command.relative_to(resolved_worker_root)
    except ValueError as exc:
        raise RuntimeError(f"Codex command must be inside worker_root {worker_root}: {command}") from exc
    return str(command_path)

@dataclass(frozen=True)
class CodexSdkRuntime:
    Codex: Any
    CodexConfig: Any
    ApprovalMode: Any
    Sandbox: Any


@dataclass(frozen=True)
class CodexTurnMetrics:
    duration_ms: int
    token_usage: CodexTokenUsage | None = None


def load_codex_sdk_runtime() -> CodexSdkRuntime:
    try:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
    except ImportError as exc:  # pragma: no cover - exercised on hosts missing the runtime dependency.
        raise RuntimeError("openai-codex Python SDK is required; install the pullwise-worker package dependencies") from exc
    return CodexSdkRuntime(Codex=Codex, CodexConfig=CodexConfig, ApprovalMode=ApprovalMode, Sandbox=Sandbox)


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class CodexSdkClient:
    def __init__(
        self,
        command: str,
        env: dict[str, str],
        cwd: Path,
        events_path: Path,
        rate_limit_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.command = str(command or "").strip()
        self.env = env
        self.cwd = cwd
        self._runtime_resources = CodexRuntimeResources(events_path)
        self.events_path = self._runtime_resources.events_path
        self.rate_limit_callback = rate_limit_callback
        self._runtime: CodexSdkRuntime | None = None
        self._codex: Any | None = None
        self._client: Any | None = None
        self._threads = self._runtime_resources.threads
        self._approval_workspace = cwd
        self._events_lock = self._runtime_resources.events_lock
        self._rate_limit_callback_lock = threading.Lock()
        self._threads_lock = self._runtime_resources.threads_lock
        self._lifecycle_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._unhealthy_reason: str | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            self._raise_if_unhealthy()
            if self._codex is not None:
                return
            self.events_path.parent.mkdir(parents=True, exist_ok=True)
            runtime = load_codex_sdk_runtime()
            config_kwargs = {
                "cwd": str(self.cwd),
                "env": self.env,
                "client_name": "codex_repo_review_worker",
                "client_title": "Codex Repo Review Worker",
                "client_version": WORKER_VERSION,
                "experimental_api": False,
            }
            if self.command:
                config_kwargs["codex_bin"] = self.command
            config = runtime.CodexConfig(**config_kwargs)
            codex = runtime.Codex(config)
            client = getattr(codex, "_client", None)
            if client is not None and hasattr(client, "_approval_handler"):
                client._approval_handler = self._approval_handler
            self._runtime = runtime
            self._codex = codex
            self._client = client

    def is_running(self) -> bool:
        with self._lifecycle_lock:
            with self._health_lock:
                return self._codex is not None and self._unhealthy_reason is None

    def runtime_metadata(self) -> dict[str, Any]:
        def distribution_version(name: str) -> str:
            try:
                return importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                return "not_installed"

        payload: dict[str, Any] = {
            "schema_version": "codex-runtime/v1",
            "mode": "managed_standalone" if self.command else "sdk_pinned",
            "worker_version": WORKER_VERSION,
            "python_sdk_version": distribution_version("openai-codex"),
            "sdk_bundled_cli_version": distribution_version("openai-codex-cli-bin"),
            "configured_cli_command": self.command or None,
            "configured_cli_version": None,
        }
        if not self.command:
            return payload
        try:
            completed = subprocess.run(
                [self.command, "--version"],
                cwd=str(self.cwd),
                env=self.env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            payload["configured_cli_probe_error"] = type(exc).__name__
            return payload
        version_line = str(completed.stdout or "").replace("\x00", "").splitlines()
        if completed.returncode == 0 and version_line:
            payload["configured_cli_version"] = version_line[0].strip()[:200]
        else:
            payload["configured_cli_probe_error"] = f"exit_code_{completed.returncode}"
        return payload

    def set_events_path(self, events_path: Path) -> None:
        self._runtime_resources.switch_run(events_path)
        self.events_path = self._runtime_resources.events_path

    def usage_snapshot(self) -> dict[str, Any]:
        return self._runtime_resources.usage_snapshot()

    def start_thread(
        self,
        repo_dir: Path,
        model: str,
        *,
        timeout_seconds: int = 30,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> str:
        self._raise_if_unhealthy()
        if self._codex is None:
            self.start()
        if self._codex is None or self._runtime is None:
            raise RuntimeError("Codex SDK is not running")
        self._approval_workspace = repo_dir
        try:
            thread = run_bounded_call(
                lambda: self._codex.thread_start(
                    approval_mode=self._runtime.ApprovalMode.deny_all,
                    cwd=str(repo_dir),
                    sandbox=self._runtime.Sandbox.read_only,
                    service_name="codex_repo_review_worker",
                    model=model or None,
                ),
                timeout_seconds=max(1, int(timeout_seconds)),
                timeout_message="codex thread start timed out",
                cancel_requested=cancel_requested,
                cancelled_error=lambda: JobCancelled("cancel requested"),
                late_result=self._archive_late_thread,
            )
        except (TimeoutError, JobCancelled) as exc:
            self._mark_unhealthy(str(exc))
            raise
        thread_id = require_identifier(getattr(thread, "id", ""), label="thread id")
        self._runtime_resources.register_thread(thread_id, thread)
        return thread_id

    def release_thread(self, thread_id: str) -> None:
        if not thread_id:
            return
        try:
            self._raise_if_unhealthy()
            archive = getattr(self._codex, "thread_archive", None)
            if callable(archive):
                run_bounded_call(
                    lambda: archive(thread_id),
                    timeout_seconds=CODEX_THREAD_ARCHIVE_TIMEOUT_SECONDS,
                    timeout_message=f"codex thread archive timed out: {thread_id}",
                )
            else:
                if self._client is None:
                    raise RuntimeError("Codex SDK client is not running for thread archive")
                self.request(
                    "thread/archive",
                    {"threadId": thread_id},
                    timeout_seconds=CODEX_THREAD_ARCHIVE_TIMEOUT_SECONDS,
                )
        except TimeoutError as exc:
            self._mark_unhealthy(str(exc))
            raise
        finally:
            self._runtime_resources.release_thread(thread_id)

    def _archive_late_thread(self, thread: Any) -> None:
        thread_id = str(getattr(thread, "id", "") or "").strip()
        if not thread_id:
            return
        try:
            self.release_thread(thread_id)
        except Exception:
            # The caller has already abandoned this start request. Closing the
            # App Server is the only bounded way to guarantee the orphan does
            # not remain loaded when archive itself is unhealthy.
            self.close()

    def run_turn(
        self,
        *,
        thread_id: str,
        repo_dir: Path,
        turn_cwd: Path | None = None,
        prompt: str,
        effort: str,
        read_only: bool,
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
        writable_roots: list[Path] | None = None,
        metrics_phase: str = "",
    ) -> CodexTurnMetrics:
        turn_started_monotonic = time.monotonic()
        self._approval_workspace = repo_dir
        target_cwd = repo_dir
        if not read_only:
            if turn_cwd is None:
                raise ValueError("writable Codex turns require a worker-owned external turn_cwd")
            if writable_roots:
                raise ValueError("additional writable roots are forbidden for semantic Codex turns")
            allowed_turn_root = (repo_dir.parent / MODEL_TURN_WORKSPACES_DIR_NAME).resolve(strict=False)
            target_cwd = Path(turn_cwd).resolve(strict=False)
            if not _path_is_within(target_cwd, allowed_turn_root):
                raise ValueError("Codex turn_cwd is outside the worker-owned model-turn workspace")
            if path_has_symlink_component(Path(turn_cwd)) or not Path(turn_cwd).is_dir():
                raise ValueError("Codex turn_cwd must be an existing non-symlink directory")
        params = {
            "cwd": str(target_cwd),
            "approvalPolicy": "never",
            "sandboxPolicy": self._sandbox_policy(
                repo_dir,
                read_only=read_only,
                writable_roots=writable_roots,
            ),
            "effort": effort,
            "summary": "concise",
        }
        client = self._sdk_client()
        event_scope = self._runtime_resources.begin_turn(
            phase=metrics_phase,
            thread_id=thread_id,
        )
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        start_completed = threading.Event()
        start_lock = threading.Lock()
        start_state: dict[str, Any] = {"abandoned": False}

        def start_turn() -> None:
            try:
                started_turn = client.turn_start(thread_id, [{"type": "text", "text": prompt}], params=params)
                with start_lock:
                    start_state["started"] = started_turn
                    abandoned = bool(start_state["abandoned"])
                if abandoned:
                    orphaned_turn = getattr(started_turn, "turn", None)
                    orphaned_turn_id = str(
                        getattr(orphaned_turn, "id", "") or getattr(started_turn, "turn_id", "") or ""
                    )
                    if orphaned_turn_id:
                        self.interrupt(thread_id, orphaned_turn_id)
            except BaseException as exc:  # noqa: BLE001 - surfaced to the worker phase as a Codex turn failure.
                with start_lock:
                    start_state["error"] = exc
            finally:
                start_completed.set()

        def abandon_start() -> None:
            with start_lock:
                start_state["abandoned"] = True
                started_turn = start_state.get("started")
            orphaned_turn = getattr(started_turn, "turn", None)
            orphaned_turn_id = str(
                getattr(orphaned_turn, "id", "") or getattr(started_turn, "turn_id", "") or ""
            )
            if orphaned_turn_id:
                self.interrupt(thread_id, orphaned_turn_id)

        threading.Thread(target=start_turn, name=f"pullwise-codex-turn-start-{thread_id}", daemon=True).start()
        while not start_completed.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                abandon_start()
                self._runtime_resources.abandon_turn(event_scope)
                self._mark_unhealthy("codex turn start timed out")
                raise TimeoutError("codex turn start timed out")
            if start_completed.wait(min(0.5, remaining)):
                break
            if cancel_requested is not None and cancel_requested():
                abandon_start()
                self._runtime_resources.abandon_turn(event_scope)
                self._mark_unhealthy("codex turn start was cancelled while the SDK call was active")
                raise JobCancelled("cancel requested")
        start_error = start_state.get("error")
        if isinstance(start_error, BaseException):
            self._runtime_resources.abandon_turn(event_scope)
            raise start_error
        started = start_state.get("started")
        turn = getattr(started, "turn", None)
        try:
            turn_id = require_identifier(
                getattr(turn, "id", "") or getattr(started, "turn_id", ""),
                label="turn id",
            )
            self._runtime_resources.bind_turn(
                event_scope,
                turn_id,
                thread_id=thread_id,
                phase=metrics_phase,
            )
        except BaseException:
            self._runtime_resources.abandon_turn(event_scope)
            raise

        completed = threading.Event()
        abandoned = threading.Event()
        failure: dict[str, BaseException] = {}
        metrics: dict[str, int] = {}

        def consume_turn() -> None:
            try:
                while True:
                    notification = client.next_turn_notification(turn_id)
                    if abandoned.is_set():
                        break
                    self._record_sdk_notification(notification, event_scope=event_scope)
                    if abandoned.is_set():
                        break
                    payload = getattr(notification, 'payload', None)
                    payload_data = self._model_to_dict(payload)
                    turn_data = payload_data.get('turn')
                    if not isinstance(turn_data, dict):
                        turn_data = {}
                    reported_duration = (
                        turn_data.get('durationMs')
                        if turn_data.get('durationMs') is not None
                        else turn_data.get('duration_ms')
                    )
                    if reported_duration is None:
                        reported_duration = payload_data.get('durationMs')
                    if reported_duration is None:
                        reported_duration = payload_data.get('duration_ms')
                    try:
                        parsed_duration = int(reported_duration)
                    except (TypeError, ValueError):
                        parsed_duration = -1
                    if parsed_duration >= 0:
                        metrics['duration_ms'] = parsed_duration
                    method = str(getattr(notification, "method", "") or "")
                    payload = getattr(notification, "payload", None)
                    if method == "account/rateLimits/updated" and self.rate_limit_callback is not None:
                        params = self._model_to_dict(payload)
                        with self._rate_limit_callback_lock:
                            self.rate_limit_callback(params)
                    if method == "error":
                        params = self._model_to_dict(payload)
                        notification_turn_id = str(params.get("turnId") or params.get("turn_id") or "")
                        if notification_turn_id and notification_turn_id != turn_id:
                            continue
                        will_retry = params.get("willRetry")
                        if will_retry is None:
                            will_retry = params.get("will_retry")
                        if bool(will_retry):
                            continue
                        turn_error = params.get("error") or getattr(payload, "error", None) or params
                        failure["exception"] = RuntimeError(self._json_text(turn_error))
                        break
                    if method != "turn/completed":
                        continue
                    completed_turn_id = str(
                        turn_data.get("id")
                        or turn_data.get("turnId")
                        or turn_data.get("turn_id")
                        or payload_data.get("turnId")
                        or payload_data.get("turn_id")
                        or ""
                    ).strip()
                    if not completed_turn_id:
                        failure["exception"] = RuntimeError(
                            f"Codex turn/completed notification is missing turn id; expected {turn_id}"
                        )
                        break
                    if completed_turn_id != turn_id:
                        continue
                    raw_status = turn_data.get("status")
                    if raw_status is None:
                        raw_status = payload_data.get("status")
                    turn_status = str(raw_status or "").strip().lower()
                    turn_error = turn_data.get("error")
                    if turn_error is None:
                        turn_error = turn_data.get("lastError")
                    if turn_error is None:
                        turn_error = turn_data.get("last_error")
                    if turn_error is None:
                        turn_error = payload_data.get("error")
                    if turn_status != "completed":
                        status_text = turn_status or "missing"
                        detail = f": {self._json_text(turn_error)}" if turn_error else ""
                        failure["exception"] = RuntimeError(
                            f"Codex turn {turn_id} completed with non-success status {status_text!r}{detail}"
                        )
                        break
                    if turn_error:
                        failure["exception"] = RuntimeError(
                            f"Codex turn {turn_id} reported status 'completed' with an error: "
                            f"{self._json_text(turn_error)}"
                        )
                    break
            except BaseException as exc:  # noqa: BLE001 - surfaced to the worker phase as a Codex turn failure.
                if not str(exc).strip():
                    diagnostic = (
                        f"Codex turn notification stream failed for {turn_id}: "
                        f"{type(exc).__name__}"
                    )
                    try:
                        exc.args = (diagnostic,)
                    except Exception:
                        exc = RuntimeError(diagnostic)
                failure["exception"] = exc
            finally:
                self._runtime_resources.abandon_turn(event_scope)
                try:
                    client.unregister_turn_notifications(turn_id)
                except Exception:
                    pass
                completed.set()

        consumer = threading.Thread(target=consume_turn, daemon=True)
        consumer.start()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                abandoned.set()
                self._runtime_resources.abandon_turn(event_scope)
                self.interrupt(thread_id, turn_id)
                self._mark_unhealthy(f"codex turn timed out: {turn_id}")
                raise TimeoutError(f"codex turn timed out: {turn_id}")
            if completed.wait(min(0.5, remaining)):
                break
            if cancel_requested is not None and cancel_requested():
                abandoned.set()
                self._runtime_resources.abandon_turn(event_scope)
                self.interrupt(thread_id, turn_id)
                self._mark_unhealthy(f"codex turn was cancelled while active: {turn_id}")
                raise JobCancelled("cancel requested")
        turn_failure = failure.get("exception")
        if turn_failure is not None:
            raise turn_failure

        duration_ms = metrics.get(
            'duration_ms',
            max(0, int((time.monotonic() - turn_started_monotonic) * 1000)),
        )
        return CodexTurnMetrics(
            duration_ms=duration_ms,
            token_usage=self._runtime_resources.turn_usage(event_scope),
        )

    def _sandbox_policy(
        self,
        repo_dir: Path,
        *,
        read_only: bool,
        writable_roots: list[Path] | None = None,
    ) -> dict[str, Any]:
        if read_only:
            return {"type": "readOnly", "networkAccess": False}
        if writable_roots:
            raise ValueError("additional writable roots are forbidden for semantic Codex turns")
        return {
            "type": "workspaceWrite",
            "networkAccess": False,
            "writableRoots": [],
        }

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        completed = threading.Event()

        def send_interrupt() -> None:
            try:
                self._sdk_client().turn_interrupt(thread_id, turn_id)
            except Exception:
                pass
            finally:
                completed.set()

        threading.Thread(
            target=send_interrupt,
            name=f"pullwise-codex-turn-interrupt-{turn_id}",
            daemon=True,
        ).start()
        # A wedged App Server must not turn timeout/cancellation into another
        # unbounded RPC wait. Preserve fast-path ordering for responsive SDKs.
        completed.wait(0.1)

    def request(self, method: str, params: dict[str, Any] | None = None, timeout_seconds: float = 30) -> dict[str, Any]:
        client = self._sdk_client()
        if hasattr(client, "_request_raw"):
            try:
                result = run_bounded_call(
                    lambda: client._request_raw(method, params or {}),
                    timeout_seconds=timeout_seconds,
                    timeout_message=f"Codex raw request timed out: {method}",
                )
            except TimeoutError as exc:
                self._mark_unhealthy(str(exc))
                raise
            return result if isinstance(result, dict) else {}
        raise RuntimeError(f"Codex SDK client does not expose raw request support: {method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        client = self._sdk_client()
        if hasattr(client, "notify"):
            client.notify(method, params or {})

    def login_chatgpt(self) -> Any:
        self._raise_if_unhealthy()
        if self._codex is None:
            self.start()
        if self._codex is None:
            raise RuntimeError("Codex SDK is not running")
        return self._codex.login_chatgpt()

    def login_chatgpt_device_code(self) -> Any:
        self._raise_if_unhealthy()
        if self._codex is None:
            self.start()
        if self._codex is None:
            raise RuntimeError("Codex SDK is not running")
        return self._codex.login_chatgpt_device_code()

    def login_api_key(self, api_key: str) -> None:
        self._raise_if_unhealthy()
        if self._codex is None:
            self.start()
        if self._codex is None:
            raise RuntimeError("Codex SDK is not running")
        self._codex.login_api_key(api_key)

    def account(
        self,
        *,
        refresh_token: bool = False,
        timeout_seconds: float = CODEX_ACCOUNT_READ_TIMEOUT_SECONDS,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        self._raise_if_unhealthy()
        if self._codex is None:
            self.start()
        if self._codex is None:
            raise RuntimeError("Codex SDK is not running")
        try:
            result = run_bounded_call(
                lambda: self._codex.account(refresh_token=refresh_token),
                timeout_seconds=timeout_seconds,
                timeout_message="Codex account read timed out",
                cancel_requested=cancel_requested,
                cancelled_error=lambda: JobCancelled("Codex account read cancelled"),
            )
        except (TimeoutError, JobCancelled) as exc:
            self._mark_unhealthy(str(exc))
            raise
        return self._model_to_dict(result)

    def close(self) -> None:
        with self._lifecycle_lock:
            codex = self._codex
            self._runtime = None
            self._codex = None
            self._client = None
            self._runtime_resources.clear()
        if codex is not None:
            run_bounded_call(
                codex.close,
                timeout_seconds=CODEX_CLOSE_TIMEOUT_SECONDS,
                timeout_message="Codex SDK close timed out",
            )

    def _sdk_client(self) -> Any:
        self._raise_if_unhealthy()
        if self._client is None:
            if self._codex is None:
                self.start()
            self._client = getattr(self._codex, "_client", None) if self._codex is not None else None
        if self._client is None:
            raise RuntimeError("Codex SDK client is not running")
        return self._client

    def _mark_unhealthy(self, reason: str) -> None:
        normalized_reason = str(reason or "Codex SDK runtime failure").strip()
        with self._health_lock:
            if self._unhealthy_reason is None:
                self._unhealthy_reason = normalized_reason

    def _raise_if_unhealthy(self) -> None:
        with self._health_lock:
            reason = self._unhealthy_reason
        if reason is not None:
            raise RuntimeError(f"Codex SDK runtime is unhealthy after {reason}")

    def _approval_handler(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        return approval_response_for_request({"method": method, "params": params or {}}, self._approval_workspace)

    def _record_sdk_notification(
        self,
        notification: Any,
        *,
        event_scope: TurnEventScope | None = None,
    ) -> bool:
        method = str(getattr(notification, "method", "") or "")
        payload = self._model_to_dict(getattr(notification, "payload", None))
        scope = event_scope or self._runtime_resources.begin_turn()
        return self._runtime_resources.record_event(scope, method, payload)

    def _model_to_dict(self, value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if hasattr(value, "model_dump"):
            dumped = value.model_dump(by_alias=True, exclude_none=True, mode="json")
            return dumped if isinstance(dumped, dict) else {}
        if hasattr(value, "__dict__"):
            return {key: self._jsonable(item) for key, item in vars(value).items() if not key.startswith("_")}
        return {}

    def _jsonable(self, value: Any) -> Any:
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        if isinstance(value, dict):
            return {str(key): self._jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._jsonable(item) for item in value]
        if hasattr(value, "model_dump"):
            return value.model_dump(by_alias=True, exclude_none=True, mode="json")
        if hasattr(value, "__dict__"):
            return {key: self._jsonable(item) for key, item in vars(value).items() if not key.startswith("_")}
        return str(value)

    def _json_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(self._jsonable(value), ensure_ascii=False, sort_keys=True)

def codex_enum_variant_error(error: object) -> bool:
    message = str(error or "").lower()
    return "unknown variant" in message and "expected one of" in message

def quota_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not number == number or number in {float("inf"), float("-inf")}:
        return None
    return number


def quota_int(value: object) -> int | None:
    number = quota_float(value)
    return int(number) if number is not None else None


def quota_text(value: object, limit: int = 160) -> str:
    return " ".join(str(value or "").replace("\x00", "").split())[:limit]


def quota_remaining_percent(used_percent: float | None) -> float | None:
    if used_percent is None:
        return None
    return max(0.0, min(100.0, 100.0 - used_percent))


def quota_window_payload(name: str, value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used = quota_float(value.get("usedPercent") if "usedPercent" in value else value.get("used_percent"))
    duration = quota_int(value.get("windowDurationMins") if "windowDurationMins" in value else value.get("window_duration_mins"))
    resets_at = quota_int(value.get("resetsAt") if "resetsAt" in value else value.get("resets_at"))
    if used is None and duration is None and resets_at is None:
        return None
    remaining = quota_remaining_percent(used)
    if duration == 300:
        window_kind = "five_hour"
        label = "5 hour"
    elif duration == 10080:
        window_kind = "weekly"
        label = "weekly"
    else:
        window_kind = "custom"
        label = f"{duration} minute" if duration else name
    return {
        "name": name,
        "windowKind": window_kind,
        "label": label,
        "usedPercent": round(used, 3) if used is not None else None,
        "remainingPercent": round(remaining, 3) if remaining is not None else None,
        "windowDurationMins": duration,
        "resetsAt": resets_at,
    }


def quota_compact_text(value: object) -> str:
    return "".join(ch for ch in quota_text(value, 240).lower() if ch.isalnum())


def quota_bucket_identity(limit_id: object, value: object) -> str:
    if not isinstance(value, dict):
        return quota_text(limit_id, 240).lower()
    return " ".join(
        part
        for part in (
            quota_text(limit_id, 120),
            quota_text(value.get("limitId") or value.get("limit_id"), 120),
            quota_text(value.get("limitName") or value.get("limit_name"), 160),
        )
        if part
    ).lower()


def codex_quota_preferred_models(config: Any | None = None) -> list[str]:
    configured = quota_text(getattr(config, "codex_model", None), 80) if config is not None else ""
    env_model = quota_text(os.environ.get("PULLWISE_CODEX_MODEL"), 80)
    models = [configured, env_model, "gpt-5.5", "gpt-5.4", "gpt-5"]
    seen: set[str] = set()
    preferred: list[str] = []
    for model in models:
        compact = quota_compact_text(model)
        if not compact or compact in seen:
            continue
        seen.add(compact)
        preferred.append(model)
    return preferred


def codex_quota_bucket_is_spark(limit_id: object, value: object) -> bool:
    identity = quota_bucket_identity(limit_id, value)
    compact = quota_compact_text(identity)
    return "spark" in identity or "bengalfox" in compact


def codex_quota_bucket_matches_model(limit_id: object, value: object, preferred_models: list[str]) -> bool:
    compact_identity = quota_compact_text(quota_bucket_identity(limit_id, value))
    return any(quota_compact_text(model) in compact_identity for model in preferred_models)


def codex_rate_limit_snapshot(response: dict[str, Any], preferred_models: list[str] | None = None) -> dict[str, Any]:
    preferred_models = preferred_models or codex_quota_preferred_models()
    by_limit = response.get("rateLimitsByLimitId") if isinstance(response.get("rateLimitsByLimitId"), dict) else None
    if by_limit:
        for key, value in by_limit.items():
            if (
                isinstance(value, dict)
                and not codex_quota_bucket_is_spark(key, value)
                and codex_quota_bucket_matches_model(key, value, preferred_models)
            ):
                return value
        for key, value in by_limit.items():
            if quota_text(key).lower() == "codex" and isinstance(value, dict) and not codex_quota_bucket_is_spark(key, value):
                return value
        for key, value in by_limit.items():
            identity = quota_bucket_identity(key, value)
            if "codex" in identity and isinstance(value, dict) and not codex_quota_bucket_is_spark(key, value):
                return value
    rate_limits = response.get("rateLimits")
    if isinstance(rate_limits, dict) and not codex_quota_bucket_is_spark(rate_limits.get("limitId") or rate_limits.get("limit_id"), rate_limits):
        return rate_limits
    return {}

def merge_rate_limit_bucket(current: object, update: object) -> dict[str, Any]:
    merged = dict(current) if isinstance(current, dict) else {}
    if not isinstance(update, dict):
        return merged
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            nested = dict(merged[key])
            nested.update(value)
            merged[key] = nested
        elif value is not None:
            merged[key] = value
    return merged


def merge_rate_limit_response(current: object, params: object) -> dict[str, Any]:
    merged = dict(current) if isinstance(current, dict) else {}
    if not isinstance(params, dict):
        return merged
    rate_limits_update = params.get("rateLimits") if isinstance(params.get("rateLimits"), dict) else None
    if rate_limits_update is None and any(
        key in params
        for key in ("limitId", "limit_id", "primary", "secondary", "rateLimitReachedType", "rate_limit_reached_type")
    ):
        rate_limits_update = params
    if rate_limits_update is not None:
        merged["rateLimits"] = merge_rate_limit_bucket(merged.get("rateLimits"), rate_limits_update)
        limit_id = quota_text(merged["rateLimits"].get("limitId") or merged["rateLimits"].get("limit_id"), 80) or "codex"
        by_limit = dict(merged.get("rateLimitsByLimitId")) if isinstance(merged.get("rateLimitsByLimitId"), dict) else {}
        by_limit[limit_id] = merge_rate_limit_bucket(by_limit.get(limit_id), merged["rateLimits"])
        merged["rateLimitsByLimitId"] = by_limit
    by_limit_update = params.get("rateLimitsByLimitId") if isinstance(params.get("rateLimitsByLimitId"), dict) else None
    if by_limit_update is not None:
        by_limit = dict(merged.get("rateLimitsByLimitId")) if isinstance(merged.get("rateLimitsByLimitId"), dict) else {}
        for limit_id, bucket in by_limit_update.items():
            if isinstance(bucket, dict):
                key = quota_text(limit_id, 80) or "codex"
                by_limit[key] = merge_rate_limit_bucket(by_limit.get(key), bucket)
                if key.lower() == "codex":
                    merged["rateLimits"] = merge_rate_limit_bucket(merged.get("rateLimits"), by_limit[key])
        merged["rateLimitsByLimitId"] = by_limit
    reset_update = params.get("rateLimitResetCredits") if isinstance(params.get("rateLimitResetCredits"), dict) else None
    if reset_update is not None:
        current_reset = merged.get("rateLimitResetCredits") if isinstance(merged.get("rateLimitResetCredits"), dict) else {}
        merged["rateLimitResetCredits"] = merge_rate_limit_bucket(current_reset, reset_update)
    return merged

def codex_quota_payload_from_rate_limits(
    response: dict[str, Any],
    *,
    threshold_percent: float,
    checked_at: int,
    next_check_at: int,
    preferred_models: list[str] | None = None,
) -> dict[str, Any]:
    snapshot = codex_rate_limit_snapshot(response, preferred_models=preferred_models)
    if not snapshot:
        return {
            "provider": "codex",
            "status": "unavailable",
            "ready": True,
            "reason": "codex_quota_unavailable",
            "checkedAt": checked_at,
            "nextCheckAt": next_check_at,
            "thresholdPercent": threshold_percent,
        }
    windows = []
    for name in ("primary", "secondary"):
        window = quota_window_payload(name, snapshot.get(name))
        if window:
            windows.append(window)
    reset_source = response.get("rateLimitResetCredits") if isinstance(response.get("rateLimitResetCredits"), dict) else {}
    reset_credits = {
        "availableCount": quota_int(reset_source.get("availableCount")) if reset_source else None,
    }
    credits_source = snapshot.get("credits") if isinstance(snapshot.get("credits"), dict) else {}
    credits = {
        "hasCredits": bool(credits_source.get("hasCredits")) if "hasCredits" in credits_source else None,
        "unlimited": bool(credits_source.get("unlimited")) if "unlimited" in credits_source else None,
        "balance": quota_text(credits_source.get("balance"), 80) if credits_source.get("balance") is not None else None,
    }
    remaining_values = [window["remainingPercent"] for window in windows if window.get("remainingPercent") is not None]
    used_values = [window["usedPercent"] for window in windows if window.get("usedPercent") is not None]
    remaining = min(remaining_values) if remaining_values else None
    used = max(used_values) if used_values else None
    blocked_windows = [window for window in windows if window.get("remainingPercent") is not None and window["remainingPercent"] <= threshold_percent]
    reached_type = quota_text(snapshot.get("rateLimitReachedType") or snapshot.get("rate_limit_reached_type"), 120)
    exhausted = bool(reached_type) or any(window.get("remainingPercent") is not None and window["remainingPercent"] <= 0 for window in windows)
    low = bool(blocked_windows)
    status = "exhausted" if exhausted else "low" if low else "ok"
    ready = status == "ok"
    reason = "" if ready else "codex_quota_exhausted" if exhausted else "codex_quota_low"
    return {
        "provider": "codex",
        "limitId": quota_text(snapshot.get("limitId") or snapshot.get("limit_id"), 80) or None,
        "limitName": quota_text(snapshot.get("limitName") or snapshot.get("limit_name"), 120) or None,
        "planType": quota_text(snapshot.get("planType") or snapshot.get("plan_type"), 80) or None,
        "status": status,
        "ready": ready,
        "reason": reason,
        "checkedAt": checked_at,
        "nextCheckAt": next_check_at,
        "thresholdPercent": threshold_percent,
        "usedPercent": round(used, 3) if used is not None else None,
        "remainingPercent": round(remaining, 3) if remaining is not None else None,
        "rateLimitReachedType": reached_type or None,
        "rateLimitResetCredits": reset_credits,
        "credits": credits,
        "windows": windows,
        "blockedWindows": blocked_windows,
    }


def codex_quota_check_seconds(config: Any, *, degraded: bool = False) -> int:
    if degraded:
        default = int(getattr(config, "degraded_readiness_check_seconds", 300) or 300)
        return max(10, int(getattr(config, "codex_quota_degraded_check_seconds", default) or default))
    default = int(getattr(config, "active_readiness_check_seconds", 300) or 300)
    return max(10, int(getattr(config, "codex_quota_check_seconds", default) or default))


def codex_quota_threshold_percent(config: Any) -> float:
    raw = quota_float(getattr(config, "codex_quota_min_remaining_percent", 5.0))
    if raw is None:
        return 5.0
    return max(0.0, min(100.0, raw))


def quota_refresh_error_is_exhaustion(error: object) -> bool:
    if codex_error_code(error) != "CODEX_QUOTA_EXHAUSTED":
        return False
    lowered = str(error or "").lower()
    fetch_failure_markers = (
        "authentication required to read rate limits",
        "account authentication required",
        "auth required to read rate limits",
        "unauthorized to read rate limits",
        "endpoint unavailable",
        "unavailable",
        "timed out",
        "timeout",
        "connection",
        "method not found",
        "unknown method",
        "not found",
        "failed to read",
        "failed to fetch",
        "error sending request",
    )
    return not any(marker in lowered for marker in fetch_failure_markers)


class CodexQuotaMonitor:
    def __init__(
        self,
        config: Any,
        isolation: Isolation,
        codex_client_provider: Callable[[], CodexSdkClient] | None = None,
    ) -> None:
        self.config = config
        self.isolation = isolation
        self.codex_client_provider = codex_client_provider
        self.snapshot: dict[str, Any] | None = None
        self.rate_limits_response: dict[str, Any] = {}
        self.next_check_at = 0
        self._lock = threading.RLock()

    def snapshot_if_due(self, *, active: bool = False) -> dict[str, Any] | None:
        with self._lock:
            current_time = int(time.time())
            if active and self.snapshot is not None:
                return self.snapshot
            if active or current_time < self.next_check_at:
                return self.snapshot
            return self.refresh(current_time)

    def refresh(self, current_time: int | None = None) -> dict[str, Any]:
        with self._lock:
            return self._refresh(current_time)

    def _refresh(self, current_time: int | None = None) -> dict[str, Any]:
        checked_at = int(current_time if current_time is not None else time.time())
        threshold = codex_quota_threshold_percent(self.config)
        interval = codex_quota_check_seconds(self.config)
        next_check_at = checked_at + interval
        server: CodexSdkClient | None = None
        close_server = False
        try:
            if self.codex_client_provider is not None:
                server = self.codex_client_provider()
            else:
                self.isolation.runtime.mkdir(parents=True, exist_ok=True)
                self.isolation.logs.mkdir(parents=True, exist_ok=True)
                server = CodexSdkClient(
                    scoped_codex_command(self.config),
                    self.isolation.env(self.config),
                    self.isolation.runtime,
                    self.isolation.logs / "codex-quota-events.jsonl",
                )
                close_server = True
                server.start()
            response = server.request("account/rateLimits/read", {}, timeout_seconds=15)
            self.rate_limits_response = response
            self.snapshot = codex_quota_payload_from_rate_limits(
                response,
                threshold_percent=threshold,
                checked_at=checked_at,
                next_check_at=next_check_at,
                preferred_models=codex_quota_preferred_models(self.config),
            )
        except Exception as exc:
            if quota_refresh_error_is_exhaustion(exc):
                self.mark_exhausted(str(exc), checked_at=checked_at)
            else:
                degraded_next_check_at = checked_at + codex_quota_check_seconds(
                    self.config,
                    degraded=True,
                )
                self.snapshot = {
                    "provider": "codex",
                    "status": "unavailable",
                    "ready": False,
                    "reason": "codex_quota_unavailable",
                    "checkedAt": checked_at,
                    "nextCheckAt": degraded_next_check_at,
                    "thresholdPercent": threshold,
                    "lastError": quota_text(exc, 500),
                }
        finally:
            if close_server and server is not None:
                server.close()
        self.next_check_at = int((self.snapshot or {}).get("nextCheckAt") or next_check_at)
        return self.snapshot or {}

    def apply_rate_limit_update(self, params: dict[str, Any]) -> None:
        with self._lock:
            self._apply_rate_limit_update(params)

    def _apply_rate_limit_update(self, params: dict[str, Any]) -> None:
        current_time = int(time.time())
        threshold = codex_quota_threshold_percent(self.config)
        degraded = bool(self.snapshot) and not bool((self.snapshot or {}).get("ready", True))
        next_check_at = current_time + codex_quota_check_seconds(self.config, degraded=degraded)
        self.rate_limits_response = merge_rate_limit_response(self.rate_limits_response, params)
        if not self.rate_limits_response:
            return
        self.snapshot = codex_quota_payload_from_rate_limits(
            self.rate_limits_response,
            threshold_percent=threshold,
            checked_at=current_time,
            next_check_at=next_check_at,
            preferred_models=codex_quota_preferred_models(self.config),
        )
        self.next_check_at = int((self.snapshot or {}).get("nextCheckAt") or next_check_at)

    def mark_exhausted(self, error: object, *, checked_at: int | None = None) -> dict[str, Any]:
        with self._lock:
            return self._mark_exhausted(error, checked_at=checked_at)

    def _mark_exhausted(self, error: object, *, checked_at: int | None = None) -> dict[str, Any]:
        current_time = int(checked_at if checked_at is not None else time.time())
        threshold = codex_quota_threshold_percent(self.config)
        next_check_at = current_time + codex_quota_check_seconds(self.config, degraded=True)
        self.snapshot = {
            "provider": "codex",
            "status": "exhausted",
            "ready": False,
            "reason": "codex_quota_exhausted",
            "checkedAt": current_time,
            "nextCheckAt": next_check_at,
            "thresholdPercent": threshold,
            "remainingPercent": 0.0,
            "lastError": quota_text(error, 500),
        }
        self.next_check_at = next_check_at
        return self.snapshot

READ_ONLY_COMMANDS = {"find", "wc", "cat", "grep", "rg"}
GIT_READ_ONLY_SUBCOMMANDS = {"status", "diff", "log", "show", "ls-files", "rev-parse", "grep"}
DENIED_COMMAND_TOKENS = {
    "brew",
    "checkout",
    "commit",
    "curl",
    "install",
    "pip",
    "push",
    "reset",
    "rm",
    "clean",
    "wget",
}
DENIED_INTENT_EXECUTABLES = {
    "bash",
    "brew",
    "cmd",
    "curl",
    "npx",
    "pip",
    "pip3",
    "powershell",
    "pwsh",
    "sh",
    "wget",
}
SHELL_CONTROL_TOKENS = {"|", "||", "&&", ";", "&", ">", ">>", "<", "<<", "<<<"}
CODEX_APPROVAL_RESPONSE_METHODS = {
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
}
LEGACY_CODEX_APPROVAL_RESPONSE_METHODS = {"execCommandApproval", "applyPatchApproval"}


def approval_response_for_request(message: dict[str, Any], workspace: Path) -> dict[str, Any]:
    decision, _reason = decide_approval(message, workspace)
    if str(message.get("method") or "") in LEGACY_CODEX_APPROVAL_RESPONSE_METHODS:
        legacy_decision = "approved_for_session" if decision in {"accept", "acceptForSession"} else "denied"
        return {"decision": legacy_decision}
    return {"decision": decision}


def decide_approval(message: dict[str, Any], workspace: Path) -> tuple[str, str]:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    request = params.get("request") if isinstance(params.get("request"), dict) else params
    request_type = str(request.get("type") or request.get("kind") or message.get("method") or "").lower()
    if "file" in request_type or request.get("paths") or request.get("path") or request.get("grantRoot") or request.get("fileChanges"):
        return "decline", "file-change approvals are disabled; writable turns use an isolated sandbox cwd"
    if "command" in request_type or request.get("command") or request.get("argv"):
        command = request.get("argv") if isinstance(request.get("argv"), list) else request.get("command")
        if command_is_allowed(command, workspace, request.get("cwd")):
            return "acceptForSession", "command is an allowed mechanical helper"
        return "decline", "command is not allowed by worker policy"
    return "decline", "unknown approval request type"


def path_is_under_codex_review(workspace: Path, raw_path: object) -> bool:
    if not raw_path:
        return False
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = workspace / path
    try:
        path.resolve(strict=False).relative_to((workspace / ".codex-review").resolve(strict=False))
    except ValueError:
        return False
    return True


def path_is_under_validation_workspace(workspace: Path, raw_path: object) -> bool:
    if not raw_path:
        return False
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = workspace / path
    validation_root = workspace.parent / "validation-repo"
    try:
        path.resolve(strict=False).relative_to(validation_root.resolve(strict=False))
    except ValueError:
        return False
    return True


def path_is_under_allowed_write_root(workspace: Path, raw_path: object) -> bool:
    return path_is_under_codex_review(workspace, raw_path)


def command_is_allowed(command: object, workspace: Path, raw_cwd: object = None) -> bool:
    try:
        argv = [str(part) for part in command] if isinstance(command, list) else shlex.split(str(command or ""))
    except ValueError:
        return False
    if not argv:
        return False
    executable = normalized_executable_name(argv[0])
    lowered = {part.lower() for part in argv}
    if lowered.intersection(DENIED_COMMAND_TOKENS) or command_has_shell_control(argv):
        return False
    cwd = Path(str(raw_cwd)) if raw_cwd else workspace
    if not cwd.is_absolute():
        cwd = workspace / cwd
    cwd_in_workspace = False
    try:
        cwd.resolve(strict=False).relative_to(workspace.resolve(strict=False))
        cwd_in_workspace = True
    except ValueError:
        cwd_in_workspace = False
    if not cwd_in_workspace:
        return False
    if executable == "git":
        return git_command_is_read_only(argv, workspace, cwd)
    if executable in READ_ONLY_COMMANDS:
        return read_command_operands_are_contained(executable, argv, workspace, cwd)
    return False


def command_has_shell_control(argv: list[str]) -> bool:
    for part in argv:
        if part in SHELL_CONTROL_TOKENS or "\n" in part or "\r" in part or "`" in part or "$(" in part:
            return True
    return False


def read_operand_is_contained(raw_path: object, workspace: Path, cwd: Path) -> bool:
    value = str(raw_path or "").strip()
    if not value or value == "-":
        return False
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return path_is_under(candidate, workspace) or path_is_under_validation_workspace(workspace, candidate)


def simple_read_file_operands(
    argv: list[str],
    *,
    safe_short_options: set[str],
    safe_long_options: set[str],
) -> list[str] | None:
    operands: list[str] = []
    options_finished = False
    for part in argv[1:]:
        if not options_finished and part == "--":
            options_finished = True
            continue
        if not options_finished and part.startswith("-"):
            if part == "-":
                return None
            if part.startswith("--"):
                if part not in safe_long_options:
                    return None
            elif not set(part[1:]).issubset(safe_short_options):
                return None
            continue
        operands.append(part)
    return operands or None


def grep_read_operands(argv: list[str]) -> list[str] | None:
    safe_short = set("EFGHhILlnoqrsvwxyZ")
    safe_long = {
        "--basic-regexp",
        "--extended-regexp",
        "--fixed-strings",
        "--ignore-case",
        "--invert-match",
        "--line-number",
        "--files-with-matches",
        "--files-without-match",
        "--only-matching",
        "--quiet",
        "--recursive",
        "--no-messages",
        "--word-regexp",
        "--line-regexp",
        "--text",
        "--binary",
    }
    positionals: list[str] = []
    options_finished = False
    for part in argv[1:]:
        if not options_finished and part == "--":
            options_finished = True
            continue
        if not options_finished and part.startswith("-"):
            if part == "-" or (part.startswith("--") and part not in safe_long):
                return None
            if not part.startswith("--") and not set(part[1:]).issubset(safe_short):
                return None
            continue
        positionals.append(part)
    if len(positionals) < 2:
        return None
    return positionals[1:]


def rg_read_operands(argv: list[str]) -> list[str] | None:
    safe_short_flags = set("0aFHhilLnoqSsvwxyNPU")
    safe_long_flags = {
        "--files",
        "--hidden",
        "--ignore-case",
        "--invert-match",
        "--line-number",
        "--files-with-matches",
        "--files-without-match",
        "--fixed-strings",
        "--only-matching",
        "--quiet",
        "--no-messages",
        "--smart-case",
        "--text",
        "--unrestricted",
        "--word-regexp",
        "--line-regexp",
        "--pcre2",
        "--multiline",
    }
    value_options = {"-g", "--glob", "-t", "--type", "-T", "--type-not", "-A", "-B", "-C", "--max-depth"}
    pattern_options = {"-e", "--regexp"}
    positionals: list[str] = []
    options_finished = False
    pattern_from_option = False
    files_mode = False
    index = 1
    while index < len(argv):
        part = argv[index]
        if not options_finished and part == "--":
            options_finished = True
            index += 1
            continue
        if not options_finished and part in value_options | pattern_options:
            if index + 1 >= len(argv):
                return None
            pattern_from_option = pattern_from_option or part in pattern_options
            index += 2
            continue
        if not options_finished and any(part.startswith(f"{option}=") for option in value_options | pattern_options if option.startswith("--")):
            pattern_from_option = pattern_from_option or part.startswith("--regexp=")
            index += 1
            continue
        if not options_finished and (part.startswith("-g") or part.startswith("-t") or part.startswith("-T") or part.startswith("-e")) and len(part) > 2:
            pattern_from_option = pattern_from_option or part.startswith("-e")
            index += 1
            continue
        if not options_finished and part.startswith("-"):
            if part == "-" or (part.startswith("--") and part not in safe_long_flags):
                return None
            if not part.startswith("--") and not set(part[1:]).issubset(safe_short_flags):
                return None
            files_mode = files_mode or part == "--files"
            index += 1
            continue
        positionals.append(part)
        index += 1
    if files_mode or pattern_from_option:
        return positionals
    if not positionals:
        return None
    return positionals[1:]


def find_read_roots(argv: list[str]) -> list[str] | None:
    unsafe = {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
        "-files0-from",
        "-follow",
        "-newer",
        "-anewer",
        "-cnewer",
        "-samefile",
        "-l",
        "-h",
    }
    lowered = {str(part).lower() for part in argv[1:]}
    if lowered.intersection(unsafe):
        return None
    roots: list[str] = []
    for part in argv[1:]:
        if part == "-P" and not roots:
            continue
        if part.startswith("-") or part in {"!", "(", ")", ","}:
            break
        roots.append(part)
    return roots


def read_command_operands_are_contained(executable: str, argv: list[str], workspace: Path, cwd: Path) -> bool:
    operands: list[str] | None
    if executable == "cat":
        operands = simple_read_file_operands(
            argv,
            safe_short_options=set("AbEeEnstTuv"),
            safe_long_options={
                "--show-all",
                "--number-nonblank",
                "--show-ends",
                "--number",
                "--squeeze-blank",
                "--show-tabs",
                "--show-nonprinting",
            },
        )
    elif executable == "wc":
        operands = simple_read_file_operands(
            argv,
            safe_short_options=set("cLlmw"),
            safe_long_options={"--bytes", "--chars", "--lines", "--max-line-length", "--words"},
        )
    elif executable == "grep":
        operands = grep_read_operands(argv)
    elif executable == "rg":
        operands = rg_read_operands(argv)
        if operands == []:
            operands = [str(cwd)]
    elif executable == "find":
        operands = find_read_roots(argv)
        if operands == []:
            operands = [str(cwd)]
    else:
        return False
    return operands is not None and all(read_operand_is_contained(part, workspace, cwd) for part in operands)


def git_command_is_read_only(argv: list[str], workspace: Path, cwd: Path) -> bool:
    unsafe_options = {
        "--no-index",
        "--ext-diff",
        "--textconv",
        "--open-files-in-pager",
        "--output",
        "--paginate",
    }
    lowered = {str(part).lower() for part in argv[1:]}
    if lowered.intersection(unsafe_options) or any(
        str(part).lower().startswith(f"{option}=")
        for part in argv[1:]
        for option in unsafe_options
    ):
        return False
    option_parts = argv[1 : argv.index("--") if "--" in argv else len(argv)]
    if any(
        part == "-O"
        or (
            part.startswith("-")
            and not part.startswith("--")
            and "O" in part[1:]
        )
        for part in option_parts
    ):
        return False
    if "--" in argv:
        separator = argv.index("--")
        if not all(read_operand_is_contained(part, workspace, cwd) for part in argv[separator + 1 :]):
            return False
    index = 1
    while index < len(argv):
        part = argv[index]
        if part in {"-C", "--git-dir", "--work-tree"}:
            if index + 1 >= len(argv):
                return False
            candidate = Path(argv[index + 1])
            if not candidate.is_absolute():
                candidate = cwd / candidate
            if not (path_is_under(candidate, workspace) or path_is_under_validation_workspace(workspace, candidate)):
                return False
            index += 2
            continue
        if part == "--no-pager" or part.startswith("--no-"):
            index += 1
            continue
        if part == "-p":
            return False
        if part.startswith("-"):
            return False
        return part.lower() in GIT_READ_ONLY_SUBCOMMANDS
    return False


def normalized_executable_name(value: object) -> str:
    name = Path(str(value or "")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def package_json_has_test_script(package_json_path: Path, command: list[str] | None = None) -> bool:
    try:
        payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    scripts = payload.get("scripts") if isinstance(payload, dict) and isinstance(payload.get("scripts"), dict) else {}
    if not scripts:
        return False
    if command is None:
        return "test" in scripts or any(str(key).startswith("test:") for key in scripts)
    lowered = [str(part).lower() for part in command]
    if len(lowered) >= 2 and lowered[1] == "test":
        return "test" in scripts
    if "run" in lowered:
        run_index = lowered.index("run")
        if len(lowered) > run_index + 1:
            script_name = str(command[run_index + 1])
            return script_name in scripts and (script_name == "test" or script_name.startswith("test:"))
    for part in command[1:]:
        script_name = str(part)
        if script_name in scripts and (script_name == "test" or script_name.startswith("test:")):
            return True
    return False


def package_json_test_script(package_json_path: Path, command: list[str]) -> str:
    try:
        payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    scripts = payload.get("scripts") if isinstance(payload, dict) and isinstance(payload.get("scripts"), dict) else {}
    lowered = [str(part).lower() for part in command]
    script_name = ""
    if len(lowered) >= 2 and lowered[1] == "test":
        script_name = "test"
    elif "run" in lowered:
        run_index = lowered.index("run")
        if len(command) > run_index + 1:
            script_name = str(command[run_index + 1])
    if not script_name:
        script_name = next(
            (
                str(part)
                for part in command[1:]
                if str(part) in scripts and (str(part) == "test" or str(part).startswith("test:"))
            ),
            "",
        )
    value = scripts.get(script_name)
    return str(value or "").strip()


def node_package_script_dependency_error(package_json_path: Path, command: list[str]) -> str:
    script = package_json_test_script(package_json_path, command)
    if not script:
        return ""
    try:
        tokens = shlex.split(script, posix=True)
    except ValueError:
        return ""
    executable = ""
    for token in tokens:
        text = str(token).strip()
        if not text or text in {"&&", "||", ";", "|"} or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", text):
            continue
        executable = text
        break
    if not executable:
        return ""
    executable_name = Path(executable).name
    allowed_runtime_commands = {
        "bash",
        "bun",
        "echo",
        "node",
        "npm",
        "pnpm",
        "python",
        "python3",
        "sh",
        "true",
        "yarn",
    }
    if executable_name in allowed_runtime_commands:
        return ""
    if "/" in executable or "\\" in executable:
        candidate = (package_json_path.parent / executable).resolve(strict=False)
        if candidate.is_file():
            return ""
    else:
        local_bin = package_json_path.parent / "node_modules" / ".bin" / executable_name
        if local_bin.is_file():
            return ""
    return f"dependency_missing: package test script requires unavailable local executable {executable_name}"


def _command_executable_available(command: list[str], cwd: Path) -> tuple[bool, str]:
    if not command:
        return False, "command is empty"
    executable = str(command[0])
    executable_name = normalized_executable_name(executable)
    path = Path(executable)
    if path.is_absolute():
        if path.is_file() and os.access(path, os.X_OK):
            return True, ""
        return False, f"dependency_missing: {executable_name} executable is not available"
    if not path.is_absolute() and ("/" in executable or chr(92) in executable):
        contained_candidate = cwd / path
        if (
            contained_candidate.is_file()
            and not contained_candidate.is_symlink()
            and os.access(contained_candidate, os.X_OK)
        ):
            return True, ""
        return False, f"dependency_missing: {executable_name} executable is not available"
    if shutil.which(executable) is not None:
        return True, ""
    return False, f"dependency_missing: {executable_name} executable is not available"


def _python_module_available(
    executable: str,
    module_name: str,
    *,
    cwd: Path,
) -> bool:
    probe = (
        "import importlib.util; "
        f"raise SystemExit(0 if importlib.util.find_spec({module_name!r}) else 1)"
    )
    try:
        completed = subprocess.run(
            [executable, "-I", "-c", probe],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _node_package_json_for_cwd(cwd: Path, validation_repo: Path) -> Path:
    current = cwd.resolve(strict=False)
    root = validation_repo.resolve(strict=False)
    while path_is_under(current, root):
        candidate = current / "package.json"
        if candidate.is_file():
            return candidate
        if current == root:
            break
        current = current.parent
    return validation_repo / "package.json"


def intent_command_is_runnable_for_repo(command: list[str], cwd: Path, validation_repo: Path, profile: dict[str, Any] | None) -> tuple[bool, str]:
    argv = [str(part) for part in command if str(part).strip()]
    if not argv:
        return False, "skipped_not_runnable: command is empty"
    executable = normalized_executable_name(argv[0])
    lowered = [part.lower() for part in argv]
    available, reason = _command_executable_available(argv, cwd)
    if not available:
        return False, reason
    if executable in {"npm", "pnpm", "yarn"}:
        package_json = _node_package_json_for_cwd(cwd, validation_repo)
        if not package_json.is_file():
            return False, "skipped_not_runnable: package.json is missing"
        if not package_json_has_test_script(package_json, argv):
            return False, "skipped_not_runnable: package.json has no test script"
        dependency_error = node_package_script_dependency_error(package_json, argv)
        if dependency_error:
            return False, dependency_error
    if executable in {"terraform", "helm", "kubectl"}:
        return False, "skipped_not_runnable: external provider or cluster initialization is not allowed"
    if _is_python_intent_executable(executable) and len(lowered) >= 3 and lowered[1] == "-m" and lowered[2] == "pytest":
        if not _python_module_available(argv[0], "pytest", cwd=cwd):
            return False, "dependency_missing: pytest is not available"
    return True, "runnable"


def _intent_preflight_classification(reason: str) -> str:
    if reason.startswith("dependency_missing:"):
        return "dependency_missing"
    if reason.startswith("environment_error:"):
        return "environment_error"
    return "skipped_not_runnable"


AGENT_TEST_COMMAND_MARKERS = ("test", "spec", "check", "verify", "behavior", "intent")


def _generic_agent_test_command_allowed(
    argv: list[str],
    cwd: Path,
    validation_repo: Path,
) -> tuple[bool, str]:
    """Admit safe agent-proposed tests without encoding a framework matrix."""

    if not argv:
        return False, "agent-proposed command is empty"
    executable_path = Path(argv[0])
    contained_executable = (
        executable_path.is_absolute()
        and executable_path.is_file()
        and not executable_path.is_symlink()
        and path_is_under(executable_path, validation_repo)
    )
    test_intent = any(
        marker in normalized_executable_name(argv[0])
        for marker in AGENT_TEST_COMMAND_MARKERS
    )
    for raw_argument in argv[1:]:
        argument = str(raw_argument).strip()
        lowered = argument.lower()
        if not argument or argument.startswith("-"):
            continue
        if re.match(r"^[a-z][a-z0-9+.-]*://", lowered):
            return False, "agent-proposed test commands may not contain network URLs"
        candidate = Path(argument)
        path_like = candidate.is_absolute() or "/" in argument or chr(92) in argument
        if path_like:
            if not candidate.is_absolute():
                candidate = cwd / candidate
            if not path_is_under(candidate, validation_repo):
                return False, "agent-proposed command references a path outside the validation workspace"
            if candidate.exists() and path_is_under(candidate, validation_repo):
                test_intent = test_intent or any(
                    marker in candidate.name.lower()
                    for marker in AGENT_TEST_COMMAND_MARKERS
                )
        test_intent = test_intent or any(marker in lowered for marker in AGENT_TEST_COMMAND_MARKERS)
    if contained_executable and test_intent:
        return True, "contained agent-proposed test runner is allowed"
    if test_intent:
        return True, "agent-proposed test command is allowed"
    return False, "agent-proposed command does not identify a contained test or test operation"


def _is_python_intent_executable(executable: str) -> bool:
    return executable in {"python", "python3", "py"} or re.fullmatch(r"python3(?:\.\d+)+", executable) is not None

def intent_test_command_policy(command: list[str], cwd: Path, validation_repo: Path) -> tuple[bool, str]:
    argv = [str(part) for part in command if str(part).strip()]
    if not argv:
        return False, "command is empty"
    if not path_is_under(cwd, validation_repo):
        return False, "cwd escapes validation workspace"
    executable = normalized_executable_name(argv[0])
    if executable in DENIED_INTENT_EXECUTABLES:
        return False, f"{executable} is not allowed"
    lowered = [part.lower() for part in argv]
    shell_control = {"&&", "||", ";", "|"}
    if any(part in shell_control for part in lowered):
        return False, "shell control operators are not allowed"
    denied = sorted(set(lowered).intersection(DENIED_COMMAND_TOKENS))
    if denied:
        return False, f"command contains denied token {denied[0]}"
    for raw_argument in argv[1:]:
        argument = str(raw_argument).strip()
        if re.search(r"[a-z][a-z0-9+.-]*://", argument, flags=re.IGNORECASE):
            return False, "test commands may not contain network URLs"
        if not argument or argument.startswith("-"):
            continue
        candidate = Path(argument)
        if candidate.is_absolute() or "/" in argument or chr(92) in argument:
            if not candidate.is_absolute():
                candidate = cwd / candidate
            if not path_is_under(candidate, validation_repo):
                return False, "test command references a path outside the validation workspace"
    if executable == "pytest":
        return True, "pytest is allowed"
    if _is_python_intent_executable(executable):
        if len(lowered) >= 3 and lowered[1] == "-m" and lowered[2] in {"pytest", "unittest"}:
            return True, f"python -m {lowered[2]} is allowed"
        if len(argv) >= 2 and not lowered[1].startswith("-"):
            script_path = Path(argv[1])
            if not script_path.is_absolute():
                script_path = cwd / script_path
            if path_is_under(script_path, validation_repo) and script_path.suffix == ".py":
                script_name = script_path.name.lower()
                if script_name.startswith("test") or "_test" in script_name:
                    return True, "python test file is allowed"
        return False, "python command must run pytest, unittest, or a test file"
    if executable in {"npm", "pnpm", "yarn"}:
        if any(part == "test" or part.startswith("test:") for part in lowered[1:]):
            return True, f"{executable} test command is allowed"
        return False, f"{executable} command must run a test script"
    if executable == "node":
        if len(argv) < 3 or lowered[1] != "--test":
            return False, "node command must use --test with a contained test file"
        for raw_path in argv[2:]:
            if raw_path.startswith("-"):
                return False, "node --test options are not allowed"
            test_path = Path(raw_path)
            if not test_path.is_absolute():
                test_path = cwd / test_path
            if not path_is_under(test_path, validation_repo) or test_path.suffix.lower() not in {".js", ".mjs", ".cjs"}:
                return False, "node --test path must be a contained JavaScript test file"
        return True, "node --test is allowed"
    if executable == "go":
        return (len(lowered) >= 2 and lowered[1] == "test", "go command must be go test")
    if executable == "cargo":
        return (len(lowered) >= 2 and lowered[1] == "test", "cargo command must be cargo test")
    if executable == "mvn":
        return ("test" in lowered[1:], "mvn command must run test")
    if executable == "gradle":
        return ("test" in lowered[1:] or any(part.endswith(":test") for part in lowered[1:]), "gradle command must run test")
    if executable == "make":
        return ("test" in lowered[1:], "make command must run test")
    return _generic_agent_test_command_allowed(argv, cwd, validation_repo)


def _intent_blocked_execution_diagnostic(
    reason: str,
    *,
    reason_code: str = "not_runnable",
    classification: str = "skipped_not_runnable",
    agent_repairable: bool = True,
    missing_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    lowered = reason.lower()
    missing = list(missing_capabilities or [])
    local_runner = re.search(r"unavailable local executable ([^\s]+)", reason)
    unavailable_executable = re.search(r"dependency_missing:\s*([^\s]+) executable is not available", reason)
    if local_runner:
        runner = local_runner.group(1)
        reason_code = "package_local_runner_missing"
        classification = "dependency_missing"
        missing = [f"node_modules/.bin/{runner}"]
    elif unavailable_executable:
        executable = unavailable_executable.group(1)
        reason_code = "executable_missing"
        classification = "dependency_missing"
        missing = [executable]
    elif reason.startswith("dependency_missing:"):
        reason_code = "project_dependency_missing"
        classification = "dependency_missing"
        if "pytest" in lowered:
            missing = ["python module pytest"]
    elif "package.json is missing" in lowered:
        reason_code = "package_manifest_missing"
    elif "no test script" in lowered:
        reason_code = "package_test_script_missing"
    elif "command is empty" in lowered or "no generated test command" in lowered:
        reason_code = "command_missing"
    elif "cwd escapes" in lowered:
        reason_code = "cwd_escape"
    elif "cwd does not exist" in lowered:
        reason_code = "cwd_missing"
    elif "sandbox" in lowered:
        reason_code = "sandbox_unavailable"
        classification = "environment_error"
        agent_repairable = False
    display_reason = reason
    for prefix in ("dependency_missing: ", "environment_error: ", "skipped_not_runnable: "):
        if display_reason.startswith(prefix):
            display_reason = display_reason[len(prefix):]
            break
    return {
        "status": "blocked",
        "reason_code": reason_code,
        "reason": display_reason or "not runnable",
        "classification": classification,
        "agent_repairable": bool(agent_repairable),
        "missing_capabilities": missing,
    }


def intent_execution_preflight(
    command: list[str],
    cwd: Path,
    validation_repo: Path,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    argv = [str(part) for part in command if str(part).strip()]
    if not argv:
        return _intent_blocked_execution_diagnostic(
            "no generated test command was produced",
            reason_code="command_missing",
        )
    if not path_is_under(cwd, validation_repo):
        return _intent_blocked_execution_diagnostic(
            "generated test cwd escapes validation workspace",
            reason_code="cwd_escape",
        )
    if not cwd.is_dir():
        return _intent_blocked_execution_diagnostic(
            "generated test cwd does not exist",
            reason_code="cwd_missing",
        )
    allowed, policy_reason = intent_test_command_policy(argv, cwd, validation_repo)
    if not allowed:
        return _intent_blocked_execution_diagnostic(
            f"generated test command is not allowed by worker policy: {policy_reason}",
            reason_code="command_policy_denied",
        )
    runnable, runnable_reason = intent_command_is_runnable_for_repo(argv, cwd, validation_repo, profile)
    if not runnable:
        return _intent_blocked_execution_diagnostic(runnable_reason)
    return {
        "status": "ready",
        "reason_code": "runnable",
        "reason": "command passed policy and runtime capability checks",
        "classification": "",
        "agent_repairable": False,
        "missing_capabilities": [],
    }


def result_status_from_envelope(envelope: dict[str, Any]) -> str:
    execution = envelope.get("execution") if isinstance(envelope.get("execution"), dict) else {}
    status = str(execution.get("status") or envelope.get("status") or "").strip().lower()
    if status in {"completed", "done"}:
        return "done"
    if status in {"failed", "cancelled", "partial_completed"}:
        return status
    return "failed"


def terminal_state_from_result_status(status: object) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "done":
        return "completed"
    if normalized in {"failed", "cancelled", "partial_completed"}:
        return normalized
    return "failed"


@dataclass(frozen=True)
class ReviewerAssignmentWork:
    index: int
    bundle_id: str
    reviewer_id: str
    output_name: str
    output_path: Path
    staging_dir: Path
    staging_output_path: Path
    estimated_weight: float = 1.0


@dataclass
class ReviewerAssignmentOutcome:
    work: ReviewerAssignmentWork
    thread_id: str
    attempt: int
    valid_output: bool = False
    finding_count: int = 0
    duration_ms: int = 0
    output_bytes: int = 0
    output_payload: bytes | None = None
    error: BaseException | None = None


class ReviewerFanoutOutputBudgetExceeded(OSError):
    def __init__(
        self,
        *,
        limit_bytes: int,
        reserved_bytes: int,
        requested_bytes: int,
        work: ReviewerAssignmentWork,
    ) -> None:
        self.limit_bytes = limit_bytes
        self.reserved_bytes = reserved_bytes
        self.requested_bytes = requested_bytes
        self.bundle_id = work.bundle_id
        self.reviewer_id = work.reviewer_id
        super().__init__(
            "reviewer fanout output exceeds aggregate byte limit: "
            f"limit_bytes={limit_bytes}, "
            f"reserved_bytes={reserved_bytes}, "
            f"requested_bytes={requested_bytes}, "
            f"assignment={work.bundle_id}/{work.reviewer_id}"
        )


class ReviewerFanoutOutputBudget:
    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = max(0, int(limit_bytes))
        self._reserved_bytes = 0
        self._lock = threading.Lock()

    @property
    def reserved_bytes(self) -> int:
        with self._lock:
            return self._reserved_bytes

    def reserve(
        self,
        requested_bytes: int,
        work: ReviewerAssignmentWork,
    ) -> int:
        requested = max(0, int(requested_bytes))
        with self._lock:
            projected = self._reserved_bytes + requested
            if projected > self.limit_bytes:
                raise ReviewerFanoutOutputBudgetExceeded(
                    limit_bytes=self.limit_bytes,
                    reserved_bytes=self._reserved_bytes,
                    requested_bytes=requested,
                    work=work,
                )
            self._reserved_bytes = projected
            return self._reserved_bytes


def reviewer_error_is_transient_capacity(error: BaseException) -> bool:
    message = str(error).lower()
    if any(marker in message for marker in ("usagelimitexceeded", "usage limit exceeded", "quota exhausted")):
        return False
    return any(
        marker in message
        for marker in (
            "429",
            "rate limit",
            "too many requests",
            "server busy",
            "overloaded",
            "temporarily unavailable",
        )
    )


def control_plane_error_is_retryable(error: BaseException) -> bool:
    if not isinstance(error, PullwiseRequestError):
        return False
    status_code = getattr(error, "status_code", None)
    if status_code is None:
        return True
    if isinstance(status_code, bool):
        return False
    try:
        normalized_status = int(status_code)
    except (TypeError, ValueError, OverflowError):
        return False
    return normalized_status in {408, 429} or 500 <= normalized_status < 600


class ReviewWorkerV1:
    def __init__(self, config: Any, client: Any | None = None) -> None:
        self.config = config
        self.client = client
        self.state = WorkerState()
        self.isolation = Isolation(config)
        self.codex_client: CodexSdkClient | None = None
        self.quota_monitor = CodexQuotaMonitor(config, self.isolation, self.ensure_codex_client)
        self.lock = WorkerLock(self.isolation.worker_root, str(config.worker_id), self.isolation.codex_home)
        self._machine_metrics_payload: dict[str, Any] | None = None
        self._machine_metrics_collected_at = 0.0
        self._heartbeat_lock = threading.RLock()
        self._progress_lock = threading.RLock()
        self._event_send_lock = threading.RLock()
        self._last_workspace_cleanup_monotonic = 0.0
        self._persisted_unfinished_workspace_names: set[str] = set()
        self._empty_poll_count = 0
        self._control_plane_error_count = 0

    def default_codex_events_path(self) -> Path:
        return self.isolation.logs / "codex-sdk-events.jsonl"

    def active_run_marker_path(self) -> Path:
        return self.isolation.runtime / "active-run.json"

    def read_active_run_marker(self) -> dict[str, Any]:
        path = self.active_run_marker_path()
        if path.is_symlink():
            return {}
        payload = read_json(path, {})
        return payload if isinstance(payload, dict) else {}

    def persist_active_run_marker(self, active: ActiveJob) -> None:
        self.isolation.runtime.mkdir(parents=True, exist_ok=True)
        write_json(
            self.active_run_marker_path(),
            {
                "job_id": active.job_id,
                "run_id": active.run_id,
                "lease_id": active.lease_id,
                "attempt_id": active.attempt_id,
                "state": active.state,
                "current_phase": active.current_phase or "prepare_workspace",
                "current_phase_status": active.current_phase_status or "running",
                "updated_at": iso_time(time.time()),
            },
        )

    def clear_active_run_marker(self, active: ActiveJob | None = None) -> None:
        path = self.active_run_marker_path()
        if not path.exists() and not path.is_symlink():
            return
        if active is not None:
            payload = self.read_active_run_marker()
            marker_run_id = str(payload.get("run_id") or "").strip()
            marker_job_id = str(payload.get("job_id") or "").strip()
            if marker_run_id and marker_run_id != active.run_id:
                return
            if marker_job_id and marker_job_id != active.job_id:
                return
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def ensure_codex_client(self, events_path: Path | None = None) -> CodexSdkClient:
        self.isolation.runtime.mkdir(parents=True, exist_ok=True)
        self.isolation.logs.mkdir(parents=True, exist_ok=True)
        target_events_path = events_path or self.default_codex_events_path()
        if self.codex_client is not None and not self.codex_client.is_running():
            self.codex_client.close()
            self.codex_client = None
        if self.codex_client is None:
            self.codex_client = CodexSdkClient(
                scoped_codex_command(self.config),
                self.isolation.env(self.config),
                self.isolation.runtime,
                target_events_path,
                rate_limit_callback=self.quota_monitor.apply_rate_limit_update,
            )
            self.codex_client.start()
        else:
            self.codex_client.set_events_path(target_events_path)
        return self.codex_client

    def close_codex_client(self) -> None:
        if self.codex_client is None:
            return
        self.codex_client.close()
        self.codex_client = None

    def machine_metrics_payload(self) -> dict[str, Any] | None:
        interval_seconds = max(1, int(getattr(self.config, "machine_metrics_interval_seconds", 10) or 10))
        current_time = time.time()
        if self._machine_metrics_payload is not None and current_time - self._machine_metrics_collected_at < interval_seconds:
            return self._machine_metrics_payload
        storage_path = str(getattr(self.config, "work_dir", None) or self.isolation.workspaces)
        try:
            payload = worker_machine_metrics_payload(storage_path=storage_path, timestamp=int(current_time))
        except Exception:
            return self._machine_metrics_payload
        self._machine_metrics_payload = payload
        self._machine_metrics_collected_at = current_time
        return payload

    def _persisted_submit_marker(self, run_name: str, marker_name: str) -> dict[str, Any]:
        payload = read_json(self.isolation.artifacts / run_name / marker_name, {})
        if not isinstance(payload, dict):
            return {}
        marker_run_id = str(payload.get("run_id") or "").strip()
        if marker_run_id and marker_run_id != run_name:
            return {}
        return payload

    def _persisted_run_is_terminal(self, run_name: str, run_state: dict[str, Any]) -> bool:
        if self._persisted_submit_marker(run_name, "result-submit-succeeded.json"):
            return True
        progress = run_state.get("progress") if isinstance(run_state.get("progress"), dict) else {}
        return (
            str(progress.get("current_phase") or "") == "cleanup_active_job"
            and str(progress.get("current_phase_status") or "") == "completed"
        )

    def _persisted_unfinished_runs(self) -> list[dict[str, Any]]:
        records: dict[str, dict[str, Any]] = {}
        try:
            workspaces = list(self.isolation.workspaces.iterdir())
        except OSError:
            workspaces = []
        for workspace in workspaces:
            try:
                if workspace.is_symlink() or not workspace.is_dir():
                    continue
            except OSError:
                continue
            run_name = workspace.name
            run_dir = workspace / "repo" / ".codex-review" / "runs" / run_name
            run_state = read_json(run_dir / "run-state.json", {})
            if not isinstance(run_state, dict):
                run_state = {}
            if self._persisted_run_is_terminal(run_name, run_state):
                from ._main_part_08_lifecycle_cleanup import cleanup_v1_workspace_path

                cleanup_v1_workspace_path(self.isolation.workspaces, workspace)
                continue
            active_job = run_state.get("active_job") if isinstance(run_state.get("active_job"), dict) else {}
            failed_marker = self._persisted_submit_marker(run_name, "result-submit-failed.json")
            blocked_marker = self._persisted_submit_marker(run_name, "result-submit-blocked.json")
            submit_marker = failed_marker or blocked_marker
            persisted_state = str(active_job.get("state") or "").strip().lower()
            if not submit_marker and persisted_state not in ACTIVE_HEARTBEAT_STATUSES:
                continue
            evidence_path = (
                self.isolation.artifacts
                / run_name
                / ("result-submit-failed.json" if failed_marker else "result-submit-blocked.json")
                if submit_marker
                else run_dir / "run-state.json"
            )
            try:
                evidence_mtime = evidence_path.stat().st_mtime
            except OSError:
                evidence_mtime = 0.0
            records[run_name] = {
                "run_name": run_name,
                "run_dir": run_dir,
                "run_state": run_state,
                "active_job": active_job,
                "submit_marker": submit_marker,
                "evidence_mtime": evidence_mtime,
            }

        try:
            artifact_runs = list(self.isolation.artifacts.iterdir())
        except OSError:
            artifact_runs = []
        for artifact_run in artifact_runs:
            try:
                if artifact_run.is_symlink() or not artifact_run.is_dir():
                    continue
            except OSError:
                continue
            run_name = artifact_run.name
            if run_name in records or self._persisted_submit_marker(run_name, "result-submit-succeeded.json"):
                continue
            failed_marker = self._persisted_submit_marker(run_name, "result-submit-failed.json")
            blocked_marker = self._persisted_submit_marker(run_name, "result-submit-blocked.json")
            submit_marker = failed_marker or blocked_marker
            if not submit_marker:
                continue
            evidence_path = artifact_run / (
                "result-submit-failed.json" if failed_marker else "result-submit-blocked.json"
            )
            try:
                evidence_mtime = evidence_path.stat().st_mtime
            except OSError:
                evidence_mtime = 0.0
            run_dir = self.isolation.workspaces / run_name / "repo" / ".codex-review" / "runs" / run_name
            records[run_name] = {
                "run_name": run_name,
                "run_dir": run_dir,
                "run_state": {},
                "active_job": {},
                "submit_marker": submit_marker,
                "evidence_mtime": evidence_mtime,
            }

        runtime_marker = self.read_active_run_marker()
        marker_run_name = str(runtime_marker.get("run_id") or "").strip()
        if marker_run_name:
            marker_run_name = safe_id(marker_run_name, "recovered_run")
            marker_run_dir = (
                self.isolation.workspaces
                / marker_run_name
                / "repo"
                / ".codex-review"
                / "runs"
                / marker_run_name
            )
            marker_run_state = read_json(marker_run_dir / "run-state.json", {})
            if not isinstance(marker_run_state, dict):
                marker_run_state = {}
            if self._persisted_run_is_terminal(marker_run_name, marker_run_state):
                self.clear_active_run_marker()
            else:
                existing = records.get(marker_run_name, {})
                existing_active = (
                    existing.get("active_job")
                    if isinstance(existing.get("active_job"), dict)
                    else {}
                )
                try:
                    marker_mtime = self.active_run_marker_path().stat().st_mtime
                except OSError:
                    marker_mtime = 0.0
                records[marker_run_name] = {
                    "run_name": marker_run_name,
                    "run_dir": existing.get("run_dir") or marker_run_dir,
                    "run_state": existing.get("run_state") or marker_run_state,
                    "active_job": {**runtime_marker, **existing_active},
                    "submit_marker": existing.get("submit_marker") or {},
                    "evidence_mtime": max(
                        float(existing.get("evidence_mtime") or 0.0),
                        marker_mtime,
                    ),
                }
        return sorted(records.values(), key=lambda item: (float(item["evidence_mtime"]), item["run_name"]), reverse=True)

    def recover_persisted_active_job(self) -> ActiveJob | None:
        records = self._persisted_unfinished_runs()
        self._persisted_unfinished_workspace_names = {str(record["run_name"]) for record in records}
        if not records:
            return None
        for persisted_record in records:
            persisted_run_dir = Path(persisted_record["run_dir"])
            if not persisted_run_dir.is_dir():
                continue
            try:
                _recover_model_output_publication(persisted_run_dir)
            except OSError as exc:
                persisted_record["publication_recovery_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                try:
                    append_jsonl(
                        persisted_run_dir / "worker.log.jsonl",
                        {
                            "event": "model_output_publication_recovery_failed",
                            "error": persisted_record["publication_recovery_error"],
                            "time": iso_time(time.time()),
                        },
                    )
                except OSError:
                    pass
        record = records[0]
        active_payload = record["active_job"]
        submit_marker = record["submit_marker"]
        run_name = str(record["run_name"])

        def persisted_id(value: object, fallback: str) -> str:
            raw = str(value or "").strip()
            return safe_id(raw, fallback) if raw else safe_id(fallback, fallback)

        run_id = persisted_id(
            submit_marker.get("run_id") or active_payload.get("run_id") or run_name,
            f"recovered_run_{run_name}",
        )
        active = ActiveJob(
            job_id=persisted_id(
                submit_marker.get("job_id") or active_payload.get("job_id"),
                f"recovered_job_{run_id}",
            ),
            run_id=run_id,
            lease_id=persisted_id(
                submit_marker.get("lease_id") or active_payload.get("lease_id"),
                f"recovered_lease_{run_id}",
            ),
            attempt_id=persisted_id(
                submit_marker.get("attempt_id") or active_payload.get("attempt_id"),
                f"{self.config.worker_id}-recovered",
            ),
            state="finishing",
        )
        active.run_dir = record["run_dir"] if Path(record["run_dir"]).is_dir() else None
        progress = record["run_state"].get("progress") if isinstance(record["run_state"].get("progress"), dict) else {}
        active.current_phase = str(
            progress.get("current_phase")
            or active_payload.get("current_phase")
            or "submit_result_envelope"
        )
        active.current_phase_status = str(progress.get("current_phase_status") or "blocked")
        publication_recovery_error = str(
            record.get("publication_recovery_error") or ""
        )
        if publication_recovery_error:
            active.message = (
                "Recovered unfinished run with an unresolved model-output publication: "
                + publication_recovery_error
            )
        else:
            active.message = str(
                progress.get("message")
                or "Recovered unfinished run; operator intervention is required before this slot can be reused."
            )
        try:
            active.overall_percent = max(0.0, min(100.0, float(progress.get("overall_percent") or 0.0)))
            active.current_phase_percent = max(
                0.0,
                min(100.0, float(progress.get("current_phase_percent") or 0.0)),
            )
            active.last_event_sequence = max(0, int(progress.get("last_event_sequence") or 0))
        except (TypeError, ValueError, OverflowError):
            pass
        counters = progress.get("counters") if isinstance(progress.get("counters"), dict) else {}
        active.apply_progress_data(counters)
        self.state.set_active(active)
        self.state.state = "finishing"
        return active

    def cleanup_idle_v1_workspaces_if_due(self, *, force: bool = False) -> list[Path]:
        if self.state.active_job is not None or self.state.state != "idle":
            return []
        now_monotonic = time.monotonic()
        interval = max(1, int(getattr(self.config, "cleanup_interval_seconds", 3600) or 3600))
        if (
            not force
            and self._last_workspace_cleanup_monotonic > 0
            and now_monotonic - self._last_workspace_cleanup_monotonic < interval
        ):
            return []
        self._last_workspace_cleanup_monotonic = now_monotonic
        from ._main_part_08_lifecycle_cleanup import cleanup_v1_workspaces

        return cleanup_v1_workspaces(
            self.isolation.workspaces,
            protected_run_ids=self._persisted_unfinished_workspace_names,
        )

    def next_poll_sleep(
        self,
        *,
        claimed_job: bool,
        loop_error: bool,
        worker_busy: bool = False,
    ) -> float:
        try:
            poll_seconds = float(getattr(self.config, "poll_seconds", 5) or 5)
        except (TypeError, ValueError, OverflowError):
            poll_seconds = 5.0
        if not math.isfinite(poll_seconds):
            poll_seconds = 5.0
        poll_seconds = max(1.0, poll_seconds)

        try:
            max_backoff_seconds = float(
                getattr(self.config, "max_backoff_seconds", 60) or 60
            )
        except (TypeError, ValueError, OverflowError):
            max_backoff_seconds = 60.0
        if not math.isfinite(max_backoff_seconds):
            max_backoff_seconds = 60.0
        max_backoff_seconds = max(poll_seconds, max_backoff_seconds)

        if loop_error:
            self._control_plane_error_count += 1
            self._empty_poll_count = 0
            exponent = min(self._control_plane_error_count - 1, 30)
            base_seconds = poll_seconds * (2**exponent)
        elif claimed_job or worker_busy:
            self._control_plane_error_count = 0
            self._empty_poll_count = 0
            base_seconds = poll_seconds
        else:
            self._control_plane_error_count = 0
            self._empty_poll_count += 1
            exponent = min(self._empty_poll_count - 1, 30)
            base_seconds = poll_seconds * (2**exponent)

        try:
            jitter_seconds = float(
                getattr(self.config, "poll_jitter_seconds", 0) or 0
            )
        except (TypeError, ValueError, OverflowError):
            jitter_seconds = 0.0
        if not math.isfinite(jitter_seconds):
            jitter_seconds = 0.0
        jitter_window = min(
            max(0.0, jitter_seconds),
            poll_seconds,
            max(0.0, max_backoff_seconds - poll_seconds),
        )
        base_cap = max(poll_seconds, max_backoff_seconds - jitter_window)
        bounded_base = min(base_seconds, base_cap)
        jitter = random.uniform(0.0, jitter_window) if jitter_window else 0.0
        return bounded_base + jitter

    def run(self, *, once: bool = False) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("Pullwise review worker v1 is Linux only")
        self.isolation.prepare()
        self.lock.acquire()
        run_error: BaseException | None = None
        try:
            register = getattr(self.client, "register", None)
            if callable(register):
                while True:
                    try:
                        register()
                        break
                    except PullwiseRequestError as exc:
                        if once or not control_plane_error_is_retryable(exc):
                            raise
                        time.sleep(
                            self.next_poll_sleep(
                                claimed_job=False,
                                loop_error=True,
                            )
                        )
            if self.recover_persisted_active_job() is None:
                self.state.state = "idle"
            while True:
                try:
                    self.heartbeat()
                except PullwiseRequestError as exc:
                    if once or not control_plane_error_is_retryable(exc):
                        raise
                    time.sleep(
                        self.next_poll_sleep(
                            claimed_job=False,
                            loop_error=True,
                        )
                    )
                    continue
                ran_job = False
                if self.state.can_lease():
                    try:
                        job = self.client.claim()
                    except PullwiseRequestError as exc:
                        if once or not control_plane_error_is_retryable(exc):
                            raise
                        time.sleep(
                            self.next_poll_sleep(
                                claimed_job=False,
                                loop_error=True,
                            )
                        )
                        continue
                    if job:
                        ran_job = True
                        self.run_job(job)
                self.cleanup_idle_v1_workspaces_if_due(force=ran_job)
                if once:
                    return
                time.sleep(
                    self.next_poll_sleep(
                        claimed_job=ran_job,
                        loop_error=False,
                        worker_busy=self.state.active_job is not None,
                    )
                )
        except BaseException as exc:
            run_error = exc
            raise
        finally:
            try:
                self.close_codex_client()
            except BaseException:
                if run_error is None:
                    raise
            finally:
                self.lock.release()

    def heartbeat(self) -> dict[str, Any]:
        with self._heartbeat_lock:
            return self._heartbeat_once()

    def _heartbeat_once(self, *, process_worker_command: bool = True) -> dict[str, Any]:
        active = self.state.active_job
        codex_client_error = ""
        if active is None and (self.codex_client is None or not self.codex_client.is_running()):
            try:
                self.ensure_codex_client()
            except Exception as exc:
                codex_client_error = quota_text(exc, 500)
        quota = self.quota_monitor.snapshot_if_due(active=active is not None)
        if not isinstance(quota, dict):
            checked_at = int(time.time())
            quota = {
                "provider": "codex",
                "status": "unavailable",
                "ready": False,
                "reason": "codex_quota_unavailable",
                "checkedAt": checked_at,
                "nextCheckAt": checked_at + codex_quota_check_seconds(self.config, degraded=True),
                "thresholdPercent": codex_quota_threshold_percent(self.config),
            }
        quota_ready = quota.get("ready") is True
        codex_client_ready = self.codex_client is not None and self.codex_client.is_running()
        provider_ready = codex_client_ready and quota_ready
        self.state.provider_ready = provider_ready
        readiness_reason = quota_text((quota or {}).get("reason") or (quota or {}).get("status"), 160)
        if codex_client_error:
            readiness_reason = f"codex_sdk_unavailable: {codex_client_error}"
        elif not codex_client_ready:
            readiness_reason = readiness_reason or "codex_sdk_not_running"
        active_jobs = 1 if active else 0
        worker_status = "idle"
        if active is not None:
            worker_status = active.state if active.state in ACTIVE_HEARTBEAT_STATUSES else "busy"
        heartbeat_payload = {
            "protocol_version": PROTOCOL_VERSION,
            "worker_id": str(self.config.worker_id),
            "status": worker_status,
            "active_run_id": active.run_id if active else None,
            "hostname": socket.gethostname(),
            "concurrency": {
                "max_active_jobs": 1,
                "active_jobs": active_jobs,
                "available_job_slots": 0 if active else 1,
                "maintains_local_queue": False,
                "local_queue_depth": 0,
            },
            "codex_app_server": {
                "status": "ready" if codex_client_ready else "needs_attention",
                "transport": "stdio",
                "active_thread_id": active.thread_id if active and active.thread_id else None,
            },
            "last_error": readiness_reason if not provider_ready else None,
            "doctor_status": "ok" if provider_ready else "degraded",
            "codex_ready": provider_ready,
            "ready_providers": ["codex"] if provider_ready else [],
            "codex_quota": quota,
        }
        machine_metrics = self.machine_metrics_payload()
        if machine_metrics is not None:
            heartbeat_payload["machine_metrics"] = machine_metrics
        if active is not None:
            with self._progress_lock:
                heartbeat_payload["progress"] = active.progress_snapshot()
        response = self.client.heartbeat(**heartbeat_payload)
        cancelled = response.get("cancelled_job_ids") if isinstance(response, dict) else []
        cancellation_accepted = False
        if active and active.job_id in (cancelled or []):
            cancellation_accepted = self.request_cancel(active, reason="server_cancelled")
        commands = response.get("commands") if isinstance(response, dict) and isinstance(response.get("commands"), list) else []
        if active and not cancellation_accepted:
            for command in commands:
                if not isinstance(command, dict):
                    continue
                if command.get("type") == "cancel_run" and str(command.get("run_id") or "") == active.run_id:
                    cancellation_accepted = self.request_cancel(
                        active,
                        reason=str(command.get("reason") or "server_cancelled"),
                    )
                    if cancellation_accepted:
                        break
        if process_worker_command:
            self.handle_worker_command(response)
        return response if isinstance(response, dict) else {}

    def handle_worker_command(self, response: object) -> bool:
        if not isinstance(response, dict):
            return False
        command = response.get("command")
        if not isinstance(command, dict):
            return False
        command_id = quota_text(command.get("id"), 128)
        command_name = quota_text(command.get("command"), 80).lower()
        command_status = quota_text(command.get("status"), 40).lower()
        if (
            not command_id
            or command_name != REFRESH_CODEX_QUOTA_COMMAND
            or command_status not in WORKER_COMMAND_ACTIVE_STATUSES
        ):
            return False
        report_status = getattr(self.client, "command_status", None)
        if not callable(report_status):
            return False
        try:
            if command_status == "pending":
                report_status(command_id, "running")
            self.quota_monitor.refresh()
            # Persist the new snapshot before the command becomes terminal so
            # Admin never observes success alongside the previous quota value.
            self._heartbeat_once(process_worker_command=False)
            report_status(command_id, "succeeded")
        except Exception as exc:
            try:
                report_status(command_id, "failed", error=quota_text(exc, 500))
            except Exception:
                pass
            return False
        return True

    def active_job_heartbeat_interval_seconds(self) -> float:
        return max(1.0, float(getattr(self.config, "poll_seconds", 5) or 5))

    def start_active_job_supervisor(self, active: ActiveJob) -> tuple[threading.Event, threading.Thread]:
        stop = threading.Event()

        def supervise() -> None:
            while not stop.is_set() and self.state.active_job is active:
                try:
                    self.heartbeat()
                except Exception as exc:
                    if active.run_dir is not None:
                        append_jsonl(
                            active.run_dir / "worker.log.jsonl",
                            {
                                "event": "active_job_heartbeat_failed",
                                "error": str(exc),
                                "time": iso_time(time.time()),
                            },
                        )
                if stop.wait(self.active_job_heartbeat_interval_seconds()):
                    return

        thread = threading.Thread(
            target=supervise,
            name=f"pullwise-heartbeat-{active.run_id}",
            daemon=True,
        )
        thread.start()
        return stop, thread

    def poll_cancel_requested(self) -> bool:
        active = self.state.active_job
        if active is None:
            return False
        # The active-job supervisor owns server polling. This callback runs in
        # the Codex wait loop every 0.5 seconds, so it must remain local-only.
        return bool(active.cancel_requested)

    def emit_event(
        self,
        active: ActiveJob,
        run_dir: Path,
        event_type: str,
        phase: str,
        *,
        status: str = "running",
        progress: float | int | None = None,
        current_phase_percent: float = 0.0,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._event_send_lock:
            with self._progress_lock:
                active.last_event_sequence += 1
                if progress is not None:
                    active.overall_percent = round(float(progress), 2)
                active.current_phase = phase
                active.current_phase_status = status
                active.current_phase_percent = round(float(current_phase_percent), 2)
                active.message = message
                active.apply_progress_data(data)
                if active.current_run_estimator and (
                    event_type in {"run_completed", "run_failed", "run_cancelled", "run_partial_completed"}
                ):
                    active.current_run_estimator.mark_terminal()
                event = {
                    "protocol_version": PROTOCOL_VERSION,
                    "run_id": active.run_id,
                    "worker_id": str(self.config.worker_id),
                    "sequence": active.last_event_sequence,
                    "timestamp": iso_time(time.time()),
                    "event_type": event_type,
                    "phase": phase,
                    "severity": "info" if status not in {"failed", "cancelled"} else "error",
                    "message": message,
                    "progress": {
                        "overall_percent": active.overall_percent,
                        "current_phase_percent": active.current_phase_percent,
                        "status": status,
                        "steps": active.progress_steps(),
                    },
                    "data": data or {},
                }
                estimate = active.current_run_estimator.snapshot() if active.current_run_estimator else None
                if isinstance(estimate, dict):
                    event["progress"]["estimate"] = estimate
                append_jsonl(run_dir / "progress.log.jsonl", event)
                snapshot = active.progress_snapshot()
                write_json(run_dir / "progress.json", snapshot)
                run_state = read_json(run_dir / "run-state.json", {})
                if not isinstance(run_state, dict):
                    run_state = {}
                run_state.update({"active_job": active.heartbeat_payload(), "progress": snapshot})
                write_json(run_dir / "run-state.json", run_state)
            if hasattr(self.client, "event"):
                try:
                    self.client.event(active.run_id, event)
                except Exception:
                    append_jsonl(
                        run_dir / "worker.log.jsonl",
                        {"event": "progress_event_post_failed", "phase": phase, "time": iso_time(time.time())},
                    )
            return event

    def request_cancel(self, active: ActiveJob, *, reason: str = "server_cancelled") -> bool:
        with self._progress_lock:
            if active.terminal_result_in_flight or active.terminal_result_submitted:
                return False
            reason_text = str(reason or "server_cancelled").strip() or "server_cancelled"
            active.cancel_requested = True
            active.cancel_reason = reason_text
            active.state = "cancelling"
            active.message = "Cancellation requested."
            self.persist_active_run_marker(active)
            run_dir = active.run_dir
        if run_dir is not None:
            self.emit_cancel_requested(active, run_dir)
        return True

    def emit_cancel_requested(self, active: ActiveJob, run_dir: Path) -> None:
        with self._event_send_lock:
            with self._progress_lock:
                if active.cancel_requested_reported:
                    return
                reason = active.cancel_reason or "server_cancelled"
                active.cancel_requested = True
                active.state = "cancelling"
                active.cancel_requested_reported = True
                append_jsonl(
                    run_dir / "worker.log.jsonl",
                    {"event": "run_cancel_requested", "reason": reason, "time": iso_time(time.time())},
                )
            self.emit_event(
                active,
                run_dir,
                "run_cancel_requested",
                active.current_phase,
                status="running",
                progress=active.overall_percent,
                current_phase_percent=active.current_phase_percent,
                message="Cancellation requested.",
                data={"reason": reason, "cancel_requested": True},
            )
            with self._progress_lock:
                self.persist_active_run_marker(active)

    def start_phase(self, active: ActiveJob, run_dir: Path, phase: str, progress: int) -> None:
        with self._event_send_lock:
            with self._progress_lock:
                if active.cancel_requested:
                    return
                estimator = active.current_run_estimator
                estimate_unit_id = phase_estimate_unit_id(phase)
                if (
                    estimator is not None
                    and estimator.has_work_unit(estimate_unit_id)
                    and estimator.work_unit_state(estimate_unit_id) in {"pending", "retrying"}
                ):
                    estimator.start_work_unit(estimate_unit_id)
                active.state = "finishing" if phase in {"upload_artifacts", "submit_result_envelope", "cleanup_active_job"} else "busy"
                append_jsonl(
                    run_dir / "worker.log.jsonl",
                    {"event": "phase_started", "phase": phase, "progress": progress, "time": iso_time(time.time())},
                )
            self.emit_event(
                active,
                run_dir,
                "phase_started",
                phase,
                status="running",
                progress=progress,
                current_phase_percent=0.0,
                message=phase.replace("_", " "),
            )
            with self._progress_lock:
                self.persist_active_run_marker(active)

    def complete_phase(self, active: ActiveJob, run_dir: Path, phase: str, progress: int, *, data: dict[str, Any] | None = None) -> None:
        estimator = active.current_run_estimator
        estimate_unit_id = phase_estimate_unit_id(phase)
        if estimator is not None and estimator.has_work_unit(estimate_unit_id):
            estimator.finish_work_unit(estimate_unit_id)
        append_jsonl(run_dir / "worker.log.jsonl", {"event": "phase_completed", "phase": phase, "progress": progress, "time": iso_time(time.time())})
        self.emit_event(
            active,
            run_dir,
            "phase_completed",
            phase,
            status="completed",
            progress=progress,
            current_phase_percent=100.0,
            message=f"{phase.replace('_', ' ')} completed.",
            data=data,
        )

    def skip_phase(self, active: ActiveJob, run_dir: Path, phase: str, progress: int, *, reason: str, data: dict[str, Any] | None = None) -> None:
        estimator = active.current_run_estimator
        estimate_unit_id = phase_estimate_unit_id(phase)
        if estimator is not None and estimator.has_work_unit(estimate_unit_id):
            estimator.finish_work_unit(estimate_unit_id, duration_seconds=0.0)
        payload = {"skip_reason": reason}
        if data:
            payload.update(data)
        append_jsonl(run_dir / "worker.log.jsonl", {"event": "phase_skipped", "phase": phase, "progress": progress, "reason": reason, "time": iso_time(time.time())})
        self.emit_event(
            active,
            run_dir,
            "phase_completed",
            phase,
            status="skipped",
            progress=progress,
            current_phase_percent=100.0,
            message=f"{phase.replace('_', ' ')} skipped.",
            data=payload,
        )

    def progress_phase(
        self,
        active: ActiveJob,
        run_dir: Path,
        phase: str,
        progress: int,
        *,
        current_phase_percent: float,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        append_jsonl(
            run_dir / "worker.log.jsonl",
            {
                "event": "progress_updated",
                "phase": phase,
                "progress": progress,
                "current_phase_percent": round(float(current_phase_percent), 2),
                "time": iso_time(time.time()),
                "data": data or {},
            },
        )
        self.emit_event(
            active,
            run_dir,
            "progress_updated",
            phase,
            status="running",
            progress=progress,
            current_phase_percent=current_phase_percent,
            message=message,
            data=data,
        )

    def fail_phase(self, active: ActiveJob, run_dir: Path, phase: str, error: object) -> None:
        with self._event_send_lock:
            with self._progress_lock:
                estimator = active.current_run_estimator
                estimate_unit_id = phase_estimate_unit_id(phase)
                if estimator is not None and estimator.has_work_unit(estimate_unit_id):
                    estimator.finish_work_unit(estimate_unit_id, state="failed")
                failure = failure_payload_for_error(error, status="failed", phase=phase)
                failure_record = {
                    "event": "phase_failed",
                    "phase": phase,
                    "error": str(error),
                    "time": iso_time(time.time()),
                    "failure_category": failure.get("category"),
                    "failure_action": failure.get("failure_action"),
                }
                append_jsonl(run_dir / "worker.log.jsonl", failure_record)
                run_state = read_json(run_dir / "run-state.json", {})
                if not isinstance(run_state, dict):
                    run_state = {}
                run_state["failure"] = {
                    "phase": phase,
                    "message": str(error),
                    "category": failure.get("category"),
                    "failure_action": failure.get("failure_action"),
                    "updated_at": iso_time(time.time()),
                }
                write_json(run_dir / "run-state.json", run_state)
            self.emit_event(
                active,
                run_dir,
                "phase_failed",
                phase,
                status="failed",
                progress=active.overall_percent,
                current_phase_percent=active.current_phase_percent,
                message=str(error),
            )


    def run_job(self, job: dict[str, Any]) -> None:
        started = time.time()
        started_monotonic = time.monotonic()
        job_id = safe_id(job.get("job_id"), "job")
        run_id = safe_id(job.get("run_id") or f"run_{job_id}", "run")
        lease_id = safe_id(job.get("lease_id") or f"lease_{job_id}", "lease")
        attempt = int(job.get("attempt") or 1)
        active = ActiveJob(job_id=job_id, run_id=run_id, lease_id=lease_id, attempt_id=f"{self.config.worker_id}-{attempt}")
        self.state.set_active(active)
        self.persist_active_run_marker(active)
        heartbeat_stop, heartbeat_thread = self.start_active_job_supervisor(active)
        terminal_state = "finishing"
        terminal_result_confirmed = False
        codex_client: CodexSdkClient | None = None
        repo_dir: Path | None = None
        run_dir: Path | None = None
        artifact_dir: Path | None = None
        try:
            job_policy = validate_job_policy(job)
            scan_deadline_seconds = int(job_policy.get("review_worker", {}).get("scanDeadlineSeconds") or 0)
            deadline_monotonic = (
                started_monotonic + scan_deadline_seconds
                if scan_deadline_seconds > 0
                else None
            )
            active.current_run_estimator = current_run_estimator_for_job(
                job,
                started_monotonic=started_monotonic,
            )
            repo_dir, run_dir, artifact_dir = invoke_with_lifecycle_controls(
                self.prepare_workspace,
                job,
                run_id,
                deadline_monotonic=deadline_monotonic,
                cancel_requested=self.poll_cancel_requested,
            )
            active.run_dir = run_dir
            events_path = run_dir / "codex-events.jsonl"
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_started", "job_id": job_id, "run_id": run_id, "time": iso_time(started)})
            self.emit_event(active, run_dir, "run_started", "prepare_workspace", status="running", progress=0, message="Run started.")
            for phase, progress in PIPELINE_PHASES:
                remaining_wall_time_seconds(deadline_monotonic)
                if active.cancel_requested:
                    raise JobCancelled(active.cancel_reason or "cancel requested")
                self.start_phase(active, run_dir, phase, progress)
                if active.cancel_requested:
                    raise JobCancelled(active.cancel_reason or "cancel requested")
                try:
                    if phase in INTENT_VALIDATION_CHILD_PHASES and not intent_validation_config(job)["enabled"]:
                        self.skip_phase(
                            active,
                            run_dir,
                            phase,
                            progress,
                            reason="intent test validation disabled",
                            data={"intent_test_validation_enabled": False},
                        )
                        continue
                    if phase == "prepare_workspace":
                        pass
                    elif phase == "start_codex_app_server":
                        codex_client = self.ensure_codex_client(events_path)
                        runtime_metadata = getattr(codex_client, "runtime_metadata", None)
                        if callable(runtime_metadata):
                            write_json(run_dir / "codex-runtime.json", runtime_metadata())
                    elif phase == "initialize_codex_connection":
                        thread_id = (
                            start_codex_thread_with_lifecycle(
                                codex_client,
                                repo_dir,
                                model_for_job(job),
                                timeout_seconds=turn_timeout_with_deadline(job, deadline_monotonic),
                                cancel_requested=self.poll_cancel_requested,
                            )
                            if codex_client
                            else ""
                        )
                        active.thread_id = thread_id
                        run_state = read_json(run_dir / "run-state.json", {})
                        if not isinstance(run_state, dict):
                            run_state = {}
                        run_state.update({"thread_id": thread_id, "active_job": active.heartbeat_payload()})
                        write_json(run_dir / "run-state.json", run_state)
                    elif phase == "check_codex_auth":
                        invoke_with_lifecycle_controls(
                            self.run_codex_auth_check,
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            deadline_monotonic=deadline_monotonic,
                        )
                    elif phase == "submit_result_envelope":
                        pass
                    elif phase == "cleanup_active_job":
                        pass
                    elif phase == "reviewer_fanout":
                        invoke_with_lifecycle_controls(
                            self.run_reviewer_fanout_phase,
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            deadline_monotonic=deadline_monotonic,
                            active=active,
                            progress=progress,
                        )
                    elif phase in SEMANTIC_PHASES:
                        invoke_with_lifecycle_controls(
                            self.run_semantic_phase,
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            phase,
                            deadline_monotonic=deadline_monotonic,
                        )
                        if phase == "intent_test_writing":
                            repair_intent_test_source_artifact(
                                run_dir / "intent" / "intent-test-source.json",
                                run_dir,
                            )
                        if phase == "validator_disproof":
                            repair_validation_output_artifact(run_dir / "validated-findings.json")
                        if phase == "final_report_json":
                            repair_agent_report_artifact(run_dir, job)
                    elif phase == "reviewer_json_validation":
                        invoke_with_lifecycle_controls(
                            self.run_reviewer_json_validation_phase,
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            deadline_monotonic=deadline_monotonic,
                            active=active,
                        )
                    elif phase in MECHANICAL_PHASES:
                        invoke_with_lifecycle_controls(
                            self.run_mechanical_phase,
                            repo_dir,
                            run_dir,
                            job,
                            phase,
                            deadline_monotonic=deadline_monotonic,
                            cancel_requested=self.poll_cancel_requested,
                            active=active,
                            progress=progress,
                        )
                        if phase == "intent_test_running":
                            validation_config = intent_validation_config(job)
                            invoke_with_lifecycle_controls(
                                self.repair_intent_test_runtime,
                                codex_client,
                                repo_dir,
                                run_dir,
                                job,
                                deadline_monotonic=deadline_monotonic,
                                max_attempts=int(validation_config.get("max_runtime_repair_attempts") or 0),
                                active=active,
                            )
                    if phase in {"reviewer_fanout", "intent_test_validation"}:
                        self.progress_phase(
                            active,
                            run_dir,
                            phase,
                            progress,
                            current_phase_percent=90.0,
                            message=f"{phase.replace('_', ' ')} progress updated.",
                            data=phase_progress_data(run_dir, phase),
                        )
                    try:
                        if phase == "bundle_planning":
                            materialize_agent_bundle_plan(run_dir, job)
                        validate_phase_outputs(run_dir, phase, artifact_dir)
                    except Exception as validation_exc:
                        if phase not in SEMANTIC_PHASES:
                            raise
                        invoke_with_lifecycle_controls(
                            self.repair_semantic_phase_outputs,
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            phase,
                            validation_exc,
                            deadline_monotonic=deadline_monotonic,
                            active=active,
                        )
                        validate_phase_outputs(run_dir, phase, artifact_dir)
                    if phase == "intent_test_writing":
                        validation_config = intent_validation_config(job)
                        invoke_with_lifecycle_controls(
                            self.repair_intent_test_preflight,
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            deadline_monotonic=deadline_monotonic,
                            max_attempts=int(validation_config.get("max_preflight_repair_attempts") or 0),
                            active=active,
                        )
                        validate_phase_outputs(run_dir, phase, artifact_dir)
                    if phase in {"intent_test_planning", "intent_test_running", "intent_test_failure_analysis"}:
                        refresh_coverage_intent_counters(run_dir)
                    if phase == "qa_gate":
                        qa = read_json(run_dir / "qa.json", {})
                        if isinstance(qa, dict) and str(qa.get("status") or "").strip().lower() == "fail":
                            errors = qa.get("errors") if isinstance(qa.get("errors"), list) else []
                            reason = "; ".join(str(item) for item in errors if str(item).strip()) or "qa gate failed"
                            append_jsonl(run_dir / "worker.log.jsonl", {"event": "phase_failed", "phase": phase, "error": reason, "time": iso_time(time.time())})
                            self.emit_event(
                                active,
                                run_dir,
                                "phase_failed",
                                phase,
                                status="failed",
                                progress=progress,
                                current_phase_percent=100.0,
                                message=reason,
                                data={"errors": errors},
                            )
                            self.emit_event(
                                active,
                                run_dir,
                                "qa_failed",
                                phase,
                                status="failed",
                                progress=progress,
                                current_phase_percent=100.0,
                                message=reason,
                                data={"errors": errors},
                            )
                            raise JobPartialCompleted(reason)
                    self.complete_phase(active, run_dir, phase, progress, data=phase_completion_data(run_dir, phase, artifact_dir))
                    if phase == "submit_result_envelope":
                        envelope = self.build_envelope(job, run_id, "completed", started, artifact_dir, run_dir)
                        if not self.submit_result_or_record_failure(active, job_id, result_payload(active, envelope, "done", run_dir), artifact_dir, envelope):
                            terminal_state = "result_submit_failed"
                            return
                        terminal_result_confirmed = True
                        terminal_state = "completed"
                        self.emit_event(
                            active,
                            run_dir,
                            "run_completed",
                            "cleanup_active_job",
                            status="completed",
                            progress=100,
                            current_phase_percent=100,
                            message="Run completed.",
                        )
                        upload_log_artifacts_best_effort(self.client, job_id, active.attempt_id, run_dir, artifact_dir)
                        return
                except JobCancelled:
                    raise
                except JobPartialCompleted:
                    raise
                except Exception as phase_exc:
                    self.fail_phase(active, run_dir, phase, phase_exc)
                    raise
        except JobCancelled:
            artifact_dir = artifact_dir or self.isolation.artifacts / run_id
            run_dir = run_dir or self.isolation.workspaces / run_id / "repo" / ".codex-review" / "runs" / run_id
            active.run_dir = active.run_dir or run_dir
            cancel_reason = active.cancel_reason or "cancel requested"
            self.request_cancel(active, reason=cancel_reason)
            self.emit_cancel_requested(active, run_dir)
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_cancelled", "error": "cancel requested", "reason": cancel_reason, "time": iso_time(time.time())})
            self.emit_event(
                active,
                run_dir,
                "run_cancelled",
                active.current_phase,
                status="cancelled",
                progress=active.overall_percent,
                message="Run cancelled.",
                data={"reason": cancel_reason},
            )
            envelope = self.build_envelope(
                job,
                run_id,
                "cancelled",
                started,
                artifact_dir,
                run_dir,
                error=f"cancel requested: {cancel_reason}",
                phase=active.current_phase,
            )
            upload_error = upload_artifacts_best_effort(self.client, job_id, active.attempt_id, artifact_dir)
            reconcile_envelope_artifact_manifest_with_uploads(envelope, artifact_dir)
            if upload_error:
                envelope.setdefault("extensions", {}).setdefault("worker_internal", {})["artifact_upload_error"] = upload_error
            if self.submit_result_or_record_failure(active, job_id, result_payload(active, envelope, "cancelled", run_dir), artifact_dir, envelope):
                terminal_result_confirmed = True
                terminal_state = "cancelled"
            else:
                terminal_state = "result_submit_failed"
                return
        except JobPartialCompleted as exc:
            artifact_dir = artifact_dir or self.isolation.artifacts / run_id
            run_dir = run_dir or self.isolation.workspaces / run_id / "repo" / ".codex-review" / "runs" / run_id
            active.run_dir = active.run_dir or run_dir
            reason = str(exc) or "partial result"
            active.state = "failure_handling"
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_partial_completed", "error": reason, "time": iso_time(time.time())})
            self.emit_event(
                active,
                run_dir,
                "run_partial_completed",
                active.current_phase,
                status="partial_completed",
                progress=active.overall_percent,
                current_phase_percent=active.current_phase_percent,
                message="Run produced a partial result.",
                data={"reason": reason},
            )
            envelope = self.build_envelope(
                job,
                run_id,
                "partial_completed",
                started,
                artifact_dir,
                run_dir,
                error=reason,
                phase=active.current_phase,
            )
            upload_error = upload_artifacts_best_effort(self.client, job_id, active.attempt_id, artifact_dir)
            reconcile_envelope_artifact_manifest_with_uploads(envelope, artifact_dir)
            if upload_error:
                envelope.setdefault("extensions", {}).setdefault("worker_internal", {})["artifact_upload_error"] = upload_error
            if self.submit_result_or_record_failure(active, job_id, result_payload(active, envelope, "partial_completed", run_dir), artifact_dir, envelope):
                terminal_result_confirmed = True
                terminal_state = "partial_completed"
            else:
                terminal_state = "result_submit_failed"
                return
        except Exception as exc:
            if codex_error_code(str(exc)) == "CODEX_QUOTA_EXHAUSTED":
                self.quota_monitor.mark_exhausted(str(exc))
            artifact_dir = artifact_dir or self.isolation.artifacts / run_id
            run_dir = run_dir or self.isolation.workspaces / run_id / "repo" / ".codex-review" / "runs" / run_id
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_failed", "error": str(exc), "time": iso_time(time.time())})
            if isinstance(exc, RepositoryLimitExceeded):
                write_json(run_dir / "preflight.json", exc.preflight)
            self.emit_event(active, run_dir, "run_failed", active.current_phase, status="failed", progress=active.overall_percent, message=str(exc))
            envelope = self.build_envelope(
                job,
                run_id,
                "failed",
                started,
                artifact_dir,
                run_dir,
                error=str(exc),
                phase=active.current_phase,
            )
            upload_error = upload_artifacts_best_effort(self.client, job_id, active.attempt_id, artifact_dir)
            reconcile_envelope_artifact_manifest_with_uploads(envelope, artifact_dir)
            if upload_error:
                envelope.setdefault("extensions", {}).setdefault("worker_internal", {})["artifact_upload_error"] = upload_error
            if self.submit_result_or_record_failure(active, job_id, result_payload(active, envelope, "failed", run_dir), artifact_dir, envelope):
                terminal_result_confirmed = True
                terminal_state = "failed"
            else:
                terminal_state = "result_submit_failed"
                return
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join()
            if codex_client is not None:
                cleanup_error: BaseException | None = None
                is_running = getattr(codex_client, "is_running", None)
                runtime_is_running = not callable(is_running) or bool(is_running())
                if runtime_is_running and active.thread_id:
                    try:
                        release_codex_thread_reference(
                            codex_client,
                            active.thread_id,
                            suppress_errors=False,
                        )
                    except BaseException as exc:
                        cleanup_error = exc
                        if run_dir is not None:
                            append_jsonl(
                                run_dir / "worker.log.jsonl",
                                {
                                    "event": "codex_root_thread_archive_failed",
                                    "thread_id": active.thread_id,
                                    "error": quota_text(exc, 500),
                                    "time": iso_time(time.time()),
                                },
                            )
                elif not runtime_is_running:
                    cleanup_error = RuntimeError("Codex runtime is not healthy")
                if cleanup_error is not None:
                    close = getattr(codex_client, "close", None)
                    if callable(close):
                        try:
                            close()
                        except Exception:
                            pass
                    if self.codex_client is codex_client:
                        self.codex_client = None
                else:
                    codex_client.set_events_path(self.default_codex_events_path())
            if terminal_state in TERMINAL_STATES and terminal_result_confirmed:
                self.clear_active_run_marker(active)
                self.state.clear_active(terminal_state)
            else:
                active.state = "finishing"
                self.persist_active_run_marker(active)
            try:
                self.heartbeat()
            except PullwiseRequestError as exc:
                if not control_plane_error_is_retryable(exc):
                    raise
                if run_dir is not None:
                    try:
                        append_jsonl(
                            run_dir / "worker.log.jsonl",
                            {
                                "event": "final_idle_heartbeat_deferred",
                                "error": quota_text(exc, 500),
                                "time": iso_time(time.time()),
                            },
                        )
                    except Exception:
                        pass

    def prepare_workspace(
        self,
        job: dict[str, Any],
        run_id: str,
        *,
        deadline_monotonic: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[Path, Path, Path]:
        check_lifecycle_cancelled(cancel_requested)
        remaining_wall_time_seconds(deadline_monotonic)
        workspace = self.isolation.workspaces / run_id
        repo_dir = workspace / "repo"
        artifact_dir = self.isolation.artifacts / run_id
        run_dir = repo_dir / ".codex-review" / "runs" / run_id
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        repo_dir.mkdir(parents=True, exist_ok=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        source = str(job.get("checkout_dir") or job.get("checkoutDir") or "").strip()
        limits = repository_limits_for_job(job)
        if source:
            copy_tree(
                Path(source),
                repo_dir,
                max_files=limits.get("maxFiles") if limits else None,
                max_bytes=limits.get("maxBytes") if limits else None,
                deadline_monotonic=deadline_monotonic,
                cancel_requested=cancel_requested,
            )
        else:
            work_dir = Path(str(getattr(self.config, "work_dir", "") or self.isolation.worker_root))
            clone_repository_checkout(
                job,
                repo_dir,
                mirror_cache_root=work_dir / REPOSITORY_MIRROR_CACHE_DIR_NAME,
                deadline_monotonic=deadline_monotonic,
                cancel_requested=cancel_requested,
            )
        repo_supplied_review_root = repo_dir / ".codex-review"
        if repo_supplied_review_root.exists():
            if repo_supplied_review_root.is_symlink() or repo_supplied_review_root.is_file():
                repo_supplied_review_root.unlink()
            else:
                shutil.rmtree(repo_supplied_review_root)
        enforce_repository_limits(
            repo_dir,
            max_files=limits.get("maxFiles") if limits else None,
            max_bytes=limits.get("maxBytes") if limits else None,
            context="preparing checkout",
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        )
        if repository_file_count(
            repo_dir,
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        ) <= 0:
            raise RuntimeError("repository checkout produced no repository files")
        for path in (
            repo_dir / ".codex-review",
            run_dir,
            run_dir / "bundles",
            run_dir / "raw-reviewers",
            run_dir / "verified-reviewers",
            run_dir / "intent",
            run_dir / "intent" / "generated-tests",
            run_dir / "intent" / "test-output",
        ):
            path.mkdir(parents=True, exist_ok=True)
        write_worker_config(repo_dir / ".codex-review" / "worker-config.json", job, self.config)
        write_review_instruction_tree(repo_dir)
        return repo_dir, run_dir, artifact_dir

    def submit_result_or_record_failure(
        self,
        active: ActiveJob,
        job_id: str,
        payload: dict[str, Any],
        artifact_dir: Path,
        envelope: dict[str, Any],
    ) -> bool:
        try:
            validate_result_manifest_matches_uploaded_snapshot(envelope, artifact_dir)
        except Exception as exc:
            active.state = "finishing"
            active.current_phase = "submit_result_envelope"
            active.current_phase_status = "blocked"
            active.message = f"Result submit blocked: {exc}"
            write_json(artifact_dir / "result-envelope.json", envelope)
            write_json(
                artifact_dir / "result-submit-blocked.json",
                {
                    "run_id": active.run_id,
                    "job_id": active.job_id,
                    "lease_id": active.lease_id,
                    "attempt_id": active.attempt_id,
                    "result_status": result_status_from_envelope(envelope),
                    "status": "result_submit_blocked",
                    "created_at": iso_time(time.time()),
                    "error": str(exc),
                },
            )
            self.persist_active_run_marker(active)
            return False
        try:
            result_status = result_status_from_envelope(envelope)
            with self._progress_lock:
                if result_status == "done" and active.cancel_requested:
                    raise JobCancelled(active.cancel_reason or "cancel requested")
                active.terminal_result_in_flight = True
            self.client.result(job_id, payload)
            with self._progress_lock:
                active.terminal_result_in_flight = False
                active.terminal_result_submitted = True
            try:
                write_json(
                    artifact_dir / "result-submit-succeeded.json",
                    {
                        "run_id": active.run_id,
                        "job_id": active.job_id,
                        "lease_id": active.lease_id,
                        "attempt_id": active.attempt_id,
                        "result_status": result_status_from_envelope(envelope),
                        "status": "result_submit_succeeded",
                        "created_at": iso_time(time.time()),
                    },
                )
            except Exception:
                # The control plane has already accepted the terminal result.
                # A local marker failure must not be reclassified as a failed
                # submission; recovery remains conservatively blocked instead.
                pass
            return True
        except JobCancelled:
            with self._progress_lock:
                active.terminal_result_in_flight = False
            raise
        except Exception as exc:
            with self._progress_lock:
                active.terminal_result_in_flight = False
            active.state = "finishing"
            active.current_phase = "submit_result_envelope"
            active.current_phase_status = "failed"
            active.message = f"Result submit failed: {exc}"
            write_json(artifact_dir / "result-envelope.json", envelope)
            write_json(
                artifact_dir / "result-submit-failed.json",
                {
                    "run_id": active.run_id,
                    "job_id": active.job_id,
                    "lease_id": active.lease_id,
                    "attempt_id": active.attempt_id,
                    "result_status": result_status_from_envelope(envelope),
                    "status": "result_submit_failed",
                    "created_at": iso_time(time.time()),
                    "error": str(exc),
                },
            )
            self.persist_active_run_marker(active)
            return False


    def run_codex_auth_check(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if codex_client is None:
            raise RuntimeError("Codex SDK client is missing")
        del repo_dir, run_dir, job
        timeout_seconds = float(CODEX_ACCOUNT_READ_TIMEOUT_SECONDS)
        remaining = remaining_wall_time_seconds(deadline_monotonic)
        if remaining is not None:
            timeout_seconds = max(0.001, min(timeout_seconds, remaining))
        payload = codex_client.account(
            refresh_token=True,
            timeout_seconds=timeout_seconds,
            cancel_requested=self.poll_cancel_requested,
        )
        requires_auth = payload.get("requiresOpenaiAuth", payload.get("requires_openai_auth"))
        if not isinstance(requires_auth, bool):
            raise RuntimeError("Codex account response is missing requiresOpenaiAuth")
        if requires_auth and payload.get("account") is None:
            raise RuntimeError("Codex account authentication required")

    def run_semantic_phase(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        phase: str,
        *,
        deadline_monotonic: float | None = None,
    ) -> None:
        if codex_client is None:
            raise RuntimeError("Codex SDK client is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        if phase == "bundle_planning":
            prepare_bundle_planning_input(run_dir, job)
        effort = effort_for_phase(job, phase)
        declared_outputs = SEMANTIC_PHASE_MODEL_OUTPUTS.get(phase)
        if declared_outputs is None:
            raise RuntimeError(f"semantic phase has no declared model outputs: {phase}")
        turn_cwd = prepare_model_turn_workspace(
            repo_dir,
            run_dir,
            phase,
            declared_outputs,
            include_existing=False,
        )
        prompt = phase_prompt(phase, run_dir, job, output_dir=turn_cwd)
        try:
            codex_client.run_turn(
                thread_id=thread_id,
                repo_dir=repo_dir,
                turn_cwd=turn_cwd,
                prompt=prompt,
                effort=effort,
                read_only=False,
                timeout_seconds=turn_timeout_with_deadline(job, deadline_monotonic),
                cancel_requested=self.poll_cancel_requested,
                metrics_phase=phase,
            )
            publish_model_turn_outputs(turn_cwd, run_dir, declared_outputs)
        finally:
            cleanup_model_turn_workspace_after_turn(
                repo_dir,
                turn_cwd,
                run_dir=run_dir,
                primary_error=sys.exc_info()[1],
            )

    def _execute_reviewer_assignment(
        self,
        codex_client: CodexSdkClient,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        work: ReviewerAssignmentWork,
        *,
        thread_id: str,
        attempt: int,
        expected_assignments: set[tuple[str, str]],
        cancel_event: threading.Event,
        deadline_monotonic: float | None,
        output_budget: ReviewerFanoutOutputBudget | None = None,
    ) -> ReviewerAssignmentOutcome:
        def cancel_requested() -> bool:
            return cancel_event.is_set() or self.poll_cancel_requested()

        started_monotonic = time.monotonic()
        try:
            turn_metrics = codex_client.run_turn(
                thread_id=thread_id,
                repo_dir=repo_dir,
                turn_cwd=work.staging_dir,
                prompt=reviewer_assignment_prompt(
                    run_dir,
                    work.bundle_id,
                    work.reviewer_id,
                    job,
                    output_path=work.staging_output_path,
                ),
                effort=effort_for_phase(job, "reviewer_fanout"),
                read_only=False,
                timeout_seconds=turn_timeout_with_deadline(job, deadline_monotonic),
                cancel_requested=cancel_requested,
                metrics_phase="reviewer_fanout",
            )
        except Exception as exc:
            return ReviewerAssignmentOutcome(
                work=work,
                thread_id=thread_id,
                attempt=attempt,
                duration_ms=max(0, int((time.monotonic() - started_monotonic) * 1000)),
                error=exc,
            )

        reported_duration = getattr(turn_metrics, "duration_ms", None)
        if isinstance(reported_duration, bool) or not isinstance(reported_duration, (int, float)):
            reported_duration = (time.monotonic() - started_monotonic) * 1000
        duration_ms = max(0, int(reported_duration))

        try:
            output_payload = _bounded_regular_file_bytes(work.staging_output_path)
        except FileNotFoundError:
            output_payload = None
        except OSError as exc:
            return ReviewerAssignmentOutcome(
                work=work,
                thread_id=thread_id,
                attempt=attempt,
                duration_ms=duration_ms,
                error=exc,
            )
        try:
            payload = json.loads(output_payload) if output_payload is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        covered = _reviewer_output_assignments(payload, work.staging_output_path, expected_assignments)
        valid_output = (
            isinstance(payload, dict)
            and isinstance(payload.get("findings"), list)
            and covered == {(work.bundle_id, work.reviewer_id)}
        )
        finding_count = len(payload.get("findings") or []) if isinstance(payload, dict) else 0
        output_bytes = len(output_payload) if output_payload is not None else 0
        if valid_output and output_budget is not None:
            try:
                output_budget.reserve(output_bytes, work)
            except ReviewerFanoutOutputBudgetExceeded as exc:
                return ReviewerAssignmentOutcome(
                    work=work,
                    thread_id=thread_id,
                    attempt=attempt,
                    finding_count=finding_count,
                    duration_ms=duration_ms,
                    output_bytes=output_bytes,
                    error=exc,
                )
        return ReviewerAssignmentOutcome(
            work=work,
            thread_id=thread_id,
            attempt=attempt,
            valid_output=valid_output,
            finding_count=finding_count,
            duration_ms=duration_ms,
            output_bytes=output_bytes,
            output_payload=output_payload if valid_output else None,
        )

    def run_reviewer_fanout_phase(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        *,
        active: ActiveJob,
        progress: int,
        deadline_monotonic: float | None = None,
    ) -> None:
        if codex_client is None:
            raise RuntimeError("Codex SDK client is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        bundle_plan = read_json(run_dir / "bundle-plan.json", {})
        enforce_review_plan_resource_limits(bundle_plan, job)
        assignments = planned_reviewer_assignment_sequence(run_dir)
        if not assignments:
            raise RuntimeError("reviewer_fanout has no planned reviewer assignments")

        (
            max_concurrency,
            initial_effective_concurrency,
            concurrency_limit_reason,
            memory_snapshot,
        ) = reviewer_concurrency_decision_for_job(job)
        raw_dir = run_dir / "raw-reviewers"
        raw_dir.mkdir(parents=True, exist_ok=True)
        staging_root = model_turn_workspace_path(repo_dir, run_dir, "reviewer-fanout")
        if staging_root.is_symlink():
            staging_root.unlink()
        elif staging_root.exists():
            shutil.rmtree(staging_root)
        staging_root.mkdir(parents=True, exist_ok=True)
        execution_path = run_dir / "reviewer-execution.json"
        expected_assignments = set(assignments)
        bundle_weights: dict[str, float] = {}
        raw_bundles = bundle_plan.get("bundles") if isinstance(bundle_plan, dict) else []
        if isinstance(raw_bundles, list):
            for bundle in raw_bundles:
                if not isinstance(bundle, dict):
                    continue
                bundle_key = _normalized_review_bundle_id(bundle.get("bundle_id") or bundle.get("id"))
                if not bundle_key:
                    continue
                try:
                    bundle_weight = float(bundle.get("estimated_tokens") or 1)
                except (TypeError, ValueError):
                    bundle_weight = 1.0
                bundle_weights[bundle_key] = bundle_weight if math.isfinite(bundle_weight) and bundle_weight > 0 else 1.0
        works: list[ReviewerAssignmentWork] = []
        records: list[dict[str, Any]] = []
        for index, (bundle_id, reviewer_id) in enumerate(assignments, start=1):
            output_name = reviewer_assignment_output_name(bundle_id, reviewer_id)
            output_path = raw_dir / output_name
            if output_path.exists():
                output_path.unlink()
            assignment_hash = hashlib.sha256(
                f"{bundle_id}\0{reviewer_id}".encode("utf-8")
            ).hexdigest()[:12]
            staging_dir = staging_root / f"assignment-{index:04d}-{assignment_hash}"
            work = ReviewerAssignmentWork(
                index=index,
                bundle_id=bundle_id,
                reviewer_id=reviewer_id,
                output_name=output_name,
                output_path=output_path,
                staging_dir=staging_dir,
                staging_output_path=staging_dir / output_name,
                estimated_weight=bundle_weights.get(bundle_id, 1.0),
            )
            works.append(work)
            records.append(
                {
                    "bundle_id": bundle_id,
                    "reviewer_id": reviewer_id,
                    "output": f"raw-reviewers/{output_name}",
                    "status": "pending",
                    "attempts": [],
                }
            )
        output_budget = ReviewerFanoutOutputBudget(MAX_MODEL_OUTPUT_TOTAL_BYTES)

        estimator = active.current_run_estimator

        def estimate_unit_id(work: ReviewerAssignmentWork, attempt: int) -> str:
            return f"reviewer:{work.index}:attempt:{attempt}"

        latest_estimate_unit_ids: dict[int, str] = {}
        downstream_estimate_unit_id = phase_estimate_unit_id("reviewer_json_validation")
        if estimator is not None:
            estimator.set_resource_pool(
                "reviewer",
                configured_concurrency=max_concurrency,
                effective_concurrency=initial_effective_concurrency,
            )
            fanout_barrier_unit_id = phase_estimate_unit_id("reviewer_fanout")
            dependencies: tuple[str, ...] = ()
            if estimator.has_work_unit(fanout_barrier_unit_id):
                estimator.finish_work_unit(fanout_barrier_unit_id)
                dependencies = (fanout_barrier_unit_id,)
            for work in works:
                unit_id = estimate_unit_id(work, 1)
                estimator.add_work_unit(
                    unit_id,
                    kind="reviewer_turn",
                    resource_pool="reviewer",
                    dependencies=dependencies,
                    order=work.index,
                    weight=work.estimated_weight,
                )
                latest_estimate_unit_ids[work.index] = unit_id
            if estimator.has_work_unit(downstream_estimate_unit_id):
                estimator.replace_dependencies(
                    downstream_estimate_unit_id,
                    tuple(latest_estimate_unit_ids[index] for index in sorted(latest_estimate_unit_ids)),
                )
            estimator.mark_plan_ready()

        execution = {
            "schema_version": "reviewer-execution/v1",
            "strategy": "one_turn_per_assignment",
            "thread_strategy": "one_thread_per_assignment",
            "scheduler": "bounded_parallel",
            "root_thread_id": thread_id,
            "max_concurrency": max_concurrency,
            "effective_concurrency": initial_effective_concurrency,
            "concurrency_limit_reason": concurrency_limit_reason,
            "memory_at_start": memory_snapshot,
            "max_observed_concurrency": 0,
            "assignments_total": len(assignments),
            "assignments_completed": 0,
            "output_bytes_limit": output_budget.limit_bytes,
            "output_bytes_reserved": 0,
            "output_bytes_published": 0,
            "output_budget_status": "within_limit",
            "assignments": records,
        }
        write_json(execution_path, execution)
        if concurrency_limit_reason:
            append_jsonl(
                run_dir / "worker.log.jsonl",
                {
                    "event": "reviewer_concurrency_reduced",
                    "reason": concurrency_limit_reason,
                    "from": max_concurrency,
                    "to": initial_effective_concurrency,
                    "memory": memory_snapshot,
                    "time": iso_time(time.time()),
                },
            )

        pending = deque(works)
        active_futures: dict[
            Future[ReviewerAssignmentOutcome],
            tuple[
                ReviewerAssignmentWork,
                ReviewerAssignmentWork,
                dict[str, Any],
                dict[str, Any],
            ],
        ] = {}
        cancel_event = threading.Event()

        def assignment_cancel_requested() -> bool:
            return cancel_event.is_set() or self.poll_cancel_requested()

        completed = 0
        finished = 0
        published_output_bytes = 0
        effective_concurrency = initial_effective_concurrency
        fatal_error: BaseException | None = None
        max_rate_limit_retries = 1
        runtime_marked_unhealthy = False

        def mark_codex_runtime_unhealthy(error: BaseException, reviewer_thread_id: str) -> None:
            nonlocal runtime_marked_unhealthy
            if runtime_marked_unhealthy:
                return
            runtime_marked_unhealthy = True
            append_jsonl(
                run_dir / "worker.log.jsonl",
                {
                    "event": "codex_thread_archive_failed",
                    "thread_id": reviewer_thread_id,
                    "error": quota_text(error, 500),
                    "time": iso_time(time.time()),
                },
            )
            close = getattr(codex_client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        with ThreadPoolExecutor(
            max_workers=initial_effective_concurrency,
            thread_name_prefix="pullwise-reviewer",
        ) as executor:
            while (pending and fatal_error is None) or active_futures:
                if fatal_error is None and self.poll_cancel_requested():
                    fatal_error = JobCancelled("cancel requested")
                    cancel_event.set()

                while (
                    fatal_error is None
                    and pending
                    and len(active_futures) < effective_concurrency
                ):
                    work = pending.popleft()
                    record = records[work.index - 1]
                    attempt = len(record["attempts"]) + 1
                    attempt_staging_dir = work.staging_dir / f"attempt-{attempt:02d}"
                    if attempt_staging_dir.exists():
                        shutil.rmtree(attempt_staging_dir)
                    attempt_staging_dir.mkdir(parents=True, exist_ok=True)
                    attempt_work = replace(
                        work,
                        staging_dir=attempt_staging_dir,
                        staging_output_path=attempt_staging_dir / work.output_name,
                    )
                    if estimator is not None:
                        estimator.start_work_unit(estimate_unit_id(work, attempt))
                    try:
                        reviewer_thread_id = start_codex_thread_with_lifecycle(
                            codex_client,
                            repo_dir,
                            model_for_job(job),
                            timeout_seconds=turn_timeout_with_deadline(job, deadline_monotonic),
                            cancel_requested=assignment_cancel_requested,
                        )
                        if not reviewer_thread_id:
                            raise RuntimeError("Codex reviewer thread is missing")
                    except Exception as exc:
                        if estimator is not None:
                            estimator.finish_work_unit(
                                estimate_unit_id(work, attempt),
                                state="failed",
                            )
                        record.update(
                            {
                                "status": "failed",
                                "completed_at": iso_time(time.time()),
                                "error": quota_text(exc, 500),
                            }
                        )
                        fatal_error = exc
                        cancel_event.set()
                        write_json(execution_path, execution)
                        break

                    attempt_record = {
                        "attempt": attempt,
                        "thread_id": reviewer_thread_id,
                        "status": "running",
                        "started_at": iso_time(time.time()),
                    }
                    record["attempts"].append(attempt_record)
                    record.update(
                        {
                            "status": "running",
                            "thread_id": reviewer_thread_id,
                            "started_at": record.get("started_at") or attempt_record["started_at"],
                        }
                    )
                    execution["max_observed_concurrency"] = max(
                        int(execution["max_observed_concurrency"]),
                        len(active_futures) + 1,
                    )
                    write_json(execution_path, execution)
                    append_jsonl(
                        run_dir / "worker.log.jsonl",
                        {
                            "event": "reviewer_assignment_started",
                            "bundle_id": work.bundle_id,
                            "reviewer_id": work.reviewer_id,
                            "assignment_index": work.index,
                            "assignments_total": len(assignments),
                            "attempt": attempt,
                            "thread_id": reviewer_thread_id,
                            "time": iso_time(time.time()),
                        },
                    )
                    try:
                        future = executor.submit(
                            self._execute_reviewer_assignment,
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            attempt_work,
                            thread_id=reviewer_thread_id,
                            attempt=attempt,
                            expected_assignments=expected_assignments,
                            cancel_event=cancel_event,
                            deadline_monotonic=deadline_monotonic,
                            output_budget=output_budget,
                        )
                    except BaseException as exc:
                        try:
                            release_codex_thread_reference(
                                codex_client,
                                reviewer_thread_id,
                                suppress_errors=False,
                            )
                        except BaseException as cleanup_exc:
                            mark_codex_runtime_unhealthy(
                                cleanup_exc,
                                reviewer_thread_id,
                            )
                        if estimator is not None:
                            estimator.finish_work_unit(
                                estimate_unit_id(work, attempt),
                                state="failed",
                            )
                        attempt_record.update(
                            {
                                "status": "failed",
                                "completed_at": iso_time(time.time()),
                                "error": quota_text(exc, 500),
                            }
                        )
                        record.update(
                            {
                                "status": "failed",
                                "completed_at": attempt_record["completed_at"],
                                "error": attempt_record["error"],
                            }
                        )
                        fatal_error = exc
                        cancel_event.set()
                        write_json(execution_path, execution)
                        break
                    active_futures[future] = (work, attempt_work, record, attempt_record)
                    self.progress_phase(
                        active,
                        run_dir,
                        "reviewer_fanout",
                        progress,
                        current_phase_percent=min(90.0, (finished / len(assignments)) * 90.0),
                        message=f"Started reviewer assignment {work.index} of {len(assignments)}.",
                        data={
                            "reviewer_runs_total": len(assignments),
                            "reviewer_runs_completed": completed,
                            "active_unit": {
                                "bundle_id": work.bundle_id,
                                "reviewer_id": work.reviewer_id,
                                "assignment_index": work.index,
                                "attempt": attempt,
                            },
                        },
                    )

                if not active_futures:
                    break

                done, _pending_futures = wait(
                    tuple(active_futures),
                    return_when=FIRST_COMPLETED,
                )
                for future in sorted(done, key=lambda item: active_futures[item][0].index):
                    work, attempt_work, record, attempt_record = active_futures.pop(future)
                    try:
                        outcome = future.result()
                    except BaseException as exc:
                        outcome = ReviewerAssignmentOutcome(
                            work=attempt_work,
                            thread_id=str(attempt_record["thread_id"]),
                            attempt=int(attempt_record["attempt"]),
                            error=exc,
                        )

                    execution["output_bytes_reserved"] = output_budget.reserved_bytes
                    if outcome.output_bytes > 0:
                        attempt_record["output_bytes"] = outcome.output_bytes
                    if isinstance(
                        outcome.error,
                        ReviewerFanoutOutputBudgetExceeded,
                    ):
                        execution.update(
                            {
                                "output_budget_status": "exceeded",
                                "output_bytes_rejected": outcome.error.requested_bytes,
                                "output_budget_error": str(outcome.error),
                            }
                        )

                    if outcome.valid_output:
                        try:
                            if outcome.output_payload is None:
                                raise OSError("validated reviewer output payload is missing")
                            _write_worker_owned_bytes(
                                work.output_path,
                                outcome.output_payload,
                            )
                            published_output_bytes += len(outcome.output_payload)
                            execution["output_bytes_published"] = (
                                published_output_bytes
                            )
                        except OSError as exc:
                            outcome.error = exc
                            outcome.valid_output = False

                    reviewer_thread_id = str(attempt_record["thread_id"])
                    try:
                        release_codex_thread_reference(
                            codex_client,
                            reviewer_thread_id,
                            suppress_errors=False,
                        )
                    except BaseException as cleanup_exc:
                        mark_codex_runtime_unhealthy(
                            cleanup_exc,
                            reviewer_thread_id,
                        )
                        outcome.error = RuntimeError(
                            "Codex reviewer thread archive failed: "
                            f"{quota_text(cleanup_exc, 500)}"
                        )
                        outcome.valid_output = False
                        cancel_event.set()

                    duration_ms = max(0, int(outcome.duration_ms or 0))
                    attempt_record["duration_ms"] = duration_ms
                    if estimator is not None:
                        estimator.finish_work_unit(
                            estimate_unit_id(work, outcome.attempt),
                            duration_seconds=(duration_ms / 1000.0) if duration_ms > 0 else None,
                            state="failed" if outcome.error is not None else "completed",
                        )

                    if outcome.error is not None:
                        error_text = quota_text(outcome.error, 500)
                        attempt_record.update(
                            {
                                "status": "failed",
                                "completed_at": iso_time(time.time()),
                                "error": error_text,
                            }
                        )
                        can_retry_capacity = (
                            fatal_error is None
                            and outcome.attempt <= max_rate_limit_retries
                            and reviewer_error_is_transient_capacity(outcome.error)
                        )
                        if can_retry_capacity:
                            record.update(
                                {
                                    "status": "pending_retry",
                                    "error": error_text,
                                }
                            )
                            previous_effective_concurrency = effective_concurrency
                            effective_concurrency = 1
                            execution["effective_concurrency"] = 1
                            execution["concurrency_limit_reason"] = (
                                "transient_capacity_error"
                            )
                            if estimator is not None:
                                retry_attempt = outcome.attempt + 1
                                estimator.set_resource_pool(
                                    "reviewer",
                                    configured_concurrency=max_concurrency,
                                    effective_concurrency=effective_concurrency,
                                )
                                estimator.add_work_unit(
                                    estimate_unit_id(work, retry_attempt),
                                    kind="reviewer_turn",
                                    resource_pool="reviewer",
                                    dependencies=(estimate_unit_id(work, outcome.attempt),),
                                    order=work.index,
                                    weight=work.estimated_weight,
                                    state="retrying",
                                )
                                latest_estimate_unit_ids[work.index] = estimate_unit_id(
                                    work,
                                    retry_attempt,
                                )
                                if estimator.has_work_unit(downstream_estimate_unit_id):
                                    estimator.replace_dependencies(
                                        downstream_estimate_unit_id,
                                        tuple(
                                            latest_estimate_unit_ids[index]
                                            for index in sorted(latest_estimate_unit_ids)
                                        ),
                                    )
                            pending.appendleft(work)
                            cleanup_model_turn_workspace_after_turn(
                                repo_dir,
                                attempt_work.staging_dir,
                                run_dir=run_dir,
                                primary_error=outcome.error,
                            )
                            append_jsonl(
                                run_dir / "worker.log.jsonl",
                                {
                                    "event": "reviewer_concurrency_reduced",
                                    "reason": "transient_capacity_error",
                                    "from": previous_effective_concurrency,
                                    "to": 1,
                                    "bundle_id": work.bundle_id,
                                    "reviewer_id": work.reviewer_id,
                                    "error": error_text,
                                    "time": iso_time(time.time()),
                                },
                            )
                            write_json(execution_path, execution)
                            self.progress_phase(
                                active,
                                run_dir,
                                "reviewer_fanout",
                                progress,
                                current_phase_percent=min(90.0, (finished / len(assignments)) * 90.0),
                                message="Reviewer capacity reduced; retrying with concurrency 1.",
                                data={
                                    "reviewer_runs_total": len(assignments),
                                    "reviewer_runs_completed": completed,
                                    "active_unit": {
                                        "bundle_id": work.bundle_id,
                                        "reviewer_id": work.reviewer_id,
                                        "assignment_index": work.index,
                                        "attempt": outcome.attempt + 1,
                                    },
                                },
                            )
                            continue

                        cancelled = isinstance(outcome.error, JobCancelled)
                        record.update(
                            {
                                "status": "cancelled" if cancelled else "failed",
                                "completed_at": iso_time(time.time()),
                                "error": error_text,
                            }
                        )
                        append_jsonl(
                            run_dir / "worker.log.jsonl",
                            {
                                "event": "reviewer_assignment_cancelled" if cancelled else "reviewer_assignment_failed",
                                "bundle_id": work.bundle_id,
                                "reviewer_id": work.reviewer_id,
                                "attempt": outcome.attempt,
                                "thread_id": outcome.thread_id,
                                "error": error_text,
                                "time": iso_time(time.time()),
                            },
                        )
                        if fatal_error is None:
                            fatal_error = outcome.error
                            cancel_event.set()
                        write_json(execution_path, execution)
                        cleanup_model_turn_workspace_after_turn(
                            repo_dir,
                            attempt_work.staging_dir,
                            run_dir=run_dir,
                            primary_error=outcome.error,
                        )
                        continue

                    attempt_record.update(
                        {
                            "status": "completed" if outcome.valid_output else "invalid_output",
                            "completed_at": iso_time(time.time()),
                            "finding_count": outcome.finding_count,
                        }
                    )
                    record.update(
                        {
                            "status": "completed" if outcome.valid_output else "invalid_output",
                            "completed_at": iso_time(time.time()),
                            "finding_count": outcome.finding_count,
                        }
                    )
                    if outcome.valid_output:
                        completed += 1
                    else:
                        record["error"] = (
                            "exact assignment output is missing, malformed, or covers a different assignment"
                        )
                    cleanup_model_turn_workspace_after_turn(
                        repo_dir,
                        attempt_work.staging_dir,
                        run_dir=run_dir,
                        primary_error=None,
                    )
                    finished += 1
                    execution["assignments_completed"] = completed
                    write_json(execution_path, execution)
                    append_jsonl(
                        run_dir / "worker.log.jsonl",
                        {
                            "event": "reviewer_assignment_completed"
                            if outcome.valid_output
                            else "reviewer_assignment_invalid_output",
                            "bundle_id": work.bundle_id,
                            "reviewer_id": work.reviewer_id,
                            "finding_count": outcome.finding_count,
                            "assignment_index": work.index,
                            "assignments_total": len(assignments),
                            "attempt": outcome.attempt,
                            "thread_id": outcome.thread_id,
                            "time": iso_time(time.time()),
                        },
                    )
                    self.progress_phase(
                        active,
                        run_dir,
                        "reviewer_fanout",
                        progress,
                        current_phase_percent=min(
                            90.0,
                            (finished / len(assignments)) * 90.0,
                        ),
                        message=f"Completed reviewer assignment {finished} of {len(assignments)}.",
                        data={
                            "reviewer_runs_total": len(assignments),
                            "reviewer_runs_completed": completed,
                            "active_unit": {
                                "bundle_id": work.bundle_id,
                                "reviewer_id": work.reviewer_id,
                                "assignment_index": work.index,
                                "attempt": outcome.attempt,
                            },
                        },
                    )

        if fatal_error is not None:
            for work in pending:
                record = records[work.index - 1]
                if record.get("status") in {"pending", "pending_retry"}:
                    record.update(
                        {
                            "status": "cancelled",
                            "completed_at": iso_time(time.time()),
                            "error": "reviewer fanout stopped before this assignment started",
                        }
                    )
            write_json(execution_path, execution)
            cleanup_model_turn_workspace_after_turn(
                repo_dir,
                staging_root,
                run_dir=run_dir,
                primary_error=fatal_error,
            )
            raise fatal_error

        cleanup_model_turn_workspace_after_turn(
            repo_dir,
            staging_root,
            run_dir=run_dir,
            primary_error=None,
        )

    def _start_phase_repair_estimate(
        self,
        active: ActiveJob | None,
        phase: str,
    ) -> str | None:
        if active is None or active.current_run_estimator is None:
            return None
        estimator = active.current_run_estimator
        phase_unit_id = phase_estimate_unit_id(phase)
        if not estimator.has_work_unit(phase_unit_id):
            return None
        estimator.finish_work_unit(phase_unit_id)
        repair_number = active.estimate_repair_counts.get(phase, 0) + 1
        active.estimate_repair_counts[phase] = repair_number
        repair_unit_id = f"repair:{phase}:{repair_number}"
        repair_dependency_id = phase_unit_id
        previous_repair_unit_id = f"repair:{phase}:{repair_number - 1}"
        if repair_number > 1 and estimator.has_work_unit(previous_repair_unit_id):
            repair_dependency_id = previous_repair_unit_id
        phase_index = next(
            (index for index, (candidate, _progress) in enumerate(PIPELINE_PHASES) if candidate == phase),
            len(PIPELINE_PHASES),
        )
        estimator.add_work_unit(
            repair_unit_id,
            kind="semantic_turn",
            resource_pool="pipeline",
            dependencies=(repair_dependency_id,),
            order=(phase_index * 100) + repair_number,
            state="retrying",
        )
        next_phase = next(
            (
                PIPELINE_PHASES[index + 1][0]
                for index, (candidate, _progress) in enumerate(PIPELINE_PHASES[:-1])
                if candidate == phase
            ),
            "",
        )
        next_phase_unit_id = phase_estimate_unit_id(next_phase) if next_phase else ""
        if next_phase_unit_id and estimator.has_work_unit(next_phase_unit_id):
            estimator.replace_dependencies(next_phase_unit_id, (repair_unit_id,))
        estimator.start_work_unit(repair_unit_id)
        return repair_unit_id

    @staticmethod
    def _finish_phase_repair_estimate(
        active: ActiveJob | None,
        repair_unit_id: str | None,
        turn_metrics: object = None,
        *,
        state: str = "completed",
    ) -> None:
        if active is None or active.current_run_estimator is None or not repair_unit_id:
            return
        raw_duration_ms = getattr(turn_metrics, "duration_ms", None)
        duration_seconds: float | None = None
        try:
            parsed_duration_ms = int(raw_duration_ms)
        except (TypeError, ValueError):
            parsed_duration_ms = -1
        if parsed_duration_ms >= 0:
            duration_seconds = parsed_duration_ms / 1000.0
        active.current_run_estimator.finish_work_unit(
            repair_unit_id,
            duration_seconds=duration_seconds,
            state=state,
        )

    def repair_semantic_phase_outputs(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        phase: str,
        validation_error: object,
        *,
        active: ActiveJob | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        append_jsonl(
            run_dir / "worker.log.jsonl",
            {
                "event": "semantic_phase_output_repair",
                "phase": phase,
                "error": str(validation_error),
                "time": iso_time(time.time()),
            },
        )
        if codex_client is None:
            raise RuntimeError("Codex SDK client is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        repair_unit_id = self._start_phase_repair_estimate(active, phase)
        turn_metrics: object = None
        declared_outputs = SEMANTIC_PHASE_MODEL_OUTPUTS.get(phase)
        if declared_outputs is None:
            raise RuntimeError(f"semantic phase has no declared model outputs: {phase}")
        turn_cwd = prepare_model_turn_workspace(
            repo_dir,
            run_dir,
            f"{phase}-repair",
            declared_outputs,
            include_existing=True,
        )
        try:
            try:
                turn_metrics = codex_client.run_turn(
                    thread_id=thread_id,
                    repo_dir=repo_dir,
                    turn_cwd=turn_cwd,
                    prompt=phase_repair_prompt(
                        phase,
                        run_dir,
                        validation_error,
                        job,
                        output_dir=turn_cwd,
                    ),
                    effort=effort_for_phase(job, phase),
                    read_only=False,
                    timeout_seconds=turn_timeout_with_deadline(job, deadline_monotonic),
                    cancel_requested=self.poll_cancel_requested,
                    metrics_phase=f"{phase}_repair",
                )
                publish_model_turn_outputs(turn_cwd, run_dir, declared_outputs)
                fallback_semantic_artifact(run_dir, job, phase)
            finally:
                cleanup_model_turn_workspace_after_turn(
                    repo_dir,
                    turn_cwd,
                    run_dir=run_dir,
                    primary_error=sys.exc_info()[1],
                )
        except BaseException:
            self._finish_phase_repair_estimate(active, repair_unit_id, turn_metrics, state="failed")
            raise
        self._finish_phase_repair_estimate(active, repair_unit_id, turn_metrics)

    def _run_intent_execution_repair_turn(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        *,
        stage: str,
        attempt: int,
        diagnostics: dict[str, Any],
        active: ActiveJob | None = None,
        deadline_monotonic: float | None = None,
    ) -> bool:
        phase = "intent_test_writing" if stage == "preflight" else "intent_test_running"
        if codex_client is None:
            append_jsonl(
                run_dir / "worker.log.jsonl",
                {
                    "event": "intent_test_execution_repair_unavailable",
                    "stage": stage,
                    "attempt": attempt,
                    "reason": "Codex SDK client is missing",
                    "time": iso_time(time.time()),
                },
            )
            return False
        state = read_json(run_dir / "run-state.json", {})
        thread_id = str(state.get("thread_id") or "") if isinstance(state, dict) else ""
        if not thread_id:
            append_jsonl(
                run_dir / "worker.log.jsonl",
                {
                    "event": "intent_test_execution_repair_unavailable",
                    "stage": stage,
                    "attempt": attempt,
                    "reason": "Codex thread is missing",
                    "time": iso_time(time.time()),
                },
            )
            return False
        append_jsonl(
            run_dir / "worker.log.jsonl",
            {
                "event": "intent_test_execution_repair_started",
                "stage": stage,
                "attempt": attempt,
                "time": iso_time(time.time()),
            },
        )
        declared_outputs = SEMANTIC_PHASE_MODEL_OUTPUTS["intent_test_writing"]
        turn_cwd = prepare_model_turn_workspace(
            repo_dir,
            run_dir,
            f"intent-{stage}-repair-{attempt}",
            declared_outputs,
            include_existing=True,
        )
        prompt = intent_execution_repair_prompt(
            run_dir,
            stage=stage,
            attempt=attempt,
            job=job,
            output_dir=turn_cwd,
        )
        prompt += "\nCurrent structured diagnostics:\n"
        prompt += json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)[:20000]
        repair_unit_id = self._start_phase_repair_estimate(active, phase)
        turn_metrics: object = None
        primary_error: BaseException | None = None
        try:
            turn_metrics = codex_client.run_turn(
                thread_id=thread_id,
                repo_dir=repo_dir,
                turn_cwd=turn_cwd,
                prompt=prompt,
                effort=effort_for_phase(job, phase),
                read_only=False,
                timeout_seconds=turn_timeout_with_deadline(job, deadline_monotonic),
                cancel_requested=self.poll_cancel_requested,
                metrics_phase=f"{phase}_repair",
            )
            publish_model_turn_outputs(turn_cwd, run_dir, declared_outputs)
        except JobCancelled as exc:
            primary_error = exc
            self._finish_phase_repair_estimate(active, repair_unit_id, turn_metrics, state="failed")
            raise
        except Exception as exc:
            primary_error = exc
            self._finish_phase_repair_estimate(active, repair_unit_id, turn_metrics, state="failed")
            append_jsonl(
                run_dir / "worker.log.jsonl",
                {
                    "event": "intent_test_execution_repair_failed",
                    "stage": stage,
                    "attempt": attempt,
                    "error": str(exc),
                    "time": iso_time(time.time()),
                },
            )
            return False
        except BaseException as exc:
            primary_error = exc
            self._finish_phase_repair_estimate(active, repair_unit_id, turn_metrics, state="failed")
            raise
        finally:
            try:
                cleanup_model_turn_workspace_after_turn(
                    repo_dir,
                    turn_cwd,
                    run_dir=run_dir,
                    primary_error=primary_error,
                )
            except OSError:
                self._finish_phase_repair_estimate(
                    active,
                    repair_unit_id,
                    turn_metrics,
                    state="failed",
                )
                raise
        self._finish_phase_repair_estimate(active, repair_unit_id, turn_metrics)
        append_jsonl(
            run_dir / "worker.log.jsonl",
            {
                "event": "intent_test_execution_repair_completed",
                "stage": stage,
                "attempt": attempt,
                "duration_ms": getattr(turn_metrics, "duration_ms", None),
                "time": iso_time(time.time()),
            },
        )
        return True

    def repair_intent_test_preflight(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        *,
        max_attempts: int = 1,
        active: ActiveJob | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
        validation_root = str(validation.get("validation_repo_root") or "").strip() if isinstance(validation, dict) else ""
        execution_repo = Path(validation_root) if validation_root else repo_dir
        refresh_agentic_execution_capabilities(execution_repo, run_dir)
        preflight = intent_test_source_preflight_payload(run_dir)
        write_json(run_dir / "intent" / "intent-test-preflight.json", preflight)
        for repair_attempt in range(1, max(0, int(max_attempts)) + 1):
            summary = preflight.get("summary") if isinstance(preflight.get("summary"), dict) else {}
            if int(summary.get("agent_repairable") or 0) <= 0:
                break
            if not self._run_intent_execution_repair_turn(
                codex_client,
                repo_dir,
                run_dir,
                job,
                stage="preflight",
                attempt=repair_attempt,
                diagnostics=preflight,
                active=active,
                deadline_monotonic=deadline_monotonic,
            ):
                break
            repair_intent_test_source_artifact(run_dir / "intent" / "intent-test-source.json", run_dir)
            refresh_agentic_execution_capabilities(execution_repo, run_dir)
            preflight = intent_test_source_preflight_payload(run_dir)
            write_json(run_dir / "intent" / "intent-test-preflight.json", preflight)
            workspace_integrity = (
                preflight.get("workspace_integrity")
                if isinstance(preflight.get("workspace_integrity"), dict)
                else {}
            )
            if workspace_integrity.get("status") == "violation":
                append_jsonl(
                    run_dir / "worker.log.jsonl",
                    {
                        "event": "intent_test_execution_repair_rejected",
                        "stage": "preflight",
                        "attempt": repair_attempt,
                        "reason": "validation workspace repository files differ from the immutable inventory",
                        "time": iso_time(time.time()),
                    },
                )
                break
        return preflight

    def repair_intent_test_runtime(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        *,
        max_attempts: int = 1,
        active: ActiveJob | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        raw_path = run_dir / "intent" / "intent-test-results.raw.json"
        current = read_json(raw_path, {})
        if not isinstance(current, dict):
            current = {"schema_version": "intent-test-run-results/v1", "run_id": run_dir.name, "test_runs": []}
        history_path = run_dir / "intent" / "intent-test-execution-history.json"
        history = read_json(history_path, {})
        if not isinstance(history, dict) or history.get("schema_version") != "intent-test-execution-history/v1":
            history = {
                "schema_version": "intent-test-execution-history/v1",
                "run_id": run_dir.name,
                "attempts": [],
            }
        attempts = history.get("attempts") if isinstance(history.get("attempts"), list) else []
        if not attempts:
            attempts.append(current)
        history["attempts"] = attempts
        write_json(history_path, history)

        diagnostics_path = run_dir / "intent" / "intent-test-runtime-diagnostics.json"
        diagnostics = intent_runtime_repair_diagnostics(current)
        write_json(diagnostics_path, diagnostics)
        validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
        validation_root = str(validation.get("validation_repo_root") or "").strip() if isinstance(validation, dict) else ""
        execution_repo = Path(validation_root) if validation_root else repo_dir
        current_attempt = max(
            [
                max(1, int(item.get("attempt") or 1))
                for item in current.get("test_runs", [])
                if isinstance(item, dict)
            ]
            or [1]
        )
        for repair_attempt in range(1, max(0, int(max_attempts)) + 1):
            candidates = diagnostics.get("repair_candidates") if isinstance(diagnostics.get("repair_candidates"), list) else []
            candidate_ids = {
                str(candidate.get("test_id") or "").strip()
                for candidate in candidates
                if isinstance(candidate, dict) and str(candidate.get("test_id") or "").strip()
            }
            if not candidate_ids:
                break
            if not self._run_intent_execution_repair_turn(
                codex_client,
                repo_dir,
                run_dir,
                job,
                stage="runtime",
                attempt=repair_attempt,
                diagnostics=diagnostics,
                active=active,
                deadline_monotonic=deadline_monotonic,
            ):
                break
            repair_intent_test_source_artifact(run_dir / "intent" / "intent-test-source.json", run_dir)
            refresh_agentic_execution_capabilities(execution_repo, run_dir)
            preflight = intent_test_source_preflight_payload(run_dir)
            write_json(run_dir / "intent" / "intent-test-preflight.json", preflight)
            workspace_integrity = (
                preflight.get("workspace_integrity")
                if isinstance(preflight.get("workspace_integrity"), dict)
                else {}
            )
            if workspace_integrity.get("status") == "violation":
                repair_rejections = (
                    history.get("repair_rejections")
                    if isinstance(history.get("repair_rejections"), list)
                    else []
                )
                repair_rejections.append(
                    {
                        "stage": "runtime",
                        "attempt": repair_attempt,
                        "reason_code": "validation_workspace_modified",
                        "violations": workspace_integrity.get("violations") or [],
                    }
                )
                history["repair_rejections"] = repair_rejections
                write_json(history_path, history)
                append_jsonl(
                    run_dir / "worker.log.jsonl",
                    {
                        "event": "intent_test_execution_repair_rejected",
                        "stage": "runtime",
                        "attempt": repair_attempt,
                        "reason": "validation workspace repository files differ from the immutable inventory",
                        "time": iso_time(time.time()),
                    },
                )
                break
            current_attempt += 1
            retry = run_intent_tests(
                run_dir,
                only_test_ids=candidate_ids,
                attempt=current_attempt,
                deadline_monotonic=deadline_monotonic,
                cancel_requested=self.poll_cancel_requested,
            )
            retry_runs = retry.get("test_runs") if isinstance(retry.get("test_runs"), list) else []
            retry_by_id = {
                str(item.get("test_id") or "").strip(): item
                for item in retry_runs
                if isinstance(item, dict) and str(item.get("test_id") or "").strip()
            }
            merged_runs: list[dict[str, Any]] = []
            for item in current.get("test_runs", []):
                if not isinstance(item, dict):
                    continue
                test_id = str(item.get("test_id") or "").strip()
                merged_runs.append(retry_by_id.pop(test_id, item))
            merged_runs.extend(retry_by_id.values())
            current = {
                **current,
                "schema_version": "intent-test-run-results/v1",
                "run_id": run_dir.name,
                "test_runs": merged_runs,
            }
            write_json(raw_path, current)
            attempts.append(retry)
            history["attempts"] = attempts
            history["final_result"] = current
            write_json(history_path, history)
            diagnostics = intent_runtime_repair_diagnostics(current)
            write_json(diagnostics_path, diagnostics)
        return current

    def run_reviewer_json_validation_phase(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        *,
        active: ActiveJob | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        max_repair_attempts = 2
        for repair_attempt in range(max_repair_attempts + 1):
            try:
                validate_reviewer_outputs(run_dir)
                return
            except RuntimeError as validation_exc:
                if repair_attempt >= max_repair_attempts:
                    raise
                self.repair_reviewer_outputs(
                    codex_client,
                    repo_dir,
                    run_dir,
                    job,
                    validation_exc,
                    active=active,
                    deadline_monotonic=deadline_monotonic,
                )

    def repair_reviewer_outputs(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        validation_error: object,
        *,
        active: ActiveJob | None = None,
        deadline_monotonic: float | None = None,
    ) -> None:
        append_jsonl(
            run_dir / "worker.log.jsonl",
            {
                "event": "reviewer_json_output_repair",
                "phase": "reviewer_json_validation",
                "error": str(validation_error),
                "time": iso_time(time.time()),
            },
        )
        if codex_client is None:
            raise RuntimeError("Codex SDK client is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        repair_unit_id = self._start_phase_repair_estimate(active, "reviewer_json_validation")
        turn_metrics: object = None
        declared_outputs = ("raw-reviewers",)
        turn_cwd = prepare_model_turn_workspace(
            repo_dir,
            run_dir,
            "reviewer-json-repair",
            declared_outputs,
            include_existing=True,
        )
        try:
            try:
                turn_metrics = codex_client.run_turn(
                    thread_id=thread_id,
                    repo_dir=repo_dir,
                    turn_cwd=turn_cwd,
                    prompt=reviewer_json_repair_prompt(
                        run_dir,
                        validation_error,
                        job,
                        output_dir=turn_cwd,
                    ),
                    effort=effort_for_phase(job, "reviewer_json_validation"),
                    read_only=False,
                    timeout_seconds=turn_timeout_with_deadline(job, deadline_monotonic),
                    cancel_requested=self.poll_cancel_requested,
                    metrics_phase="reviewer_json_validation_repair",
                )
                publish_model_turn_outputs(turn_cwd, run_dir, declared_outputs)
            finally:
                cleanup_model_turn_workspace_after_turn(
                    repo_dir,
                    turn_cwd,
                    run_dir=run_dir,
                    primary_error=sys.exc_info()[1],
                )
        except BaseException:
            self._finish_phase_repair_estimate(active, repair_unit_id, turn_metrics, state="failed")
            raise
        self._finish_phase_repair_estimate(active, repair_unit_id, turn_metrics)

    def run_mechanical_phase(
        self,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        phase: str,
        *,
        active: ActiveJob | None = None,
        progress: int = 0,
        deadline_monotonic: float | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        check_lifecycle_cancelled(cancel_requested)
        remaining_wall_time_seconds(deadline_monotonic)
        if phase == "bootstrap_helper_scripts":
            write_review_instruction_tree(repo_dir)
            write_bootstrap_helper_summary(run_dir)
        elif phase == "inventory_repository":
            policy = validate_job_policy(job)
            limits = policy["repository_limits"] if isinstance(policy.get("repository_limits"), dict) else {}
            inv = inventory(
                repo_dir,
                max_files=int(limits.get("maxFiles")) if limits.get("maxFiles") is not None else None,
                max_bytes=int(limits.get("maxBytes")) if limits.get("maxBytes") is not None else None,
                deadline_monotonic=deadline_monotonic,
                cancel_requested=cancel_requested,
            )
            write_immutable_inventory_baseline(repo_dir, run_dir, inv)
            write_json(run_dir / "inventory.json", inv)
            try:
                write_json(run_dir / "repo-profile.json", minimal_repo_profile_payload(inv, repo_dir))
            except Exception as exc:
                append_jsonl(
                    run_dir / "worker.log.jsonl",
                    {"event": "repo_profile_skipped", "reason": str(exc), "time": iso_time(time.time())},
                )
        elif phase == "token_budget":
            write_json(run_dir / "token-budget.json", token_budget_payload(run_dir, job))
        elif phase == "intent_test_validation":
            ensure_intent_directories(run_dir)
            write_json(run_dir / "intent" / "intent-test-validation.json", intent_validation_config(job))
            refresh_agentic_execution_capabilities(repo_dir, run_dir)
        elif phase == "bundle_packing":
            pack_bundles(repo_dir, run_dir)
        elif phase == "reviewer_json_validation":
            validate_reviewer_outputs(run_dir)
        elif phase == "location_validation":
            write_json(run_dir / "location-verification.json", location_verification_payload(repo_dir, run_dir))
        elif phase == "validation_workspace_prepare":
            prepare_validation_workspace(
                repo_dir,
                run_dir,
                deadline_monotonic=deadline_monotonic,
                cancel_requested=cancel_requested,
            )
        elif phase == "intent_test_running":
            write_json(
                run_dir / "intent" / "intent-test-results.raw.json",
                run_intent_tests(
                    run_dir,
                    deadline_monotonic=deadline_monotonic,
                    cancel_requested=cancel_requested,
                ),
            )
            refresh_coverage_intent_counters(run_dir)
        elif phase == "render_markdown_report":
            report = read_json(run_dir / "report.agent.json", default_agent_report(job))
            (run_dir / "report.md").write_text(
                render_markdown(report, output_language=output_language_for_job(job)),
                encoding="utf-8",
            )
        elif phase == "qa_gate":
            artifact_dir = self.isolation.artifacts / safe_id(job.get("run_id") or f"run_{job.get('job_id')}", "run")
            expected_language = output_language_for_job(job)
            write_json(
                run_dir / "qa.json",
                qa_gate_payload(repo_dir, run_dir, expected_output_language=expected_language),
            )
            for _attempt in range(2):
                materialize_artifacts(run_dir, artifact_dir)
                write_json(
                    run_dir / "qa.json",
                    qa_gate_payload(
                        repo_dir,
                        run_dir,
                        artifact_dir,
                        expected_output_language=expected_language,
                    ),
                )
            materialize_artifacts(run_dir, artifact_dir)
        elif phase == "upload_artifacts":
            artifact_dir = self.isolation.artifacts / safe_id(job.get("run_id") or f"run_{job.get('job_id')}", "run")

            def upload_progress(uploaded: int, total: int, item: dict[str, Any]) -> None:
                if active is None:
                    return
                self.progress_phase(
                    active,
                    run_dir,
                    "upload_artifacts",
                    progress,
                    current_phase_percent=(100.0 if total <= 0 else min(100.0, (uploaded / total) * 100.0)),
                    message=f"Uploaded artifact {uploaded} of {total}.",
                    data={
                        "artifacts_total": total,
                        "artifacts_uploaded": uploaded,
                        "artifact_id": str(item.get("artifact_id") or ""),
                    },
                )

            upload_artifacts(
                self.client,
                safe_id(job.get("job_id"), "job"),
                active_attempt_id(self.config, job),
                artifact_dir,
                progress_callback=upload_progress,
                source_run_dir=run_dir,
            )
        elif phase == "hash_artifacts":
            materialize_artifacts(run_dir, self.isolation.artifacts / safe_id(job.get("run_id") or f"run_{job.get('job_id')}", "run"))

    def codex_usage_for_run(self, run_dir: Path) -> dict[str, Any] | None:
        codex_client = self.codex_client
        if codex_client is None:
            return None
        raw_events_path = getattr(codex_client, "events_path", None)
        if raw_events_path is None:
            return None
        try:
            current_events_path = Path(raw_events_path).resolve(strict=False)
            expected_events_path = (run_dir / "codex-events.jsonl").resolve(strict=False)
        except (OSError, TypeError, ValueError):
            return None
        if current_events_path != expected_events_path:
            return None
        snapshot_reader = getattr(codex_client, "usage_snapshot", None)
        if not callable(snapshot_reader):
            return None
        try:
            payload = snapshot_reader()
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("schema_version") != "codex-usage/v1":
            return None
        if not isinstance(payload.get("observed"), bool) or not isinstance(payload.get("tokens"), dict):
            return None
        return copy.deepcopy(payload)

    def build_envelope(
        self,
        job: dict[str, Any],
        run_id: str,
        status: str,
        started: float,
        artifact_dir: Path,
        run_dir: Path,
        *,
        error: str = "",
        phase: str = "",
    ) -> dict[str, Any]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if status == "completed":
            if not (artifact_dir / "artifact-manifest.json").exists():
                materialize_artifacts(run_dir, artifact_dir)
        else:
            materialize_terminal_artifacts(run_dir, artifact_dir, status, error=error)
        refresh_terminal_run_snapshot(run_dir, run_id, status)
        if status != "completed":
            refresh_log_artifacts(run_dir, artifact_dir, status=status, error=error)
        manifest = result_artifact_manifest_items(artifact_dir)
        now = time.time()
        error_payload = failure_payload_for_error(error, status=status, phase=phase) if error else None
        bundle_plan = read_json(run_dir / "bundle-plan.json", {})
        planned_bundles = bundle_plan.get("bundles") if isinstance(bundle_plan, dict) else []
        bundle_count = len(planned_bundles) if isinstance(planned_bundles, list) else 0
        usage = self.codex_usage_for_run(run_dir)
        return {
            "protocol_version": PROTOCOL_VERSION,
            "message_type": "review_run_result",
            "created_at": iso_time(now),
            "job": {
                "job_id": safe_id(job.get("job_id"), "job"),
                "run_id": run_id,
                "lease_id": safe_id(job.get("lease_id") or f"lease_{job.get('job_id')}", "lease"),
                "job_type": "repo_review.full_scan",
            },
            "worker": {
                "worker_id": str(self.config.worker_id),
                "worker_version": WORKER_VERSION,
                "concurrency": {"max_active_jobs": 1, "maintains_local_queue": False},
                "engine": {"type": "codex_app_server", "app_server_transport": "stdio"},
            },
            "repository": repository_payload(job, run_dir),
            "execution": {
                "status": status,
                "review_mode": "full_repo",
                "started_at": iso_time(started),
                "completed_at": iso_time(now),
                "duration_ms": int((now - started) * 1000),
            },
            "progress_final": progress_final_payload(run_dir, run_id, status),
            "error": error_payload,
            "summary": summary_payload(run_dir, status),
            "quality_gate": read_json(run_dir / "qa.json", {"status": "fail", "errors": ["run did not reach qa gate"], "warnings": []}),
            "preflight": read_json(run_dir / "preflight.json", {}),
            **({"usage": usage} if usage is not None else {}),
            "artifact_manifest": manifest,
            "extensions": {"worker_internal": {"bundle_count": bundle_count}},
        }


class JobCancelled(RuntimeError):
    pass


class JobPartialCompleted(RuntimeError):
    pass


def check_lifecycle_cancelled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise JobCancelled("cancel requested")


def remaining_wall_time_seconds(deadline_monotonic: float | None) -> float | None:
    if deadline_monotonic is None:
        return None
    remaining = float(deadline_monotonic) - time.monotonic()
    if remaining <= 0:
        raise JobPartialCompleted("review wall-time deadline exceeded")
    return remaining


def turn_timeout_with_deadline(job: dict[str, Any], deadline_monotonic: float | None) -> int:
    configured = max(1, int(turn_timeout_for_job(job)))
    remaining = remaining_wall_time_seconds(deadline_monotonic)
    if remaining is None:
        return configured
    if remaining < 1:
        raise JobPartialCompleted("review wall-time deadline exceeded")
    return max(1, min(configured, int(remaining)))


def invoke_with_lifecycle_controls(
    method: Callable[..., Any],
    *args: Any,
    deadline_monotonic: float | None,
    cancel_requested: Callable[[], bool] | None = None,
    **kwargs: Any,
) -> Any:
    """Pass lifecycle controls while preserving legacy subclass overrides."""
    parameters = inspect.signature(method).parameters.values()
    names = {parameter.name for parameter in parameters}
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    if accepts_kwargs or "deadline_monotonic" in names:
        kwargs["deadline_monotonic"] = deadline_monotonic
    if cancel_requested is not None and (accepts_kwargs or "cancel_requested" in names):
        kwargs["cancel_requested"] = cancel_requested
    return method(*args, **kwargs)


def start_codex_thread_with_lifecycle(
    codex_client: Any,
    repo_dir: Path,
    model: str,
    *,
    timeout_seconds: int,
    cancel_requested: Callable[[], bool] | None,
) -> str:
    method = codex_client.start_thread
    parameters = inspect.signature(method).parameters.values()
    names = {parameter.name for parameter in parameters}
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
    kwargs: dict[str, Any] = {}
    if accepts_kwargs or "timeout_seconds" in names:
        kwargs["timeout_seconds"] = timeout_seconds
    if accepts_kwargs or "cancel_requested" in names:
        kwargs["cancel_requested"] = cancel_requested
    result = method(repo_dir, model, **kwargs)
    return str(result or "").strip()


def release_codex_thread_reference(
    codex_client: Any,
    thread_id: str,
    *,
    suppress_errors: bool = True,
) -> bool:
    release_thread = getattr(codex_client, "release_thread", None)
    if not callable(release_thread) or not thread_id:
        return False
    try:
        release_thread(thread_id)
    except Exception:
        if not suppress_errors:
            raise
        return False
    return True


class RepositoryLimitExceeded(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        files_seen: int,
        bytes_seen: int,
        max_files: int | None,
        max_bytes: int | None,
    ) -> None:
        super().__init__(message)
        self.preflight = repository_limit_preflight_payload(
            files_seen=files_seen,
            bytes_seen=bytes_seen,
            max_files=max_files,
            max_bytes=max_bytes,
        )


def repository_limit_preflight_payload(
    *,
    files_seen: int,
    bytes_seen: int,
    max_files: int | None,
    max_bytes: int | None,
) -> dict[str, Any]:
    reasons = []
    if max_files is not None and files_seen > max_files:
        reasons.append("file_count")
    if max_bytes is not None and bytes_seen > max_bytes:
        reasons.append("total_bytes")
    limits: dict[str, int] = {}
    if max_files is not None:
        limits["maxFiles"] = int(max_files)
    if max_bytes is not None:
        limits["maxBytes"] = int(max_bytes)
    return {
        "mode": "static",
        "execution": "repository_limit_check",
        "summary": "Repository checkout exceeds Pullwise worker repository limits.",
        "repositoryStats": {
            "fileCount": int(files_seen),
            "totalBytes": int(bytes_seen),
            "scanStoppedEarly": False,
        },
        "repositoryLimits": limits,
        "repositoryLimitExceeded": True,
        "repositoryLimitReasons": reasons,
    }


def safe_id(value: Any, prefix: str) -> str:
    fallback = f"{prefix}_{int(time.time())}"
    text = str(value or "").strip()
    text = text.replace("/", "_").replace("\\", "_")
    text = re.sub(r"[\x00-\x1f\x7f]+", "_", text)
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("._-")
    if not text or text in {".", ".."}:
        return fallback
    return text


def model_turn_workspace_path(
    repo_dir: Path,
    run_dir: Path,
    purpose: str,
) -> Path:
    workspace_root = repo_dir.parent / MODEL_TURN_WORKSPACES_DIR_NAME
    candidate = workspace_root / safe_id(run_dir.name, "run") / safe_id(purpose, "turn")
    if not _path_is_within(candidate.resolve(strict=False), workspace_root.resolve(strict=False)):
        raise ValueError("model turn workspace escapes the worker-owned staging root")
    return candidate


@dataclass(frozen=True)
class ModelOutputFile:
    payload: bytes
    executable: bool = False


def _bounded_regular_model_file(path: Path) -> ModelOutputFile:
    if path_has_symlink_component(path):
        raise OSError(f"model output path contains a symlink: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        stat_result = os.fstat(descriptor)
        if not stat.S_ISREG(stat_result.st_mode):
            raise OSError(f"model output is not a regular file: {path}")
        if stat_result.st_size > MAX_MODEL_OUTPUT_FILE_BYTES:
            raise OSError(f"model output exceeds the per-file limit: {path}")
        expected_size = int(stat_result.st_size)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            payload = handle.read(expected_size + 1)
            final_stat = os.fstat(handle.fileno())
        if len(payload) != expected_size or any(
            getattr(final_stat, field, None) != getattr(stat_result, field, None)
            for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
        ):
            raise OSError(f"model output changed while it was being read: {path}")
        return ModelOutputFile(
            payload=payload,
            executable=bool(stat.S_IMODE(stat_result.st_mode) & 0o111),
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bounded_regular_file_bytes(path: Path) -> bytes:
    return _bounded_regular_model_file(path).payload


def _declared_model_output_snapshot(
    source_root: Path,
    declared_outputs: tuple[str, ...],
) -> dict[str, ModelOutputFile]:
    snapshot: dict[str, ModelOutputFile] = {}
    total_bytes = 0
    resolved_root = source_root.resolve(strict=False)

    def record(relative_file: str, file_path: Path) -> None:
        nonlocal total_bytes
        previous = snapshot.get(relative_file)
        if previous is None and len(snapshot) >= MAX_MODEL_OUTPUT_FILES:
            raise OSError("model output exceeds the file-count limit")
        output = _bounded_regular_model_file(file_path)
        projected_total = total_bytes - (
            len(previous.payload) if previous is not None else 0
        ) + len(output.payload)
        if projected_total > MAX_MODEL_OUTPUT_TOTAL_BYTES:
            raise OSError("model output exceeds the aggregate byte limit")
        snapshot[relative_file] = output
        total_bytes = projected_total

    for declared in declared_outputs:
        relative = PurePosixPath(declared)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid declared model output path: {declared}")
        source = source_root.joinpath(*relative.parts)
        if not source.exists() and not source.is_symlink():
            continue
        if path_has_symlink_component(source):
            raise OSError(f"declared model output contains a symlink: {source}")
        if not _path_is_within(source.resolve(strict=False), resolved_root):
            raise OSError(f"declared model output escapes staging: {source}")
        if source.is_file():
            record(relative.as_posix(), source)
            continue
        if not source.is_dir():
            raise OSError(f"declared model output is not a file or directory: {source}")
        for current_root, directories, filenames in os.walk(source, followlinks=False):
            current = Path(current_root)
            for name in directories:
                directory = current / name
                if directory.is_symlink():
                    raise OSError(f"declared model output directory contains a symlink: {directory}")
            for name in filenames:
                file_path = current / name
                relative_file = file_path.relative_to(source_root).as_posix()
                record(relative_file, file_path)
    return snapshot


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_tree(path: Path) -> None:
    if os.name == "nt":
        return
    for current_root, _directories, _filenames in os.walk(path, topdown=False):
        _fsync_directory(Path(current_root))
    _fsync_directory(path.parent)


def _fsync_directory_chain(path: Path, root: Path) -> None:
    if os.name == "nt":
        return
    resolved_root = root.resolve(strict=False)
    current = path.resolve(strict=False)
    if not _path_is_within(current, resolved_root):
        raise OSError(f"directory sync path escapes its root: {path}")
    while True:
        _fsync_directory(current)
        if current == resolved_root:
            return
        current = current.parent


def _write_worker_owned_bytes(
    path: Path,
    payload: bytes,
    *,
    executable: bool = False,
) -> None:
    if path_has_symlink_component(path.parent):
        raise OSError(f"worker output parent contains a symlink: {path.parent}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    mode = 0o700 if executable else 0o600
    descriptor = os.open(temporary, flags, mode)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _validate_worker_owned_output(path: Path, root: Path) -> None:
    resolved_root = root.resolve(strict=False)
    if not _path_is_within(path.resolve(strict=False), resolved_root):
        raise OSError(f"worker output path escapes its root: {path}")
    if path.is_symlink() or path_has_symlink_component(path.parent):
        raise OSError(f"worker output path contains a symlink: {path}")
    if not path.exists():
        return
    mode = path.lstat().st_mode
    if stat.S_ISREG(mode):
        return
    if not stat.S_ISDIR(mode):
        raise OSError(f"worker output path is not regular: {path}")
    for current_root, directories, filenames in os.walk(path, followlinks=False):
        current = Path(current_root)
        for name in directories:
            child_mode = (current / name).lstat().st_mode
            if not stat.S_ISDIR(child_mode) or stat.S_ISLNK(child_mode):
                raise OSError(f"worker output directory contains an unsafe entry: {current / name}")
        for name in filenames:
            child_mode = (current / name).lstat().st_mode
            if not stat.S_ISREG(child_mode) or stat.S_ISLNK(child_mode):
                raise OSError(f"worker output directory contains an unsafe entry: {current / name}")


def _remove_worker_owned_output(path: Path, root: Path) -> None:
    _validate_worker_owned_output(path, root)
    if not path.exists():
        return
    if _is_regular_file_no_follow(path):
        path.unlink()
        _fsync_directory(path.parent)
        return
    shutil.rmtree(path)
    _fsync_directory(path.parent)


def _remove_model_output_transaction_path(path: Path, root: Path) -> None:
    resolved_root = root.resolve(strict=False)
    resolved_parent = path.parent.resolve(strict=False)
    if not _path_is_within(resolved_parent, resolved_root) or path.parent == path:
        raise OSError(f"model output transaction path escapes its root: {path}")
    if path_has_symlink_component(path.parent):
        raise OSError(f"model output transaction parent contains a symlink: {path.parent}")
    if path.is_symlink():
        path.unlink()
        _fsync_directory(path.parent)
        return
    _remove_worker_owned_output(path, root)


def cleanup_model_turn_workspace(repo_dir: Path, staging: Path) -> None:
    workspace_root = (repo_dir.parent / MODEL_TURN_WORKSPACES_DIR_NAME).resolve(
        strict=False
    )
    if not _path_is_within(staging.resolve(strict=False), workspace_root):
        raise OSError("model turn cleanup path escapes the staging root")
    if staging.is_symlink():
        staging.unlink()
    elif staging.exists():
        shutil.rmtree(staging)
    parent = staging.parent
    while parent != workspace_root and _path_is_within(
        parent.resolve(strict=False), workspace_root
    ):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def cleanup_model_turn_workspace_after_turn(
    repo_dir: Path,
    staging: Path,
    *,
    run_dir: Path,
    primary_error: BaseException | None,
) -> None:
    try:
        cleanup_model_turn_workspace(repo_dir, staging)
    except OSError as cleanup_error:
        if primary_error is None:
            raise
        note = (
            f"Model turn workspace cleanup failed for {staging}: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
        add_note = getattr(primary_error, "add_note", None)
        try:
            if callable(add_note):
                add_note(note)
            else:
                notes = getattr(primary_error, "__notes__", None)
                if not isinstance(notes, list):
                    notes = []
                    setattr(primary_error, "__notes__", notes)
                notes.append(note)
        except Exception:
            pass
        try:
            append_jsonl(
                run_dir / "worker.log.jsonl",
                {
                    "event": "model_turn_cleanup_failed",
                    "staging_path": str(staging),
                    "error": f"{type(cleanup_error).__name__}: {cleanup_error}",
                    "primary_error": type(primary_error).__name__,
                    "time": iso_time(time.time()),
                },
            )
        except Exception as log_error:
            logging_note = (
                f"Worker log recording for the cleanup failure also failed: "
                f"{type(log_error).__name__}: {log_error}"
            )
            add_note = getattr(primary_error, "add_note", None)
            try:
                if callable(add_note):
                    add_note(logging_note)
                else:
                    notes = getattr(primary_error, "__notes__", None)
                    if isinstance(notes, list):
                        notes.append(logging_note)
            except Exception:
                pass


def prepare_model_turn_workspace(
    repo_dir: Path,
    run_dir: Path,
    purpose: str,
    declared_outputs: tuple[str, ...],
    *,
    include_existing: bool,
) -> Path:
    staging = model_turn_workspace_path(repo_dir, run_dir, purpose)
    existing = (
        _declared_model_output_snapshot(run_dir, declared_outputs)
        if include_existing
        else {}
    )
    if staging.is_symlink():
        staging.unlink()
    elif staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=False)
    for relative, output in existing.items():
        _write_worker_owned_bytes(
            staging.joinpath(*PurePosixPath(relative).parts),
            output.payload,
            executable=output.executable,
        )
    return staging


MODEL_OUTPUT_PUBLICATION_JOURNAL = ".model-output-publication.json"
MODEL_OUTPUT_PUBLICATION_SCHEMA = "model-output-publication/v2"
LEGACY_MODEL_OUTPUT_PUBLICATION_SCHEMA = "model-output-publication/v1"


def _validated_model_output_declarations(
    declared_outputs: tuple[str, ...],
) -> tuple[str, ...]:
    paths: list[PurePosixPath] = []
    normalized: list[str] = []
    for raw_declared in declared_outputs:
        if not isinstance(raw_declared, str):
            raise OSError("model output declaration must be a string")
        declared = raw_declared.strip()
        relative = PurePosixPath(declared)
        if (
            not declared
            or declared != raw_declared
            or declared == MODEL_OUTPUT_PUBLICATION_JOURNAL
            or not relative.parts
            or relative.is_absolute()
            or relative.as_posix() != declared
            or "\\" in declared
            or any(char in declared for char in "\r\n\x00")
            or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
        ):
            raise OSError(f"model output declaration is unsafe: {raw_declared}")
        if any(
            relative == existing
            or relative in existing.parents
            or existing in relative.parents
            for existing in paths
        ):
            raise OSError("model output declarations overlap")
        paths.append(relative)
        normalized.append(relative.as_posix())
    return tuple(normalized)


def _model_output_snapshot_digest(
    relative: str,
    replacement_kind: str,
    snapshot: dict[str, ModelOutputFile],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"model-output-snapshot/v1\x00")
    digest.update(replacement_kind.encode("ascii"))
    digest.update(b"\x00")
    if replacement_kind == "file":
        entries = [("", snapshot.get(relative))]
    elif replacement_kind == "directory":
        prefix = relative.rstrip("/") + "/"
        entries = [
            (path[len(prefix) :], output)
            for path, output in sorted(snapshot.items())
            if path.startswith(prefix)
        ]
    else:
        return digest.hexdigest()
    for nested_path, output in entries:
        if output is None:
            raise OSError(f"declared model output snapshot is missing: {relative}")
        path_bytes = nested_path.encode("utf-8")
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(b"\x01" if output.executable else b"\x00")
        digest.update(len(output.payload).to_bytes(8, "big"))
        digest.update(output.payload)
    return digest.hexdigest()


def _model_output_publication_path(run_dir: Path, raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise OSError("model output publication journal contains an invalid path")
    relative = PurePosixPath(raw_path)
    if (
        not relative.parts
        or relative.is_absolute()
        or relative.as_posix() != raw_path
        or "\\" in raw_path
        or any(part in {"", ".", ".."} or ":" in part for part in relative.parts)
    ):
        raise OSError("model output publication journal contains an unsafe path")
    candidate = run_dir.joinpath(*relative.parts)
    if not _path_is_within(candidate.resolve(strict=False), run_dir.resolve(strict=False)):
        raise OSError("model output publication journal path escapes the run directory")
    return candidate


def _write_model_output_publication_journal(
    run_dir: Path,
    payload: dict[str, Any],
) -> None:
    _write_worker_owned_bytes(
        run_dir / MODEL_OUTPUT_PUBLICATION_JOURNAL,
        (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )


def _durable_model_output_replace(source: Path, destination: Path) -> None:
    if source.parent != destination.parent:
        raise OSError("model output transaction rename crosses directories")
    os.replace(source, destination)
    _fsync_directory(destination.parent)


def _parse_model_output_publication_journal(
    run_dir: Path,
    journal: object,
) -> tuple[str, str, list[dict[str, Any]]]:
    if not isinstance(journal, dict):
        raise OSError("model output publication journal is not an object")
    schema = journal.get("schema_version")
    if schema not in {
        MODEL_OUTPUT_PUBLICATION_SCHEMA,
        LEGACY_MODEL_OUTPUT_PUBLICATION_SCHEMA,
    }:
        raise OSError("model output publication journal has an unsupported schema")
    state = journal.get("state")
    if state not in {"preparing", "committing", "committed"} or (
        schema == LEGACY_MODEL_OUTPUT_PUBLICATION_SCHEMA and state == "preparing"
    ):
        raise OSError("model output publication journal has an invalid state")
    raw_items = journal.get("items")
    if not isinstance(raw_items, list):
        raise OSError("model output publication journal has invalid items")
    raw_destinations = tuple(
        str(item.get("destination") or "") if isinstance(item, dict) else ""
        for item in raw_items
    )
    _validated_model_output_declarations(raw_destinations)

    items: list[dict[str, Any]] = []
    transaction_paths: set[Path] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise OSError("model output publication journal contains an invalid item")
        destination = _model_output_publication_path(run_dir, raw_item.get("destination"))
        prepared = _model_output_publication_path(run_dir, raw_item.get("prepared"))
        backup = _model_output_publication_path(run_dir, raw_item.get("backup"))
        had_destination = raw_item.get("had_destination")
        has_replacement = raw_item.get("has_replacement")
        if not isinstance(had_destination, bool) or not isinstance(has_replacement, bool):
            raise OSError("model output publication journal contains invalid flags")
        if prepared.parent != destination.parent or backup.parent != destination.parent:
            raise OSError("model output publication journal paths have different parents")
        if not prepared.name.startswith(f".{destination.name}.") or not prepared.name.endswith(".publish"):
            raise OSError("model output publication journal contains an invalid prepared path")
        if not backup.name.startswith(f".{destination.name}.") or not backup.name.endswith(".backup"):
            raise OSError("model output publication journal contains an invalid backup path")
        if prepared in transaction_paths or backup in transaction_paths:
            raise OSError("model output publication journal reuses a transaction path")
        transaction_paths.update((prepared, backup))
        replacement_kind = raw_item.get("replacement_kind")
        expected_digest = raw_item.get("expected_digest")
        if schema == MODEL_OUTPUT_PUBLICATION_SCHEMA:
            if replacement_kind not in {"absent", "file", "directory"}:
                raise OSError("model output publication journal contains an invalid replacement kind")
            if has_replacement != (replacement_kind != "absent"):
                raise OSError("model output publication journal replacement flags conflict")
            if replacement_kind == "absent":
                if expected_digest not in {None, ""}:
                    raise OSError("absent model output publication item has a digest")
            elif not isinstance(expected_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
                raise OSError("model output publication journal contains an invalid digest")
        items.append(
            {
                **raw_item,
                "destination_path": destination,
                "prepared_path": prepared,
                "backup_path": backup,
            }
        )
    destination_paths = {item["destination_path"] for item in items}
    if destination_paths.intersection(transaction_paths):
        raise OSError("model output publication journal aliases a destination path")
    return str(schema), str(state), items


def _rollback_model_output_publication(
    run_dir: Path,
    items: list[dict[str, Any]],
) -> None:
    for item in reversed(items):
        destination = item["destination_path"]
        backup = item["backup_path"]
        backup_exists = backup.exists() or backup.is_symlink()
        destination_exists = destination.exists() or destination.is_symlink()
        if backup_exists:
            if backup.is_symlink():
                raise OSError(f"model output publication backup is a symlink: {backup}")
            _validate_worker_owned_output(backup, run_dir)
            if destination_exists:
                _remove_model_output_transaction_path(destination, run_dir)
            try:
                _durable_model_output_replace(backup, destination)
            except OSError as exc:
                raise OSError(
                    f"could not restore model output publication backup {backup} "
                    f"to {destination}: {exc}"
                ) from exc
        elif item["had_destination"]:
            if not destination_exists:
                raise OSError("model output publication lost its previous destination")
            _validate_worker_owned_output(destination, run_dir)
        elif destination_exists:
            _remove_model_output_transaction_path(destination, run_dir)
    for item in items:
        prepared = item["prepared_path"]
        backup = item["backup_path"]
        if prepared.exists() or prepared.is_symlink():
            _remove_model_output_transaction_path(prepared, run_dir)
        if backup.exists() or backup.is_symlink():
            raise OSError(f"model output publication backup was not restored: {backup}")
    _remove_worker_owned_output(run_dir / MODEL_OUTPUT_PUBLICATION_JOURNAL, run_dir)


def _model_output_destination_error(
    run_dir: Path,
    item: dict[str, Any],
) -> str:
    destination = item["destination_path"]
    replacement_kind = str(item.get("replacement_kind") or "")
    destination_exists = destination.exists() or destination.is_symlink()
    if replacement_kind == "absent":
        return "destination still exists" if destination_exists else ""
    if not destination_exists:
        return "destination is missing"
    if destination.is_symlink():
        return "destination is a symlink"
    try:
        _validate_worker_owned_output(destination, run_dir)
    except OSError as exc:
        return str(exc)
    if replacement_kind == "file" and not _is_regular_file_no_follow(destination):
        return "destination is not a regular file"
    if replacement_kind == "directory" and not destination.is_dir():
        return "destination is not a directory"
    try:
        snapshot = _declared_model_output_snapshot(
            run_dir,
            (str(item["destination"]),),
        )
        actual_digest = _model_output_snapshot_digest(
            str(item["destination"]),
            replacement_kind,
            snapshot,
        )
    except OSError as exc:
        return str(exc)
    if actual_digest != item.get("expected_digest"):
        return "destination digest differs from the committed snapshot"
    return ""


def _committed_publication_can_rollback(
    run_dir: Path,
    items: list[dict[str, Any]],
) -> tuple[bool, str]:
    for item in items:
        if not item["had_destination"]:
            continue
        backup = item["backup_path"]
        if not (backup.exists() or backup.is_symlink()):
            return False, f"previous destination backup is missing: {backup}"
        if backup.is_symlink():
            return False, f"previous destination backup is a symlink: {backup}"
        try:
            _validate_worker_owned_output(backup, run_dir)
        except OSError as exc:
            return False, str(exc)
    return True, ""


def _recover_model_output_publication(run_dir: Path) -> None:
    journal_path = run_dir / MODEL_OUTPUT_PUBLICATION_JOURNAL
    if not journal_path.exists() and not journal_path.is_symlink():
        return
    if journal_path.is_symlink():
        raise OSError("model output publication journal is a symlink")
    try:
        journal = json.loads(_bounded_regular_model_file(journal_path).payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OSError(f"model output publication journal is invalid: {exc}") from exc
    schema, state, items = _parse_model_output_publication_journal(run_dir, journal)

    if state == "preparing":
        for item in items:
            backup = item["backup_path"]
            if backup.exists() or backup.is_symlink():
                raise OSError("preparing model output publication unexpectedly has a backup")
        for item in items:
            prepared = item["prepared_path"]
            if prepared.exists() or prepared.is_symlink():
                _remove_model_output_transaction_path(prepared, run_dir)
        _remove_worker_owned_output(journal_path, run_dir)
        return

    if state == "committing":
        _rollback_model_output_publication(run_dir, items)
        return

    if schema != MODEL_OUTPUT_PUBLICATION_SCHEMA:
        raise OSError("legacy committed model output publication requires operator recovery")
    validation_errors = [
        f"{item['destination']}: {error}"
        for item in items
        if (error := _model_output_destination_error(run_dir, item))
    ]
    if validation_errors:
        can_rollback, rollback_reason = _committed_publication_can_rollback(run_dir, items)
        if not can_rollback:
            raise OSError(
                "committed model output publication is invalid and cannot be rolled back: "
                + "; ".join(validation_errors + [rollback_reason])
            )
        rollback_journal = {**journal, "state": "committing"}
        _write_model_output_publication_journal(run_dir, rollback_journal)
        _rollback_model_output_publication(run_dir, items)
        return

    for item in items:
        prepared = item["prepared_path"]
        backup = item["backup_path"]
        if prepared.exists() or prepared.is_symlink():
            _remove_model_output_transaction_path(prepared, run_dir)
        if backup.exists() or backup.is_symlink():
            _remove_model_output_transaction_path(backup, run_dir)
    _remove_worker_owned_output(journal_path, run_dir)


def _plan_model_output_publication_item(
    staging: Path,
    run_dir: Path,
    declared: str,
    snapshot: dict[str, ModelOutputFile],
    transaction_id: str,
) -> dict[str, Any]:
    relative = PurePosixPath(declared)
    source = staging.joinpath(*relative.parts)
    destination = run_dir.joinpath(*relative.parts)
    if not _path_is_within(destination.resolve(strict=False), run_dir.resolve(strict=False)):
        raise OSError(f"model output destination escapes the run directory: {destination}")
    if path_has_symlink_component(destination.parent):
        raise OSError(f"model output destination parent contains a symlink: {destination.parent}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination_exists = destination.exists() or destination.is_symlink()
    if destination.is_symlink():
        raise OSError(f"model output destination is a symlink: {destination}")
    if destination_exists:
        _validate_worker_owned_output(destination, run_dir)
    if not source.exists() and not source.is_symlink():
        replacement_kind = "absent"
    elif path_has_symlink_component(source):
        raise OSError(f"declared model output contains a symlink: {source}")
    elif source.is_file():
        replacement_kind = "file"
    elif source.is_dir():
        replacement_kind = "directory"
    else:
        raise OSError(f"declared model output is not regular: {source}")
    prepared = destination.with_name(f".{destination.name}.{transaction_id}.publish")
    backup = destination.with_name(f".{destination.name}.{transaction_id}.backup")
    if prepared.exists() or prepared.is_symlink() or backup.exists() or backup.is_symlink():
        raise OSError(f"model output publication transaction path already exists: {destination}")
    expected_digest = (
        _model_output_snapshot_digest(declared, replacement_kind, snapshot)
        if replacement_kind != "absent"
        else None
    )
    return {
        "destination": declared,
        "prepared": prepared.relative_to(run_dir).as_posix(),
        "backup": backup.relative_to(run_dir).as_posix(),
        "had_destination": destination_exists,
        "has_replacement": replacement_kind != "absent",
        "replacement_kind": replacement_kind,
        "expected_digest": expected_digest,
    }


def _materialize_prepared_model_output(
    run_dir: Path,
    item: dict[str, Any],
    snapshot: dict[str, ModelOutputFile],
) -> None:
    replacement_kind = str(item["replacement_kind"])
    if replacement_kind == "absent":
        return
    relative = str(item["destination"])
    prepared = _model_output_publication_path(run_dir, item["prepared"])
    try:
        if replacement_kind == "file":
            output = snapshot.get(relative)
            if output is None:
                raise OSError(f"declared model output snapshot is missing: {relative}")
            _write_worker_owned_bytes(
                prepared,
                output.payload,
                executable=output.executable,
            )
        else:
            prepared.mkdir(parents=False, exist_ok=False)
            prefix = relative.rstrip("/") + "/"
            for snapshot_path, output in snapshot.items():
                if not snapshot_path.startswith(prefix):
                    continue
                nested = PurePosixPath(snapshot_path[len(prefix) :])
                _write_worker_owned_bytes(
                    prepared.joinpath(*nested.parts),
                    output.payload,
                    executable=output.executable,
                )
            _fsync_directory_tree(prepared)
    except BaseException:
        if prepared.exists() or prepared.is_symlink():
            _remove_model_output_transaction_path(prepared, run_dir)
        raise


def publish_model_turn_outputs(
    staging: Path,
    run_dir: Path,
    declared_outputs: tuple[str, ...],
) -> list[str]:
    _recover_model_output_publication(run_dir)
    declarations = _validated_model_output_declarations(declared_outputs)
    if not declarations:
        return []
    snapshot = _declared_model_output_snapshot(staging, declarations)
    transaction_id = f"{os.getpid()}.{threading.get_ident()}.{time.time_ns()}"
    transaction_items = [
        _plan_model_output_publication_item(
            staging,
            run_dir,
            declared,
            snapshot,
            transaction_id,
        )
        for declared in declarations
    ]
    for item in transaction_items:
        destination = _model_output_publication_path(run_dir, item["destination"])
        _fsync_directory_chain(destination.parent, run_dir)
    journal: dict[str, Any] = {
        "schema_version": MODEL_OUTPUT_PUBLICATION_SCHEMA,
        "state": "preparing",
        "items": transaction_items,
    }
    try:
        _write_model_output_publication_journal(run_dir, journal)
        for item in transaction_items:
            _materialize_prepared_model_output(run_dir, item, snapshot)
        journal["state"] = "committing"
        _write_model_output_publication_journal(run_dir, journal)
        _schema, _state, parsed_items = _parse_model_output_publication_journal(
            run_dir,
            journal,
        )
        for item in parsed_items:
            if item["had_destination"]:
                _durable_model_output_replace(
                    item["destination_path"],
                    item["backup_path"],
                )
        for item in parsed_items:
            if item["has_replacement"]:
                _durable_model_output_replace(
                    item["prepared_path"],
                    item["destination_path"],
                )
        validation_errors = [
            f"{item['destination']}: {error}"
            for item in parsed_items
            if (error := _model_output_destination_error(run_dir, item))
        ]
        if validation_errors:
            raise OSError(
                "model output publication validation failed: "
                + "; ".join(validation_errors)
            )
        journal["state"] = "committed"
        _write_model_output_publication_journal(run_dir, journal)
    except BaseException as publication_error:
        if journal.get("state") == "committed":
            raise
        try:
            _recover_model_output_publication(run_dir)
        except BaseException as rollback_error:
            raise OSError(
                f"model output publication failed and rollback could not complete: {rollback_error}"
            ) from publication_error
        raise
    _recover_model_output_publication(run_dir)
    return sorted(snapshot)


def iso_time(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def model_for_job(job: dict[str, Any]) -> str:
    return str(validate_job_policy(job)["model"])


def core_effort_for_job(job: dict[str, Any]) -> str:
    return str(validate_job_policy(job)["reasoning_effort"])


def review_worker_policy_for_job(job: dict[str, Any]) -> dict[str, int]:
    return dict(validate_job_policy(job)["review_worker"])


def reviewer_concurrency_for_job(job: dict[str, Any]) -> int:
    return int(review_worker_policy_for_job(job)["reviewerConcurrency"])


def reviewer_concurrency_decision_for_job(
    job: dict[str, Any],
) -> tuple[int, int, str, dict[str, Any]]:
    configured = reviewer_concurrency_for_job(job)
    memory = worker_memory_payload()

    def metric(name: str) -> int | None:
        try:
            value = int(memory.get(name))
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    total_bytes = metric("totalBytes")
    available_bytes = metric("availableBytes")
    reason = ""
    effective = configured
    if configured > 1 and total_bytes is not None and total_bytes <= LOW_MEMORY_REVIEWER_TOTAL_BYTES:
        effective = 1
        reason = "low_memory_host"
    elif (
        configured > 1
        and available_bytes is not None
        and available_bytes <= LOW_MEMORY_REVIEWER_AVAILABLE_BYTES
    ):
        effective = 1
        reason = "low_available_memory"
    return configured, effective, reason, memory


def review_plan_resource_counts(plan: object) -> tuple[int, int]:
    bundles = (
        plan.get("bundles")
        if isinstance(plan, dict) and isinstance(plan.get("bundles"), list)
        else []
    )
    bundle_count = sum(1 for bundle in bundles if isinstance(bundle, dict))
    assignment_count = 0
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        reviewers = bundle.get("reviewers")
        if not isinstance(reviewers, list):
            continue
        assignment_count += len(
            {
                str(reviewer or "").strip().lower()
                for reviewer in reviewers
                if str(reviewer or "").strip()
            }
        )
    return bundle_count, assignment_count


def enforce_review_plan_resource_limits(
    plan: object,
    job: dict[str, Any],
) -> tuple[int, int]:
    policy = review_worker_policy_for_job(job)
    max_bundles = int(policy["maxBundles"])
    max_assignments = int(policy["maxReviewerAssignments"])
    bundle_count, assignment_count = review_plan_resource_counts(plan)
    if bundle_count > max_bundles or assignment_count > max_assignments:
        raise RuntimeError(
            "REVIEW_PLAN_LIMIT_EXCEEDED: "
            f"bundles={bundle_count}, max_bundles={max_bundles}; "
            f"reviewer_assignments={assignment_count}, "
            f"max_reviewer_assignments={max_assignments}"
        )
    return bundle_count, assignment_count


def turn_timeout_for_job(job: dict[str, Any]) -> int:
    return int(review_worker_policy_for_job(job)["turnTimeoutSeconds"])


def effort_for_phase(job: dict[str, Any], phase: str) -> str:
    policy = validate_job_policy(job)
    if phase not in CORE_EFFORT_PHASES:
        return str(policy.get("non_core_effort") or "medium")
    phase_efforts = policy.get("phase_efforts") if isinstance(policy.get("phase_efforts"), dict) else {}
    if phase in {"validator_disproof", "intent_test_failure_analysis"}:
        return str(phase_efforts.get("validator") or policy["reasoning_effort"])
    if phase == "final_report_json":
        return str(phase_efforts.get("reporter") or policy["reasoning_effort"])
    if phase.startswith("intent_"):
        return str(phase_efforts.get("intent_test") or policy["reasoning_effort"])
    return str(phase_efforts.get("reviewer") or policy["reasoning_effort"])


def _job_agent_config(job: dict[str, Any]) -> dict[str, Any]:
    return job.get("agentConfig") if isinstance(job.get("agentConfig"), dict) else {}


def _job_model_profile(job: dict[str, Any]) -> dict[str, Any]:
    return job.get("model_profile") if isinstance(job.get("model_profile"), dict) else {}


def _job_review_request(job: dict[str, Any]) -> dict[str, Any]:
    return job.get("review_request") if isinstance(job.get("review_request"), dict) else {}


def output_language_for_job(job: dict[str, Any] | None) -> str:
    source = job if isinstance(job, dict) else {}
    request = _job_review_request(source)
    language = str(
        request.get("output_language")
        or source.get("review_output_language")
        or source.get("reviewOutputLanguage")
        or "en"
    ).strip()
    return language if re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?", language) else "en"


def output_language_prompt_lines(job: dict[str, Any] | None) -> list[str]:
    language = output_language_for_job(job)
    if language == "en":
        return [
            "Output language: en.",
            "Write every natural-language title, explanation, evidence summary, recommendation, and report passage in English.",
            "Keep JSON keys, schema identifiers, code, paths, commands, and quoted source text unchanged.",
        ]
    if language == "zh-CN":
        instruction = "所有自然语言标题、说明、证据摘要、建议和报告正文都必须使用简体中文。"
    else:
        instruction = f"Write every natural-language title, explanation, evidence summary, recommendation, and report passage in {language}."
    return [
        f"Output language: {language}.",
        instruction,
        "Keep JSON keys, schema identifiers, code, paths, commands, and quoted source text unchanged.",
    ]


def _job_review_policy(job: dict[str, Any]) -> dict[str, Any]:
    request = _job_review_request(job)
    return request.get("policy") if isinstance(request.get("policy"), dict) else {}


def _job_review_budget(job: dict[str, Any]) -> dict[str, Any]:
    request = _job_review_request(job)
    return request.get("budget") if isinstance(request.get("budget"), dict) else {}


def _clean_effort(value: object, *, field: str) -> str:
    effort = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", effort) is None:
        raise ValueError(f"claimed job must include valid {field}")
    return effort


def _policy_int(source: dict[str, Any], *keys: str, default: int | None = None) -> int:
    for key in keys:
        if source.get(key) is not None:
            try:
                return int(source.get(key))
            except (TypeError, ValueError):
                break
    if default is not None:
        return default
    raise ValueError(f"claimed job policy is missing {keys[0]}")


def repository_limits_for_job(job: dict[str, Any]) -> dict[str, int] | None:
    limits = job.get("repositoryLimits") if isinstance(job.get("repositoryLimits"), dict) else None
    if limits is None:
        return None
    try:
        max_files = int(limits.get("maxFiles"))
        max_bytes = int(limits.get("maxBytes"))
    except (TypeError, ValueError):
        raise ValueError("claimed job must include repositoryLimits.maxFiles and repositoryLimits.maxBytes") from None
    if max_files <= 0 or max_bytes <= 0:
        raise ValueError("repositoryLimits.maxFiles and repositoryLimits.maxBytes must be positive")
    return {"maxFiles": max_files, "maxBytes": max_bytes}


def _validate_restrictive_review_policy(policy: dict[str, Any]) -> None:
    if not policy:
        return
    if policy.get("allow_source_modification") is not False:
        raise ValueError("review_request.policy.allow_source_modification must be false")
    if policy.get("allow_dependency_install") is not False:
        raise ValueError("review_request.policy.allow_dependency_install must be false")
    if policy.get("allow_network") is not False:
        raise ValueError("review_request.policy.allow_network must be false")
    if policy.get("helper_scripts_standard_library_only") is not True:
        raise ValueError("review_request.policy.helper_scripts_standard_library_only must be true")


def intent_validation_policy_for_job(job: dict[str, Any]) -> dict[str, Any]:
    policy = _job_review_policy(job)
    canonical = policy.get("intent_test_validation") if isinstance(policy.get("intent_test_validation"), dict) else {}
    max_preflight_repair_attempts = _policy_int(
        canonical,
        "max_preflight_repair_attempts",
        "maxPreflightRepairAttempts",
        default=1,
    )
    max_runtime_repair_attempts = _policy_int(
        canonical,
        "max_runtime_repair_attempts",
        "maxRuntimeRepairAttempts",
        default=1,
    )
    for field, value in (
        ("max_preflight_repair_attempts", max_preflight_repair_attempts),
        ("max_runtime_repair_attempts", max_runtime_repair_attempts),
    ):
        if value < 0 or value > 3:
            raise ValueError(f"review_request.policy.intent_test_validation.{field} must be between 0 and 3")
    return {
        "enabled": canonical.get("enabled", True) is not False,
        "only_tiers": canonical.get("only_tiers") or canonical.get("onlyTiers") or ["P0", "P1"],
        "max_tests_per_run": _policy_int(canonical, "max_tests_per_run", "maxTestsPerRun", default=20),
        "max_tests_per_bundle": _policy_int(canonical, "max_tests_per_bundle", "maxTestsPerBundle", default=2),
        "max_test_run_seconds_per_test": _policy_int(
            canonical,
            "max_test_run_seconds_per_test",
            "maxTestRunSecondsPerTest",
            default=60,
        ),
        "max_total_test_run_seconds": _policy_int(
            canonical,
            "max_total_test_run_seconds",
            "maxTotalTestRunSeconds",
            default=900,
        ),
        "max_preflight_repair_attempts": max_preflight_repair_attempts,
        "max_runtime_repair_attempts": max_runtime_repair_attempts,
    }


def validate_job_policy(job: dict[str, Any]) -> dict[str, Any]:
    agent = _job_agent_config(job)
    provider = str(agent.get("provider") or "").strip().lower()
    if provider and provider != "codex":
        raise ValueError("agentConfig.provider must be codex")
    model_profile = _job_model_profile(job)
    model = str(model_profile.get("default_model") or "").strip()
    if not model:
        raise ValueError("claimed job must include model_profile.default_model")
    effort = _clean_effort(
        model_profile.get("core_effort") or model_profile.get("reviewer_effort"),
        field="model_profile.core_effort",
    )
    non_core_effort = _clean_effort(model_profile.get("non_core_effort") or "medium", field="model_profile.non_core_effort")
    review_policy = _job_review_policy(job)
    review_budget = _job_review_budget(job)
    _validate_restrictive_review_policy(review_policy)
    try:
        turn_timeout_seconds = _policy_int(review_policy, "turn_timeout_seconds", "turnTimeoutSeconds", default=None)
    except (TypeError, ValueError):
        raise ValueError("claimed job must include review_request.policy.turn_timeout_seconds") from None
    reviewer_concurrency = _policy_int(
        review_policy,
        "reviewer_concurrency",
        "reviewerConcurrency",
        default=1,
    )
    try:
        max_bundles = _policy_int(
            review_policy,
            "max_bundles",
            "maxBundles",
            default=None,
        )
    except (TypeError, ValueError):
        raise ValueError("claimed job must include review_request.policy.max_bundles") from None
    try:
        max_reviewer_assignments = _policy_int(
            review_policy,
            "max_reviewer_assignments",
            "maxReviewerAssignments",
            default=None,
        )
    except (TypeError, ValueError):
        raise ValueError(
            "claimed job must include review_request.policy.max_reviewer_assignments"
        ) from None
    try:
        scan_deadline_seconds = _policy_int(review_budget, "max_wall_time_seconds", "maxWallTimeSeconds", default=None)
    except (TypeError, ValueError):
        raise ValueError("claimed job must include review_request.budget.max_wall_time_seconds") from None
    if turn_timeout_seconds <= 0 or scan_deadline_seconds <= 0:
        raise ValueError("review worker turn timeout and scan deadline must be positive")
    if reviewer_concurrency < 1 or reviewer_concurrency > MAX_REVIEWER_CONCURRENCY:
        raise ValueError(
            f"review_request.policy.reviewer_concurrency must be between 1 and {MAX_REVIEWER_CONCURRENCY}"
        )
    if max_bundles < 1 or max_bundles > MAX_REVIEW_BUNDLES:
        raise ValueError(
            f"review_request.policy.max_bundles must be between 1 and {MAX_REVIEW_BUNDLES}"
        )
    if (
        max_reviewer_assignments < 1
        or max_reviewer_assignments > MAX_REVIEWER_ASSIGNMENTS
    ):
        raise ValueError(
            "review_request.policy.max_reviewer_assignments must be between "
            f"1 and {MAX_REVIEWER_ASSIGNMENTS}"
        )
    limits = repository_limits_for_job(job)
    if limits is None:
        raise ValueError("claimed job must include repositoryLimits.maxFiles and repositoryLimits.maxBytes")
    return {
        "model": model,
        "reasoning_effort": effort,
        "non_core_effort": non_core_effort,
        "phase_efforts": {
            "reviewer": _clean_effort(model_profile.get("reviewer_effort") or effort, field="model_profile.reviewer_effort"),
            "validator": _clean_effort(model_profile.get("validator_effort") or effort, field="model_profile.validator_effort"),
            "reporter": _clean_effort(model_profile.get("reporter_effort") or effort, field="model_profile.reporter_effort"),
            "intent_test": _clean_effort(model_profile.get("intent_test_effort") or effort, field="model_profile.intent_test_effort"),
        },
        "review_worker": {
            "turnTimeoutSeconds": turn_timeout_seconds,
            "scanDeadlineSeconds": scan_deadline_seconds,
            "reviewerConcurrency": reviewer_concurrency,
            "maxBundles": max_bundles,
            "maxReviewerAssignments": max_reviewer_assignments,
        },
        "repository_limits": limits,
        "intent_test_validation": intent_validation_policy_for_job(job),
    }


SEMANTIC_PHASE_PROMPT_SPECS: dict[str, dict[str, Any]] = {
    "repo_map": {
        "role": "Repo Mapper",
        "prompt_files": ["00_repo_mapper.md"],
        "inputs": ["inventory.json", "README/docs/manifest files", "AGENTS"],
        "outputs": ["repo-map.json"],
        "instructions": [
            "Identify languages, frameworks, entrypoints, trust boundaries, critical areas, data flows, and test strategy.",
            "Do not report bugs in this phase.",
            "Write JSON only using repo-map/v1.",
        ],
    },
    "risk_routing": {
        "role": "Risk Router",
        "prompt_files": ["01_risk_router.md"],
        "inputs": ["repo-map.json", "inventory.json"],
        "outputs": ["risk-routing.json"],
        "instructions": [
            "Classify files and directories into P0/P1/P2/P3/SKIP using role, entrypoint, trust boundary, auth/payment/data/upload/config/concurrency signals.",
            "Cover every non-hard-skipped inventory path with an explicit route or provide an intentional default_depth in P0/P1/P2/P3/SKIP for all unmatched paths.",
            "The Worker will not infer an unmatched tier from path names, repository profile, file suffixes, or risk hints; make the route set or default complete.",
            "Do not report findings in this phase.",
            "Write JSON only using risk-routing/v1.",
        ],
    },
    "bundle_planning": {
        "role": "Semantic Bundle Planner",
        "prompt_files": ["02_bundle_planner.md"],
        "inputs": [
            "bundle-planning-input.json",
            "repo-map.json",
            "risk-routing.json",
            "effective-risk-routing.json",
        ],
        "outputs": ["bundle-grouping.json"],
        "instructions": [
            "Write bundle-grouping.json using schema_version bundle-grouping/v1.",
            "Every group must include a stable lowercase group_id, a non-empty title, a non-empty grouping_reasons list, its P0/P1/P2 tier, and a non-empty paths list.",
            "Place every item from bundle-planning-input.json in exactly one group; do not omit or duplicate paths.",
            "Keep every group within one tier and copy each path's P0/P1/P2 tier exactly.",
            "Treat every output group as an agent-owned semantic bundle boundary: the Worker will not merge separate groups.",
            "Honor constraints.max_bundles and constraints.max_reviewer_assignments while preserving exact coverage. Reviewer assignment cost per bundle is P0=3, P1=2, P2=1, so minimize the weighted total as well as the bundle count.",
            "Minimize the number of groups while preserving semantic cohesion by feature, entrypoint, trust boundary, state flow, and implementation/test affinity; prefer a smaller coherent group set over mechanically fragmented groups.",
            "Avoid singleton or tiny groups unless the path is genuinely isolated or combining it would materially reduce semantic coherence.",
            "Use estimated_tokens and the supplied constraints to make groups substantial; the Worker may split only when the rendered payload exceeds a hard safety boundary.",
            "If the limits cannot be met safely, still return the most compact valid exact-cover grouping; never omit a path, change a tier, or reduce reviewer coverage to force the counts under a limit. The Worker will reject an impossible rendered plan before fanout.",
            "Use only repository evidence exposed by the inputs and paths; do not build dependency graphs or call graphs.",
            "Do not assign reviewers, file ranges, final bundle ids, token estimates, or final size limits; group_id is only the required stable semantic-group identifier, while the Worker derives and enforces final bundle fields.",
        ],
    },
    "reviewer_fanout": {
        "role": "Bounded Parallel Logical Reviewer Fanout",
        "prompt_files": [
            "reviewers/security.md",
            "reviewers/correctness.md",
            "reviewers/test_gap.md",
            "reviewers/correctness_lite.md",
        ],
        "inputs": ["bundles/*.md", "repo-map.json", "risk-routing.json", "reviewer prompts"],
        "outputs": ["raw-reviewers/*.json"],
        "instructions": [
            "Review each planned bundle/reviewer assignment on its own independent thread; the worker may run up to the server-provided bounded concurrency while preserving the deterministic plan order for scheduling.",
            "Every finding must be concrete, located, evidenced, actionable, and include false-positive risk.",
            REVIEWER_CONFIDENCE_PROMPT_CONTRACT,
            "For security severity, demonstrate an end-to-end attacker-controlled path to the sink and account for producer-side validation, generated server values, and browser/process isolation. If controllability is unproven, label the issue defense-in-depth and do not rate it high or critical.",
            "Before reporting an async UI race or duplicate mutation, inspect disabled state, synchronous ref/lock guards, event ordering, and server idempotency; demonstrate that a second user action reaches a harmful non-idempotent operation.",
            "Write JSON only using codex-reviewer-output/v1.",
        ],
    },
    "clustering_and_voting": {
        "role": "Finding Clusterer and Vote Aggregator",
        "prompt_files": ["03_clusterer.md"],
        "inputs": ["verified-reviewers/*.json", "location-verification.json"],
        "outputs": ["clusters.json", "validation-input.json"],
        "instructions": [
            'If every verified reviewer findings array is empty, immediately write canonical clusters.json as {"schema_version": "cluster-output/v1", "clusters": []} and validation-input.json as {"schema_version": "validation-input/v1", "candidates": []}. Do not inspect or rescan application source in this empty-input case.',
            "Merge duplicate findings, preserve supporting agents, compute weighted confidence, and suppress vague findings.",
            "Merge test-gap evidence into the underlying defect when it covers the same contract, sink, and fix; do not inflate the main finding count with a duplicate test-gap cluster.",
            "Do not inspect source code and do not create new findings.",
            "Write JSON only using cluster-output/v1 and validation-input/v1 compatible fields.",
        ],
    },
    "intent_mining": {
        "role": "Intent Miner",
        "prompt_files": ["intent/04_intent_miner.md"],
        "inputs": ["repo-map.json", "clusters.json", "selected bundle sources", "docs/tests/API contracts/types"],
        "outputs": ["intent/intent-map.json"],
        "instructions": [
            "Extract behavioral contracts from docs, tests, API specs, types, comments, route definitions, and error messages.",
            "Do not infer intent only from implementation code.",
            'Write JSON only using intent-map/v1 with a top-level "bundle_id" and a top-level "behavioral_contracts" array, even when the array is empty.',
        ],
    },
    "intent_test_planning": {
        "role": "Intent Test Planner",
        "prompt_files": ["intent/05_intent_test_planner.md"],
        "inputs": ["clusters.json", "intent/intent-map.json", "validation-input.json", "intent/execution-capabilities.json"],
        "outputs": ["intent/intent-test-plan.json"],
        "instructions": [
            "Select only high-value P0/P1 candidate findings for temporary tests.",
            "Every test target must link to finding IDs and behavioral contract IDs.",
            "For each target, propose one or more execution_candidates with command and cwd. These are Agent hypotheses for the Worker to verify, not selections from a fixed framework template list.",
            "Prefer faithful execution of repository code over dependency-free imitation. Include alternate candidates when different available runtimes can preserve the same oracle.",
            'Write JSON only using intent-test-plan/v1 with a top-level "test_targets" array. Each target must include test_id, title, linked_finding_ids, contract_ids, and expected_result_before_fix set to fail, pass, or unknown.',
        ],
    },
    "intent_test_writing": {
        "role": "Intent Test Writer",
        "prompt_files": ["intent/06_intent_test_writer.md"],
        "inputs": ["intent/intent-test-plan.json", "intent/execution-capabilities.json", "target snippets", "existing tests", "disposable validation workspace"],
        "outputs": ["intent/intent-test-source.json", "intent/generated-tests/**"],
        "instructions": [
            "Write generated test source only under intent/generated-tests/** in the writable phase output directory; the Worker owns validation-workspace materialization and execution.",
            "Return JSON only using intent-test-source/v1. Put every executable test record in the top-level generated_tests array, not only in aliases such as generated_test_files, created_test_files, or test_sources.",
            "Every generated test record must include path, command, and target_test_ids linking it to the intent-test-plan target(s) it implements.",
            "Use the observed capability and candidate preflight data, but remain free to propose a different safe command or test approach when it preserves the behavioral oracle and executes real repository code.",
            "An unchanged repository test may be reused only by setting reuse_existing to true; never present an application/source file as a generated test.",
            "If no faithful runnable approach exists, record an explicit top-level skip_reason instead of copying or reimplementing application logic in a self-contained imitation.",
            "Verify each expected outcome against AGENTS instructions, documentation, types, API contracts, and existing tests. If the intended behavior remains uncertain, do not encode it as an asserted oracle.",
            "For Python unittest entry points, do not leave imported TestCase subclasses at module scope where unittest.main() can discover unrelated repository suites; import the module under an alias or explicitly load only the generated test class or method.",
            "Prefer a repository-native runner when it is runnable, but use any observed runtime or contained Agent-created harness that faithfully executes the real code and oracle. Do not install missing tooling.",
            "Do not modify the main repo workspace, install dependencies, use production secrets, or use network.",
        ],
    },
    "intent_test_failure_analysis": {
        "role": "Test Failure Analyzer",
        "prompt_files": ["intent/07_intent_test_failure_analyzer.md"],
        "inputs": ["intent/intent-test-results.raw.json", "generated tests", "linked findings"],
        "outputs": ["intent/intent-test-results.json"],
        "instructions": [
            "Classify each generated test result as confirmed_bug, plausible_bug, test_oracle_wrong, test_harness_error, environment_error, flaky_or_nondeterministic, dependency_missing, unclear_requirement, passed_no_bug_reproduced, or skipped_not_runnable.",
            "A failing generated test is not automatically a bug.",
            'Write JSON only using intent-test-result/v1 with a top-level "test_results" array. Every result must include test_id, status, classification, confidence in 0..1, evidence, and artifacts; status must be one of "passed", "failed", "skipped", "timeout", or "error".',
        ],
    },
    "validator_disproof": {
        "role": "Validation Reviewer",
        "prompt_files": ["08_validator.md"],
        "inputs": ["clusters.json", "location-verification.json", "intent/intent-test-results.json", "related snippets"],
        "outputs": ["validated-findings.json"],
        "instructions": [
            'If the validation input contains no candidates, immediately write canonical validated-findings.json as {"schema_version": "validation-output/v1", "validated_findings": [], "weak_findings": [], "disproven_findings": []}. Do not inspect or rescan application source in this empty-input case.',
            "Try to disprove each candidate finding using reviewer evidence, location verification, related code, existing tests, and intent-test evidence.",
            "Check producer invariants and the complete trust boundary before confirming exploitability. Treat an unknown cross-service producer as unresolved controllability, not proof of attacker control, and lower confidence or severity accordingly.",
            "Do not transfer a payload shape from one endpoint to another. If the failure depends solely on an uninspected producer using the assumed shape, classify the candidate weak rather than plausible or confirmed.",
            "dependency_missing is absence of dynamic evidence, not disproof; do not downgrade an otherwise well-supported correctness finding solely because the cloned workspace lacks a local test runner or dependencies.",
            "Disprove UI race candidates when disabled controls or synchronous in-flight locks prevent the second user event. Treat duplicate requests to a demonstrated idempotent endpoint as informational unless concrete user-visible harm remains.",
            "When no evidence contradicts the failure scenario, concrete static source and contract evidence can still support plausible even if the intent test could not execute.",
            "Classify confirmed, plausible, weak, or disproven; do not add unrelated findings.",
            "Write JSON only using validation-output/v1.",
        ],
    },
    "final_report_json": {
        "role": "Final Reporter",
        "prompt_files": ["09_reporter.md"],
        "inputs": ["validated-findings.json", "coverage.json", "token-budget.json", "artifact refs"],
        "outputs": ["report.agent.json"],
        "instructions": [
            'If the validated and weak finding collections are empty, immediately write a canonical no-findings report.agent.json with "findings": [] and "appendix_findings": [], preserving the required summary, coverage, language, and artifact fields from existing inputs. Do not inspect or rescan application source in this empty-input case.',
            "Include only confirmed or plausible actionable findings in the main list.",
            "Do not inherit reviewer severity without re-calibrating it to demonstrated reachability, attacker control, user impact, and existing containment.",
            "Operator-only UI stale-state races without durable server-side data loss, privilege bypass, or service outage are normally medium or lower, not high.",
            "Exclude harmless duplicate requests when the receiving endpoint is idempotent and no user-visible incorrect state, privilege impact, data loss, or material load is demonstrated.",
            "Weak findings go to the top-level appendix_findings list; disproven findings are excluded from main findings.",
            "Preserve coverage, skipped scope, validation sources, and next_agent_task.",
            "Write JSON only using codex-full-repo-review/v1.",
        ],
    },
}


def review_root_for_run_dir(run_dir: Path) -> Path:
    return run_dir.parent.parent


def prompt_template_text(run_dir: Path, name: str) -> str:
    path = review_root_for_run_dir(run_dir) / "prompts" / name
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return prompt_template_for_name(name).strip()


def _profile_list(profile: dict[str, Any], key: str) -> list[str]:
    value = profile.get(key)
    if not isinstance(value, list):
        return []
    return sorted(str(item).strip() for item in value if str(item).strip())


def _adaptive_prompt_context(run_dir: Path) -> list[str]:
    profile = read_json(run_dir / "repo-profile.json", {})
    if not isinstance(profile, dict) or profile.get("schema_version") != "repo-profile/v1":
        return []
    languages = _profile_list(profile, "primary_languages")
    frameworks = _profile_list(profile, "framework_signals")
    tests = _profile_list(profile, "test_frameworks")
    adapters = set(_profile_list(profile, "adapter_ids"))
    surfaces: list[str] = []
    emphasis: list[str] = []
    if "python-backend" in adapters or "python" in languages:
        surfaces.append("auth, migrations, webhooks, DB transactions")
        emphasis.extend(
            [
                "Verify auth decorators and permission boundaries",
                "Check tenant isolation and SQL query boundaries",
                "Check background job idempotency",
            ]
        )
    if "frontend" in adapters or any(signal in frameworks for signal in ("nextjs", "vite", "react")):
        surfaces.append("API routes, server actions, auth middleware, SSR data fetching, env handling")
        emphasis.extend(
            [
                "Check SSR/server action trust boundaries",
                "Check auth-gated UI versus server authorization mismatch",
                "Check env leakage into client bundles",
                "Check unsafe client-side trust assumptions",
            ]
        )
    if "infra" in adapters or any(language in languages for language in ("terraform", "yaml")):
        surfaces.append("deployment/config safety, secrets, external provider boundaries")
        emphasis.append("Check external provider and deployment blast radius")
    if not languages and not frameworks and not tests and not surfaces and not emphasis:
        return []
    lines = ["Adaptive repository context:"]
    if languages:
        lines.append(f"- Primary languages: {', '.join(languages)}")
    if frameworks:
        lines.append(f"- Framework signals: {', '.join(frameworks)}")
    if tests:
        lines.append(f"- Test frameworks: {', '.join(tests)}")
    if surfaces:
        lines.append(f"- High-risk surfaces: {'; '.join(surfaces)}")
    if emphasis:
        lines.append("- Review emphasis:")
        lines.extend(f"  - {item}" for item in dict.fromkeys(emphasis))
    return lines

def phase_prompt(
    phase: str,
    run_dir: Path,
    job: dict[str, Any] | None = None,
    *,
    output_dir: Path | None = None,
) -> str:
    spec = SEMANTIC_PHASE_PROMPT_SPECS.get(phase, {})
    role = str(spec.get("role") or phase.replace("_", " ").title())
    inputs = [str(item) for item in spec.get("inputs", []) if str(item).strip()]
    outputs = [str(item) for item in spec.get("outputs", []) if str(item).strip()]
    instructions = [str(item) for item in spec.get("instructions", []) if str(item).strip()]
    prompt_files = [str(item) for item in spec.get("prompt_files", []) if str(item).strip()]
    output_root = output_dir or run_dir
    repo_root = review_root_for_run_dir(run_dir).parent
    lines = [
        f"Phase: {phase}",
        f"Role: {role}",
        "Perform only this full-repository review phase.",
        "Do not modify application source files.",
        "Do not install dependencies.",
        "Do not call external review/scanning services.",
        f"Source repository (read-only): {repo_root}",
        f"Input run artifact directory (read-only): {run_dir}",
        f"Writable phase output directory: {output_root}",
        "Write only the declared phase outputs inside the writable phase output directory.",
    ]
    lines.extend(output_language_prompt_lines(job))
    if phase == "intent_test_writing":
        lines.append(
            "Worker Python runtime: "
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}; "
            "generated Python tests must compile and import on this exact runtime."
        )
    if inputs:
        lines.append("Inputs:")
        lines.extend(f"- {item}" for item in inputs)
    if outputs:
        lines.append("Required outputs:")
        lines.append(f"- Paths are relative to the writable phase output directory: {output_root}")
        lines.extend(f"- {item}" for item in outputs)
    if instructions:
        lines.append("Phase instructions:")
        lines.extend(f"- {item}" for item in instructions)
    adaptive_context = _adaptive_prompt_context(run_dir)
    if adaptive_context:
        lines.extend(adaptive_context)
    if phase in {"intent_test_planning", "intent_test_writing"}:
        capabilities_path = run_dir / "intent" / "execution-capabilities.json"
        capabilities = read_json(capabilities_path, {})
        runtimes = capabilities.get("runtimes") if isinstance(capabilities, dict) else []
        runtimes = runtimes if isinstance(runtimes, list) else []
        available_runtimes = [
            str(runtime.get("name") or "")
            for runtime in runtimes
            if isinstance(runtime, dict) and runtime.get("available") is True and str(runtime.get("name") or "")
        ]
        lines.extend(
            [
                "Agentic execution contract:",
                "- Read intent/execution-capabilities.json as observed evidence, not as a fixed language template list.",
                "- agent-proposed commands and fallback strategies are allowed when Worker policy and preflight accept them.",
                "- Keep every fallback faithful to the real repository behavior and behavioral oracle; do not test a copied implementation.",
                f"- Currently observed executable names: {', '.join(available_runtimes) if available_runtimes else 'none'}.",
            ]
        )
    if prompt_files:
        lines.append("Prompt templates:")
        for name in prompt_files:
            lines.append(f"--- {name} ---")
            lines.append(prompt_template_text(run_dir, name))
    lines.append("Required output discipline:")
    lines.append("- Produce the required output file(s); do not rely on prose in the turn response.")
    lines.append("- For schema-bound outputs, return/write JSON only with no Markdown wrapper.")
    lines.append("- If required evidence is missing, record the uncertainty in the phase output rather than inventing facts.")
    return "\n".join(lines) + "\n"


def reviewer_assignment_output_name(bundle_id: str, reviewer_id: str) -> str:
    return f"{bundle_id}.{reviewer_id.replace('_', '-')}.json"


def reviewer_assignment_prompt(
    run_dir: Path,
    bundle_id: str,
    reviewer_id: str,
    job: dict[str, Any] | None = None,
    *,
    output_path: Path | None = None,
) -> str:
    reviewer_template_names = {
        "security": "reviewers/security.md",
        "correctness": "reviewers/correctness.md",
        "test_gap": "reviewers/test_gap.md",
        "correctness_lite": "reviewers/correctness_lite.md",
    }
    template_name = reviewer_template_names.get(reviewer_id)
    if template_name is None:
        raise RuntimeError(f"unsupported reviewer assignment: {reviewer_id}")
    output_name = reviewer_assignment_output_name(bundle_id, reviewer_id)
    exact_output_path = output_path or run_dir / "raw-reviewers" / output_name
    try:
        relative_output_path = exact_output_path.relative_to(run_dir).as_posix()
    except ValueError:
        relative_output_path = exact_output_path.name
    lines = [
        "Phase: reviewer_fanout",
        "Role: Independent Bundle Reviewer",
        "Perform exactly one logical reviewer assignment in this turn.",
        f"Bundle assignment: {bundle_id}",
        f"Reviewer assignment: {reviewer_id}",
        f"Read the packed bundle at: {run_dir / 'bundles' / f'{bundle_id}.md'}",
        f"Exact output path: {exact_output_path}",
        f"Output filename relative to the writable reviewer workspace: {relative_output_path}",
        "Do not review or emit output for any other bundle or reviewer assignment in this turn.",
        "Treat this assignment as an independent review; do not inherit an earlier reviewer's empty conclusion.",
        "Inspect the concrete source in the packed bundle and follow referenced repository call sites when needed.",
        "Existing tests are contract evidence, not proof that the implementation is correct.",
        "Actively look for concrete failure scenarios before concluding that findings is empty.",
        "Before reporting an async UI race or duplicate mutation, inspect disabled state, synchronous ref/lock guards, event ordering, and server idempotency; prove that the second action reaches a harmful non-idempotent operation.",
        "Every finding must include id, title, severity, confidence, failure_scenario, evidence, impact, recommendation, false_positive_risk, and next_agent_task.",
        REVIEWER_CONFIDENCE_PROMPT_CONTRACT,
        'Every finding must include a non-empty locations array. Each location must use the exact shape {"path": "relative/source/path", "start_line": 1, "end_line": 1} with positive repository line numbers.',
        "If findings is empty, review_summary must document concrete areas examined, checks performed, and rejected candidates with source-backed reasons.",
        "Write one JSON object only using schema_version codex-reviewer-output/v1.",
        "The object must include bundle_id, reviewer, reviewed_paths, findings, review_summary, and uncertainties.",
        "Do not modify application source files, install dependencies, use network, or call external scanning services.",
        "Write only the exact output file in the writable reviewer workspace; do not rely on prose in the turn response.",
        f"--- {template_name} ---",
        prompt_template_text(run_dir, template_name),
    ]
    lines.extend(output_language_prompt_lines(job))
    adaptive_context = _adaptive_prompt_context(run_dir)
    if adaptive_context:
        lines.extend(adaptive_context)
    return "\n".join(lines) + "\n"


def phase_repair_prompt(
    phase: str,
    run_dir: Path,
    validation_error: object,
    job: dict[str, Any] | None = None,
    *,
    output_dir: Path | None = None,
) -> str:
    return "\n".join(
        [
            f"Phase output repair: {phase}",
            f"Local validation failed: {validation_error}",
            "Repair only the required output file(s) for this phase.",
            "Do not modify application source files.",
            "Do not install dependencies.",
            "Do not call external review/scanning services.",
            "Preserve valid existing evidence and fields; fix malformed or missing JSON/output files.",
            "",
            phase_prompt(phase, run_dir, job, output_dir=output_dir).rstrip(),
        ]
    ) + "\n"


def reviewer_json_repair_prompt(
    run_dir: Path,
    validation_error: object,
    job: dict[str, Any] | None = None,
    *,
    output_dir: Path | None = None,
) -> str:
    output_root = output_dir or run_dir
    lines = [
            "Reviewer JSON output repair",
            f"Local validation failed: {validation_error}",
            f"Repair only malformed files under {output_root / 'raw-reviewers'}.",
            "Each repaired file must be JSON using schema_version codex-reviewer-output/v1 with a findings array.",
            REVIEWER_CONFIDENCE_PROMPT_CONTRACT,
            'Every finding must contain a non-empty locations array using the exact item shape {"path": "relative/source/path", "start_line": 1, "end_line": 1} with positive repository line numbers.',
            "If a finding has no source-backed location, remove that unsupported finding instead of inventing a path or line.",
            "Preserve valid reviewer evidence, locations, severity, confidence, and false-positive context.",
            "Do not add unrelated findings.",
            "Do not modify application source files.",
            "Do not install dependencies or call external review/scanning services.",
            "",
            f"Run artifact directory: {run_dir}",
            f"Writable repair output directory: {output_root}",
        ]
    lines.extend(output_language_prompt_lines(job))
    return "\n".join(lines) + "\n"


RISK_HINT_KEYWORDS = {
    "auth",
    "session",
    "token",
    "oauth",
    "jwt",
    "permission",
    "rbac",
    "tenant",
    "payment",
    "billing",
    "checkout",
    "upload",
    "parser",
    "path",
    "sql",
    "orm",
    "migration",
    "crypto",
    "signature",
    "webhook",
    "queue",
    "lock",
    "idempotency",
    "concurrency",
}
SOURCE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".php",
    ".rb",
    ".cs",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".swift",
    ".sql",
    ".sh",
}
CONFIG_NAMES = {
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "Makefile",
    "Dockerfile",
}
GENERATED_PARTS = {"node_modules", "dist", "build", "target", "vendor", "coverage", ".pytest_cache", "__pycache__"}


def repository_scan_stats(
    repo_dir: Path,
    *,
    context: str,
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, int]:
    files_seen = 0
    bytes_seen = 0
    for path in sorted(repo_dir.rglob("*")):
        check_lifecycle_cancelled(cancel_requested)
        if ".git" in path.parts or ".codex-review" in path.parts:
            continue
        remaining_wall_time_seconds(deadline_monotonic)
        if not _is_regular_file_no_follow(path):
            continue
        stat_result = path.stat(follow_symlinks=False)
        files_seen += 1
        bytes_seen += stat_result.st_size
    return {"files": files_seen, "bytes": bytes_seen}


def raise_repository_limit_if_exceeded(
    stats: dict[str, int],
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
    context: str,
) -> None:
    files_seen = int(stats.get("files") or 0)
    bytes_seen = int(stats.get("bytes") or 0)
    if max_files is not None and files_seen > max_files:
        raise RepositoryLimitExceeded(
            f"repositoryLimits.maxFiles exceeded while {context}",
            files_seen=files_seen,
            bytes_seen=bytes_seen,
            max_files=max_files,
            max_bytes=max_bytes,
        )
    if max_bytes is not None and bytes_seen > max_bytes:
        raise RepositoryLimitExceeded(
            f"repositoryLimits.maxBytes exceeded while {context}",
            files_seen=files_seen,
            bytes_seen=bytes_seen,
            max_files=max_files,
            max_bytes=max_bytes,
        )


def enforce_repository_limits(
    repo_dir: Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
    context: str = "preparing checkout",
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    raise_repository_limit_if_exceeded(
        repository_scan_stats(
            repo_dir,
            context=context,
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        ),
        max_files=max_files,
        max_bytes=max_bytes,
        context=context,
    )


def inventory(
    repo_dir: Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    raise_repository_limit_if_exceeded(
        repository_scan_stats(
            repo_dir,
            context="inventorying checkout",
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        ),
        max_files=max_files,
        max_bytes=max_bytes,
        context="inventorying checkout",
    )
    files = []
    for path in sorted(repo_dir.rglob("*")):
        check_lifecycle_cancelled(cancel_requested)
        if ".git" in path.parts or ".codex-review" in path.parts:
            continue
        remaining_wall_time_seconds(deadline_monotonic)
        if not _is_regular_file_no_follow(path):
            continue
        rel = path.relative_to(repo_dir).as_posix()
        stat_result = path.stat(follow_symlinks=False)
        data = path.read_bytes()
        is_binary = b"\x00" in data[:4096]
        text = ""
        if not is_binary:
            text = data.decode("utf-8", errors="replace")
        extension = path.suffix
        name = path.name
        parts = set(path.parts)
        is_generated = bool(parts.intersection(GENERATED_PARTS)) or rel.endswith((".min.js", ".lock"))
        is_test = any(part in {"test", "tests", "__tests__"} for part in path.parts) or ".test." in name or "_test." in name or name.startswith("test_")
        is_docs = extension.lower() in {".md", ".rst", ".txt", ".adoc"}
        is_config = name in CONFIG_NAMES or extension.lower() in {".toml", ".yaml", ".yml", ".json", ".ini", ".cfg"}
        is_source_like = not is_binary and not is_generated and (extension in SOURCE_EXTENSIONS or is_config)
        lowered = rel.lower().replace("-", "_")
        risk_hints = [keyword for keyword in sorted(RISK_HINT_KEYWORDS) if keyword in lowered]
        files.append(
            {
                "path": rel,
                "extension": extension,
                "size_bytes": stat_result.st_size,
                "line_count": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
                "estimated_tokens": max(1, (stat_result.st_size + 3) // 4) if stat_result.st_size else 0,
                "sha256": hashlib.sha256(data).hexdigest(),
                "is_binary": is_binary,
                "is_source_like": is_source_like,
                "is_generated_candidate": is_generated,
                "is_test_candidate": is_test,
                "is_config_candidate": is_config,
                "is_docs_candidate": is_docs,
                "risk_hints": risk_hints,
            }
        )
    summary = {
        "total_files": len(files),
        "files_total": len(files),
        "source_like_files": sum(1 for item in files if item["is_source_like"]),
        "bytes_total": sum(int(item["size_bytes"]) for item in files),
        "estimated_source_tokens": sum(int(item["estimated_tokens"]) for item in files if item["is_source_like"]),
        "binary_files": sum(1 for item in files if item["is_binary"]),
        "generated_candidates": sum(1 for item in files if item["is_generated_candidate"]),
        "test_candidates": sum(1 for item in files if item["is_test_candidate"]),
        "config_candidates": sum(1 for item in files if item["is_config_candidate"]),
        "docs_candidates": sum(1 for item in files if item["is_docs_candidate"]),
        "risk_reasons": sorted({hint for item in files for hint in item.get("risk_hints", [])})[:12],
    }
    return {
        "schema_version": "inventory/v1",
        "repo": {"root": str(repo_dir), "commit_sha": git_commit(repo_dir), "generated_at": iso_time(time.time())},
        "summary": summary,
        "files": files,
    }


LANGUAGE_EXTENSIONS = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".php": "php",
    ".rb": "ruby",
    ".cs": "csharp",
    ".c": "c_cpp",
    ".cc": "c_cpp",
    ".cpp": "c_cpp",
    ".h": "c_cpp",
    ".hpp": "c_cpp",
    ".swift": "swift",
    ".sql": "sql",
    ".sh": "shell",
}

PROFILE_MANIFEST_NAMES = {
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "go.mod",
    "Cargo.toml",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "poetry.lock",
    "go.sum",
    "Cargo.lock",
}


def _profile_text(repo_dir: Path, rel: str, *, max_chars: int = 200000) -> str:
    try:
        return (repo_dir / rel).read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return ""


def _add_unique(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _profile_package_json(repo_dir: Path) -> tuple[list[str], list[str], list[str]]:
    package_managers: list[str] = []
    frameworks: list[str] = []
    tests: list[str] = []
    path = repo_dir / "package.json"
    if not path.is_file():
        return package_managers, frameworks, tests
    _add_unique(package_managers, "npm")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return package_managers, frameworks, tests
    if not isinstance(payload, dict):
        return package_managers, frameworks, tests
    deps: dict[str, Any] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        value = payload.get(key)
        if isinstance(value, dict):
            deps.update(value)
    dep_names = {str(name).lower() for name in deps}
    scripts = payload.get("scripts") if isinstance(payload.get("scripts"), dict) else {}
    if "next" in dep_names:
        _add_unique(frameworks, "nextjs")
    if "vite" in dep_names or (repo_dir / "vite.config.ts").is_file() or (repo_dir / "vite.config.js").is_file():
        _add_unique(frameworks, "vite")
    if "react" in dep_names:
        _add_unique(frameworks, "react")
    if "express" in dep_names:
        _add_unique(frameworks, "express")
    if "jest" in dep_names or any("jest" in str(value).lower() for value in scripts.values()):
        _add_unique(tests, "jest")
    if "vitest" in dep_names or any("vitest" in str(value).lower() for value in scripts.values()):
        _add_unique(tests, "vitest")
    if any(str(key) == "test" or str(key).startswith("test:") for key in scripts):
        _add_unique(tests, "npm-test")
    return package_managers, frameworks, tests


def minimal_repo_profile_payload(inv: dict[str, Any], repo_dir: Path) -> dict[str, Any]:
    files = inv.get("files") if isinstance(inv, dict) and isinstance(inv.get("files"), list) else []
    language_counts: dict[str, dict[str, int]] = {}
    manifest_files: list[str] = []
    package_managers: list[str] = []
    framework_signals: list[str] = []
    test_frameworks: list[str] = []
    entrypoint_candidates: list[str] = []
    warnings: list[str] = []

    paths = [str(item.get("path") or "") for item in files if isinstance(item, dict) and str(item.get("path") or "")]
    path_set = set(paths)
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        extension = str(item.get("extension") or Path(path).suffix).lower()
        language = LANGUAGE_EXTENSIONS.get(extension)
        if language:
            counts = language_counts.setdefault(language, {"files": 0, "estimated_tokens": 0})
            counts["files"] += 1
            counts["estimated_tokens"] += int(item.get("estimated_tokens") or 0)
        name = Path(path).name
        if name in PROFILE_MANIFEST_NAMES or name.endswith((".yaml", ".yml")):
            _add_unique(manifest_files, path)
        lowered = path.lower()
        if name in {"main.py", "app.py", "worker.py", "manage.py"} or lowered.endswith("/main.go") or lowered.startswith("cmd/"):
            _add_unique(entrypoint_candidates, path)
        if item.get("is_test_candidate"):
            if extension == ".py":
                _add_unique(test_frameworks, "unittest")
            if extension == ".go" and name.endswith("_test.go"):
                _add_unique(test_frameworks, "go-test")

    explicit_pytest_files = {
        "pytest.ini",
        ".pytest.ini",
        "conftest.py",
    }
    if any(Path(path).name in explicit_pytest_files for path in paths):
        _add_unique(test_frameworks, "pytest")
    if "setup.cfg" in path_set and "pytest" in _profile_text(repo_dir, "setup.cfg").lower():
        _add_unique(test_frameworks, "pytest")

    package_managers_from_json, frameworks_from_json, tests_from_json = _profile_package_json(repo_dir)
    for value in package_managers_from_json:
        _add_unique(package_managers, value)
    for value in frameworks_from_json:
        _add_unique(framework_signals, value)
    for value in tests_from_json:
        _add_unique(test_frameworks, value)

    if "package-lock.json" in path_set:
        _add_unique(package_managers, "npm")
    if "pnpm-lock.yaml" in path_set:
        _add_unique(package_managers, "pnpm")
    if "yarn.lock" in path_set:
        _add_unique(package_managers, "yarn")
    if "pyproject.toml" in path_set or any(Path(path).name.startswith("requirements") and path.endswith(".txt") for path in paths):
        _add_unique(package_managers, "pip")
    if "poetry.lock" in path_set:
        _add_unique(package_managers, "poetry")
    if "go.mod" in path_set or "go.sum" in path_set:
        _add_unique(package_managers, "go")
    if "Cargo.toml" in path_set or "Cargo.lock" in path_set:
        _add_unique(package_managers, "cargo")

    pyproject = _profile_text(repo_dir, "pyproject.toml").lower()
    requirements_text = "\n".join(_profile_text(repo_dir, path).lower() for path in paths if Path(path).name.startswith("requirements") and path.endswith(".txt"))
    python_text = f"{pyproject}\n{requirements_text}"
    if "pytest" in python_text:
        _add_unique(test_frameworks, "pytest")
    if "fastapi" in python_text:
        _add_unique(framework_signals, "fastapi")
    if "django" in python_text:
        _add_unique(framework_signals, "django")
    if "flask" in python_text:
        _add_unique(framework_signals, "flask")
    if "sqlalchemy" in python_text:
        _add_unique(framework_signals, "sqlalchemy")

    for config_name, signal in (("next.config.js", "nextjs"), ("next.config.mjs", "nextjs"), ("next.config.ts", "nextjs"), ("vite.config.js", "vite"), ("vite.config.ts", "vite")):
        if config_name in path_set:
            _add_unique(framework_signals, signal)

    adapter_ids: list[str] = []
    if "python" in language_counts:
        _add_unique(adapter_ids, "python")
        if any(signal in framework_signals for signal in ("fastapi", "django", "flask", "sqlalchemy")) or any("app/" in path or "server/" in path for path in paths):
            _add_unique(adapter_ids, "python-backend")
    if any(language in language_counts for language in ("typescript", "javascript")):
        _add_unique(adapter_ids, "node")
        if any(signal in framework_signals for signal in ("nextjs", "vite", "react")) or any(path.startswith(("pages/", "app/", "src/")) for path in paths):
            _add_unique(adapter_ids, "frontend")
    if "go" in language_counts:
        _add_unique(adapter_ids, "go")
    if not adapter_ids:
        adapter_ids = ["generic"]

    primary_languages = [
        language
        for language, _counts in sorted(
            language_counts.items(), key=lambda item: (-int(item[1].get("estimated_tokens") or 0), item[0])
        )[:3]
    ]
    if not primary_languages:
        primary_languages = ["generic"]
        warnings.append("no recognized source language extensions")

    confidence = 0.25
    if language_counts:
        confidence += 0.25
    if framework_signals:
        confidence += 0.15
    if package_managers:
        confidence += 0.15
    if test_frameworks:
        confidence += 0.10
    if adapter_ids != ["generic"]:
        confidence += 0.10

    return {
        "schema_version": "repo-profile/v1",
        "source": "mechanical_inventory",
        "profile_status": "generated",
        "primary_languages": primary_languages,
        "language_counts": language_counts,
        "framework_signals": sorted(framework_signals),
        "package_managers": sorted(package_managers),
        "test_frameworks": sorted(test_frameworks),
        "manifest_files": sorted(manifest_files),
        "entrypoint_candidates": sorted(entrypoint_candidates),
        "hard_skip_patterns": ["**/node_modules/**", "**/dist/**", "**/build/**", "**/vendor/**", "**/.venv/**", "**/__pycache__/**", "**/*.min.js", "**/*lock*"],
        "soft_skip_patterns": ["**/fixtures/**", "**/snapshots/**", "**/*.stories.*", "**/examples/**"],
        "adapter_ids": adapter_ids,
        "confidence": round(min(confidence, 0.95), 2),
        "warnings": warnings,
    }

def git_commit(repo_dir: Path) -> str:
    head = repo_dir / ".git" / "HEAD"
    try:
        text = head.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    if text.startswith("ref:"):
        ref = repo_dir / ".git" / text.split(":", 1)[1].strip()
        try:
            return ref.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            return "unknown"
    return text or "unknown"

def token_budget_payload(run_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    inv = read_json(run_dir / "inventory.json", {})
    summary = inv.get("summary") if isinstance(inv.get("summary"), dict) else {}
    estimated = int(summary.get("estimated_source_tokens") or 0)
    return {
        "schema_version": "token-budget/v1",
        "run_id": run_dir.name,
        "created_at": iso_time(time.time()),
        "summary": {
            "max_total_estimated_input_tokens": 800000,
            "max_bundle_estimated_tokens": 60000,
            "hard_bundle_estimated_tokens": 80000,
            "estimated_source_tokens": estimated,
            "review_budget_ratio": 0.58,
            "intent_test_budget_ratio": 0.12,
            "validation_budget_ratio": 0.18,
            "aggregation_budget_ratio": 0.07,
            "reserve_ratio": 0.05,
        },
        "model_profile": {
            "default_model": model_for_job(job),
            "core_effort": core_effort_for_job(job),
            "non_core_effort": "medium",
            "intent_test_effort": "medium",
        },
        "review_worker": review_worker_policy_for_job(job),
        "intent_test_validation": intent_validation_config(job),
    }


def intent_validation_config(job: dict[str, Any]) -> dict[str, Any]:
    configured = validate_job_policy(job)["intent_test_validation"]
    return {
        "schema_version": "intent-test-validation/v1",
        "enabled": configured.get("enabled", True) is not False,
        "only_tiers": configured.get("only_tiers") or ["P0", "P1"],
        "max_tests_per_run": int(configured.get("max_tests_per_run") or 20),
        "max_tests_per_bundle": int(configured.get("max_tests_per_bundle") or 2),
        "max_test_run_seconds_per_test": int(configured.get("max_test_run_seconds_per_test") or 60),
        "max_total_test_run_seconds": int(configured.get("max_total_test_run_seconds") or 900),
        "max_preflight_repair_attempts": int(configured.get("max_preflight_repair_attempts") or 0),
        "max_runtime_repair_attempts": int(configured.get("max_runtime_repair_attempts") or 0),
        "run_tests_in_disposable_workspace": True,
        "require_intent_evidence": True,
    }


REVIEW_FILE_TIERS = {"P0", "P1", "P2", "P3", "SKIP"}
REVIEW_TIER_ALIASES = {
    "DEEP": "P0",
    "HIGH": "P0",
    "STANDARD": "P1",
    "MEDIUM": "P1",
    "LIGHT": "P2",
    "LOW": "P2",
    "INVENTORY": "P3",
    "INVENTORY_ONLY": "P3",
    "NONE": "SKIP",
    "SKIPPED": "SKIP",
}


def canonical_review_file_tier(value: object) -> str:
    tier = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if tier in REVIEW_FILE_TIERS:
        return tier
    return REVIEW_TIER_ALIASES.get(tier, "")


def risk_route_tier(route: dict[str, Any]) -> str:
    for key in ("tier", "risk", "risk_tier", "riskTier", "depth", "review_depth", "reviewDepth", "priority", "classification"):
        tier = canonical_review_file_tier(route.get(key))
        if tier:
            return tier
    return ""


def route_patterns(route: dict[str, Any]) -> list[str]:
    patterns: list[str] = []
    for key in ("path", "file", "glob", "pattern", "prefix", "directory"):
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            patterns.append(value.strip())
    for key in ("paths", "files", "globs", "patterns", "prefixes", "directories"):
        value = route.get(key)
        if isinstance(value, list):
            patterns.extend(str(item).strip() for item in value if str(item).strip())
    return patterns


def _canonical_risk_route(value: object, *, tier_hint: str = "") -> dict[str, Any] | None:
    if isinstance(value, str):
        path = value.strip()
        tier = canonical_review_file_tier(tier_hint)
        return {"path": path, "tier": tier} if path and tier else None
    if not isinstance(value, dict):
        return None
    tier = canonical_review_file_tier(
        value.get("tier")
        or value.get("risk")
        or value.get("priority")
        or value.get("risk_tier")
        or value.get("riskTier")
        or tier_hint
    )
    patterns = route_patterns(value)
    if not tier or not patterns:
        return None
    route: dict[str, Any] = {
        "path": patterns[0],
        "tier": tier,
    }
    if len(patterns) > 1:
        route.pop("path", None)
        route["paths"] = list(dict.fromkeys(patterns))
    raw_reasons = value.get("reasons")
    reasons = (
        [str(reason).strip() for reason in raw_reasons if str(reason).strip()]
        if isinstance(raw_reasons, list)
        else []
    )
    single_reason = str(value.get("reason") or value.get("rationale") or "").strip()
    if single_reason and single_reason not in reasons:
        reasons.append(single_reason)
    if reasons:
        route["reasons"] = reasons
    return route


def normalize_risk_routing_artifact(path: Path) -> None:
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return
    raw_routes = payload.get("routes")
    if isinstance(raw_routes, list):
        routes = []
        changed = False
        for raw_route in raw_routes:
            if not isinstance(raw_route, dict):
                routes.append(raw_route)
                continue
            route = dict(raw_route)
            tier = risk_route_tier(route)
            if tier and route.get("tier") != tier:
                route["tier"] = tier
                changed = True
            routes.append(route)
        if changed:
            normalized = dict(payload)
            normalized["routes"] = routes
            write_json(path, normalized)
        return
    routes: list[dict[str, Any]] = []
    tiers = payload.get("tiers") if isinstance(payload.get("tiers"), dict) else {}
    for tier in ("P0", "P1", "P2", "P3", "SKIP"):
        tier_payload = tiers.get(tier)
        if isinstance(tier_payload, dict):
            values = tier_payload.get("files")
            if not isinstance(values, list):
                values = tier_payload.get("paths")
        else:
            values = tier_payload
        if not isinstance(values, list):
            continue
        for value in values:
            route = _canonical_risk_route(value, tier_hint=tier)
            if route is not None:
                routes.append(route)
    if not routes and isinstance(payload.get("files"), list):
        for value in payload["files"]:
            route = _canonical_risk_route(value)
            if route is not None:
                routes.append(route)
    if not routes:
        return
    normalized = dict(payload)
    normalized["routes"] = routes
    normalized.pop("tiers", None)
    normalized.pop("files", None)
    write_json(path, normalized)


def risk_routing_contract_errors(payload: dict[str, Any], run_dir: Path) -> list[str]:
    routes = payload.get("routes")
    if not isinstance(routes, list):
        return ["risk-routing.json routes must be a list"]
    errors: list[str] = []
    for index, raw_route in enumerate(routes):
        if not isinstance(raw_route, dict):
            errors.append(f"risk-routing.json routes[{index}] must be an object")
            continue
        if not risk_route_tier(raw_route):
            errors.append(f"risk-routing.json routes[{index}] has an invalid tier")
        if not route_patterns(raw_route):
            errors.append(f"risk-routing.json routes[{index}] has no path pattern")
    default_keys = (
        "default_depth",
        "defaultDepth",
        "default_tier",
        "defaultTier",
        "default",
    )
    default_tier = risk_routing_default_tier(payload)
    if any(key in payload for key in default_keys) and not default_tier:
        errors.append(
            "risk-routing.json default tier must be P0, P1, P2, P3, or SKIP"
        )
    inventory = read_json(run_dir / "inventory.json", {})
    inventory_files = [
        item
        for item in inventory.get("files", [])
        if isinstance(item, dict)
    ] if isinstance(inventory, dict) else []
    source_like = [
        item for item in inventory_files if item.get("is_source_like") is True
    ]
    if source_like and not routes and not default_tier:
        errors.append(
            "risk-routing.json must provide routes or an explicit default tier "
            "for a non-empty source inventory"
        )
    if not default_tier:
        uncovered_paths = [
            str(item.get("path") or "").strip()
            for item in inventory_files
            if str(item.get("path") or "").strip()
            and not is_hard_skip_item(item)
            and semantic_file_route(item, payload) is None
        ]
        if uncovered_paths:
            errors.append(
                "risk-routing.json does not cover eligible path(s): "
                + ", ".join(uncovered_paths[:20])
            )
    return errors


def normalize_cluster_output_artifact(path: Path) -> None:
    payload = read_json(path, {})
    if not isinstance(payload, dict) or isinstance(payload.get("clusters"), list):
        return
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return
    normalized = dict(payload)
    normalized["clusters"] = findings
    normalized.pop("findings", None)
    write_json(path, normalized)


def cluster_output_contract_errors(payload: dict[str, Any]) -> list[str]:
    clusters = payload.get("clusters")
    if not isinstance(clusters, list):
        return ["clusters.json clusters must be a list"]
    errors: list[str] = []
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            errors.append(f"clusters.json clusters[{index}] must be an object")
            continue
        if not str(cluster.get("cluster_id") or cluster.get("id") or "").strip():
            errors.append(f"clusters.json clusters[{index}] is missing cluster_id")
    return errors


def route_matches_path(pattern: str, path: str) -> bool:
    normalized_pattern = pattern.strip().lstrip("./")
    normalized_path = path.strip().lstrip("./")
    if not normalized_pattern:
        return False
    if normalized_pattern == normalized_path:
        return True
    if normalized_pattern.endswith("/"):
        return normalized_path.startswith(normalized_pattern)
    if any(char in normalized_pattern for char in "*?[]"):
        return fnmatch.fnmatch(normalized_path, normalized_pattern)
    return normalized_path.startswith(f"{normalized_pattern}/")


def semantic_file_route(item: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any] | None:
    routes = routing.get("routes") if isinstance(routing.get("routes"), list) else []
    path = str(item.get("path") or "")
    best_route: dict[str, Any] | None = None
    best_specificity = -1
    for route in routes:
        if not isinstance(route, dict):
            continue
        tier = risk_route_tier(route)
        if not tier:
            continue
        for pattern in route_patterns(route):
            if route_matches_path(pattern, path) and len(pattern) > best_specificity:
                best_route = route
                best_specificity = len(pattern)
    return best_route


def semantic_file_tier(item: dict[str, Any], routing: dict[str, Any]) -> str:
    route = semantic_file_route(item, routing)
    return risk_route_tier(route) if isinstance(route, dict) else ""


def risk_routing_default_tier(routing: dict[str, Any]) -> str:
    for key in ("default_depth", "defaultDepth", "default_tier", "defaultTier", "default"):
        tier = canonical_review_file_tier(routing.get(key))
        if tier:
            return tier
    return ""


def is_hard_skip_item(item: dict[str, Any]) -> bool:
    if item.get("is_binary") or item.get("is_hard_generated_candidate") or item.get("is_generated_candidate"):
        return True
    path = str(item.get("path") or "").replace("\\", "/").lower().lstrip("./")
    parts = set(part for part in path.split("/") if part)
    if parts.intersection({"node_modules", "dist", "build", "vendor", ".venv", "__pycache__", ".cache"}):
        return True
    name = Path(path).name
    if name.endswith(".min.js") or name in {"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "go.sum", "cargo.lock"}:
        return True
    return False


def _semantic_effective_route(item: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any] | None:
    route = semantic_file_route(item, routing)
    if not isinstance(route, dict):
        return None
    tier = risk_route_tier(route)
    if not tier:
        return None
    reasons = route.get("reasons") if isinstance(route.get("reasons"), list) else []
    return {
        "path": str(item.get("path") or ""),
        "tier": tier,
        "source": "semantic",
        "reasons": [str(reason) for reason in reasons if str(reason).strip()] or ["semantic_routing"],
    }


def effective_routing(semantic_routing: dict[str, Any] | None, profile: dict[str, Any] | None, inventory_payload: dict[str, Any] | None) -> dict[str, Any]:
    del profile
    routing = semantic_routing if isinstance(semantic_routing, dict) and semantic_routing.get("schema_version") == "risk-routing/v1" else {}
    inv = inventory_payload if isinstance(inventory_payload, dict) else {}
    files = inv.get("files") if isinstance(inv.get("files"), list) else []
    routes: list[dict[str, Any]] = []
    sources = {
        "semantic_routes": 0,
        "semantic_default_routes": 0,
        "hard_skip_routes": 0,
    }
    default_tier = risk_routing_default_tier(routing)
    for item in files:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        if is_hard_skip_item(item):
            routes.append({"path": path, "tier": "SKIP", "source": "hard_skip", "reasons": ["hard_skip_generated_vendor_cache_or_lock"]})
            sources["hard_skip_routes"] += 1
            continue
        semantic_route = _semantic_effective_route(item, routing)
        if semantic_route:
            routes.append(semantic_route)
            sources["semantic_routes"] += 1
            continue
        if not default_tier:
            raise RuntimeError(
                "risk-routing.json does not cover eligible path and has no "
                f"explicit default tier: {path}"
            )
        routes.append(
            {
                "path": path,
                "tier": default_tier,
                "source": "semantic_default",
                "reasons": ["semantic_default_depth"],
            }
        )
        sources["semantic_default_routes"] += 1
    return {"schema_version": "effective-risk-routing/v1", "run_id": "", "sources": sources, "routes": routes}


def file_tier(item: dict[str, Any], routing: dict[str, Any] | None = None) -> str:
    if is_hard_skip_item(item):
        return "SKIP"
    if isinstance(routing, dict) and routing:
        semantic_tier = semantic_file_tier(item, routing)
        if semantic_tier:
            return semantic_tier
    raise RuntimeError(
        "effective risk routing does not contain an authoritative tier for "
        f"{str(item.get('path') or '').strip()}"
    )


def _bundle_planning_context(
    run_dir: Path,
) -> tuple[
    Path,
    dict[str, Any],
    dict[str, Any],
    dict[str, list[dict[str, Any]]],
]:
    repo_dir = run_dir.parent.parent.parent
    inventory_payload = read_json(run_dir / "inventory.json", {})
    files = (
        inventory_payload.get("files")
        if isinstance(inventory_payload, dict)
        and isinstance(inventory_payload.get("files"), list)
        else []
    )
    semantic_routing = read_json(run_dir / "risk-routing.json", {})
    semantic_routing = (
        semantic_routing
        if isinstance(semantic_routing, dict)
        and semantic_routing.get("schema_version") == "risk-routing/v1"
        else {}
    )
    profile = read_json(run_dir / "repo-profile.json", {})
    profile = (
        profile
        if isinstance(profile, dict)
        and profile.get("schema_version") == "repo-profile/v1"
        else {}
    )
    routing = effective_routing(semantic_routing, profile, inventory_payload)
    routing["run_id"] = run_dir.name
    write_json(run_dir / "effective-risk-routing.json", routing)
    route_source_by_path = {
        str(route.get("path") or ""): str(route.get("source") or "")
        for route in routing.get("routes", [])
        if isinstance(route, dict)
    }
    grouped: dict[str, list[dict[str, Any]]] = {
        "P0": [],
        "P1": [],
        "P2": [],
        "P3": [],
        "SKIP": [],
    }
    for raw_item in files:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        path = str(item.get("path") or "").strip()
        item["_routing_source"] = route_source_by_path.get(path, "")
        grouped[file_tier(item, routing)].append(item)
    return repo_dir, inventory_payload, routing, grouped


def _write_bundle_coverage(
    run_dir: Path,
    inventory_payload: dict[str, Any],
    grouped: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    files = (
        inventory_payload.get("files")
        if isinstance(inventory_payload.get("files"), list)
        else []
    )
    source_like_by_tier = {
        tier: [
            item
            for item in grouped[tier]
            if isinstance(item, dict) and item.get("is_source_like")
        ]
        for tier in grouped
    }
    coverage = {
        "schema_version": "coverage/v1",
        "source_like_files_total": sum(
            1
            for item in files
            if isinstance(item, dict) and item.get("is_source_like")
        ),
        "deep_reviewed_files": len(source_like_by_tier["P0"]),
        "standard_reviewed_files": len(source_like_by_tier["P1"]),
        "light_reviewed_files": len(source_like_by_tier["P2"]),
        "inventory_only_files": len(source_like_by_tier["P3"]),
        "skipped_files": len(source_like_by_tier["SKIP"]),
        "intent_tests_planned": 0,
        "intent_tests_run": 0,
        "intent_tests_supporting_findings": 0,
        "skipped_scope": [
            item.get("path") for item in source_like_by_tier["SKIP"][:100]
        ],
    }
    if source_like_by_tier["SKIP"]:
        coverage["skipped_reasons"] = {
            "semantic_or_inventory_skip": coverage["skipped_scope"]
        }
    write_json(run_dir / "coverage.json", coverage)
    return coverage


def prepare_bundle_planning_input(
    run_dir: Path,
    job: dict[str, Any],
) -> dict[str, Any]:
    _repo_dir, inventory_payload, routing, grouped = _bundle_planning_context(run_dir)
    review_worker_policy = review_worker_policy_for_job(job)
    _write_bundle_coverage(run_dir, inventory_payload, grouped)
    items: list[dict[str, Any]] = []
    for tier in ("P0", "P1", "P2"):
        for item in grouped[tier]:
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            risk_hints = (
                item.get("risk_hints")
                if isinstance(item.get("risk_hints"), list)
                else []
            )
            items.append(
                {
                    "path": path,
                    "tier": tier,
                    "estimated_tokens": max(
                        0, int(item.get("estimated_tokens") or 0)
                    ),
                    "line_count": max(0, int(item.get("line_count") or 0)),
                    "is_test_candidate": item.get("is_test_candidate") is True,
                    "risk_hints": [
                        str(value)
                        for value in risk_hints
                        if str(value).strip()
                    ][:12],
                    "routing_source": str(item.get("_routing_source") or ""),
                }
            )
    payload = {
        "schema_version": "bundle-planning-input/v1",
        "run_id": run_dir.name,
        "constraints": {
            "max_files_per_bundle": 25,
            "max_rendered_size": MAX_BUNDLE_ESTIMATED_TOKENS,
            "max_bundles": int(review_worker_policy["maxBundles"]),
            "max_reviewer_assignments": int(
                review_worker_policy["maxReviewerAssignments"]
            ),
            "reviewer_assignments_per_bundle_by_tier": {
                "P0": 3,
                "P1": 2,
                "P2": 1,
            },
            "allowed_tiers": ["P0", "P1", "P2"],
            "worker_may_split_oversized_groups": True,
            "worker_preserves_semantic_group_boundaries": True,
        },
        "routing_sources": routing.get("sources", {}),
        "items": items,
    }
    write_json(run_dir / "bundle-planning-input.json", payload)
    return payload


def bundle_grouping_contract_errors(
    run_dir: Path,
    payload: object,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["bundle-grouping.json must be an object"]
    if payload.get("schema_version") != "bundle-grouping/v1":
        errors.append(
            "bundle-grouping.json must use schema_version bundle-grouping/v1"
        )
    run_id = str(payload.get("run_id") or "").strip()
    if run_id and run_id != run_dir.name:
        errors.append("bundle-grouping.json run_id must match the active run")
    planning_input = read_json(run_dir / "bundle-planning-input.json", {})
    raw_items = (
        planning_input.get("items")
        if isinstance(planning_input, dict)
        and isinstance(planning_input.get("items"), list)
        else []
    )
    eligible_tiers = {
        str(item.get("path") or ""): str(item.get("tier") or "")
        for item in raw_items
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }
    groups = payload.get("groups")
    if not isinstance(groups, list):
        return [*errors, "bundle-grouping.json groups must be a list"]
    if eligible_tiers and not groups:
        errors.append(
            "bundle-grouping.json groups must not be empty when eligible paths exist"
        )
    seen_group_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, group in enumerate(groups):
        label = f"bundle-grouping.json groups[{index}]"
        if not isinstance(group, dict):
            errors.append(f"{label} must be an object")
            continue
        group_id = str(group.get("group_id") or "").strip()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", group_id) is None:
            errors.append(f"{label}.group_id must be a stable lowercase identifier")
        elif group_id in seen_group_ids:
            errors.append(f"{label} has duplicate group_id {group_id}")
        else:
            seen_group_ids.add(group_id)
        tier = str(group.get("tier") or "").strip().upper()
        if tier not in {"P0", "P1", "P2"}:
            errors.append(f"{label}.tier must be P0, P1, or P2")
        if not str(group.get("title") or "").strip():
            errors.append(f"{label}.title must be non-empty")
        reasons = group.get("grouping_reasons")
        if (
            not isinstance(reasons, list)
            or not any(str(reason).strip() for reason in reasons)
        ):
            errors.append(f"{label}.grouping_reasons must be a non-empty list")
        paths = group.get("paths")
        if not isinstance(paths, list) or not paths:
            errors.append(f"{label}.paths must be a non-empty list")
            continue
        for raw_path in paths:
            path = str(raw_path or "").strip()
            if not path:
                errors.append(f"{label} contains an empty path")
                continue
            if path not in eligible_tiers:
                errors.append(f"{label} contains unknown or ineligible path {path}")
                continue
            if path in seen_paths:
                errors.append(f"{label} contains duplicate path {path}")
                continue
            seen_paths.add(path)
            if tier in {"P0", "P1", "P2"} and eligible_tiers[path] != tier:
                errors.append(
                    f"{label} tier {tier} does not match {path} tier "
                    f"{eligible_tiers[path]}"
                )
    missing_paths = sorted(set(eligible_tiers) - seen_paths)
    if missing_paths:
        errors.append(
            "bundle-grouping.json missing eligible path(s): "
            + ", ".join(missing_paths)
        )
    return errors


def materialize_agent_bundle_plan(
    run_dir: Path,
    job: dict[str, Any],
) -> dict[str, Any]:
    repo_dir = run_dir.parent.parent.parent
    plan_path = run_dir / "bundle-plan.json"
    try:
        plan_path.unlink()
    except FileNotFoundError:
        pass
    planning_input = prepare_bundle_planning_input(run_dir, job)
    grouping = parse_required_json_output(run_dir / "bundle-grouping.json")
    errors = bundle_grouping_contract_errors(run_dir, grouping)
    if errors:
        raise RuntimeError("invalid bundle grouping: " + "; ".join(errors))
    inventory_payload = read_json(run_dir / "inventory.json", {})
    raw_inventory = (
        inventory_payload.get("files")
        if isinstance(inventory_payload, dict)
        and isinstance(inventory_payload.get("files"), list)
        else []
    )
    inventory_by_path = {
        str(item.get("path") or ""): item
        for item in raw_inventory
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }
    input_by_path = {
        str(item.get("path") or ""): item
        for item in planning_input.get("items", [])
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }
    groups = grouping.get("groups")
    assert isinstance(groups, list)
    bundles: list[dict[str, Any]] = []
    for group in groups:
        assert isinstance(group, dict)
        tier = str(group["tier"]).upper()
        group_id = str(group["group_id"])
        title = str(group["title"]).strip()
        semantic_reasons = [
            str(reason).strip()
            for reason in group["grouping_reasons"]
            if str(reason).strip()
        ]
        group_items: list[dict[str, Any]] = []
        for path in group["paths"]:
            item = dict(inventory_by_path[str(path)])
            item["_routing_source"] = str(
                input_by_path[str(path)].get("routing_source") or ""
            )
            item["_semantic_group_id"] = group_id
            item["_semantic_group_title"] = title
            item["_semantic_group_reasons"] = semantic_reasons
            group_items.extend(
                split_oversized_bundle_item(item, repo_dir)
            )
        _append_render_fitted_bundle_chunks(
            repo_dir,
            tier,
            group_items,
            bundles,
        )

    review_worker_policy = review_worker_policy_for_job(job)
    plan = {
        "schema_version": "bundle-plan/v1",
        "run_id": run_dir.name,
        "routing_sources": planning_input.get("routing_sources", {}),
        "planning_strategy": (
            "codex_count_aware_semantic_grouping_then_worker_bounded_split"
        ),
        "semantic_group_count": len(groups),
        "resource_limits": {
            "max_bundles": int(review_worker_policy["maxBundles"]),
            "max_reviewer_assignments": int(
                review_worker_policy["maxReviewerAssignments"]
            ),
        },
        "bundles": bundles,
    }
    bundle_count, assignment_count = enforce_review_plan_resource_limits(plan, job)
    plan["bundle_count"] = bundle_count
    plan["reviewer_assignment_count"] = assignment_count
    plan["bundle_counts_by_tier"] = {
        tier: sum(1 for bundle in bundles if bundle.get("tier") == tier)
        for tier in ("P0", "P1", "P2")
    }
    write_json(plan_path, plan)
    return plan


def split_oversized_bundle_item(
    item: dict[str, Any],
    repo_dir: Path | None = None,
) -> list[dict[str, Any]]:
    estimated_tokens = max(0, int(item.get("estimated_tokens") or 0))
    line_count = max(0, int(item.get("line_count") or 0))
    if estimated_tokens <= MAX_BUNDLE_ESTIMATED_TOKENS:
        return [item]
    segment_count = max(2, math.ceil(estimated_tokens / MAX_BUNDLE_ESTIMATED_TOKENS))
    source_lines: list[str] | None = None
    if repo_dir is not None:
        source_path = repo_dir / str(item.get("path") or "")
        if source_path.is_file():
            source_lines = source_path.read_text(
                encoding="utf-8",
                errors="replace",
            ).splitlines()
            line_count = len(source_lines)
    if line_count <= 1:
        estimated_characters = (
            max(1, len(source_lines[0]))
            if source_lines
            else max(1, estimated_tokens * 4)
        )
        characters_per_segment = math.ceil(estimated_characters / segment_count)
        segments: list[dict[str, Any]] = []
        for index in range(segment_count):
            start_char = index * characters_per_segment
            if start_char >= estimated_characters:
                break
            end_char = min(estimated_characters, (index + 1) * characters_per_segment)
            segment = dict(item)
            segment["estimated_tokens"] = math.ceil((end_char - start_char) / 4)
            segment["_segment_start_line"] = 1
            segment["_segment_end_line"] = 1
            segment["_segment_start_char"] = start_char
            segment["_segment_end_char"] = end_char
            segments.append(segment)
        return segments
    segment_count = min(segment_count, line_count)
    lines_per_segment = math.ceil(line_count / segment_count)
    segments: list[dict[str, Any]] = []
    for index in range(segment_count):
        start_line = index * lines_per_segment + 1
        if start_line > line_count:
            break
        end_line = min(line_count, (index + 1) * lines_per_segment)
        line_fraction = (end_line - start_line + 1) / line_count
        segment_tokens = max(1, math.ceil(estimated_tokens * line_fraction))
        segment = dict(item)
        segment["estimated_tokens"] = segment_tokens
        segment["_segment_start_line"] = start_line
        segment["_segment_end_line"] = end_line
        segments.append(segment)
    return segments


def _bundle_metadata(files: list[dict[str, Any]]) -> dict[str, Any]:
    grouping_reasons: list[str] = []
    metadata: dict[str, Any] = {}
    routing_sources = sorted({str(item.get("_routing_source") or "") for item in files if str(item.get("_routing_source") or "")})
    if routing_sources:
        metadata["routing_sources"] = routing_sources
    semantic_groups: dict[str, dict[str, Any]] = {}
    for item in files:
        group_id = str(item.get("_semantic_group_id") or "").strip()
        if not group_id:
            continue
        group = semantic_groups.setdefault(
            group_id,
            {
                "group_id": group_id,
                "title": str(item.get("_semantic_group_title") or group_id).strip(),
                "grouping_reasons": [],
                "paths": [],
            },
        )
        path = str(item.get("path") or "").strip()
        if path and path not in group["paths"]:
            group["paths"].append(path)
        for reason in (
            item.get("_semantic_group_reasons")
            if isinstance(item.get("_semantic_group_reasons"), list)
            else []
        ):
            reason_text = str(reason or "").strip()
            if reason_text and reason_text not in group["grouping_reasons"]:
                group["grouping_reasons"].append(reason_text)
            if reason_text and reason_text not in grouping_reasons:
                grouping_reasons.append(reason_text)
    if grouping_reasons:
        metadata["grouping_reasons"] = grouping_reasons
    if semantic_groups:
        group_payloads = list(semantic_groups.values())
        metadata["semantic_groups"] = group_payloads
        metadata["semantic_group_ids"] = list(semantic_groups)
        if len(group_payloads) == 1:
            metadata["semantic_group_id"] = group_payloads[0]["group_id"]
    return metadata

def bundle_payload(tier: str, index: int, files: list[dict[str, Any]], estimated_tokens: int) -> dict[str, Any]:
    reviewers = {
        "P0": ["security", "correctness", "test_gap"],
        "P1": ["correctness", "test_gap"],
        "P2": ["correctness_lite"],
    }[tier]
    metadata = _bundle_metadata(files)
    semantic_groups = (
        metadata.get("semantic_groups")
        if isinstance(metadata.get("semantic_groups"), list)
        else []
    )
    semantic_titles = [
        str(group.get("title") or "").strip()
        for group in semantic_groups
        if isinstance(group, dict) and str(group.get("title") or "").strip()
    ]
    if len(semantic_titles) == 1:
        title = semantic_titles[0]
    elif semantic_titles:
        visible_titles = semantic_titles[:3]
        suffix = (
            f" + {len(semantic_titles) - len(visible_titles)} more"
            if len(semantic_titles) > len(visible_titles)
            else ""
        )
        title = " / ".join(visible_titles) + suffix
    else:
        title = f"{tier} review bundle {index}"
    payload = {
        "bundle_id": f"{tier.lower()}-bundle-{index:03d}",
        "tier": tier,
        "title": title,
        "estimated_tokens": estimated_tokens,
        "paths": list(dict.fromkeys(str(item.get("path")) for item in files if item.get("path"))),
        "reviewers": reviewers,
        "validator_required": tier == "P0",
        "intent_test_eligible": tier in {"P0", "P1"},
        "risk_reasons": sorted({hint for item in files for hint in item.get("risk_hints", [])})[:12],
    }
    file_ranges = [
        {
            "path": str(item.get("path")),
            "start_line": int(item.get("_segment_start_line")),
            "end_line": int(item.get("_segment_end_line")),
            **(
                {
                    "start_char": int(item.get("_segment_start_char")),
                    "end_char": int(item.get("_segment_end_char")),
                }
                if item.get("_segment_start_char") is not None
                and item.get("_segment_end_char") is not None
                else {}
            ),
        }
        for item in files
        if item.get("path")
        and int(item.get("_segment_start_line") or 0) > 0
        and int(item.get("_segment_end_line") or 0) > 0
    ]
    if file_ranges:
        payload["file_ranges"] = file_ranges
    payload.update(metadata)
    return payload


def _bundle_entries(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    file_ranges = (
        bundle.get("file_ranges")
        if isinstance(bundle.get("file_ranges"), list)
        else []
    )
    ranges_by_path: dict[str, list[dict[str, Any]]] = {}
    for entry in file_ranges:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("path") or "")
        if rel:
            ranges_by_path.setdefault(rel, []).append(entry)
    entries: list[dict[str, Any]] = []
    planned_paths: set[str] = set()
    for raw_path in bundle.get("paths") or []:
        rel = str(raw_path or "")
        if not rel or rel in planned_paths:
            continue
        planned_paths.add(rel)
        entries.extend(ranges_by_path.get(rel) or [{"path": rel}])
    for rel, ranges in ranges_by_path.items():
        if rel not in planned_paths:
            entries.extend(ranges)
    return entries


def _render_bundle_markdown(repo_dir: Path, bundle: dict[str, Any]) -> str:
    bundle_id = safe_id(bundle.get("bundle_id"), "bundle")
    lines = [
        f"# Bundle: {bundle_id}",
        "",
        f"Tier: {bundle.get('tier') or 'P1'}  ",
        f"Title: {bundle.get('title') or bundle_id}  ",
        f"Estimated tokens: {bundle.get('estimated_tokens') or 0}  ",
        f"Reviewers: {', '.join(bundle.get('reviewers') or [])}  ",
        f"Intent test eligible: {str(bool(bundle.get('intent_test_eligible'))).lower()}",
        "",
        "## Files",
        "",
    ]
    semantic_group_by_path: dict[str, dict[str, Any]] = {}
    for group in (
        bundle.get("semantic_groups")
        if isinstance(bundle.get("semantic_groups"), list)
        else []
    ):
        if not isinstance(group, dict):
            continue
        for raw_path in group.get("paths") or []:
            path = str(raw_path or "").strip()
            if path:
                semantic_group_by_path[path] = group
    active_group_id = ""
    for entry in _bundle_entries(bundle):
        rel = str(entry.get("path") or "")
        semantic_group = semantic_group_by_path.get(rel)
        semantic_group_id = (
            str(semantic_group.get("group_id") or "").strip()
            if isinstance(semantic_group, dict)
            else ""
        )
        if semantic_group_id and semantic_group_id != active_group_id:
            active_group_id = semantic_group_id
            group_title = str(semantic_group.get("title") or semantic_group_id).strip()
            lines.extend((f"### Semantic group: {group_title}", ""))
        path = repo_dir / rel
        start_line = max(1, int(entry.get("start_line") or 1))
        end_line = (
            max(start_line, int(entry.get("end_line") or 0))
            if entry.get("end_line")
            else 0
        )
        start_char = max(0, int(entry.get("start_char") or 0))
        end_char = (
            max(start_char, int(entry.get("end_char") or 0))
            if entry.get("end_char") is not None
            else 0
        )
        range_label = f":{start_line}-{end_line}" if end_line else ""
        if entry.get("start_char") is not None and entry.get("end_char") is not None:
            range_label += f" chars {start_char}-{end_char}"
        heading = "####" if semantic_group_id else "###"
        lines.append(f"{heading} {rel}{range_label}")
        lines.append("")
        if not path.is_file():
            lines.extend(("```text", "<missing>", "```", ""))
            continue
        suffix = path.suffix.lstrip(".") or "text"
        lines.append(f"```{suffix}")
        source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        selected_lines = source_lines[start_line - 1 : end_line or None]
        if (
            entry.get("start_char") is not None
            and entry.get("end_char") is not None
            and selected_lines
        ):
            selected_lines = [selected_lines[0][start_char:end_char]]
        for index, source_line in enumerate(selected_lines, start=start_line):
            lines.append(f"{index} | {source_line}")
        lines.extend(("```", ""))
    return "\n".join(lines)


def _rendered_bundle_size(payload: str) -> int:
    return max(len(payload), len(payload.encode("utf-8")))


def _render_fitted_bundle_candidate(
    repo_dir: Path,
    tier: str,
    index: int,
    files: list[dict[str, Any]],
    *,
    payload_overrides: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    payload = bundle_payload(
        tier,
        index,
        files,
        MAX_BUNDLE_ESTIMATED_TOKENS,
    )
    if payload_overrides:
        payload.update(payload_overrides)
    conservative_size = _rendered_bundle_size(
        _render_bundle_markdown(repo_dir, payload)
    )
    while True:
        payload["estimated_tokens"] = conservative_size
        rendered_size = _rendered_bundle_size(
            _render_bundle_markdown(repo_dir, payload)
        )
        if rendered_size <= conservative_size:
            return payload, rendered_size
        conservative_size = rendered_size


def _bisect_bundle_item(
    repo_dir: Path,
    item: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    rel = str(item.get("path") or "")
    source_path = repo_dir / rel
    source_lines = (
        source_path.read_text(encoding="utf-8", errors="replace").splitlines()
        if source_path.is_file()
        else []
    )
    start_line = max(1, int(item.get("_segment_start_line") or 1))
    end_line = max(
        start_line,
        int(item.get("_segment_end_line") or max(1, len(source_lines))),
    )
    if source_lines:
        end_line = min(end_line, len(source_lines))

    original_estimate = max(1, int(item.get("estimated_tokens") or 1))
    child_estimate = max(1, math.ceil(original_estimate / 2))
    if start_line < end_line:
        midpoint = (start_line + end_line) // 2
        left = dict(item)
        left["estimated_tokens"] = child_estimate
        left["_segment_start_line"] = start_line
        left["_segment_end_line"] = midpoint
        left.pop("_segment_start_char", None)
        left.pop("_segment_end_char", None)
        right = dict(item)
        right["estimated_tokens"] = child_estimate
        right["_segment_start_line"] = midpoint + 1
        right["_segment_end_line"] = end_line
        right.pop("_segment_start_char", None)
        right.pop("_segment_end_char", None)
        return left, right

    source_line = (
        source_lines[start_line - 1]
        if 0 < start_line <= len(source_lines)
        else ""
    )
    has_character_range = (
        item.get("_segment_start_char") is not None
        and item.get("_segment_end_char") is not None
    )
    start_char = (
        max(0, min(int(item.get("_segment_start_char") or 0), len(source_line)))
        if has_character_range
        else 0
    )
    end_char = (
        max(
            start_char,
            min(int(item.get("_segment_end_char") or 0), len(source_line)),
        )
        if has_character_range
        else len(source_line)
    )
    if end_char - start_char <= 1:
        raise RuntimeError(
            "source item cannot be split below the hard bundle limit: "
            f"{rel}:{start_line} chars {start_char}-{end_char}"
        )
    midpoint = (start_char + end_char) // 2
    left = dict(item)
    left["estimated_tokens"] = child_estimate
    left["_segment_start_line"] = start_line
    left["_segment_end_line"] = start_line
    left["_segment_start_char"] = start_char
    left["_segment_end_char"] = midpoint
    right = dict(item)
    right["estimated_tokens"] = child_estimate
    right["_segment_start_line"] = start_line
    right["_segment_end_line"] = start_line
    right["_segment_start_char"] = midpoint
    right["_segment_end_char"] = end_char
    return left, right


def _append_render_fitted_bundle_chunks(
    repo_dir: Path,
    tier: str,
    files: list[dict[str, Any]],
    bundles: list[dict[str, Any]],
    *,
    payload_overrides: dict[str, Any] | None = None,
) -> None:
    pending = deque(files)
    current: list[dict[str, Any]] = []
    while pending:
        item = pending.popleft()
        candidate = [*current, item]
        payload, rendered_size = _render_fitted_bundle_candidate(
            repo_dir,
            tier,
            len(bundles) + 1,
            candidate,
            payload_overrides=payload_overrides,
        )
        if (
            len(candidate) <= 25
            and rendered_size <= MAX_BUNDLE_ESTIMATED_TOKENS
            and int(payload.get("estimated_tokens") or 0)
            <= MAX_BUNDLE_ESTIMATED_TOKENS
        ):
            current = candidate
            continue
        if current:
            completed, completed_size = _render_fitted_bundle_candidate(
                repo_dir,
                tier,
                len(bundles) + 1,
                current,
                payload_overrides=payload_overrides,
            )
            if (
                completed_size > MAX_BUNDLE_ESTIMATED_TOKENS
                or int(completed.get("estimated_tokens") or 0)
                > MAX_BUNDLE_ESTIMATED_TOKENS
            ):
                raise RuntimeError("fitted bundle exceeds hard bundle limit")
            bundles.append(completed)
            current = []
            pending.appendleft(item)
            continue
        left, right = _bisect_bundle_item(repo_dir, item)
        pending.appendleft(right)
        pending.appendleft(left)

    if current:
        completed, completed_size = _render_fitted_bundle_candidate(
            repo_dir,
            tier,
            len(bundles) + 1,
            current,
            payload_overrides=payload_overrides,
        )
        if (
            completed_size > MAX_BUNDLE_ESTIMATED_TOKENS
            or int(completed.get("estimated_tokens") or 0)
            > MAX_BUNDLE_ESTIMATED_TOKENS
        ):
            raise RuntimeError("fitted bundle exceeds hard bundle limit")
        bundles.append(completed)


def pack_bundles(repo_dir: Path, run_dir: Path) -> None:
    plan = read_json(run_dir / "bundle-plan.json", {})
    bundles = plan.get("bundles") if isinstance(plan.get("bundles"), list) else []
    bundle_dir = run_dir / "bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        bundle_id = safe_id(bundle.get("bundle_id"), "bundle")
        rendered = _render_bundle_markdown(repo_dir, bundle)
        rendered_size = _rendered_bundle_size(rendered)
        estimated_tokens = max(0, int(bundle.get("estimated_tokens") or 0))
        if rendered_size > MAX_BUNDLE_ESTIMATED_TOKENS:
            raise RuntimeError(
                f"packed bundle exceeds hard limit: {bundle_id} "
                f"({rendered_size} > {MAX_BUNDLE_ESTIMATED_TOKENS})"
            )
        if rendered_size > estimated_tokens:
            raise RuntimeError(
                f"packed bundle exceeds conservative plan estimate: {bundle_id} "
                f"({rendered_size} > {estimated_tokens})"
            )
        (bundle_dir / f"{bundle_id}.md").write_bytes(rendered.encode("utf-8"))



def _reviewer_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ''


def _reviewer_evidence_items(finding: dict[str, Any]) -> list[object]:
    raw_evidence: object = finding.get('evidence')
    if raw_evidence in (None, '', []):
        for alias in ('supporting_evidence', 'supportingEvidence', 'evidence_summary', 'evidenceSummary'):
            candidate = finding.get(alias)
            if candidate not in (None, '', []):
                raw_evidence = candidate
                break
    if isinstance(raw_evidence, (str, dict)):
        candidates: list[object] = [raw_evidence]
    elif isinstance(raw_evidence, list):
        candidates = raw_evidence
    else:
        candidates = []
    evidence: list[object] = []
    for candidate in candidates:
        if isinstance(candidate, str):
            if candidate.strip():
                evidence.append(candidate.strip())
            continue
        if not isinstance(candidate, dict):
            continue
        if any(
            _reviewer_text(candidate.get(field))
            for field in (
                'summary',
                'evidence',
                'text',
                'reason',
                'description',
                'observation',
                'explanation',
                'snippet',
            )
        ):
            evidence.append(dict(candidate))
    return evidence


def _canonical_reviewer_finding(
    raw_finding: dict[str, Any],
    index: int,
) -> tuple[dict[str, Any] | None, str]:
    finding = dict(raw_finding)
    if not _reviewer_text(finding.get('title')):
        return None, f'findings[{index}].title is required'
    finding_id = next(
        (
            candidate
            for field in ('id', 'finding_id', 'local_id')
            if (candidate := _reviewer_text(finding.get(field)))
        ),
        '',
    )
    if not finding_id:
        return None, f'findings[{index}].id is required'
    finding['id'] = finding_id
    if not _reviewer_text(finding.get('severity')):
        return None, f'findings[{index}].severity is required'
    confidence = finding.get('confidence')
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        return None, f'findings[{index}].confidence must be a number in 0..1'

    failure_scenario = next(
        (
            candidate
            for field in ('failure_scenario', 'failureScenario', 'scenario', 'failure_mode', 'failureMode')
            if (candidate := _reviewer_text(finding.get(field)))
        ),
        '',
    )
    if not failure_scenario:
        return None, f'findings[{index}].failure_scenario is required'
    finding['failure_scenario'] = failure_scenario
    for field in ('failureScenario', 'scenario', 'failure_mode', 'failureMode'):
        finding.pop(field, None)

    evidence = _reviewer_evidence_items(finding)
    if not evidence:
        return None, f'findings[{index}].evidence must contain substantive evidence'
    finding['evidence'] = evidence
    for field in ('supporting_evidence', 'supportingEvidence', 'evidence_summary', 'evidenceSummary'):
        finding.pop(field, None)

    for field in ('impact', 'false_positive_risk', 'next_agent_task'):
        if not _reviewer_text(finding.get(field)):
            return None, f'findings[{index}].{field} is required'

    recommendation = _reviewer_text(finding.get('recommendation'))
    if not recommendation:
        recommendation = next(
            (
                candidate
                for alias in ('recommended_fix', 'recommended_action', 'remediation')
                if (candidate := _reviewer_text(finding.get(alias)))
            ),
            '',
        )
    if not recommendation:
        return None, f'findings[{index}].recommendation is required'
    finding['recommendation'] = recommendation
    for alias in ('recommended_fix', 'recommended_action', 'remediation'):
        finding.pop(alias, None)
    return finding, ''


def validate_reviewer_outputs(run_dir: Path) -> None:
    raw_dir = run_dir / "raw-reviewers"
    verified_dir = run_dir / "verified-reviewers"
    raw_dir.mkdir(parents=True, exist_ok=True)
    verified_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    valid_outputs = 0
    validated_payloads: list[tuple[Path, dict[str, Any]]] = []
    raw_files = sorted(raw_dir.glob("*.json"))
    if not raw_files:
        errors.append({"file": "raw-reviewers", "error": "no reviewer JSON outputs were produced"})
    for path in raw_files:
        payload = read_json(path, None)
        if not isinstance(payload, dict):
            errors.append({"file": path.name, "error": "not an object"})
            continue
        schema_version = str(payload.get("schema_version") or "").strip()
        if schema_version in REVIEWER_OUTPUT_SCHEMA_ALIASES:
            payload = dict(payload)
            payload["schema_version"] = REVIEWER_OUTPUT_SCHEMA_VERSION
        elif schema_version != REVIEWER_OUTPUT_SCHEMA_VERSION:
            errors.append({"file": path.name, "error": f"schema_version must be {REVIEWER_OUTPUT_SCHEMA_VERSION}"})
            continue
        bundle_id = str(payload.get("bundle_id") or "").strip()
        if not bundle_id:
            errors.append({"file": path.name, "error": "bundle_id is required"})
            continue
        reviewer_id = str(payload.get("reviewer") or "").strip()
        if reviewer_id not in {"security", "correctness", "test_gap", "correctness_lite"}:
            errors.append({"file": path.name, "error": "reviewer is missing or unsupported"})
            continue
        reviewed_paths = payload.get("reviewed_paths")
        if (
            not isinstance(reviewed_paths, list)
            or not reviewed_paths
            or any(not isinstance(item, str) or not item.strip() for item in reviewed_paths)
        ):
            errors.append({"file": path.name, "error": "reviewed_paths must be a non-empty string list"})
            continue
        review_summary = payload.get("review_summary")
        summary_present = (
            isinstance(review_summary, str)
            and bool(review_summary.strip())
        ) or (
            isinstance(review_summary, (dict, list))
            and bool(review_summary)
        )
        if not summary_present:
            errors.append({"file": path.name, "error": "review_summary must contain review evidence"})
            continue
        if not isinstance(payload.get("uncertainties"), list):
            errors.append({"file": path.name, "error": "uncertainties must be a list"})
            continue
        if not isinstance(payload.get("findings"), list):
            errors.append({"file": path.name, "error": "findings must be a list"})
            continue
        normalized_findings: list[dict[str, Any]] = []
        finding_error = ""
        for index, raw_finding in enumerate(payload["findings"]):
            if not isinstance(raw_finding, dict):
                finding_error = f"findings[{index}] must be an object"
                break
            finding, finding_error = _canonical_reviewer_finding(raw_finding, index)
            if finding_error or finding is None:
                break
            locations = agent_report_locations(finding)
            if not locations:
                finding_error = f"findings[{index}].locations is missing or invalid"
                break
            finding["locations"] = locations
            for alias in (
                "location",
                "code_location",
                "codeLocation",
                "source_location",
                "sourceLocation",
                "affected_locations",
                "affectedLocations",
                "code_locations",
                "codeLocations",
                "source_locations",
                "sourceLocations",
                "line_evidence",
                "lineEvidence",
            ):
                finding.pop(alias, None)
            normalized_findings.append(finding)
        if finding_error:
            errors.append({"file": path.name, "error": finding_error})
            continue
        payload = dict(payload)
        payload["findings"] = normalized_findings
        valid_outputs += 1
        validated_payloads.append((path, payload))
        write_json(path, payload)
        write_json(verified_dir / path.name, payload)
    expected_assignments = _planned_reviewer_assignments(run_dir)
    if expected_assignments:
        covered_assignments: set[tuple[str, str]] = set()
        for path, payload in validated_payloads:
            covered_assignments.update(_reviewer_output_assignments(payload, path, expected_assignments))
        missing_assignments = sorted(expected_assignments - covered_assignments)
        if missing_assignments:
            missing_text = ", ".join(f"{bundle_id}:{reviewer_id}" for bundle_id, reviewer_id in missing_assignments)
            errors.append(
                {
                    "file": "raw-reviewers",
                    "error": f"missing planned reviewer assignments: {missing_text}",
                }
            )
        unexpected_assignments = sorted(covered_assignments - expected_assignments)
        if unexpected_assignments:
            unexpected_text = ", ".join(f"{bundle_id}:{reviewer_id}" for bundle_id, reviewer_id in unexpected_assignments)
            errors.append(
                {
                    "file": "raw-reviewers",
                    "error": f"reviewer outputs contain unplanned assignments: {unexpected_text}",
                }
            )
    write_json(run_dir / "json-errors.json", {"schema_version": "reviewer-json-validation/v1", "errors": errors})
    if errors or valid_outputs == 0:
        first = errors[0] if errors else {"file": "raw-reviewers", "error": "no valid reviewer JSON outputs were produced"}
        raise RuntimeError(f"reviewer JSON validation failed for {first.get('file')}: {first.get('error')}")


def location_verification_payload(repo_dir: Path, run_dir: Path) -> dict[str, Any]:
    checks = []
    repo_root = repo_dir.resolve(strict=False)
    for path in sorted((run_dir / "verified-reviewers").glob("*.json")):
        payload = read_json(path, {})
        findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            finding_id = str(
                finding.get("local_id")
                or finding.get("finding_id")
                or finding.get("id")
                or finding.get("cluster_id")
                or ""
            )
            for location in agent_report_locations(finding):
                rel = str(location.get("path") or "")
                start = int(location.get("start_line") or location.get("line") or 0)
                end = int(location.get("end_line") or start or 0)
                raw_path = Path(rel)
                file_path = raw_path if raw_path.is_absolute() else repo_root / raw_path
                file_path = file_path.resolve(strict=False)
                contained = bool(rel) and path_is_under(file_path, repo_root)
                is_file = contained and file_path.is_file()
                line_count = (
                    len(file_path.read_text(encoding="utf-8", errors="replace").splitlines())
                    if is_file
                    else 0
                )
                status = (
                    "valid"
                    if is_file and 1 <= start <= end <= line_count
                    else "invalid"
                )
                checks.append(
                    {
                        "finding_id": finding_id,
                        "path": rel,
                        "start_line": start,
                        "end_line": end,
                        "line_count": line_count,
                        "location_status": status,
                    }
                )
    return {
        "schema_version": "location-verification/v1",
        "run_id": run_dir.name,
        "items": checks,
        "summary": {
            "locations_total": len(checks),
            "valid_locations": sum(1 for item in checks if item.get("location_status") == "valid"),
            "invalid_locations": sum(1 for item in checks if item.get("location_status") == "invalid"),
        },
    }


def ensure_intent_directories(run_dir: Path) -> None:
    for path in (
        run_dir / "intent",
        run_dir / "intent" / "generated-tests",
        run_dir / "intent" / "test-output",
    ):
        path.mkdir(parents=True, exist_ok=True)


def refresh_agentic_execution_capabilities(repo_dir: Path, run_dir: Path) -> dict[str, Any]:
    ensure_intent_directories(run_dir)
    proposal_sources = [
        read_json(run_dir / "intent" / "intent-test-plan.json", {}),
        read_json(run_dir / "intent" / "intent-test-source.json", {}),
    ]
    sandbox_available = (
        not sys.platform.startswith("linux")
        or shutil.which("bwrap") is not None
        or shutil.which("bubblewrap") is not None
    )
    payload = build_execution_capabilities(
        repo_dir,
        proposal_sources=proposal_sources,
        sandbox_available=sandbox_available,
    )
    profile = read_json(run_dir / "repo-profile.json", {})
    payload["repository_profile"] = {
        "primary_languages": _profile_list(profile, "primary_languages") if isinstance(profile, dict) else [],
        "test_frameworks": _profile_list(profile, "test_frameworks") if isinstance(profile, dict) else [],
    }
    for candidate in payload.get("agent_candidates", []):
        if not isinstance(candidate, dict):
            continue
        raw_cwd = str(candidate.get("cwd") or ".").strip() or "."
        cwd = Path(raw_cwd)
        if not cwd.is_absolute():
            cwd = repo_dir / cwd
        candidate_preflight = intent_execution_preflight(
            [str(part) for part in candidate.get("command") or []],
            cwd.resolve(strict=False),
            repo_dir,
            profile if isinstance(profile, dict) else {},
        )
        escaped_required_paths: list[str] = []
        missing_required_paths: list[str] = []
        for raw_required_path in candidate.get("required_paths") or []:
            required_path_text = str(raw_required_path or "").strip()
            if not required_path_text:
                continue
            required_path = Path(required_path_text)
            if not required_path.is_absolute():
                required_path = cwd / required_path
            if not path_is_under(required_path, repo_dir):
                escaped_required_paths.append(required_path_text)
            elif not required_path.exists():
                missing_required_paths.append(required_path_text)
        if escaped_required_paths:
            candidate_preflight = _intent_blocked_execution_diagnostic(
                "agent-proposed required path escapes the validation workspace",
                reason_code="required_path_escape",
                classification="environment_error",
                agent_repairable=False,
                missing_capabilities=escaped_required_paths,
            )
        elif missing_required_paths:
            candidate_preflight = _intent_blocked_execution_diagnostic(
                "agent-proposed required paths are missing",
                reason_code="required_path_missing",
                missing_capabilities=missing_required_paths,
            )
        candidate["preflight"] = candidate_preflight
    write_json(run_dir / "intent" / "execution-capabilities.json", payload)
    return payload


def _source_repository_for_run_dir(run_dir: Path) -> Path:
    review_root = run_dir.parent.parent
    if run_dir.parent.name != "runs" or review_root.name != ".codex-review":
        raise RuntimeError("run directory is outside the canonical repository review tree")
    return review_root.parent


def immutable_inventory_baseline_path(run_dir: Path) -> Path:
    repo_dir = _source_repository_for_run_dir(run_dir)
    return repo_dir.parent / ".pullwise-integrity" / "inventory.json"


def _valid_inventory_payload(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("schema_version") == "inventory/v1"
        and isinstance(payload.get("files"), list)
    )


def write_immutable_inventory_baseline(
    repo_dir: Path,
    run_dir: Path,
    payload: dict[str, Any],
) -> Path:
    expected_repo = _source_repository_for_run_dir(run_dir)
    if repo_dir.resolve(strict=False) != expected_repo.resolve(strict=False):
        raise RuntimeError("immutable inventory repository does not match the run directory")
    if not _valid_inventory_payload(payload):
        raise RuntimeError("immutable inventory baseline must use inventory/v1")
    baseline_path = immutable_inventory_baseline_path(run_dir)
    control_root = baseline_path.parent
    if control_root.is_symlink() or baseline_path.is_symlink():
        raise RuntimeError("immutable inventory baseline path must not be a symlink")
    control_root.mkdir(parents=True, exist_ok=True)
    if baseline_path.exists():
        existing = read_json(baseline_path, {})
        if not _valid_inventory_payload(existing):
            raise RuntimeError("immutable inventory baseline is invalid")
        return baseline_path
    write_json(baseline_path, payload)
    try:
        control_root.chmod(0o700)
        baseline_path.chmod(0o600)
    except OSError:
        pass
    return baseline_path


def ensure_immutable_inventory_baseline(repo_dir: Path, run_dir: Path) -> Path:
    baseline_path = immutable_inventory_baseline_path(run_dir)
    if baseline_path.is_symlink():
        raise RuntimeError("immutable inventory baseline path must not be a symlink")
    if baseline_path.exists():
        payload = read_json(baseline_path, {})
        if not _valid_inventory_payload(payload):
            raise RuntimeError("immutable inventory baseline is invalid")
        return baseline_path
    return write_immutable_inventory_baseline(repo_dir, run_dir, inventory(repo_dir))


def prepare_validation_workspace(
    repo_dir: Path,
    run_dir: Path,
    *,
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    check_lifecycle_cancelled(cancel_requested)
    remaining_wall_time_seconds(deadline_monotonic)
    ensure_intent_directories(run_dir)
    ensure_immutable_inventory_baseline(repo_dir, run_dir)
    validation_repo = repo_dir.parent / "validation-repo"
    if validation_repo.exists():
        shutil.rmtree(validation_repo)
    validation_repo.mkdir(parents=True, exist_ok=True)
    copy_tree(
        repo_dir,
        validation_repo,
        deadline_monotonic=deadline_monotonic,
        cancel_requested=cancel_requested,
    )
    payload = {
        "schema_version": "validation-workspace/v1",
        "validation_repo_root": str(validation_repo),
        "source_repo_root": str(repo_dir),
        "commit_sha": git_commit(repo_dir),
        "created_at": iso_time(time.time()),
    }
    write_json(run_dir / "intent" / "validation-workspace.json", payload)
    refresh_agentic_execution_capabilities(validation_repo, run_dir)
    return payload


def _intent_test_id(value: dict[str, Any], fallback: str) -> str:
    return str(value.get("test_id") or value.get("id") or value.get("target_id") or fallback).strip()


def _intent_command(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        try:
            return shlex.split(value)
        except ValueError:
            return []
    return []


def _intent_relative_test_path(raw_path: str, validation_repo: Path | None) -> str:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return ""
    path = Path(path_text)
    if path.is_absolute() and validation_repo is not None:
        try:
            return path.resolve(strict=False).relative_to(validation_repo.resolve(strict=False)).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _intent_inferred_command(generated: dict[str, Any], target: dict[str, Any], validation_repo: Path | None) -> list[str]:
    path_text = _intent_source_path_from_entry(generated) or _intent_source_path_from_entry(target)
    rel_path = _intent_relative_test_path(path_text, validation_repo)
    if not rel_path:
        return []
    suffix = Path(rel_path).suffix.lower()
    framework = ""
    for source in (generated, target):
        raw_runnability = source.get("runnability") if isinstance(source.get("runnability"), dict) else {}
        framework = str(
            source.get("framework")
            or source.get("test_framework")
            or source.get("testFramework")
            or raw_runnability.get("framework")
            or framework
            or ""
        ).strip().lower()
    if framework == "node" and suffix in {".js", ".mjs", ".cjs"}:
        return ["node", "--test", rel_path]
    if framework in {"vitest", "jest", "npm"} or suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return ["npm", "test", "--", rel_path]
    if framework in {"unittest", "python-unittest"}:
        return ["python", "-m", "unittest", rel_path]
    if framework == "pytest":
        return ["python", "-m", "pytest", rel_path]
    if framework in {"unittest", "python-unittest", "python"} or suffix == ".py":
        return ["python", "-m", "unittest", rel_path]
    if framework == "go" or suffix == ".go":
        return ["go", "test", "./..."]
    return []


def _intent_related_test_ids(value: dict[str, Any]) -> list[str]:
    related: list[str] = []
    for key in (
        "test_ids",
        "testIds",
        "target_ids",
        "targetIds",
        "target_test_ids",
        "targetTestIds",
        "related_test_ids",
        "relatedTestIds",
        "targets",
    ):
        raw = value.get(key)
        items = raw if isinstance(raw, list) else [raw] if isinstance(raw, str) else []
        for item in items:
            test_id = str(item or "").strip()
            if test_id and test_id not in related:
                related.append(test_id)
    return related


def _intent_source_command_specs(source: dict[str, Any]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for key in ("test_commands", "testCommands", "intended_commands", "intendedCommands"):
        raw_specs = source.get(key)
        if not isinstance(raw_specs, list):
            continue
        for raw_spec in raw_specs:
            if isinstance(raw_spec, dict):
                specs.append(raw_spec)
            elif isinstance(raw_spec, str) and raw_spec.strip():
                specs.append({"command": raw_spec})
    return specs


def _intent_source_command_value(
    source: dict[str, Any],
    generated: dict[str, Any],
    generated_index: int,
    generated_total: int,
) -> object:
    specs = _intent_source_command_specs(source)
    if not specs:
        return None
    generated_ids = {_intent_test_id(generated, ""), *_intent_related_test_ids(generated)}
    generated_ids.discard("")
    generated_path = _path_key(_intent_source_path_from_entry(generated))
    for spec in specs:
        command = spec.get("command") or spec.get("test_command") or spec.get("run_command")
        if not _intent_command(command):
            continue
        spec_ids = {_intent_test_id(spec, ""), *_intent_related_test_ids(spec)}
        spec_ids.discard("")
        if generated_ids.intersection(spec_ids):
            return command
        spec_path = _path_key(_intent_source_path_from_entry(spec))
        if generated_path and spec_path and (
            generated_path == spec_path
            or generated_path.endswith("/" + spec_path)
            or spec_path.endswith("/" + generated_path)
        ):
            return command
    command_specs = [
        spec
        for spec in specs
        if _intent_command(spec.get("command") or spec.get("test_command") or spec.get("run_command"))
    ]
    if len(command_specs) == 1 and generated_total == 1:
        spec = command_specs[0]
        return spec.get("command") or spec.get("test_command") or spec.get("run_command")
    if len(command_specs) == generated_total and generated_index < len(command_specs):
        spec = command_specs[generated_index]
        return spec.get("command") or spec.get("test_command") or spec.get("run_command")
    return None


def _intent_generated_command(
    generated: dict[str, Any],
    target: dict[str, Any],
    validation_repo: Path | None = None,
    source: dict[str, Any] | None = None,
    generated_index: int = 0,
    generated_total: int = 1,
) -> list[str]:
    for key in (
        "command",
        "test_command",
        "testCommand",
        "run_command",
        "runCommand",
        "runnable_command",
        "runnableCommand",
        "intended_command",
        "intendedCommand",
    ):
        command = _intent_command(generated.get(key))
        if command:
            return command
    if isinstance(source, dict):
        command = _intent_command(
            _intent_source_command_value(source, generated, generated_index, generated_total)
        )
        if command:
            return command
    for key in (
        "command",
        "test_command",
        "testCommand",
        "run_command",
        "runCommand",
        "runnable_command",
        "runnableCommand",
        "intended_command",
        "intendedCommand",
    ):
        command = _intent_command(target.get(key))
        if command:
            return command
    return _intent_inferred_command(generated, target, validation_repo)


def _intent_generated_execution_records(generated_tests: list[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, item in enumerate(generated_tests):
        if not isinstance(item, dict):
            continue
        record = dict(item)
        record["_source_test_id"] = _intent_test_id(item, f"ITV-{index + 1:03d}")
        records.append(record)
    executable: list[dict[str, Any]] = []
    for record in records:
        path_kind = str(record.get("path_kind") or record.get("pathKind") or "").strip().lower()
        if path_kind in {"run_artifact_source_copy", "artifact_source_copy"}:
            continue
        executable.append(dict(record))
    executable = executable or [dict(record) for record in records]
    grouped: list[dict[str, Any]] = []
    grouped_by_path: dict[str, dict[str, Any]] = {}
    for record in executable:
        path_key = _path_key(_intent_source_path_from_entry(record))
        if not path_key:
            grouped.append(record)
            continue
        existing = grouped_by_path.get(path_key)
        if existing is None:
            grouped_by_path[path_key] = record
            grouped.append(record)
            continue
        related_ids = _intent_related_test_ids(existing)
        for candidate_id in (
            _intent_test_id(existing, ""),
            _intent_test_id(record, ""),
            *_intent_related_test_ids(record),
        ):
            if candidate_id and candidate_id not in related_ids:
                related_ids.append(candidate_id)
        existing["test_ids"] = related_ids
    return grouped


def _intent_command_path_matches_materialized(
    argument: str,
    declared_path: str,
    materialized_path: str,
) -> bool:
    candidate = argument.strip().replace("\\", "/")
    declared = declared_path.strip().replace("\\", "/")
    materialized = materialized_path.strip().replace("\\", "/")
    if not candidate or candidate.startswith("-") or not materialized:
        return False
    if candidate in {declared, materialized}:
        return True
    stripped = _strip_validation_workspace_prefix(candidate)
    if stripped and stripped == materialized:
        return True
    return False


def _intent_normalized_execution_command(
    command: list[str],
    *,
    declared_path: str = "",
    materialized_path: str = "",
) -> list[str]:
    normalized = list(command)
    if normalized and normalized[0].strip().lower() == "python":
        normalized[0] = "python3"
    if materialized_path:
        normalized = [
            materialized_path
            if index > 0 and _intent_command_path_matches_materialized(part, declared_path, materialized_path)
            else part
            for index, part in enumerate(normalized)
        ]
    if len(normalized) != 4:
        return normalized
    executable, module_flag, framework, raw_path = normalized
    if module_flag != "-m" or framework != "unittest" or not raw_path.lower().endswith(".py"):
        return normalized
    path = PurePosixPath(raw_path.replace("\\", "/"))
    start_dir = path.parent.as_posix()
    if start_dir in {"", "."}:
        return normalized
    return [
        executable,
        "-m",
        "unittest",
        "discover",
        "-s",
        start_dir,
        "-p",
        path.name,
    ]


def _intent_source_execution_skip(run_dir: Path) -> tuple[str, str]:
    source = read_json(run_dir / "intent" / "intent-test-source.json", {})
    execution = source.get("execution") if isinstance(source, dict) and isinstance(source.get("execution"), dict) else {}
    if execution.get("ran") is not False:
        return "", ""
    reason = str(execution.get("reason") or "").strip()
    if not reason:
        return "", ""
    lowered = reason.lower()
    delegated_to_runner = (
        "intent_test_running" in lowered
        or "intent-test-running" in lowered
        or "intent test running" in lowered
        or (
            any(marker in lowered for marker in ("running phase", "runner", "worker"))
            and any(marker in lowered for marker in ("delegate", "execute", "run later", "will run", "will be run"))
        )
    )
    if delegated_to_runner:
        return "", ""
    if "dependenc" in lowered or "node_modules" in lowered or "not installed" in lowered:
        return reason, "dependency_missing"
    if "environment" in lowered or "sandbox" in lowered:
        return reason, "environment_error"
    return reason, "skipped_not_runnable"


def _intent_test_cwd(validation_repo: Path, generated: dict[str, Any], target: dict[str, Any]) -> Path | None:
    raw = str(generated.get("cwd") or target.get("cwd") or "").strip()
    candidate = Path(raw) if raw else validation_repo
    if not candidate.is_absolute():
        candidate = validation_repo / candidate
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(validation_repo.resolve(strict=False))
    except ValueError:
        return None
    return candidate


def _intent_test_timeout(config: dict[str, Any], generated: dict[str, Any], target: dict[str, Any]) -> int:
    for source in (generated, target, config):
        for key in ("timeout_seconds", "timeoutSeconds", "max_test_run_seconds_per_test", "maxTestRunSecondsPerTest"):
            value = source.get(key) if isinstance(source, dict) else None
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return min(number, int(config.get("max_test_run_seconds_per_test") or number or 60))
    return int(config.get("max_test_run_seconds_per_test") or 60)


def _intent_generated_python_compile_error(
    validation_repo: Path,
    generated: dict[str, Any],
    target: dict[str, Any],
) -> str:
    raw_path = _intent_source_path_from_entry(generated) or _intent_source_path_from_entry(target)
    rel_path = _intent_relative_test_path(raw_path, validation_repo)
    if not rel_path or Path(rel_path).suffix.lower() != ".py":
        return ""
    path = (validation_repo / rel_path).resolve(strict=False)
    if not path_is_under(path, validation_repo) or not path.is_file():
        return ""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
        compile(source, str(path), "exec", dont_inherit=True)
        tree = ast.parse(source, filename=str(path), mode="exec")
    except (OSError, SyntaxError, ValueError) as exc:
        return f"generated Python test does not compile on worker Python {sys.version_info.major}.{sys.version_info.minor}: {exc}"
    harness_error = _intent_module_scope_project_test_import_error(tree)
    if harness_error:
        return harness_error
    return ""


def _intent_module_scope_project_test_import_error(tree: ast.AST) -> str:
    violations: list[tuple[str, str]] = []

    class ModuleScopeImportVisitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            return

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            module = str(node.module or "").strip()
            module_parts = [part for part in module.split(".") if part]
            if not any(part.startswith("test_") or part.endswith("_test") for part in module_parts):
                return
            for alias in node.names:
                exposed_name = str(alias.asname or alias.name or "").rsplit(".", 1)[-1]
                if (
                    exposed_name == "*"
                    or exposed_name.startswith("test_")
                    or re.search(r"(?:Test|Tests|TestCase)$", exposed_name)
                ):
                    violations.append((module, exposed_name))

    ModuleScopeImportVisitor().visit(tree)
    if not violations:
        return ""
    module, name = violations[0]
    return (
        f"generated Python test exposes imported project test object {name!r} from {module!r} at module scope; "
        "import the test module instead and access the helper through that module so unittest or pytest cannot "
        "over-discover unrelated tests"
    )


def _intent_output_path(run_dir: Path, test_id: str, suffix: str, *, attempt: int = 1) -> Path:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in test_id).strip("._")
    attempt_suffix = "" if attempt <= 1 else f".attempt-{attempt}"
    return run_dir / "intent" / "test-output" / f"{safe or 'intent-test'}{attempt_suffix}.{suffix}.log"


def _intent_output_artifact_id(name: str) -> str:
    return f"art_intent_test_output_{safe_artifact_suffix(name, fallback='log')}"


def safe_artifact_suffix(name: str, *, fallback: str = "artifact") -> str:
    return "".join(char if char.isalnum() else "_" for char in name).strip("_") or fallback


def _intent_is_rust_command(command: list[str] | None) -> bool:
    if not command:
        return False
    executable = Path(str(command[0])).name.lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable in {"cargo", "rustc", "rustup"}


def _intent_host_rustup_home() -> Path | None:
    configured_home = str(os.environ.get("RUSTUP_HOME") or "").strip()
    if configured_home:
        candidate = Path(configured_home)
    else:
        configured_host_home = str(os.environ.get("HOME") or "").strip()
        if configured_host_home:
            candidate = Path(configured_host_home) / ".rustup"
        else:
            try:
                candidate = Path.home() / ".rustup"
            except (OSError, RuntimeError):
                return None
    try:
        if not candidate.is_absolute() or not candidate.is_dir() or candidate.is_symlink():
            return None
    except OSError:
        return None
    return candidate


def _intent_test_env(
    validation_repo: Path,
    *,
    sandboxed: bool = False,
    command: list[str] | None = None,
) -> dict[str, str]:
    env: dict[str, str] = {}
    passthrough = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
    for key, value in os.environ.items():
        normalized = key.upper()
        if normalized in passthrough or normalized.startswith("LC_"):
            env[key] = value
    if _intent_is_rust_command(command):
        rustup_home = _intent_host_rustup_home()
        if rustup_home is not None:
            env["RUSTUP_HOME"] = str(rustup_home)
        rustup_toolchain = str(os.environ.get("RUSTUP_TOOLCHAIN") or "").strip()
        if rustup_toolchain:
            env["RUSTUP_TOOLCHAIN"] = rustup_toolchain
    sandbox_home = validation_repo / ".codex-review" / "intent-test-home"
    sandbox_tmp = sandbox_home / "tmp"
    sandbox_tmp.mkdir(parents=True, exist_ok=True)
    if sandboxed:
        runtime_cache = "/tmp/pullwise-intent-cache"
    else:
        runtime_cache_path = sandbox_home / "cache"
        runtime_cache_path.mkdir(parents=True, exist_ok=True)
        runtime_cache = str(runtime_cache_path)
    env.update(
        {
            "CI": "true",
            "HOME": "/tmp" if sandboxed else str(sandbox_home),
            "USERPROFILE": "/tmp" if sandboxed else str(sandbox_home),
            "TMPDIR": "/tmp" if sandboxed else str(sandbox_tmp),
            "TMP": "/tmp" if sandboxed else str(sandbox_tmp),
            "TEMP": "/tmp" if sandboxed else str(sandbox_tmp),
            "XDG_CACHE_HOME": runtime_cache,
            "XDG_CONFIG_HOME": f"{runtime_cache}/config",
            "XDG_DATA_HOME": f"{runtime_cache}/data",
            "APPDATA": f"{runtime_cache}/appdata",
            "LOCALAPPDATA": f"{runtime_cache}/localappdata",
            "GOCACHE": f"{runtime_cache}/go-build",
            "GOMODCACHE": f"{runtime_cache}/go-mod",
            "DOTNET_CLI_HOME": f"{runtime_cache}/dotnet-home",
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1",
            "DOTNET_CLI_USE_MSBUILD_SERVER": "0",
            "MSBUILDDISABLENODEREUSE": "1",
            "UseSharedCompilation": "false",
            "NUGET_PACKAGES": f"{runtime_cache}/nuget",
            "CARGO_HOME": f"{runtime_cache}/cargo",
            "NO_PROXY": "*",
            "PYTHONPATH": "/workspace" if sandboxed else str(validation_repo),
            "PULLWISE_INTENT_TEST": "1",
            "PULLWISE_INTENT_TEST_NETWORK_DISABLED": "1",
        }
    )
    return env


def _path_is_lexically_under(path: Path, root: Path) -> bool:
    try:
        Path(os.path.abspath(path)).relative_to(Path(os.path.abspath(root)))
    except ValueError:
        return False
    return True


def _intent_private_runtime_bind_root(command: list[str]) -> tuple[Path | None, str]:
    if not command:
        return None, ""
    executable = Path(command[0])
    if not executable.is_absolute():
        return None, ""
    visible_roots = tuple(
        Path(root)
        for root in ("/usr", "/bin", "/lib", "/lib64", "/opt")
        if Path(root).exists()
    )
    if any(_path_is_lexically_under(executable, root) for root in visible_roots):
        return None, ""
    try:
        is_active_python = executable.resolve(strict=True) == Path(sys.executable).resolve(strict=True)
    except OSError:
        is_active_python = False
    if is_active_python:
        prefix = Path(sys.prefix)
        bind_root = (
            prefix
            if prefix.is_dir() and _path_is_lexically_under(executable, prefix)
            else executable.parent
        )
    elif executable.is_file() and executable.parent.name.lower() in {"bin", "sbin"}:
        bind_root = executable.parent
    else:
        return (
            None,
            "environment_error: absolute generated test executable is outside the sandbox-visible trusted runtime",
        )
    if not bind_root.is_dir():
        return None, "environment_error: trusted runtime directory is unavailable"
    return bind_root, ""


def _append_sandbox_bind(argv: list[str], bind_root: Path) -> None:
    if bind_root.anchor == "/":
        parents: list[Path] = []
        parent = bind_root.parent
        while parent != Path("/"):
            parents.append(parent)
            parent = parent.parent
        for directory in reversed(parents):
            argv.extend(["--dir", str(directory)])
    argv.extend(["--ro-bind", str(bind_root), str(bind_root)])


def _intent_test_sandbox_command(command: list[str], cwd: Path, validation_repo: Path) -> tuple[list[str], str, str]:
    if not sys.platform.startswith("linux"):
        return command, str(cwd), ""
    bwrap = shutil.which("bwrap") or shutil.which("bubblewrap")
    if not bwrap:
        return [], "", "intent test sandbox runner is unavailable: bubblewrap is not installed"
    validation_root = validation_repo.resolve(strict=False)
    cwd_resolved = cwd.resolve(strict=False)
    try:
        rel_cwd = cwd_resolved.relative_to(validation_root)
    except ValueError:
        return [], "", "generated test cwd escapes validation workspace"
    sandbox_cwd = "/workspace" if rel_cwd == Path(".") else "/workspace/" + rel_cwd.as_posix()
    sandboxed_command: list[str] = []
    for argument in command:
        argument_path = Path(argument)
        if argument_path.is_absolute() and path_is_under(argument_path, validation_root):
            relative_argument = argument_path.resolve(strict=False).relative_to(validation_root)
            sandboxed_command.append(
                "/workspace" if relative_argument == Path(".") else "/workspace/" + relative_argument.as_posix()
            )
        else:
            sandboxed_command.append(argument)
    argv = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--bind",
        str(validation_root),
        "/workspace",
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--chdir",
        sandbox_cwd,
    ]
    system_bind_roots = tuple(
        Path(host_path)
        for host_path in ("/usr", "/bin", "/lib", "/lib64", "/opt")
        if Path(host_path).exists()
    )
    for host_path in system_bind_roots:
        argv.extend(["--ro-bind", str(host_path), str(host_path)])
    rustup_home = _intent_host_rustup_home() if _intent_is_rust_command(command) else None
    if rustup_home is not None:
        if (
            not path_is_under(rustup_home, validation_root)
            and not any(path_is_under(rustup_home, system_root) for system_root in system_bind_roots)
        ):
            _append_sandbox_bind(argv, rustup_home)
    if command and not Path(command[0]).is_absolute():
        resolved_runtime = shutil.which(command[0])
        resolved_runtime_path = Path(resolved_runtime) if resolved_runtime else None
        if (
            resolved_runtime_path is not None
            and resolved_runtime_path.is_absolute()
            and resolved_runtime_path.is_file()
            and resolved_runtime_path.parent.name.lower() in {"bin", "sbin"}
            and not path_is_under(resolved_runtime_path, validation_root)
            and not any(
                path_is_under(resolved_runtime_path, system_root)
                for system_root in system_bind_roots
            )
        ):
            _append_sandbox_bind(argv, resolved_runtime_path.parent)
    executable_path = Path(command[0]) if command else Path()
    contained_executable = executable_path.is_absolute() and path_is_under(executable_path, validation_root)
    runtime_bind_root, runtime_bind_error = (
        (None, "")
        if contained_executable
        else _intent_private_runtime_bind_root(command)
    )
    if runtime_bind_error:
        return [], "", runtime_bind_error
    if runtime_bind_root is not None:
        _append_sandbox_bind(argv, runtime_bind_root)
    argv.extend(["--", *sandboxed_command])
    return argv, sandbox_cwd, ""


def _intent_sandbox_setup_failed(command: list[str], completed: subprocess.CompletedProcess[str]) -> bool:
    executable = Path(command[0]).name.lower() if command else ""
    if executable not in {"bwrap", "bubblewrap"}:
        return False
    stderr_lines = [
        line.strip().lower()
        for line in str(completed.stderr or "").splitlines()
        if line.strip()
    ]
    return completed.returncode != 0 and any(
        line.startswith(("bwrap:", "bubblewrap:"))
        for line in stderr_lines
    )


def _generated_intent_test_path(generated: dict[str, Any]) -> str:
    return str(
        generated.get("path")
        or generated.get("artifact_path")
        or generated.get("artifactPath")
        or generated.get("test_file")
        or generated.get("filename")
        or ""
    ).strip()


def _authorized_generated_intent_test_source(
    run_dir: Path,
    validation_repo: Path,
    raw_path: str,
) -> tuple[Path, Path] | None:
    if not raw_path or validation_repo.is_symlink():
        return None
    declared_path = Path(raw_path)
    source_repo = _source_repository_for_run_dir(run_dir)
    expected_validation_repo = source_repo.parent / "validation-repo"
    if validation_repo.resolve(strict=False) != expected_validation_repo.resolve(strict=False):
        return None
    source_candidates = (
        (
            declared_path if declared_path.is_absolute() else source_repo / declared_path,
            source_repo,
            source_repo / ".codex-review" / "generated-tests",
        ),
        (
            declared_path if declared_path.is_absolute() else run_dir / declared_path,
            run_dir,
            run_dir / "intent" / "generated-tests",
        ),
    )
    for source_candidate, relative_root, permitted_root in source_candidates:
        if (
            permitted_root.is_symlink()
            or not path_is_under(permitted_root, relative_root)
            or source_candidate.is_symlink()
            or not _is_regular_file_no_follow(source_candidate)
            or not path_is_under(source_candidate, permitted_root)
        ):
            continue
        try:
            relative_path = source_candidate.resolve(strict=True).relative_to(
                relative_root.resolve(strict=True)
            )
        except (OSError, ValueError):
            continue
        destination = validation_repo / relative_path
        if path_is_under(destination, validation_repo):
            return source_candidate, relative_path
    return None


def _regular_file_contents_match(left: Path, right: Path) -> bool:
    if (
        left.is_symlink()
        or right.is_symlink()
        or not _is_regular_file_no_follow(left)
        or not _is_regular_file_no_follow(right)
    ):
        return False
    try:
        if left.stat(follow_symlinks=False).st_size != right.stat(follow_symlinks=False).st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(1024 * 1024)
                right_chunk = right_handle.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
    except OSError:
        return False


def materialize_generated_intent_test_sources(
    run_dir: Path,
    validation_repo: Path | None,
    validation: dict[str, Any],
    source: dict[str, Any],
    *,
    materialized_paths: dict[str, str] | None = None,
) -> dict[str, str]:
    generated_tests = source.get("generated_tests") if isinstance(source.get("generated_tests"), list) else []
    if validation_repo is None:
        return {}
    canonical_source_root = _source_repository_for_run_dir(run_dir)
    canonical_validation_root = canonical_source_root.parent / "validation-repo"
    validation_root_value = str(validation.get("validation_repo_root") or "").strip()
    declared_validation_root = Path(validation_root_value) if validation_root_value else None
    validation_root_matches = (
        declared_validation_root is not None
        and declared_validation_root.resolve(strict=False) == canonical_validation_root.resolve(strict=False)
        and validation_repo.resolve(strict=False) == canonical_validation_root.resolve(strict=False)
        and not validation_repo.is_symlink()
    )
    source_root_value = str(validation.get("source_repo_root") or "").strip()
    declared_source_root = Path(source_root_value) if source_root_value else None
    source_root_matches = (
        declared_source_root is not None
        and declared_source_root.resolve(strict=False) == canonical_source_root.resolve(strict=False)
    )
    source_generation_root = canonical_source_root / ".codex-review" / "generated-tests"
    errors: dict[str, str] = {}
    for index, generated in enumerate(generated_tests):
        if not isinstance(generated, dict):
            continue
        test_id = _intent_test_id(generated, f"ITV-{index + 1:03d}")
        raw_path = _generated_intent_test_path(generated)
        if not raw_path:
            continue
        if not validation_root_matches:
            errors[test_id] = "validation workspace destination differs from the worker-owned path"
            continue
        if not source_root_matches:
            errors[test_id] = "validation workspace source repository differs from the worker-owned path"
            continue
        declared_path = Path(raw_path)
        validation_candidate = declared_path if declared_path.is_absolute() else validation_repo / declared_path
        source_kind = str(generated.get("source_kind") or generated.get("sourceKind") or "").strip().lower()
        reuse_existing = generated.get("reuse_existing") is True or source_kind in {
            "existing",
            "existing_test",
            "repository_test",
        }
        try:
            validation_relative = validation_candidate.resolve(strict=False).relative_to(
                validation_repo.resolve(strict=False)
            )
        except ValueError:
            validation_relative = None
        original_candidate = (
            canonical_source_root / validation_relative
            if validation_relative is not None
            else None
        )
        existing_repo_file = (
            original_candidate is not None
            and not original_candidate.is_symlink()
            and _is_regular_file_no_follow(original_candidate)
            and path_is_under(original_candidate, canonical_source_root)
            and not path_is_under(original_candidate, source_generation_root)
        )
        if existing_repo_file:
            if not reuse_existing:
                errors[test_id] = "generated test path overlaps an existing repository file"
                continue
            if not _regular_file_contents_match(validation_candidate, original_candidate):
                errors[test_id] = "explicitly reused existing repository test differs from source"
                continue
            if materialized_paths is not None:
                materialized_paths[test_id] = validation_relative.as_posix()
            continue
        authorized_source = _authorized_generated_intent_test_source(
            run_dir,
            validation_repo,
            raw_path,
        )
        if authorized_source is None:
            errors[test_id] = "generated test source is missing or outside the worker-owned generated-test roots"
            continue
        source_candidate, relative_path = authorized_source
        destination = validation_repo / relative_path
        if not path_is_under(destination, validation_repo):
            errors[test_id] = "generated test destination escapes the validation workspace"
            continue
        destination_exists = destination.exists() or destination.is_symlink()
        if destination.is_symlink() or (
            destination_exists and not _is_regular_file_no_follow(destination)
        ):
            errors[test_id] = "generated test destination is not a worker-owned regular file"
            continue
        try:
            source_output = _bounded_regular_model_file(source_candidate)
            destination_executable = bool(
                destination_exists
                and stat.S_IMODE(destination.lstat().st_mode) & 0o111
            )
            if (
                not destination_exists
                or not _regular_file_contents_match(source_candidate, destination)
                or destination_executable != source_output.executable
            ):
                _write_worker_owned_bytes(
                    destination,
                    source_output.payload,
                    executable=source_output.executable,
                )
            if materialized_paths is not None:
                materialized_paths[test_id] = relative_path.as_posix()
        except OSError as exc:
            errors[test_id] = f"generated test source could not be materialized: {exc}"
    return errors


def _declared_generated_test_paths(run_dir: Path, validation_repo: Path) -> set[str]:
    source = read_json(run_dir / "intent" / "intent-test-source.json", {})
    generated_tests = (
        source.get("generated_tests")
        if isinstance(source, dict) and isinstance(source.get("generated_tests"), list)
        else []
    )
    allowed: set[str] = set()
    for generated in generated_tests:
        if not isinstance(generated, dict):
            continue
        authorized_source = _authorized_generated_intent_test_source(
            run_dir,
            validation_repo,
            _generated_intent_test_path(generated),
        )
        if authorized_source is None:
            continue
        source_candidate, relative_path = authorized_source
        destination = validation_repo / relative_path
        if _regular_file_contents_match(source_candidate, destination):
            allowed.add(relative_path.as_posix())
    return allowed


def intent_validation_workspace_integrity_payload(run_dir: Path) -> dict[str, Any]:
    violations: list[dict[str, str]] = []
    source_repo = _source_repository_for_run_dir(run_dir)
    expected_validation_repo = source_repo.parent / "validation-repo"
    validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
    validation_root = str(validation.get("validation_repo_root") or "").strip() if isinstance(validation, dict) else ""
    if not validation_root:
        violations.append({"path": "", "reason": "validation workspace metadata is missing"})
    else:
        declared_validation_repo = Path(validation_root)
        if declared_validation_repo.resolve(strict=False) != expected_validation_repo.resolve(strict=False):
            violations.append({"path": validation_root, "reason": "validation workspace root differs from the worker-owned path"})
    source_root = str(validation.get("source_repo_root") or "").strip() if isinstance(validation, dict) else ""
    if not source_root:
        violations.append({"path": "", "reason": "validation workspace source repository metadata is missing"})
    elif Path(source_root).resolve(strict=False) != source_repo.resolve(strict=False):
        violations.append({"path": source_root, "reason": "validation workspace source repository differs from the worker-owned path"})

    baseline_path = immutable_inventory_baseline_path(run_dir)
    baseline_payload = read_json(baseline_path, {}) if not baseline_path.is_symlink() else {}
    inventory_files = baseline_payload.get("files") if _valid_inventory_payload(baseline_payload) else []
    if not inventory_files and not _valid_inventory_payload(baseline_payload):
        violations.append({"path": str(baseline_path), "reason": "immutable inventory baseline is missing or invalid"})

    validation_repo = expected_validation_repo
    current_files: list[dict[str, Any]] = []
    if validation_repo.is_symlink() or not validation_repo.is_dir():
        violations.append({"path": str(validation_repo), "reason": "validation workspace is missing or is not a real directory"})
    else:
        try:
            current_payload = inventory(validation_repo)
            current_files = (
                current_payload.get("files")
                if isinstance(current_payload, dict) and isinstance(current_payload.get("files"), list)
                else []
            )
        except Exception as exc:
            violations.append({"path": str(validation_repo), "reason": f"validation workspace could not be inventoried: {exc}"})

    current_by_path = {
        str(item.get("path") or "").strip(): item
        for item in current_files
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }
    baseline_paths: set[str] = set()
    for item in inventory_files:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("path") or "").strip()
        expected_sha = str(item.get("sha256") or "").strip().lower()
        if not relative or not expected_sha:
            violations.append({"path": relative, "reason": "immutable inventory entry is missing path or sha256"})
            continue
        baseline_paths.add(relative)
        candidate = validation_repo / relative
        if not path_is_under(candidate, validation_repo):
            violations.append({"path": relative, "reason": "immutable inventory path escapes validation workspace"})
            continue
        current_item = current_by_path.get(relative)
        if current_item is None or candidate.is_symlink() or not _is_regular_file_no_follow(candidate):
            violations.append({"path": relative, "reason": "repository file is missing or no longer regular"})
            continue
        actual_sha = str(current_item.get("sha256") or "").strip().lower()
        if actual_sha != expected_sha:
            violations.append({"path": relative, "reason": "repository file content differs from immutable inventory"})

    allowed_generated_paths = _declared_generated_test_paths(run_dir, validation_repo)
    unexpected_source_files = 0
    for relative, item in sorted(current_by_path.items()):
        if relative in baseline_paths or relative in allowed_generated_paths:
            continue
        if item.get("is_source_like") is not True:
            continue
        unexpected_source_files += 1
        violations.append({"path": relative, "reason": "undeclared source file was added to validation workspace"})

    status = "violation" if violations else "ok"
    return {
        "schema_version": "intent-validation-workspace-integrity/v1",
        "status": status,
        "validation_repo_root": str(validation_repo),
        "checked_files": len(inventory_files),
        "violations": violations,
        "summary": {
            "checked_files": len(inventory_files),
            "unexpected_source_files": unexpected_source_files,
            "violations": len(violations),
        },
    }


def _intent_result_with_post_execution_integrity(
    run_dir: Path,
    result: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    integrity = intent_validation_workspace_integrity_payload(run_dir)
    write_json(run_dir / "intent" / "validation-workspace-integrity.json", integrity)
    violations = integrity.get("violations") if isinstance(integrity.get("violations"), list) else []
    if not violations:
        return result, False
    diagnostic = _intent_blocked_execution_diagnostic(
        "validation workspace repository files differ from the immutable inventory after test execution",
        reason_code="validation_workspace_modified",
        classification="environment_error",
        agent_repairable=False,
    )
    return (
        {
            **result,
            "status": "error",
            "classification": diagnostic["classification"],
            "preflight": diagnostic,
            "skip_reason": diagnostic["reason"],
            "workspace_integrity": integrity,
        },
        True,
    )


def _intent_raw_required_paths(
    generated: dict[str, Any],
    target: dict[str, Any],
) -> list[str]:
    for source in (generated, target):
        for key in ("required_paths", "requiredPaths"):
            if key not in source:
                continue
            raw_paths = source.get(key)
            values = raw_paths if isinstance(raw_paths, list) else [raw_paths]
            return [
                str(value).strip()
                for value in values
                if str(value or "").strip()
            ]
    return []


def _intent_required_paths_preflight(
    generated: dict[str, Any],
    target: dict[str, Any],
    *,
    cwd: Path,
    validation_repo: Path,
) -> tuple[list[str], dict[str, Any] | None]:
    canonical_paths: list[str] = []
    escaped_paths: list[str] = []
    missing_paths: list[str] = []
    for raw_path in _intent_raw_required_paths(generated, target):
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        candidate = candidate.resolve(strict=False)
        canonical = str(candidate)
        if canonical not in canonical_paths:
            canonical_paths.append(canonical)
        if not path_is_under(candidate, validation_repo):
            escaped_paths.append(raw_path)
        elif not candidate.exists():
            missing_paths.append(raw_path)
    if escaped_paths:
        return canonical_paths, _intent_blocked_execution_diagnostic(
            "generated test required path escapes the validation workspace",
            reason_code="required_path_escape",
            classification="environment_error",
            agent_repairable=False,
            missing_capabilities=escaped_paths,
        )
    if missing_paths:
        return canonical_paths, _intent_blocked_execution_diagnostic(
            "generated test required path is missing",
            reason_code="required_path_missing",
            missing_capabilities=missing_paths,
        )
    return canonical_paths, None


def intent_test_source_preflight_payload(run_dir: Path) -> dict[str, Any]:
    validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
    validation_root = str(validation.get("validation_repo_root") or "").strip() if isinstance(validation, dict) else ""
    validation_repo = Path(validation_root) if validation_root else None
    plan = read_json(run_dir / "intent" / "intent-test-plan.json", {})
    targets = plan.get("test_targets") if isinstance(plan, dict) and isinstance(plan.get("test_targets"), list) else []
    source = read_json(run_dir / "intent" / "intent-test-source.json", {})
    generated_tests = source.get("generated_tests") if isinstance(source, dict) and isinstance(source.get("generated_tests"), list) else []
    profile = read_json(run_dir / "repo-profile.json", {})
    target_by_id = {
        _intent_test_id(target, f"ITP-{index + 1:03d}"): target
        for index, target in enumerate(targets)
        if isinstance(target, dict)
    }
    materialized_paths: dict[str, str] = {}
    materialization_errors = materialize_generated_intent_test_sources(
        run_dir,
        validation_repo,
        validation if isinstance(validation, dict) else {},
        source if isinstance(source, dict) else {},
        materialized_paths=materialized_paths,
    )
    integrity = intent_validation_workspace_integrity_payload(run_dir)
    write_json(run_dir / "intent" / "validation-workspace-integrity.json", integrity)
    integrity_violations = integrity.get("violations") if isinstance(integrity.get("violations"), list) else []
    records = _intent_generated_execution_records(generated_tests)
    tests: list[dict[str, Any]] = []
    for index, generated in enumerate(records):
        test_id = str(generated.get("_source_test_id") or "").strip() or _intent_test_id(generated, f"ITV-{index + 1:03d}")
        related_ids = _intent_related_test_ids(generated)
        target = target_by_id.get(test_id)
        if not isinstance(target, dict):
            target = next((target_by_id[item] for item in related_ids if item in target_by_id), {})
        command = _intent_generated_command(
            generated,
            target,
            validation_repo,
            source=source if isinstance(source, dict) else {},
            generated_index=index,
            generated_total=len(records),
        )
        command = _intent_normalized_execution_command(
            command,
            declared_path=_intent_source_path_from_entry(generated),
            materialized_path=materialized_paths.get(test_id, ""),
        )
        cwd = _intent_test_cwd(validation_repo, generated, target) if validation_repo is not None else None
        required_paths: list[str] = []
        if integrity_violations:
            diagnostic = _intent_blocked_execution_diagnostic(
                "validation workspace repository files differ from the immutable inventory",
                reason_code="validation_workspace_modified",
                classification="environment_error",
                agent_repairable=False,
            )
        elif validation_repo is None:
            diagnostic = _intent_blocked_execution_diagnostic(
                "validation workspace was not prepared",
                reason_code="validation_workspace_missing",
                classification="environment_error",
                agent_repairable=False,
            )
        elif materialization_errors.get(test_id):
            collision = "overlaps an existing repository file" in materialization_errors[test_id]
            diagnostic = _intent_blocked_execution_diagnostic(
                materialization_errors[test_id],
                reason_code=(
                    "generated_test_overwrites_repository_file"
                    if collision
                    else "generated_test_materialization_failed"
                ),
                classification="test_harness_error",
                agent_repairable=not collision,
            )
        elif cwd is None:
            diagnostic = _intent_blocked_execution_diagnostic(
                "generated test cwd escapes validation workspace",
                reason_code="cwd_escape",
            )
        else:
            required_paths, required_paths_diagnostic = _intent_required_paths_preflight(
                generated,
                target,
                cwd=cwd,
                validation_repo=validation_repo,
            )
            if required_paths_diagnostic is not None:
                diagnostic = required_paths_diagnostic
            else:
                diagnostic = intent_execution_preflight(
                    command,
                    cwd,
                    validation_repo,
                    profile if isinstance(profile, dict) else {},
                )
                compile_error = _intent_generated_python_compile_error(validation_repo, generated, target)
                if compile_error:
                    diagnostic = _intent_blocked_execution_diagnostic(
                        compile_error,
                        reason_code="generated_test_invalid",
                        classification="test_harness_error",
                    )
        tests.append(
            {
                "test_id": test_id,
                "target_test_ids": related_ids,
                "command": command,
                "cwd": str(cwd) if cwd is not None else "",
                "required_paths": required_paths,
                **diagnostic,
            }
        )
    ready = sum(1 for item in tests if item.get("status") == "ready")
    repairable = sum(1 for item in tests if item.get("status") == "blocked" and item.get("agent_repairable") is True)
    return {
        "schema_version": "intent-test-preflight/v1",
        "run_id": run_dir.name,
        "tests": tests,
        "workspace_integrity": integrity,
        "skip_reason": _intent_skip_reason_from_payload(source),
        "summary": {
            "total": len(tests),
            "ready": ready,
            "blocked": len(tests) - ready,
            "agent_repairable": repairable,
        },
    }


def bounded_process_output(path: Path, *, max_bytes: int = 64 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError:
        return ""
    if len(data) > max_bytes:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace")


def run_polled_intent_process(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
    deadline_monotonic: float | None,
    cancel_requested: Callable[[], bool] | None,
) -> subprocess.CompletedProcess[str]:
    check_lifecycle_cancelled(cancel_requested)
    remaining_wall_time_seconds(deadline_monotonic)
    timeout_deadline = time.monotonic() + max(1, int(timeout_seconds))
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
        process = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=env,
            stdout=stdout_file,
            stderr=stderr_file,
        )
        poll_process_communicate(
            process,
            args,
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
            timeout_deadline_monotonic=timeout_deadline,
        )
    return subprocess.CompletedProcess(
        args=args,
        returncode=int(process.returncode or 0),
        stdout=bounded_process_output(stdout_path),
        stderr=bounded_process_output(stderr_path),
    )


def _approved_intent_preflight_test(
    payload: object,
    test_id: str,
) -> dict[str, Any] | None:
    tests = payload.get("tests") if isinstance(payload, dict) else None
    if not isinstance(tests, list):
        return None
    matches = [
        item
        for item in tests
        if isinstance(item, dict)
        and str(item.get("test_id") or "").strip() == test_id
    ]
    return matches[0] if len(matches) == 1 else None


def _intent_preflight_candidate_matches(
    approved: dict[str, Any] | None,
    *,
    command: list[str],
    cwd: Path,
    required_paths: list[str],
) -> bool:
    if approved is None:
        return False
    approved_command = approved.get("command")
    approved_required_paths = approved.get("required_paths", [])
    return (
        isinstance(approved_command, list)
        and [str(part) for part in approved_command] == command
        and str(approved.get("cwd") or "") == str(cwd)
        and isinstance(approved_required_paths, list)
        and [str(path) for path in approved_required_paths] == required_paths
    )


def run_intent_tests(
    run_dir: Path,
    *,
    only_test_ids: set[str] | None = None,
    attempt: int = 1,
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    check_lifecycle_cancelled(cancel_requested)
    remaining_wall_time_seconds(deadline_monotonic)
    ensure_intent_directories(run_dir)
    validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
    validation_root = str(validation.get("validation_repo_root") or "").strip() if isinstance(validation, dict) else ""
    validation_repo = Path(validation_root) if validation_root else None
    config = read_json(run_dir / "intent" / "intent-test-validation.json", {})
    profile = read_json(run_dir / "repo-profile.json", {})
    profile = profile if isinstance(profile, dict) and profile.get("schema_version") == "repo-profile/v1" else {}
    if isinstance(config, dict) and config.get("enabled") is False:
        return {"schema_version": "intent-test-run-results/v1", "run_id": run_dir.name, "test_runs": []}
    plan = read_json(run_dir / "intent" / "intent-test-plan.json", {})
    targets = plan.get("test_targets") if isinstance(plan.get("test_targets"), list) else []
    source = read_json(run_dir / "intent" / "intent-test-source.json", {})
    generated_tests = source.get("generated_tests") if isinstance(source.get("generated_tests"), list) else []
    approved_preflight_path = run_dir / "intent" / "intent-test-preflight.json"
    if approved_preflight_path.is_symlink():
        approved_preflight = {}
    elif approved_preflight_path.is_file():
        approved_preflight = read_json(approved_preflight_path, {})
    elif approved_preflight_path.exists():
        approved_preflight = {}
    else:
        approved_preflight = intent_test_source_preflight_payload(run_dir)
        write_json(approved_preflight_path, approved_preflight)
    materialized_paths: dict[str, str] = {}
    materialization_errors = materialize_generated_intent_test_sources(
        run_dir,
        validation_repo,
        validation if isinstance(validation, dict) else {},
        source if isinstance(source, dict) else {},
        materialized_paths=materialized_paths,
    )
    integrity = intent_validation_workspace_integrity_payload(run_dir)
    write_json(run_dir / "intent" / "validation-workspace-integrity.json", integrity)
    integrity_violations = integrity.get("violations") if isinstance(integrity.get("violations"), list) else []
    target_by_id = {
        _intent_test_id(target, f"ITV-{index + 1:03d}"): target
        for index, target in enumerate(targets)
        if isinstance(target, dict)
    }
    generated_records = _intent_generated_execution_records(generated_tests)
    execution_records: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    if generated_records:
        for index, generated in enumerate(generated_records):
            test_id = str(generated.get("_source_test_id") or "").strip() or _intent_test_id(
                generated,
                f"ITV-{index + 1:03d}",
            )
            related_ids = _intent_related_test_ids(generated)
            target = target_by_id.get(test_id)
            if not isinstance(target, dict):
                target = next(
                    (target_by_id[target_id] for target_id in related_ids if target_id in target_by_id),
                    {},
                )
            execution_records.append((test_id, generated, target))
    else:
        for index, target in enumerate(targets):
            if not isinstance(target, dict):
                continue
            test_id = _intent_test_id(target, f"ITV-{index + 1:03d}")
            execution_records.append((test_id, {}, target))
    max_tests = max(0, int((config if isinstance(config, dict) else {}).get("max_tests_per_run") or 20))
    intent_deadline = time.monotonic() + max(
        0,
        int(
            (config if isinstance(config, dict) else {}).get(
                "max_total_test_run_seconds"
            )
            or 900
        ),
    )
    total_deadline = (
        min(float(deadline_monotonic), intent_deadline)
        if deadline_monotonic is not None
        else intent_deadline
    )
    raw_results = []
    selected_records = [
        record
        for record in execution_records
        if only_test_ids is None
        or record[0] in only_test_ids
        or bool(set(_intent_related_test_ids(record[1])).intersection(only_test_ids))
    ]
    limited_records = selected_records[:max_tests]
    for generated_index, (test_id, generated, target) in enumerate(limited_records):
        check_lifecycle_cancelled(cancel_requested)
        remaining_wall_time_seconds(deadline_monotonic)
        command = _intent_generated_command(
            generated,
            target,
            validation_repo,
            source=source,
            generated_index=generated_index,
            generated_total=len(execution_records),
        )
        command = _intent_normalized_execution_command(
            command,
            declared_path=_intent_source_path_from_entry(generated),
            materialized_path=materialized_paths.get(test_id, ""),
        )
        base_result = {"schema_version": "project-test-run/v1", "test_id": test_id, "attempt": max(1, int(attempt))}
        related_ids = _intent_related_test_ids(generated)
        if related_ids:
            base_result["target_test_ids"] = related_ids
        if integrity_violations:
            preflight = _intent_blocked_execution_diagnostic(
                "validation workspace repository files differ from the immutable inventory",
                reason_code="validation_workspace_modified",
                classification="environment_error",
                agent_repairable=False,
            )
            raw_results.append(
                {
                    **base_result,
                    "status": "skipped",
                    "classification": preflight["classification"],
                    "preflight": preflight,
                    "exit_code": None,
                    "duration_ms": 0,
                    "timed_out": False,
                    "skip_reason": preflight["reason"],
                }
            )
            continue
        if not validation_repo:
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": "validation workspace was not prepared"})
            continue
        if materialization_errors.get(test_id):
            collision = "overlaps an existing repository file" in materialization_errors[test_id]
            preflight = _intent_blocked_execution_diagnostic(
                materialization_errors[test_id],
                reason_code=(
                    "generated_test_overwrites_repository_file"
                    if collision
                    else "generated_test_materialization_failed"
                ),
                classification="test_harness_error",
                agent_repairable=not collision,
            )
            raw_results.append(
                {
                    **base_result,
                    "status": "skipped",
                    "classification": preflight["classification"],
                    "preflight": preflight,
                    "exit_code": None,
                    "duration_ms": 0,
                    "timed_out": False,
                    "skip_reason": preflight["reason"],
                }
            )
            continue
        source_skip_reason, source_skip_classification = _intent_source_execution_skip(run_dir)
        if source_skip_reason:
            skipped_result = {
                **base_result,
                "status": "skipped",
                "classification": source_skip_classification,
                "exit_code": None,
                "duration_ms": 0,
                "timed_out": False,
                "skip_reason": source_skip_reason,
            }
            if command:
                skipped_result["command"] = " ".join(shlex.quote(part) for part in command)
            raw_results.append(skipped_result)
            continue
        if not command:
            preflight = _intent_blocked_execution_diagnostic(
                "no generated test command was produced",
                reason_code="command_missing",
            )
            raw_results.append({**base_result, "status": "skipped", "classification": preflight["classification"], "preflight": preflight, "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": preflight["reason"]})
            continue
        cwd = _intent_test_cwd(validation_repo, generated, target)
        if cwd is None:
            preflight = _intent_blocked_execution_diagnostic("generated test cwd escapes validation workspace", reason_code="cwd_escape")
            raw_results.append({**base_result, "status": "skipped", "classification": preflight["classification"], "preflight": preflight, "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": preflight["reason"]})
            continue
        required_paths, required_paths_diagnostic = _intent_required_paths_preflight(
            generated,
            target,
            cwd=cwd,
            validation_repo=validation_repo,
        )
        base_result["required_paths"] = required_paths
        approved_test = _approved_intent_preflight_test(approved_preflight, test_id)
        if not _intent_preflight_candidate_matches(
            approved_test,
            command=command,
            cwd=cwd,
            required_paths=required_paths,
        ):
            preflight = _intent_blocked_execution_diagnostic(
                "generated test command, cwd, or required paths differ from the approved preflight candidate",
                reason_code="preflight_candidate_mismatch",
                classification="environment_error",
                agent_repairable=False,
            )
            base_result["preflight"] = preflight
            raw_results.append(
                {
                    **base_result,
                    "status": "skipped",
                    "classification": preflight["classification"],
                    "exit_code": None,
                    "duration_ms": 0,
                    "timed_out": False,
                    "skip_reason": preflight["reason"],
                }
            )
            continue
        if required_paths_diagnostic is not None:
            preflight = required_paths_diagnostic
            base_result["preflight"] = preflight
            raw_results.append(
                {
                    **base_result,
                    "status": "skipped",
                    "classification": preflight["classification"],
                    "exit_code": None,
                    "duration_ms": 0,
                    "timed_out": False,
                    "skip_reason": preflight["reason"],
                }
            )
            continue
        preflight = intent_execution_preflight(command, cwd, validation_repo, profile)
        base_result["preflight"] = preflight
        if preflight["status"] != "ready":
            raw_results.append({**base_result, "status": "skipped", "classification": preflight["classification"], "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": preflight["reason"]})
            continue
        compile_error = _intent_generated_python_compile_error(validation_repo, generated, target)
        if compile_error:
            preflight = _intent_blocked_execution_diagnostic(
                compile_error,
                reason_code="generated_test_invalid",
                classification="test_harness_error",
            )
            base_result["preflight"] = preflight
            stderr_path = _intent_output_path(run_dir, test_id, "stderr", attempt=attempt)
            stderr_path.write_text(compile_error, encoding="utf-8")
            raw_results.append(
                {
                    **base_result,
                    "status": "skipped",
                    "classification": "test_harness_error",
                    "command": " ".join(shlex.quote(part) for part in command),
                    "exit_code": None,
                    "duration_ms": 0,
                    "timed_out": False,
                    "stderr_path": str(stderr_path),
                    "skip_reason": compile_error,
                }
            )
            continue
        remaining_total = remaining_wall_time_seconds(total_deadline)
        if remaining_total is None:
            raise AssertionError("intent test deadline must be bounded")
        if remaining_total < 1:
            raise JobPartialCompleted("review wall-time deadline exceeded")
        timeout_seconds = min(_intent_test_timeout(config if isinstance(config, dict) else {}, generated, target), max(1, int(remaining_total)))
        started = time.monotonic()
        stdout_path = _intent_output_path(run_dir, test_id, "stdout", attempt=attempt)
        stderr_path = _intent_output_path(run_dir, test_id, "stderr", attempt=attempt)
        sandbox_command, sandbox_cwd, sandbox_skip_reason = _intent_test_sandbox_command(command, cwd, validation_repo)
        if sandbox_skip_reason:
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": sandbox_skip_reason})
            continue
        try:
            completed = run_polled_intent_process(
                sandbox_command,
                cwd=cwd,
                env=_intent_test_env(
                    validation_repo,
                    sandboxed=sys.platform.startswith("linux"),
                    command=command,
                ),
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout_seconds=timeout_seconds,
                deadline_monotonic=total_deadline,
                cancel_requested=cancel_requested,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            if _intent_sandbox_setup_failed(sandbox_command, completed):
                raw_results.append(
                    {
                        **base_result,
                        "status": "skipped",
                        "classification": "environment_error",
                        "command": " ".join(shlex.quote(part) for part in command),
                        "sandbox_command": " ".join(shlex.quote(part) for part in sandbox_command),
                        "cwd": sandbox_cwd or str(cwd),
                        "exit_code": int(completed.returncode),
                        "duration_ms": duration_ms,
                        "timed_out": False,
                        "stdout_path": str(stdout_path),
                        "stderr_path": str(stderr_path),
                        "skip_reason": "intent test sandbox runner failed to initialize; unsandboxed fallback is prohibited",
                    }
                )
                continue
            completed_result, integrity_failed = _intent_result_with_post_execution_integrity(
                run_dir,
                {
                    **base_result,
                    "status": "passed" if completed.returncode == 0 else "failed",
                    "command": " ".join(shlex.quote(part) for part in command),
                    "sandbox_command": " ".join(shlex.quote(part) for part in sandbox_command),
                    "cwd": sandbox_cwd or str(cwd),
                    "exit_code": int(completed.returncode),
                    "duration_ms": duration_ms,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "timed_out": False,
                },
            )
            raw_results.append(completed_result)
            if integrity_failed:
                break
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            if not stdout_path.exists():
                stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
            if not stderr_path.exists():
                stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
            timeout_result, integrity_failed = _intent_result_with_post_execution_integrity(
                run_dir,
                {
                    **base_result,
                    "status": "timeout",
                    "command": " ".join(shlex.quote(part) for part in command),
                    "sandbox_command": " ".join(shlex.quote(part) for part in sandbox_command),
                    "cwd": sandbox_cwd or str(cwd),
                    "exit_code": None,
                    "duration_ms": duration_ms,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "timed_out": True,
                },
            )
            raw_results.append(timeout_result)
            if integrity_failed:
                break
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(str(exc), encoding="utf-8")
            error_result, integrity_failed = _intent_result_with_post_execution_integrity(
                run_dir,
                {
                    **base_result,
                    "status": "error",
                    "command": " ".join(shlex.quote(part) for part in command),
                    "sandbox_command": " ".join(shlex.quote(part) for part in sandbox_command),
                    "cwd": sandbox_cwd or str(cwd),
                    "exit_code": None,
                    "duration_ms": duration_ms,
                    "stdout_path": str(stdout_path),
                    "stderr_path": str(stderr_path),
                    "timed_out": False,
                    "error": str(exc),
                },
            )
            raw_results.append(error_result)
            if integrity_failed:
                break
    return {"schema_version": "intent-test-run-results/v1", "run_id": run_dir.name, "test_runs": raw_results}


def _intent_runtime_diagnostic_text(raw_result: dict[str, Any]) -> str:
    parts = [
        str(raw_result.get(key) or "")
        for key in ("error", "skip_reason", "stderr", "stdout")
        if str(raw_result.get(key) or "").strip()
    ]
    for key in ("stderr_path", "stdout_path"):
        raw_path = str(raw_result.get(key) or "").strip()
        if not raw_path:
            continue
        try:
            parts.append(Path(raw_path).read_text(encoding="utf-8", errors="replace")[:8192])
        except OSError:
            continue
    return "\n".join(parts)


def intent_runtime_repair_diagnostics(raw_payload: Any) -> dict[str, Any]:
    raw_runs = raw_payload.get("test_runs") if isinstance(raw_payload, dict) else []
    raw_runs = raw_runs if isinstance(raw_runs, list) else []
    repair_candidates: list[dict[str, Any]] = []
    non_repairable: list[dict[str, Any]] = []
    dependency_markers = (
        "cannot find module",
        "cannot find package",
        "err_module_not_found",
        "package not found",
        "command not found",
    )
    harness_markers = (
        "syntaxerror",
        "importerror",
        "no such file or directory",
        "enoent",
        "no tests found",
        "unknown option",
        "unrecognized option",
        "collection error",
        "could not import",
    )
    for index, raw_run in enumerate(raw_runs):
        if not isinstance(raw_run, dict):
            continue
        test_id = str(raw_run.get("test_id") or f"ITV-{index + 1:03d}").strip()
        status = str(raw_run.get("status") or "").strip().lower()
        text = _intent_runtime_diagnostic_text(raw_run)
        lowered = text.lower()
        python_module_missing = re.search(
            r"(?im)^\s*(?:E\s+)?ModuleNotFoundError:\s*"
            r"No module named\s+\S+\s*$",
            text,
        ) is not None
        preflight = raw_run.get("preflight") if isinstance(raw_run.get("preflight"), dict) else {}
        diagnostic = {
            "test_id": test_id,
            "status": status,
            "exit_code": raw_run.get("exit_code"),
            "summary": text[:2000],
        }
        if status == "passed":
            continue
        if preflight.get("status") == "blocked" and preflight.get("agent_repairable") is False:
            non_repairable.append(
                {
                    **diagnostic,
                    "reason_code": str(preflight.get("reason_code") or "non_repairable_preflight"),
                    "classification": str(preflight.get("classification") or "environment_error"),
                }
            )
        elif preflight.get("agent_repairable") is True:
            repair_candidates.append(
                {
                    **diagnostic,
                    "reason_code": str(preflight.get("reason_code") or "preflight_blocked"),
                    "classification": str(preflight.get("classification") or "test_harness_error"),
                }
            )
        elif (
            python_module_missing
            or any(marker in lowered for marker in dependency_markers)
            or raw_run.get("exit_code") == 127
        ):
            repair_candidates.append(
                {
                    **diagnostic,
                    "reason_code": "project_dependency_missing",
                    "classification": "dependency_missing",
                }
            )
        elif any(marker in lowered for marker in harness_markers):
            repair_candidates.append(
                {
                    **diagnostic,
                    "reason_code": "test_harness_error",
                    "classification": "test_harness_error",
                }
            )
        else:
            non_repairable.append(
                {
                    **diagnostic,
                    "reason_code": "product_signal_or_non_repairable_environment",
                }
            )
    return {
        "schema_version": "intent-test-runtime-diagnostics/v1",
        "repair_candidates": repair_candidates,
        "non_repairable": non_repairable,
        "summary": {
            "repairable": len(repair_candidates),
            "non_repairable": len(non_repairable),
        },
    }


def intent_execution_repair_prompt(
    run_dir: Path,
    *,
    stage: str,
    attempt: int,
    job: dict[str, Any] | None = None,
    output_dir: Path | None = None,
) -> str:
    output_root = output_dir or run_dir
    lines = [
        "You are the Intent Test Execution Repair Agent.",
        f"Repair stage: {stage}; bounded attempt: {attempt}.",
        f"Run artifact directory: {run_dir}",
        f"Writable repair output directory: {output_root}",
        "Read intent/execution-capabilities.json and intent/intent-test-preflight.json.",
        "For runtime repair also read intent/intent-test-runtime-diagnostics.json and the referenced stdout/stderr logs.",
        "Modify only intent/intent-test-source.json and intent/generated-tests/** inside the writable repair output directory.",
        "You may choose a different agent-proposed command, runtime, cwd, import strategy, or faithful test harness when Worker policy can verify it.",
        "You must preserve the behavioral oracle, test_id/target_test_ids linkage, and execution of real repository behavior.",
        "Do not copy or reimplement application logic to manufacture a dependency-free passing test.",
        "Do not modify application source, install dependencies, access network, use production secrets, or weaken sandboxing.",
        "If no faithful runnable strategy exists, set a precise top-level skip_reason and retain the blocked evidence instead of fabricating execution.",
        "Finish by updating intent/intent-test-source.json; do not return a prose-only answer.",
    ]
    lines.extend(output_language_prompt_lines(job))
    return "\n".join(lines)


def _fallback_intent_classification(raw_result: dict[str, Any]) -> str:
    if _intent_dependency_missing(raw_result):
        return "dependency_missing"
    raw_classification = str(raw_result.get("classification") or "").strip()
    if raw_classification in INTENT_TEST_CLASSIFICATIONS:
        return raw_classification
    status = str(raw_result.get("status") or "").strip().lower()
    if status in {"skipped", "timeout", "error"}:
        return "test_harness_error"
    return "unclear_requirement"


def fallback_intent_test_results(run_dir: Path) -> dict[str, Any]:
    ensure_intent_directories(run_dir)
    raw = read_json(run_dir / "intent" / "intent-test-results.raw.json", {})
    raw_results = raw.get("test_runs") if isinstance(raw, dict) and isinstance(raw.get("test_runs"), list) else []
    test_results = []
    for index, raw_result in enumerate(raw_results):
        if not isinstance(raw_result, dict):
            continue
        test_id = str(raw_result.get("test_id") or raw_result.get("id") or f"ITV-{index + 1:03d}").strip()
        status = str(raw_result.get("status") or "unknown").strip() or "unknown"
        output_refs = []
        for key in ("stdout_path", "stderr_path"):
            output_path = str(raw_result.get(key) or "").strip()
            if output_path:
                output_refs.append(_intent_output_artifact_id(Path(output_path).name))
        test_results.append(
            {
                "test_id": test_id,
                "status": status if status in {"passed", "failed", "skipped", "timeout", "error"} else "error",
                "classification": _fallback_intent_classification(raw_result),
                "confidence": 0.0,
                "raw_status": status,
                "exit_code": raw_result.get("exit_code"),
                "timed_out": bool(raw_result.get("timed_out")),
                "duration_ms": _qa_int(raw_result.get("duration_ms")),
                "evidence": [
                    (
                        "Analyzer output was not materialized; this conservative fallback preserves the raw test "
                        "run without treating it as proof of a product bug."
                    )
                ],
                "evidence_summary": (
                    "Analyzer output was not materialized; this conservative fallback preserves the raw test "
                    "run without treating it as proof of a product bug."
                ),
                "finding_confidence_impact": "none",
                "confidence_delta": 0.0,
                "artifacts": output_refs,
                "artifact_refs": output_refs,
                "fallback_generated": True,
            }
        )
    return {"schema_version": "intent-test-result/v1", "test_results": test_results}


def _string_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        text = str(item or "").strip() if isinstance(item, str) else ""
        if text:
            items.append(text)
    return items


def _path_key(value: object) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")


def _intent_source_path_from_entry(entry: dict[str, Any]) -> str:
    for key in (
        "path",
        "artifact_path",
        "artifactPath",
        "file",
        "filename",
        "test_file",
        "testFile",
        "test_path",
        "testPath",
        "source_path",
        "sourcePath",
        "generated_file",
        "generatedFile",
    ):
        text = str(entry.get(key) or "").strip()
        if text:
            return text
    created = entry.get("created_files") or entry.get("createdFiles") or entry.get("files_to_create") or entry.get("filesToCreate")
    if isinstance(created, list):
        for item in created:
            if isinstance(item, dict):
                text = str(item.get("path") or item.get("file") or "").strip()
            else:
                text = str(item or "").strip() if isinstance(item, str) else ""
            if text:
                return text
    return ""



def _validation_repo_root_for_run_dir(run_dir: Path) -> Path | None:
    validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
    validation_root = str(validation.get("validation_repo_root") or "").strip() if isinstance(validation, dict) else ""
    return Path(validation_root) if validation_root else None


def _relative_path_if_under(path: Path, root: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return ""


def _strip_validation_workspace_prefix(path_text: str) -> str:
    parts = PurePosixPath(path_text.replace("\\", "/")).parts
    for marker in ("validation-repo", "validation_repo"):
        if marker not in parts:
            continue
        index = parts.index(marker)
        tail = [part for part in parts[index + 1 :] if part not in {"", "."}]
        if tail and all(part != ".." for part in tail):
            return PurePosixPath(*tail).as_posix()
    return ""


def _normalize_intent_generated_test_path(run_dir: Path, raw_path: str) -> str:
    path_text = str(raw_path or "").strip()
    if not path_text:
        return ""
    validation_root = _validation_repo_root_for_run_dir(run_dir)
    if validation_root is not None:
        for candidate in _candidate_paths_for_recorded_path(run_dir, path_text, str(validation_root)):
            rel = _relative_path_if_under(candidate, validation_root)
            if rel:
                return rel
    stripped = _strip_validation_workspace_prefix(path_text)
    if stripped:
        return stripped
    run_rel = _relative_path_if_under(Path(path_text), run_dir) if Path(path_text).is_absolute() else ""
    return run_rel or path_text.replace("\\", "/")


def _intent_generated_test_candidate_paths(run_dir: Path) -> list[str]:
    roots = [run_dir / "intent" / "generated-tests"]
    repo_root = _repo_root_for_run_dir(run_dir)
    if repo_root is not None:
        roots.append(repo_root / ".codex-review" / "generated-tests")
    paths: list[str] = []
    seen: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("*")) if root.is_dir() else []:
            if not path.is_file() or path.is_symlink():
                continue
            rel = _relative_path_if_under(path, run_dir)
            if not rel and repo_root is not None:
                rel = _relative_path_if_under(path, repo_root)
            if rel and rel not in seen:
                paths.append(rel)
                seen.add(rel)
    return paths


def _intent_test_ordinal(value: object) -> int | None:
    match = re.match(r"(?i)^IT(?:P|V|R)?[-_]?0*(\d+)(?:[-_]|$)", str(value or "").strip())
    return int(match.group(1)) if match else None


def repair_intent_test_source_artifact(path: Path, run_dir: Path) -> None:
    if not path.is_file():
        return
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        payload = {}
    tests = payload.get("tests") if isinstance(payload.get("tests"), list) else []
    tests_by_path: dict[str, dict[str, Any]] = {}
    tests_by_id: dict[str, dict[str, Any]] = {}
    for test in tests:
        if not isinstance(test, dict):
            continue
        test_path = _normalize_intent_generated_test_path(run_dir, _intent_source_path_from_entry(test))
        if test_path:
            tests_by_path[_path_key(test_path)] = test
        test_id = _intent_test_id(test, "")
        if test_id:
            tests_by_id[test_id] = test
    intended_by_id: dict[str, object] = {}
    intended_commands = payload.get("intended_commands") if isinstance(payload.get("intended_commands"), list) else []
    for intended in intended_commands:
        if not isinstance(intended, dict):
            continue
        command = intended.get("command") or intended.get("test_command") or intended.get("run_command")
        if not command:
            continue
        targets = intended.get("targets") or intended.get("test_ids") or intended.get("testIds") or []
        if not isinstance(targets, list):
            targets = [targets]
        for target_id in targets:
            test_id = str(target_id or "").strip()
            if test_id:
                intended_by_id[test_id] = command
    generated = payload.get("generated_tests")
    generated_items = generated if isinstance(generated, list) and generated else []
    if not generated_items:
        for alias in (
            "generated_test_files",
            "generatedTestFiles",
            "created_test_files",
            "createdTestFiles",
            "created_files",
            "createdFiles",
        ):
            alias_items = payload.get(alias)
            if isinstance(alias_items, list) and alias_items:
                generated_items = alias_items
                break
    if not generated_items and tests:
        generated_items = tests
    plan = read_json(run_dir / "intent" / "intent-test-plan.json", {})
    plan_targets = plan.get("test_targets") if isinstance(plan, dict) and isinstance(plan.get("test_targets"), list) else []
    plan_ids_by_ordinal: dict[int, list[str]] = {}
    for index, target in enumerate(plan_targets):
        if not isinstance(target, dict):
            continue
        target_id = _intent_test_id(target, f"ITP-{index + 1:03d}")
        ordinal = _intent_test_ordinal(target_id)
        if target_id and ordinal is not None:
            plan_ids_by_ordinal.setdefault(ordinal, []).append(target_id)
    candidate_paths = _intent_generated_test_candidate_paths(run_dir)
    repaired: list[dict[str, Any]] = []
    for index, item in enumerate(generated_items):
        if isinstance(item, dict):
            entry = dict(item)
            test_path = _normalize_intent_generated_test_path(run_dir, _intent_source_path_from_entry(entry))
        elif isinstance(item, str) and item.strip():
            test_path = _normalize_intent_generated_test_path(run_dir, item.strip())
            entry = {"path": test_path}
        else:
            continue
        entry_id = _intent_test_id(entry, "")
        supporting = tests_by_path.get(_path_key(test_path), {}) if test_path else {}
        if not supporting and entry_id:
            supporting = tests_by_id.get(entry_id, {})
        if not test_path and supporting:
            test_path = _normalize_intent_generated_test_path(run_dir, _intent_source_path_from_entry(supporting))
        if not test_path and len(candidate_paths) == len(generated_items) and index < len(candidate_paths):
            test_path = candidate_paths[index]
        if supporting:
            for key in (
                "command",
                "test_command",
                "testCommand",
                "run_command",
                "runCommand",
                "runnable_command",
                "runnableCommand",
                "intended_command",
                "intendedCommand",
                "cwd",
                "framework",
                "test_framework",
                "testFramework",
                "linked_finding_ids",
                "target_finding_ids",
                "intent_contract_ids",
                "behavioral_contract_ids",
            ):
                if key not in entry and key in supporting:
                    entry[key] = supporting[key]
        test_id = _intent_test_id(entry, _intent_test_id(supporting, f"ITV-{index + 1:03d}"))
        entry["test_id"] = test_id
        if not _intent_related_test_ids(entry):
            ordinal = _intent_test_ordinal(test_id)
            matching_plan_ids = plan_ids_by_ordinal.get(ordinal, []) if ordinal is not None else []
            if len(matching_plan_ids) == 1:
                entry["target_test_ids"] = matching_plan_ids
        has_explicit_command = any(
            _intent_command(entry.get(key))
            for key in ("command", "test_command", "testCommand", "run_command", "runCommand")
        )
        if not has_explicit_command:
            for alias in (
                "runnable_command",
                "runnableCommand",
                "intended_command",
                "intendedCommand",
            ):
                if _intent_command(entry.get(alias)):
                    entry["command"] = entry[alias]
                    has_explicit_command = True
                    break
        if not has_explicit_command:
            intended_command = intended_by_id.get(test_id)
            if not intended_command:
                intended_command = _intent_source_command_value(
                    payload,
                    entry,
                    index,
                    len(generated_items),
                )
            if intended_command:
                entry["command"] = intended_command
        if test_path:
            entry["path"] = test_path
        if not _string_items(entry.get("artifact_refs") or entry.get("artifactRefs")):
            entry["artifact_refs"] = ["art_intent_test_source"]
        repaired.append(entry)
    payload["schema_version"] = "intent-test-source/v1"
    payload["generated_tests"] = repaired
    write_json(path, payload)


def _raw_intent_runs_by_id(run_dir: Path) -> dict[str, dict[str, Any]]:
    raw = read_json(run_dir / "intent" / "intent-test-results.raw.json", {})
    raw_runs = raw.get("test_runs") if isinstance(raw, dict) and isinstance(raw.get("test_runs"), list) else []
    runs: dict[str, dict[str, Any]] = {}
    for index, raw_run in enumerate(raw_runs):
        if not isinstance(raw_run, dict):
            continue
        test_id = str(raw_run.get("test_id") or raw_run.get("id") or f"ITV-{index + 1:03d}").strip()
        if test_id:
            runs[test_id] = raw_run
    return runs


def _intent_raw_output_text(raw_result: dict[str, Any]) -> str:
    parts = []
    for key in ("error", "stderr", "stdout", "observed_output", "sandbox_fallback_reason"):
        text = str(raw_result.get(key) or "").strip()
        if text:
            parts.append(text)
    for key in ("stderr_path", "stdout_path"):
        raw_path = str(raw_result.get(key) or "").strip()
        if not raw_path:
            continue
        try:
            parts.append(Path(raw_path).read_text(encoding="utf-8", errors="replace")[:4096])
        except OSError:
            continue
    return "\n".join(parts).lower()


def _intent_dependency_missing(raw_result: dict[str, Any]) -> bool:
    if raw_result.get("exit_code") == 127:
        return True
    output = _intent_raw_output_text(raw_result)
    command = str(raw_result.get("command") or "").lower().replace("\\", "/")
    if (
        " -m unittest " in f" {command} "
        and ("failed to import test module" in output or "modulenotfounderror" in output)
        and ("generated-tests" in output or re.search(r"no module named ['\"]?[^'\"\n]*/", output))
    ):
        return False
    dependency_markers = (
        "command not found",
        ": not found",
        "no module named ",
        "modulenotfounderror",
        "cannot find module",
        "could not find module",
        "executable not found",
        "not installed",
    )
    return any(marker in output for marker in dependency_markers)


def _intent_status_from_result(result: dict[str, Any], raw_result: dict[str, Any]) -> str:
    for value in (result.get("status"), result.get("raw_status"), raw_result.get("status")):
        status = str(value or "").strip()
        if status in INTENT_TEST_STATUSES:
            return status
    outcome = str(result.get("outcome") or result.get("classification") or "").strip()
    if outcome.startswith("passed"):
        return "passed"
    if outcome.startswith("skipped"):
        return "skipped"
    if outcome in {"timeout", "timed_out"}:
        return "timeout"
    if outcome in {"error", "environment_error", "dependency_missing", "test_harness_error"}:
        return "error"
    if outcome in {"confirmed_bug", "plausible_bug", "test_oracle_wrong", "unclear_requirement"}:
        return "failed"
    return "error"


def _intent_classification_from_result(result: dict[str, Any], status: str, raw_result: dict[str, Any]) -> str:
    if _intent_dependency_missing(raw_result):
        return "dependency_missing"
    for value in (result.get("classification"), result.get("outcome")):
        classification = str(value or "").strip()
        if classification in INTENT_TEST_CLASSIFICATIONS:
            return classification
    raw_status = str(raw_result.get("status") or "").strip()
    if status == "passed":
        return "passed_no_bug_reproduced"
    if status == "skipped":
        return "skipped_not_runnable" if str(result.get("outcome") or "").strip() == "skipped_not_runnable" else "test_harness_error"
    if status in {"timeout", "error"} or raw_status in {"skipped", "timeout", "error"}:
        return "test_harness_error"
    return "unclear_requirement"


def _intent_result_evidence(result: dict[str, Any], raw_result: dict[str, Any]) -> list[str]:
    evidence = result.get("evidence")
    if isinstance(evidence, list):
        items = [str(item).strip() for item in evidence if str(item).strip()]
        if items:
            return items
    items = []
    for key in ("evidence_summary", "classification_basis", "observed_output", "notes", "note", "skip_reason", "error"):
        text = str(result.get(key) or raw_result.get(key) or "").strip()
        if text:
            items.append(text)
    if not items:
        items.append("Analyzer output was repaired into the worker intent-test-result schema.")
    return items


def _intent_result_artifacts(result: dict[str, Any], raw_result: dict[str, Any]) -> list[str]:
    for key in ("artifacts", "artifact_refs", "artifactRefs"):
        refs = _string_items(result.get(key))
        if refs:
            return refs
    refs = []
    for key in ("stdout_path", "stderr_path"):
        output_path = str(raw_result.get(key) or "").strip()
        if output_path:
            refs.append(_intent_output_artifact_id(Path(output_path).name))
    return refs


def repair_intent_test_results_artifact(path: Path, run_dir: Path) -> None:
    if not path.is_file():
        return
    payload = read_json(path, {})
    raw_by_id = _raw_intent_runs_by_id(run_dir)
    if not isinstance(payload, dict):
        write_json(path, fallback_intent_test_results(run_dir))
        return
    results = payload.get("test_results")
    if not isinstance(results, list):
        results = payload.get("results") if isinstance(payload.get("results"), list) else []
    repaired: list[dict[str, Any]] = []
    for index, item in enumerate(results):
        if not isinstance(item, dict):
            continue
        result = dict(item)
        test_id = str(result.get("test_id") or result.get("id") or f"ITV-{index + 1:03d}").strip()
        raw_result = raw_by_id.get(test_id, {})
        status = _intent_status_from_result(result, raw_result)
        classification = _intent_classification_from_result(result, status, raw_result)
        confidence = result.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            confidence = 0.0
        if classification in {"dependency_missing", "environment_error", "test_harness_error", "skipped_not_runnable"}:
            confidence = 0.0
            result["finding_confidence_impact"] = "none"
            result["confidence_delta"] = 0.0
        artifacts = _intent_result_artifacts(result, raw_result)
        result.update(
            {
                "test_id": test_id,
                "status": status,
                "classification": classification,
                "confidence": float(confidence),
                "evidence": _intent_result_evidence(result, raw_result),
                "artifacts": artifacts,
                "artifact_refs": artifacts,
            }
        )
        repaired.append(result)
    if not repaired and raw_by_id:
        write_json(path, fallback_intent_test_results(run_dir))
        return
    payload["schema_version"] = "intent-test-result/v1"
    payload["test_results"] = repaired
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    classification_counts: dict[str, int] = {}
    for result in repaired:
        classification = str(result.get("classification") or "").strip()
        if classification:
            classification_counts[classification] = classification_counts.get(classification, 0) + 1
    summary["classification_counts"] = classification_counts
    summary["analyzed_results"] = len(repaired)
    payload["summary"] = summary
    write_json(path, payload)

def _qa_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _qa_artifact_path(artifact_dir: Path, item: dict[str, Any]) -> Path:
    return artifact_dir / str(item.get("name") or "")


def _repo_root_for_run_dir(run_dir: Path) -> Path | None:
    codex_review_dir = run_dir.parent.parent
    if codex_review_dir.name == ".codex-review":
        return codex_review_dir.parent
    return None


def _string_list_errors(value: Any, artifact_name: str, field_name: str) -> tuple[list[str], list[str]]:
    if not isinstance(value, list):
        return [], [f"{artifact_name} {field_name} must be a list"]
    items = []
    errors = []
    for index, item in enumerate(value):
        item_text = str(item or "").strip() if isinstance(item, str) else ""
        if not item_text:
            errors.append(f"{artifact_name} {field_name}[{index}] must be a non-empty string")
            continue
        items.append(item_text)
    return items, errors


def _unique_test_id_errors(artifact_name: str, collection_name: str, entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return [f"{artifact_name} {collection_name} must be a list"]
    errors = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"{artifact_name} {collection_name}[{index}] must be an object")
            continue
        test_id = str(entry.get("test_id") or "").strip()
        if not test_id:
            errors.append(f"{artifact_name} {collection_name}[{index}].test_id is missing")
            continue
        if test_id in seen:
            errors.append(f"{artifact_name} {collection_name}[{index}].test_id is duplicated: {test_id}")
        seen.add(test_id)
    return errors


def _cluster_link_ids(run_dir: Path) -> set[str]:
    payload = read_json(run_dir / "clusters.json", {})
    ids: set[str] = set()
    scalar_id_fields = {
        "cluster_id",
        "id",
        "finding_id",
        "local_id",
        "candidate_id",
        "target_id",
        "source_id",
        "reviewer_finding_id",
    }
    list_id_fields = {
        "finding_ids",
        "linked_finding_ids",
        "source_finding_ids",
        "supporting_finding_ids",
        "member_ids",
        "members",
        "sources",
        "source_findings",
        "merged_findings",
        "candidate_findings",
        "findings",
        "items",
        "candidates",
        "clusters",
    }

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 7:
            return
        if isinstance(value, str):
            item_id = value.strip()
            if item_id:
                ids.add(item_id)
            return
        if isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
            return
        if not isinstance(value, dict):
            return
        for field in scalar_id_fields:
            item_id = str(value.get(field) or "").strip()
            if item_id:
                ids.add(item_id)
        for field in list_id_fields:
            children = value.get(field)
            if isinstance(children, (list, dict)):
                visit(children, depth + 1)

    visit(payload)
    visit(read_json(run_dir / "validation-input.json", {}))
    for directory in (run_dir / "raw-reviewers", run_dir / "verified-reviewers"):
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            visit(read_json(path, {}))
    return ids


def _linked_finding_errors(
    run_dir: Path,
    artifact_name: str,
    collection_name: str,
    entries: Any,
    *,
    required: bool,
) -> list[str]:
    if not isinstance(entries, list):
        return []
    errors = []
    cluster_ids: set[str] | None = None
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        field_name = f"{collection_name}[{index}].linked_finding_ids"
        if "linked_finding_ids" not in entry:
            if required:
                errors.append(f"{artifact_name} {field_name} is missing")
            continue
        linked_ids, field_errors = _string_list_errors(entry.get("linked_finding_ids"), artifact_name, field_name)
        errors.extend(field_errors)
        if not linked_ids:
            continue
        if cluster_ids is None:
            cluster_ids = _cluster_link_ids(run_dir)
        for linked_id in linked_ids:
            if linked_id not in cluster_ids:
                errors.append(f"{artifact_name} {field_name} references unknown cluster id {linked_id}")
    return errors


def intent_map_errors(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["intent-map.json must be a JSON object"]
    errors = []
    if payload.get("schema_version") != "intent-map/v1":
        errors.append("intent-map.json schema_version must be intent-map/v1")
    if not str(payload.get("bundle_id") or "").strip():
        errors.append("intent-map.json bundle_id is missing")
    if not isinstance(payload.get("behavioral_contracts"), list):
        errors.append("intent-map.json behavioral_contracts must be a list")
    return errors


def _coerce_intent_contracts(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return None
    for field in ("behavioral_contracts", "contracts", "items", "entries"):
        nested = value.get(field)
        if isinstance(nested, list):
            return nested
    if not value:
        return []
    if not all(isinstance(item, dict) for item in value.values()):
        return None
    contracts = []
    for contract_id, contract in sorted(value.items()):
        item = dict(contract)
        item.setdefault("contract_id", str(contract_id))
        contracts.append(item)
    return contracts


def repair_intent_map_artifact(path: Path) -> None:
    if not path.is_file():
        return
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return
    changed = False
    if payload.get("schema_version") != "intent-map/v1":
        payload["schema_version"] = "intent-map/v1"
        changed = True
    if not str(payload.get("bundle_id") or "").strip():
        payload["bundle_id"] = "all"
        changed = True
    if not isinstance(payload.get("behavioral_contracts"), list):
        contracts = _coerce_intent_contracts(payload.get("behavioral_contracts"))
        if contracts is None:
            contracts = _coerce_intent_contracts(payload)
        if contracts is None:
            contracts = []
            unknowns = payload.get("unknowns")
            if not isinstance(unknowns, list):
                unknowns = []
            message = "Intent contracts were omitted or malformed during semantic repair."
            if message not in unknowns:
                unknowns.append(message)
            payload["unknowns"] = unknowns
        payload["behavioral_contracts"] = contracts
        changed = True
    if changed:
        write_json(path, payload)


def _coerce_intent_test_targets(value: Any) -> list[Any] | None:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return None
    for field in ("test_targets", "targets", "tests", "items", "entries"):
        nested = value.get(field)
        if isinstance(nested, list):
            return nested
    if not value:
        return []
    if not all(isinstance(item, dict) for item in value.values()):
        return None
    targets = []
    for target_id, target in sorted(value.items()):
        item = dict(target)
        item.setdefault("test_id", str(target_id))
        targets.append(item)
    return targets


def _intent_test_plan_supporting_tests(
    payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    raw_tests = payload.get("tests")
    if not isinstance(raw_tests, list):
        return {}, []
    tests = [item for item in raw_tests if isinstance(item, dict)]
    by_id: dict[str, dict[str, Any]] = {}
    for index, test in enumerate(tests):
        for key in ("test_id", "id", "target_id", "targetId"):
            test_id = str(test.get(key) or "").strip()
            if test_id and test_id not in by_id:
                by_id[test_id] = test
        fallback_id = f"ITV-{index + 1:03d}"
        by_id.setdefault(fallback_id, test)
    return by_id, tests


def _intent_plan_cluster_aliases(run_dir: Path) -> dict[str, str]:
    aliases: dict[str, str] = {}

    def visit(value: Any, inherited_cluster_id: str = "") -> None:
        if not isinstance(value, dict):
            return
        cluster_id = str(value.get("cluster_id") or inherited_cluster_id or "").strip()
        if cluster_id:
            aliases.setdefault(cluster_id, cluster_id)
            for field in ("id", "finding_id", "local_id"):
                alias = str(value.get(field) or "").strip()
                if alias:
                    aliases.setdefault(alias, cluster_id)
        next_cluster_id = cluster_id or inherited_cluster_id
        for field in (
            "clusters",
            "candidate_findings",
            "findings",
            "items",
            "candidates",
            "source_findings",
            "merged_findings",
        ):
            children = value.get(field)
            if isinstance(children, list):
                for child in children:
                    visit(child, next_cluster_id)

    for path in (run_dir / "clusters.json", run_dir / "validation-input.json"):
        visit(read_json(path, {}))
    return aliases


def _intent_plan_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _intent_plan_first_string(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _intent_plan_linked_ids(
    target: dict[str, Any],
    supporting_test: dict[str, Any],
    aliases: dict[str, str],
) -> list[str]:
    raw_ids: list[str] = []
    for source in (target, supporting_test):
        for field in ("linked_finding_ids", "finding_ids", "related_finding_ids", "cluster_ids"):
            raw_ids.extend(_intent_plan_string_list(source.get(field)))
        for field in ("linked_finding_id", "finding_id", "cluster_id"):
            raw_ids.extend(_intent_plan_string_list(source.get(field)))
    linked_ids: list[str] = []
    for raw_id in raw_ids:
        linked_id = aliases.get(raw_id, raw_id)
        if linked_id and linked_id not in linked_ids:
            linked_ids.append(linked_id)
    return linked_ids


def repair_intent_test_plan_artifact(path: Path, run_dir: Path) -> None:
    if not path.is_file():
        return
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return
    changed = False
    if payload.get("schema_version") != "intent-test-plan/v1":
        payload["schema_version"] = "intent-test-plan/v1"
        changed = True
    targets = _coerce_intent_test_targets(payload.get("test_targets"))
    if targets is None:
        targets = _coerce_intent_test_targets(payload)
    if targets is None:
        targets = []
    if targets is not payload.get("test_targets"):
        payload["test_targets"] = targets
        changed = True
    supporting_by_id, supporting_tests = _intent_test_plan_supporting_tests(payload)
    aliases = _intent_plan_cluster_aliases(run_dir)
    repaired_targets: list[Any] = []
    for index, raw_target in enumerate(targets):
        if not isinstance(raw_target, dict):
            repaired_targets.append(raw_target)
            continue
        target = dict(raw_target)
        test_id = _intent_test_id(target, f"ITV-{index + 1:03d}")
        supporting_test = (
            supporting_by_id.get(test_id)
            or supporting_by_id.get(str(target.get("id") or "").strip())
            or (supporting_tests[index] if index < len(supporting_tests) else {})
        )
        if not isinstance(supporting_test, dict):
            supporting_test = {}
        if not str(target.get("test_id") or "").strip() and test_id:
            target["test_id"] = test_id
        if not str(target.get("title") or "").strip():
            target["title"] = _intent_plan_first_string(
                supporting_test.get("title"),
                supporting_test.get("goal"),
                target.get("goal"),
                supporting_test.get("strategy"),
                target.get("description"),
                f"Intent test {test_id}",
            )
        expected = str(
            target.get("expected_result_before_fix")
            or supporting_test.get("expected_result_before_fix")
            or supporting_test.get("expectedResultBeforeFix")
            or ""
        ).strip()
        if expected not in {"fail", "pass", "unknown"}:
            expected = "unknown"
        target["expected_result_before_fix"] = expected
        if "linked_finding_ids" not in target or not isinstance(target.get("linked_finding_ids"), list):
            target["linked_finding_ids"] = _intent_plan_linked_ids(target, supporting_test, aliases)
        if "target_files" in target and not isinstance(target.get("target_files"), list):
            target["target_files"] = _intent_plan_string_list(target.get("target_files"))
        elif "target_files" not in target:
            files = []
            for source in (target, supporting_test):
                for field in ("target_files", "files_under_test", "files", "paths"):
                    files.extend(_intent_plan_string_list(source.get(field)))
            if files:
                target["target_files"] = list(dict.fromkeys(files))
        if not str(target.get("command") or "").strip():
            command = _intent_plan_first_string(
                supporting_test.get("command"),
                supporting_test.get("test_command"),
                supporting_test.get("run_command"),
                supporting_test.get("runnable_command"),
            )
            if command:
                target["command"] = command
        if target != raw_target:
            changed = True
        repaired_targets.append(target)
    if repaired_targets != targets:
        payload["test_targets"] = repaired_targets
        changed = True
    if changed:
        write_json(path, payload)


def intent_test_plan_errors(run_dir: Path, payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["intent-test-plan.json must be a JSON object"]
    errors = []
    if payload.get("schema_version") != "intent-test-plan/v1":
        errors.append("intent-test-plan.json schema_version must be intent-test-plan/v1")
    targets = payload.get("test_targets")
    errors.extend(_unique_test_id_errors("intent-test-plan.json", "test_targets", targets))
    if not isinstance(targets, list):
        return errors
    for index, target in enumerate(targets):
        if not isinstance(target, dict):
            continue
        if not str(target.get("title") or "").strip():
            errors.append(f"intent-test-plan.json test_targets[{index}].title is missing")
        if str(target.get("expected_result_before_fix") or "").strip() not in {"fail", "pass", "unknown"}:
            errors.append(f"intent-test-plan.json test_targets[{index}].expected_result_before_fix is invalid")
        target_files = target.get("target_files")
        if target_files is not None and not isinstance(target_files, list):
            errors.append(f"intent-test-plan.json test_targets[{index}].target_files must be a list")
    errors.extend(_linked_finding_errors(run_dir, "intent-test-plan.json", "test_targets", targets, required=True))
    return errors


def _candidate_paths_for_recorded_path(run_dir: Path, raw_path: str, validation_root: str = "") -> list[Path]:
    path_text = raw_path.strip()
    if not path_text:
        return []
    path = Path(path_text)
    if path.is_absolute():
        return [path]
    candidates = [run_dir / path]
    repo_root = _repo_root_for_run_dir(run_dir)
    if repo_root is not None:
        candidates.append(repo_root / path)
    if validation_root:
        candidates.append(Path(validation_root) / path)
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _recorded_file_exists(run_dir: Path, raw_path: str, validation_root: str = "") -> bool:
    for candidate in _candidate_paths_for_recorded_path(run_dir, raw_path, validation_root):
        if candidate.is_file():
            return True
    return False


def _intent_generated_test_exists(run_dir: Path, raw_path: str) -> bool:
    validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
    validation_root = str(validation.get("validation_repo_root") or "").strip() if isinstance(validation, dict) else ""
    return _recorded_file_exists(run_dir, raw_path, validation_root)


def intent_test_source_errors(run_dir: Path, payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["intent-test-source.json must be a JSON object"]
    errors = []
    if payload.get("schema_version") != "intent-test-source/v1":
        errors.append("intent-test-source.json schema_version must be intent-test-source/v1")
    generated = payload.get("generated_tests")
    errors.extend(_unique_test_id_errors("intent-test-source.json", "generated_tests", generated))
    if not isinstance(generated, list):
        return errors
    for index, test in enumerate(generated):
        if not isinstance(test, dict):
            continue
        test_path = str(test.get("path") or test.get("artifact_path") or test.get("artifactPath") or "").strip()
        if not test_path:
            errors.append(f"intent-test-source.json generated_tests[{index}].path is missing")
        elif not _intent_generated_test_exists(run_dir, test_path):
            errors.append(f"intent-test-source.json generated_tests[{index}].path does not exist: {test_path}")
    errors.extend(_linked_finding_errors(run_dir, "intent-test-source.json", "generated_tests", generated, required=False))
    return errors


def intent_test_raw_run_errors(run_dir: Path, payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["intent-test-results.raw.json must be a JSON object"]
    errors = []
    if payload.get("schema_version") != "intent-test-run-results/v1":
        errors.append("intent-test-results.raw.json schema_version must be intent-test-run-results/v1")
    runs = payload.get("test_runs")
    errors.extend(_unique_test_id_errors("intent-test-results.raw.json", "test_runs", runs))
    if not isinstance(runs, list):
        return errors
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "").strip()
        if status not in INTENT_TEST_STATUSES:
            errors.append(f"intent-test-results.raw.json test_runs[{index}].status is invalid")
            continue
        if status == "skipped":
            continue
        if not str(run.get("command") or "").strip():
            errors.append(f"intent-test-results.raw.json test_runs[{index}].command is missing")
        for field in ("stdout_path", "stderr_path"):
            output_path = str(run.get(field) or "").strip()
            if not output_path:
                errors.append(f"intent-test-results.raw.json test_runs[{index}].{field} is missing")
            elif not _recorded_file_exists(run_dir, output_path):
                errors.append(f"intent-test-results.raw.json test_runs[{index}].{field} output artifact is missing")
    return errors


def intent_test_result_errors(payload: Any, run_dir: Path | None = None) -> list[str]:
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["intent-test-results.json must be a JSON object"]
    if payload.get("schema_version") != "intent-test-result/v1":
        errors.append("intent-test-results.json schema_version must be intent-test-result/v1")
    results = payload.get("test_results")
    if not isinstance(results, list):
        errors.append("intent-test-results.json test_results must be a list")
        return errors
    seen_test_ids: set[str] = set()
    for index, result in enumerate(results):
        if not isinstance(result, dict):
            errors.append(f"intent-test-results.json test_results[{index}] must be an object")
            continue
        test_id = str(result.get("test_id") or "").strip()
        if not test_id:
            errors.append(f"intent-test-results.json test_results[{index}].test_id is missing")
        elif test_id in seen_test_ids:
            errors.append(f"intent-test-results.json test_results[{index}].test_id is duplicated: {test_id}")
        seen_test_ids.add(test_id)
        status = str(result.get("status") or "").strip()
        if status not in INTENT_TEST_STATUSES:
            errors.append(f"intent-test-results.json test_results[{index}].status is invalid")
        classification = str(result.get("classification") or "").strip()
        if classification not in INTENT_TEST_CLASSIFICATIONS:
            errors.append(f"intent-test-results.json test_results[{index}].classification is invalid")
        confidence = result.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            errors.append(f"intent-test-results.json test_results[{index}].confidence is outside 0..1")
        for field in ("evidence", "artifacts"):
            value = result.get(field)
            if value is not None and not isinstance(value, list):
                errors.append(f"intent-test-results.json test_results[{index}].{field} must be a list")
    if run_dir is not None:
        errors.extend(_linked_finding_errors(run_dir, "intent-test-results.json", "test_results", results, required=False))
        raw_path = run_dir / 'intent' / 'intent-test-results.raw.json'
        if results and not raw_path.is_file():
            errors.append(
                'intent-test-results.json has analyzed results but no raw process evidence artifact'
            )
        elif raw_path.is_file():
            raw_by_id = _raw_intent_runs_by_id(run_dir)
            result_by_id = {
                str(result.get('test_id') or '').strip(): result
                for result in results
                if isinstance(result, dict) and str(result.get('test_id') or '').strip()
            }
            for test_id in sorted(result_by_id.keys() - raw_by_id.keys()):
                errors.append(
                    'intent-test-results.json test result '
                    f'{test_id} has no matching raw process evidence'
                )
            for test_id in sorted(raw_by_id.keys() - result_by_id.keys()):
                errors.append(
                    'intent-test-results.raw.json raw process evidence '
                    f'{test_id} has no matching analyzed result'
                )
            for test_id in sorted(raw_by_id.keys() & result_by_id.keys()):
                raw_status = str(raw_by_id[test_id].get('status') or '').strip()
                result_status = str(result_by_id[test_id].get('status') or '').strip()
                classification = str(result_by_id[test_id].get('classification') or '').strip()
                if raw_status == 'passed' and classification in {'confirmed_bug', 'plausible_bug'}:
                    errors.append(
                        f'intent-test-results.json {test_id} cannot use {classification} '
                        'for a passed raw process'
                    )
                if raw_status in INTENT_TEST_STATUSES and result_status != raw_status:
                    errors.append(
                        f'intent-test-results.json {test_id} status {result_status or "missing"} '
                        f'does not match raw process status {raw_status}'
                    )
    return errors


def _intent_skip_reason_from_payload(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("skip_reason", "skipped_reason", "skipReason", "skippedReason"):
        reason = str(payload.get(key) or "").strip()
        if reason:
            return reason
    return ""


def intent_validation_missing_results_error(run_dir: Path) -> str:
    config = read_json(run_dir / "intent" / "intent-test-validation.json", {})
    if not isinstance(config, dict) or config.get("enabled") is False:
        return ""
    if config.get("require_intent_evidence") is False:
        return ""
    raw_runs = read_json(
        run_dir / "intent" / "intent-test-results.raw.json",
        {},
    ).get("test_runs", [])
    if isinstance(raw_runs, list) and raw_runs:
        fully_skipped = all(
            isinstance(raw_run, dict)
            and str(raw_run.get("status") or "").strip().lower() == "skipped"
            and bool(_intent_skip_reason_from_payload(raw_run))
            for raw_run in raw_runs
        )
        if fully_skipped:
            return ""
        return (
            "intent-test-results.json is missing while intent-test validation "
            "has attempted or incompletely skipped raw runs"
        )
    for payload in (
        config,
        read_json(run_dir / "intent" / "intent-test-plan.json", {}),
        read_json(run_dir / "intent" / "intent-test-source.json", {}),
    ):
        if _intent_skip_reason_from_payload(payload):
            return ""
    return "intent-test-results.json is missing while intent-test validation is enabled and no skipped reason exists"


def artifact_manifest_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    items = payload.get("items") if isinstance(payload, dict) else None
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def artifact_manifest_payload(run_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": "artifact-manifest/v1",
        "run_id": run_id,
        "created_at": iso_time(time.time()),
        "summary": {
            "artifacts_total": len(items),
            "required_artifacts": sum(1 for item in items if item.get("required") is True),
        },
        "items": items,
        "errors": [],
        "warnings": [],
    }



def uploaded_artifact_manifest_path(artifact_dir: Path) -> Path:
    return artifact_dir / UPLOADED_ARTIFACT_MANIFEST_NAME


def clear_uploaded_artifact_manifest(artifact_dir: Path, *, source_run_dir: Path | None = None) -> None:
    paths = [uploaded_artifact_manifest_path(artifact_dir)]
    if source_run_dir is not None:
        paths.append(source_run_dir / UPLOADED_ARTIFACT_MANIFEST_NAME)
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

def write_uploaded_artifact_manifest(
    artifact_dir: Path,
    manifest_payload: dict[str, Any],
    uploaded_items: list[dict[str, Any]],
    *,
    source_run_dir: Path | None = None,
) -> None:
    payload = copy.deepcopy(manifest_payload)
    payload["items"] = copy.deepcopy(uploaded_items)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    summary = dict(summary)
    summary["artifacts_total"] = len(uploaded_items)
    summary["required_artifacts"] = sum(1 for item in uploaded_items if item.get("required") is True)
    payload["summary"] = summary
    write_json_atomic(uploaded_artifact_manifest_path(artifact_dir), payload)
    if source_run_dir is not None:
        write_json_atomic(source_run_dir / UPLOADED_ARTIFACT_MANIFEST_NAME, payload)


def uploaded_artifact_manifest_items(artifact_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(uploaded_artifact_manifest_path(artifact_dir), {})
    if not isinstance(payload, dict) or payload.get("schema_version") != "artifact-manifest/v1":
        return []
    if str(payload.get("run_id") or "").strip() != artifact_dir.name:
        return []
    return [copy.deepcopy(item) for item in artifact_manifest_items(payload)]


def result_artifact_manifest_items(artifact_dir: Path) -> list[dict[str, Any]]:
    snapshot_path = uploaded_artifact_manifest_path(artifact_dir)
    if snapshot_path.is_file():
        # The worker-owned upload snapshot is the exact set accepted by the
        # control plane. Re-merging the pre-upload manifest would advertise
        # optional artifacts whose upload failed.
        return uploaded_artifact_manifest_items(artifact_dir)
    return [
        copy.deepcopy(item)
        for item in artifact_manifest_items(
            read_json(artifact_dir / "artifact-manifest.json", {})
        )
    ]


def reconcile_envelope_artifact_manifest_with_uploads(envelope: dict[str, Any], artifact_dir: Path) -> None:
    manifest = result_artifact_manifest_items(artifact_dir)
    if manifest:
        envelope["artifact_manifest"] = manifest


def result_manifest_uploaded_snapshot_mismatches(envelope: dict[str, Any], artifact_dir: Path) -> list[str]:
    uploaded = uploaded_artifact_manifest_items(artifact_dir)
    uploaded_by_id = {
        str(item.get("artifact_id") or "").strip(): item
        for item in uploaded
        if str(item.get("artifact_id") or "").strip()
    }
    manifest = envelope.get("artifact_manifest") if isinstance(envelope.get("artifact_manifest"), list) else []
    mismatches: list[str] = []
    for item in manifest:
        if not isinstance(item, dict) or item.get("required") is not True:
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        if not artifact_id:
            continue
        if artifact_id not in uploaded_by_id or item != uploaded_by_id[artifact_id]:
            mismatches.append(artifact_id)
    return mismatches


def validate_result_manifest_matches_uploaded_snapshot(envelope: dict[str, Any], artifact_dir: Path) -> None:
    mismatches = result_manifest_uploaded_snapshot_mismatches(envelope, artifact_dir)
    status = result_status_from_envelope(envelope)
    extensions = envelope.get("extensions") if isinstance(envelope.get("extensions"), dict) else {}
    worker_internal = (
        extensions.get("worker_internal")
        if isinstance(extensions.get("worker_internal"), dict)
        else {}
    )
    upload_error = str(worker_internal.get("artifact_upload_error") or "").strip()
    if mismatches and status in {"failed", "cancelled", "partial_completed"} and upload_error:
        uploaded_ids = {
            str(item.get("artifact_id") or "").strip()
            for item in uploaded_artifact_manifest_items(artifact_dir)
            if str(item.get("artifact_id") or "").strip()
        }
        mismatches = [
            artifact_id
            for artifact_id in mismatches
            if artifact_id in uploaded_ids
        ]
    if mismatches:
        raise RuntimeError(
            "required result artifact is missing from or differs from the uploaded artifact snapshot: "
            + ", ".join(mismatches[:10])
        )


def validate_artifact_manifest_for_qa(run_dir: Path, artifact_dir: Path, errors: list[str]) -> None:
    manifest_path = artifact_dir / "artifact-manifest.json"
    if not manifest_path.is_file():
        errors.append("artifact-manifest.json is missing")
        return
    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append("artifact-manifest.json is not valid JSON")
        return
    if not isinstance(manifest_payload, dict):
        errors.append("artifact-manifest.json must be an object")
        return
    if manifest_payload.get("schema_version") != "artifact-manifest/v1":
        errors.append("artifact-manifest.json schema_version must be artifact-manifest/v1")
    if str(manifest_payload.get("run_id") or "").strip() != artifact_dir.name:
        errors.append("artifact-manifest.json run_id must match artifact directory")
    manifest = manifest_payload.get("items")
    if not isinstance(manifest, list):
        errors.append("artifact-manifest.json items must be a list")
        return
    run_manifest = run_dir / "artifact-manifest.json"
    if not run_manifest.is_file():
        errors.append("run artifact-manifest.json is missing")
    else:
        run_payload = read_json(run_manifest, {})
        if not isinstance(run_payload, dict) or run_payload.get("schema_version") != "artifact-manifest/v1":
            errors.append("run artifact-manifest.json must be artifact-manifest/v1")
    required_kinds = {str(item.get("kind") or "") for item in manifest if isinstance(item, dict) and item.get("required")}
    missing_required = sorted(REQUIRED_COMPLETED_ARTIFACTS - required_kinds)
    if missing_required:
        errors.append("artifact-manifest.json missing required artifacts: " + ", ".join(missing_required))
    seen_artifact_ids: set[str] = set()
    for index, item in enumerate(manifest):
        if not isinstance(item, dict):
            errors.append(f"artifact-manifest[{index}] is not an object")
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        if artifact_id:
            if artifact_id in seen_artifact_ids:
                errors.append(f"artifact-manifest[{index}].artifact_id is duplicated: {artifact_id}")
            seen_artifact_ids.add(artifact_id)
        for field in (
            "artifact_id",
            "kind",
            "name",
            "media_type",
            "schema_id",
            "schema_version",
            "encoding",
            "compression",
            "required",
            "storage",
            "sha256",
            "size_bytes",
        ):
            if item.get(field) in (None, ""):
                errors.append(f"artifact-manifest[{index}].{field} is missing")
        path = _qa_artifact_path(artifact_dir, item)
        try:
            path.resolve(strict=False).relative_to(artifact_dir.resolve(strict=False))
        except ValueError:
            errors.append(f"artifact {item.get('name') or index} path escapes artifact directory")
            continue
        if not path.is_file():
            errors.append(f"artifact {item.get('name') or index} is missing")
            continue
        data = path.read_bytes()
        if item.get("sha256") != hashlib.sha256(data).hexdigest():
            errors.append(f"artifact {path.name} sha256 mismatch")
        if _qa_int(item.get("size_bytes"), -1) != len(data):
            errors.append(f"artifact {path.name} size_bytes mismatch")
        if item.get("schema_version") != "v1":
            errors.append(f"artifact {path.name} schema_version must be v1")
        if item.get("encoding") != "utf-8":
            errors.append(f"artifact {path.name} encoding must be utf-8")
        if item.get("compression") != "none":
            errors.append(f"artifact {path.name} compression must be none")
        if item.get("required") not in {True, False}:
            errors.append(f"artifact {path.name} required must be boolean")
        storage = item.get("storage") if isinstance(item.get("storage"), dict) else {}
        storage_url = str(storage.get("url") or "")
        expected_storage_url = f"/v1/review-runs/{artifact_dir.name}/artifacts/{item.get('artifact_id')}"
        if storage.get("type") != "server_artifact" or storage_url != expected_storage_url:
            errors.append(f"artifact {path.name} storage must reference server_artifact")


def _report_artifact_reference_strings(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        references: list[str] = []
        for item in value:
            references.extend(_report_artifact_reference_strings(item))
        return references
    if isinstance(value, dict):
        references = []
        for item in value.values():
            references.extend(_report_artifact_reference_strings(item))
        return references
    return []


def _looks_like_report_artifact_reference(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.endswith((".json", ".jsonl", ".log", ".md", ".zip"))


def report_artifact_references(report: object) -> set[str]:
    if not isinstance(report, dict):
        return set()
    references = set(_report_artifact_reference_strings(report.get("artifacts")))
    references.update(_report_artifact_reference_strings(report.get("raw_artifact_refs")))
    non_artifact_validation_keys = {
        "related_code",
        "relatedcode",
        "source",
        "sources",
        "path",
        "paths",
        "location",
        "locations",
    }

    def validation_source_references(value: object) -> set[str]:
        if isinstance(value, list):
            return {
                reference
                for reference in _report_artifact_reference_strings(value)
                if _looks_like_report_artifact_reference(reference)
            }
        if not isinstance(value, dict):
            return set()
        found: set[str] = set()
        for key, item in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in non_artifact_validation_keys:
                continue
            if isinstance(item, dict):
                found.update(validation_source_references(item))
                continue
            for reference in _report_artifact_reference_strings(item):
                if _looks_like_report_artifact_reference(reference):
                    found.add(reference)
        return found

    for collection_name in ("findings", "appendix_findings", "disproven_findings"):
        collection = report.get(collection_name)
        if not isinstance(collection, list):
            continue
        for finding in collection:
            if isinstance(finding, dict):
                references.update(validation_source_references(finding.get("validation_sources")))
    return references


def validate_report_artifact_references_for_qa(
    report: object,
    run_dir: Path,
    artifact_dir: Path | None,
    errors: list[str],
) -> None:
    references = report_artifact_references(report)
    if not references:
        return
    manifest_names: set[str] | None = None
    if artifact_dir is not None:
        manifest_payload = read_json(artifact_dir / "artifact-manifest.json", {})
        manifest = manifest_payload.get("items") if isinstance(manifest_payload, dict) else None
        if isinstance(manifest, list):
            manifest_names = {
                str(item.get("name") or "").strip()
                for item in manifest
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }
    for reference in sorted(references):
        normalized = reference.replace("\\", "/")
        reference_path = PurePosixPath(normalized)
        if (
            normalized != reference
            or reference_path.is_absolute()
            or len(reference_path.parts) != 1
            or reference_path.name in {"", ".", ".."}
        ):
            errors.append(
                "report.agent.json artifact reference must name a top-level output: "
                + reference
            )
            continue
        run_output = run_dir / reference
        if run_output.is_symlink() or not run_output.is_file():
            errors.append(
                "report.agent.json artifact reference is missing from run outputs: "
                + reference
            )
            continue
        if manifest_names is not None and reference not in manifest_names:
            errors.append(
                "report.agent.json artifact reference is missing from artifact-manifest.json: "
                + reference
            )


MAIN_FINDING_VALIDATION_STATUSES = {"confirmed", "plausible", "validated"}
WEAK_FINDING_VALIDATION_STATUSES = {"weak", "suppressed", "unresolved", "appendix"}
DISPROVEN_FINDING_VALIDATION_STATUSES = {"disproven", "rejected", "false_positive", "invalid"}
FINDING_ID_ALIAS_FIELDS = (
    "id",
    "finding_id",
    "finding_ids",
    "cluster_id",
    "source_cluster_id",
    "candidate_id",
    "canonical_finding_id",
    "local_id",
    "source_finding_id",
    "source_finding_ids",
)
VALIDATION_STATUS_ALIAS_FIELDS = ("status", "validator_status", "validation_status", "classification", "disposition")


def _binding_scalar_ids(value: object) -> set[str]:
    ids: set[str] = set()
    if isinstance(value, (list, tuple, set)):
        for item in value:
            ids.update(_binding_scalar_ids(item))
        return ids
    text = str(value or "").strip()
    if text:
        ids.add(text)
    return ids


def finding_binding_ids(record: object) -> set[str]:
    if not isinstance(record, dict):
        return set()
    ids: set[str] = set()
    for field in FINDING_ID_ALIAS_FIELDS:
        ids.update(_binding_scalar_ids(record.get(field)))
    return ids


def validation_entry_status(entry: object) -> str:
    if not isinstance(entry, dict):
        return ""
    for field in VALIDATION_STATUS_ALIAS_FIELDS:
        status = str(entry.get(field) or "").strip().lower()
        if status:
            return status
    return ""


def validation_entry_is_main_backing(entry: object) -> bool:
    return validation_entry_status(entry) in MAIN_FINDING_VALIDATION_STATUSES


def _validation_collection(payload: dict[str, Any], *keys: str) -> list[Any] | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return None


def _normalized_validation_entry(entry: object, *, default_status: str = "") -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    normalized = dict(entry)
    status = validation_entry_status(entry) or default_status
    if status:
        normalized["status"] = status
    return normalized


def repair_validation_output_artifact(path: Path) -> None:
    if not path.is_file():
        return
    payload = read_json(path, {})
    if not isinstance(payload, dict):
        return
    payload["schema_version"] = "validation-output/v1"
    raw_main = _validation_collection(
        payload,
        "validated_findings",
        "validated",
        "findings",
        "results",
        "validation_results",
    ) or []
    raw_weak = _validation_collection(payload, "weak_findings", "weak") or []
    raw_disproven = _validation_collection(payload, "disproven_findings", "disproven", "rejected_findings") or []

    main: list[dict[str, Any]] = []
    weak: list[dict[str, Any]] = []
    disproven: list[dict[str, Any]] = []
    for raw_entry in raw_main:
        entry = _normalized_validation_entry(raw_entry)
        if entry is None:
            continue
        status = validation_entry_status(entry)
        if status in WEAK_FINDING_VALIDATION_STATUSES:
            weak.append(entry)
        elif status in DISPROVEN_FINDING_VALIDATION_STATUSES:
            disproven.append(entry)
        else:
            # Preserve missing or unknown dispositions in the canonical main
            # collection so strict validation requests a semantic repair instead
            # of silently treating them as confirmed or dropping them.
            main.append(entry)
    weak.extend(
        entry
        for raw_entry in raw_weak
        if (entry := _normalized_validation_entry(raw_entry, default_status="weak")) is not None
    )
    disproven.extend(
        entry
        for raw_entry in raw_disproven
        if (entry := _normalized_validation_entry(raw_entry, default_status="disproven")) is not None
    )
    payload["validated_findings"] = main
    payload["weak_findings"] = weak
    payload["disproven_findings"] = disproven
    write_json(path, payload)


def validation_output_errors(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return ["validated-findings.json must be an object"]
    errors: list[str] = []
    if payload.get("schema_version") != "validation-output/v1":
        errors.append("validated-findings.json must use schema_version validation-output/v1")
    for collection_name, allowed_statuses in (
        ("validated_findings", MAIN_FINDING_VALIDATION_STATUSES),
        ("weak_findings", WEAK_FINDING_VALIDATION_STATUSES),
        ("disproven_findings", DISPROVEN_FINDING_VALIDATION_STATUSES),
    ):
        entries = payload.get(collection_name)
        if not isinstance(entries, list):
            errors.append(f"validated-findings.json {collection_name} must be a list")
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"validated-findings.json {collection_name}[{index}] must be an object")
                continue
            status = validation_entry_status(entry)
            if status not in allowed_statuses:
                errors.append(
                    f"validated-findings.json {collection_name}[{index}] has unsupported disposition {status or 'missing'}"
                )
    return errors


def finding_validation_status(finding: object) -> str:
    if not isinstance(finding, dict):
        return ""
    direct = str(
        finding.get("validator_status")
        or finding.get("validation_status")
        or finding.get("classification")
        or ""
    ).strip().lower()
    if direct:
        return direct
    validation_sources = finding.get("validation_sources")
    if not isinstance(validation_sources, dict):
        validation_sources = finding.get("validation") if isinstance(finding.get("validation"), dict) else {}
    return str(
        validation_sources.get("validator_status")
        or validation_sources.get("validation_status")
        or validation_sources.get("status")
        or validation_sources.get("verdict")
        or ""
    ).strip().lower()


def _binding_title(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    return " ".join(str(record.get("title") or "").strip().lower().split())


def _binding_location_source(record: dict[str, Any]) -> dict[str, Any]:
    locations = record.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict):
                return location
    location = record.get("location")
    if isinstance(location, dict):
        return location
    return record


def _binding_path(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    source = _binding_location_source(record)
    for field in ("path", "file", "primaryFile", "primary_file"):
        text = str(source.get(field) or record.get(field) or "").strip().replace("\\", "/")
        if text:
            while text.startswith("./"):
                text = text[2:]
            return text
    return ""


def _binding_start_line(record: object) -> int:
    if not isinstance(record, dict):
        return 0
    source = _binding_location_source(record)
    for field in ("start_line", "line", "line_start", "primaryLine", "primary_line", "startLine", "lineStart"):
        value = source.get(field) if field in source else record.get(field)
        line = _qa_int(value)
        if line > 0:
            return line
    return 0


def finding_fallback_binding_key(record: object) -> tuple[str, str, int] | None:
    if not isinstance(record, dict):
        return None
    title = _binding_title(record)
    path = _binding_path(record)
    start_line = _binding_start_line(record)
    if title and path and start_line > 0:
        return (title, path, start_line)
    return None


def validation_binding_entries(run_dir: Path) -> tuple[bool, list[dict[str, Any]]]:
    path = run_dir / "validated-findings.json"
    if not path.is_file():
        return (False, [])
    payload = read_json(path, None)
    if not isinstance(payload, dict):
        return (False, [])
    if payload.get("schema_version") != "validation-output/v1":
        return (False, [])
    entries = payload.get("validated_findings")
    if not isinstance(entries, list):
        return (False, [])
    accepted = [entry for entry in entries if isinstance(entry, dict) and validation_entry_is_main_backing(entry)]
    return (True, accepted)


def matching_validation_entry(
    finding: object,
    accepted_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(finding, dict):
        return None
    ids = finding_binding_ids(finding)
    if ids:
        id_matches = [entry for entry in accepted_entries if ids & finding_binding_ids(entry)]
        if len(id_matches) == 1:
            return id_matches[0]
        if len(id_matches) > 1:
            return None
    key = finding_fallback_binding_key(finding)
    if key is None:
        return None
    matches = [entry for entry in accepted_entries if finding_fallback_binding_key(entry) == key]
    return matches[0] if len(matches) == 1 else None


def finding_is_backed_by_validation(finding: object, accepted_entries: list[dict[str, Any]]) -> bool:
    return matching_validation_entry(finding, accepted_entries) is not None


def validation_entry_label(entry: dict[str, Any], index: int) -> str:
    for field in ("id", "cluster_id", "candidate_id", "finding_id"):
        values = sorted(_binding_scalar_ids(entry.get(field)))
        if values:
            return values[0]
    values = sorted(finding_binding_ids(entry))
    return values[0] if values else f"validation[{index}]"

def validate_source_unmodified_for_qa(repo_dir: Path, run_dir: Path, errors: list[str]) -> None:
    inventory_payload = read_json(run_dir / "inventory.json", {})
    files = inventory_payload.get("files") if isinstance(inventory_payload.get("files"), list) else None
    if files is None:
        errors.append("inventory.json is missing or invalid; cannot verify source files are unmodified")
        return
    repo_root = repo_dir.resolve(strict=False)
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not item.get("is_source_like"):
            continue
        rel = str(item.get("path") or "").strip()
        expected_sha = str(item.get("sha256") or "").strip()
        if not rel or not expected_sha:
            errors.append(f"inventory.files[{index}] missing source path or sha256")
            continue
        path = (repo_dir / rel).resolve(strict=False)
        try:
            path.relative_to(repo_root)
        except ValueError:
            errors.append(f"inventory.files[{index}] path escapes repository")
            continue
        if not path.is_file():
            errors.append(f"source file modified or removed since inventory: {rel}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha:
            errors.append(f"source file modified since inventory: {rel}")


def qa_gate_payload(
    repo_dir: Path,
    run_dir: Path,
    artifact_dir: Path | None = None,
    *,
    expected_output_language: str = "",
) -> dict[str, Any]:
    errors = []
    warnings = []
    for name in ("report.agent.json", "report.md", "coverage.json", "token-budget.json"):
        if not (run_dir / name).is_file():
            errors.append(f"{name} is missing")
    if artifact_dir is not None and not (run_dir / "qa.json").is_file():
        errors.append("qa.json is missing")
    report = read_json(run_dir / "report.agent.json", {})
    if not isinstance(report, dict) or report.get("schema_id") != "codex-full-repo-review":
        errors.append("report.agent.json is not a codex-full-repo-review object")
    validate_report_artifact_references_for_qa(report, run_dir, artifact_dir, errors)
    if expected_output_language:
        actual_output_language = str(report.get("output_language") or "").strip()
        if actual_output_language != expected_output_language:
            errors.append(
                "report.agent.json output_language does not match the claimed review request"
            )
        try:
            markdown_text = (run_dir / "report.md").read_text(encoding="utf-8")
        except OSError:
            markdown_text = ""
        if expected_output_language == "zh-CN" and markdown_text and not markdown_text.startswith(
            "# Codex 全仓库审查报告"
        ):
            errors.append("report.md does not use the requested zh-CN output language")
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append(f"finding[{index}] is not an object")
            continue
        for field in ("title", "severity", "confidence", "evidence", "impact", "recommendation"):
            if finding.get(field) in (None, "", []):
                errors.append(f"finding[{index}].{field} is missing")
        locations = finding.get("locations") if isinstance(finding.get("locations"), list) else []
        if not locations:
            errors.append(f"finding[{index}].locations is missing")
        for location in locations:
            if not isinstance(location, dict):
                errors.append(f"finding[{index}].locations has invalid entry")
                continue
            rel = str(location.get("path") or "")
            start = _qa_int(location.get("start_line") or location.get("line_start") or location.get("line"))
            end = _qa_int(location.get("end_line") or location.get("line_end") or start)
            try:
                location_path = (repo_dir / rel).resolve(strict=False)
                location_path.relative_to(repo_dir.resolve(strict=False))
            except ValueError:
                location_path = repo_dir / "__invalid__"
            line_count = 0
            if location_path.is_file():
                try:
                    line_count = len(
                        location_path.read_text(
                            encoding='utf-8',
                            errors='replace',
                        ).splitlines()
                    )
                except OSError:
                    line_count = 0
            if (
                not rel
                or start <= 0
                or end < start
                or not location_path.is_file()
                or end > line_count
            ):
                errors.append(f"finding[{index}] has invalid location line range")
        confidence = finding.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= float(confidence) <= 1:
            errors.append(f"finding[{index}].confidence is outside 0..1")
        validation_sources = finding.get("validation_sources") if isinstance(finding.get("validation_sources"), dict) else {}
        intent_signal = validation_sources.get("intent_test") if isinstance(validation_sources.get("intent_test"), dict) else {}
        validator_status = str(finding.get("validator_status") or validation_sources.get("validator_status") or "").strip()
        intent_classification = str(intent_signal.get("classification") or "").strip()
        if intent_classification in {"confirmed_bug", "plausible_bug"} and validator_status not in {"confirmed", "plausible", "validated"}:
            errors.append(f"finding[{index}] uses bug-supporting intent test signal without validator_status")
        if expected_output_language == "zh-CN":
            for field in ("title", "impact", "recommendation"):
                value = str(finding.get(field) or "")
                if value and re.search(r"[\u3400-\u9fff]", value) is None:
                    errors.append(
                        f"finding[{index}].{field} does not use the requested zh-CN output language"
                    )
    validation_ok, accepted_validation = validation_binding_entries(run_dir)
    matched_validation_entries: set[int] = set()
    if findings:
        if not validation_ok:
            errors.append("validated-findings.json is missing or invalid for non-empty main findings")
        else:
            for index, finding in enumerate(findings):
                matching_entry = matching_validation_entry(finding, accepted_validation)
                if matching_entry is None:
                    errors.append(f"finding[{index}] is not backed by confirmed/plausible validation")
                else:
                    entry_identity = id(matching_entry)
                    if entry_identity in matched_validation_entries:
                        errors.append(
                            f'finding[{index}] reuses validation evidence already bound to another main finding'
                        )
                    else:
                        matched_validation_entries.add(entry_identity)
    if validation_ok:
        for index, entry in enumerate(accepted_validation):
            if id(entry) not in matched_validation_entries:
                errors.append(
                    f"validated main finding {validation_entry_label(entry, index)} is missing from report.agent.json"
                )
    coverage = read_json(run_dir / "coverage.json", {})
    if isinstance(coverage, dict):
        total = _qa_int(coverage.get("source_like_files_total"))
        reviewed = sum(_qa_int(coverage.get(key)) for key in ("deep_reviewed_files", "standard_reviewed_files", "light_reviewed_files", "inventory_only_files"))
        skipped = _qa_int(coverage.get("skipped_files"))
        if reviewed > total:
            errors.append("coverage reviewed counts exceed source_like_files_total")
        if total and reviewed + skipped != total:
            errors.append("coverage reviewed and skipped counts must add up to source_like_files_total")
        if skipped and not (coverage.get("skipped_reasons") or coverage.get("skipped_files_explained")):
            errors.append("coverage skipped files must be explained")
    else:
        errors.append("coverage.json is not valid")
    validate_source_unmodified_for_qa(repo_dir, run_dir, errors)
    intent_results = run_dir / "intent" / "intent-test-results.json"
    if not intent_results.exists():
        intent_validation = read_json(
            run_dir / "intent" / "intent-test-validation.json",
            None,
        )
        if not (
            isinstance(intent_validation, dict)
            and intent_validation.get("enabled") is False
        ):
            missing_error = intent_validation_missing_results_error(run_dir)
            if missing_error:
                errors.append(missing_error)
            else:
                warnings.append("intent-test-results.json is missing; no intent tests may have been selected or runnable")
    else:
        payload = read_json(intent_results, {})
        errors.extend(intent_test_result_errors(payload, run_dir))
        intent_counts = intent_test_artifact_counts(run_dir)
        if intent_counts["intent_tests_planned"] > 0 and intent_counts["intent_tests_run"] == 0:
            warnings.append(
                "intent-test validation was planned but no generated test process started; dynamic evidence is unavailable"
            )
        elif intent_counts["intent_tests_run"] > 0 and intent_counts["intent_tests_asserted"] == 0:
            warnings.append(
                "intent-test processes started but no assertion-level result was established; dynamic evidence is degraded"
            )
    source_path = run_dir / "intent" / "intent-test-source.json"
    if source_path.exists():
        source_payload = read_json(source_path, {})
        errors.extend(intent_test_source_errors(run_dir, source_payload))
        generated_tests = source_payload.get("generated_tests", []) if isinstance(source_payload, dict) else []
        if isinstance(generated_tests, list):
            for index, generated in enumerate(generated_tests):
                if not isinstance(generated, dict):
                    continue
                refs = generated.get("artifact_refs") or generated.get("artifactRefs") or []
                if not refs:
                    errors.append(f"generated_tests[{index}] missing artifact_refs")
    if artifact_dir is not None:
        validate_artifact_manifest_for_qa(run_dir, artifact_dir, errors)
    return {
        "schema_version": "qa/v1",
        "run_id": run_dir.name,
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "warnings": warnings,
    }


def _json_list_len(value: Any) -> int:
    return len(value) if isinstance(value, list) else 0


def _json_file_count(path: Path) -> int:
    return len(list(path.glob("*.json"))) if path.is_dir() else 0


def _planned_bundle_ids(run_dir: Path) -> set[str]:
    bundles = read_json(run_dir / "bundle-plan.json", {}).get("bundles", [])
    if not isinstance(bundles, list):
        return set()
    bundle_ids: set[str] = set()
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        bundle_id = str(bundle.get("bundle_id") or bundle.get("id") or "").strip()
        if bundle_id:
            bundle_ids.add(bundle_id)
    return bundle_ids


def _normalized_reviewer_id(value: object) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "correctnesslite": "correctness_lite",
        "testgap": "test_gap",
    }
    return aliases.get(text, text)


def _normalized_review_bundle_id(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        return ""
    name = PurePosixPath(text).name
    if name.lower().endswith(".md"):
        name = name[:-3]
    return name.strip()


def _planned_reviewer_assignments(run_dir: Path) -> set[tuple[str, str]]:
    bundles = read_json(run_dir / "bundle-plan.json", {}).get("bundles", [])
    if not isinstance(bundles, list):
        return set()
    assignments: set[tuple[str, str]] = set()
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        bundle_id = _normalized_review_bundle_id(bundle.get("bundle_id") or bundle.get("id"))
        reviewers = bundle.get("reviewers")
        if not bundle_id or not isinstance(reviewers, list):
            continue
        for reviewer in reviewers:
            reviewer_id = _normalized_reviewer_id(reviewer)
            if reviewer_id:
                assignments.add((bundle_id, reviewer_id))
    return assignments


def planned_reviewer_assignment_sequence(run_dir: Path) -> list[tuple[str, str]]:
    bundles = read_json(run_dir / "bundle-plan.json", {}).get("bundles", [])
    if not isinstance(bundles, list):
        return []
    assignments: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        bundle_id = _normalized_review_bundle_id(bundle.get("bundle_id") or bundle.get("id"))
        reviewers = bundle.get("reviewers")
        if not bundle_id or not isinstance(reviewers, list):
            continue
        for reviewer in reviewers:
            reviewer_id = _normalized_reviewer_id(reviewer)
            assignment = (bundle_id, reviewer_id)
            if not reviewer_id or assignment in seen:
                continue
            seen.add(assignment)
            assignments.append(assignment)
    return assignments


def _reviewer_output_assignments(
    payload: object,
    path: Path,
    expected_assignments: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    if not isinstance(payload, dict):
        return set()
    expected_bundle_ids = {bundle_id for bundle_id, _reviewer_id in expected_assignments}
    expected_reviewer_ids = {reviewer_id for _bundle_id, reviewer_id in expected_assignments}
    reviewer_id = _normalized_reviewer_id(
        payload.get("reviewer_id")
        or payload.get("reviewer")
        or payload.get("perspective")
    )
    file_key = path.stem.lower().replace("-", "_")
    if not reviewer_id:
        reviewer_id = next(
            (candidate for candidate in sorted(expected_reviewer_ids, key=len, reverse=True) if candidate in file_key),
            "",
        )
    raw_bundle_values: list[object] = []
    for key in ("bundle_id", "bundle"):
        value = payload.get(key)
        if value not in (None, ""):
            raw_bundle_values.append(value)
    for key in (
        "bundles_reviewed",
        "reviewed_bundles",
        "bundle_ids",
        "target_bundle_ids",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            raw_bundle_values.extend(value)
    bundle_ids = {
        bundle_id
        for bundle_id in (_normalized_review_bundle_id(value) for value in raw_bundle_values)
        if bundle_id
    }
    if not bundle_ids:
        bundle_ids = {
            bundle_id
            for bundle_id in expected_bundle_ids
            if bundle_id.lower().replace("-", "_") in file_key
        }
    if not reviewer_id:
        return set()
    return {(bundle_id, reviewer_id) for bundle_id in bundle_ids}


def _reviewer_assignments_from_outputs(
    run_dir: Path,
    expected_assignments: set[tuple[str, str]],
) -> set[tuple[str, str]]:
    raw_dir = run_dir / "raw-reviewers"
    verified_dir = run_dir / "verified-reviewers"
    output_dir = raw_dir if raw_dir.is_dir() and any(raw_dir.glob("*.json")) else verified_dir
    assignments: set[tuple[str, str]] = set()
    if not output_dir.is_dir():
        return assignments
    for output_path in output_dir.glob("*.json"):
        assignments.update(
            _reviewer_output_assignments(
                read_json(output_path, {}),
                output_path,
                expected_assignments,
            )
        )
    return assignments


def _reviewed_bundle_ids_from_reviewer_outputs(run_dir: Path) -> set[str]:
    reviewed: set[str] = set()
    for output_dir in (run_dir / "raw-reviewers", run_dir / "verified-reviewers"):
        if not output_dir.is_dir():
            continue
        for output_path in output_dir.glob("*.json"):
            payload = read_json(output_path, {})
            if not isinstance(payload, dict):
                continue
            for key in ("bundles_reviewed", "reviewed_bundles", "bundle_ids", "target_bundle_ids"):
                values = payload.get(key)
                if isinstance(values, list):
                    reviewed.update(
                        bundle_id
                        for bundle_id in (_normalized_review_bundle_id(value) for value in values)
                        if bundle_id
                    )
    return reviewed


def reviewer_fanout_artifact_counts(run_dir: Path) -> dict[str, int]:
    expected_assignments = _planned_reviewer_assignments(run_dir)
    if expected_assignments:
        covered_assignments = _reviewer_assignments_from_outputs(run_dir, expected_assignments)
        return {
            "reviewer_runs_total": len(expected_assignments),
            "reviewer_runs_completed": len(expected_assignments & covered_assignments),
        }
    planned_bundle_ids = _planned_bundle_ids(run_dir)
    planned = len(planned_bundle_ids)
    raw_count = _json_file_count(run_dir / "raw-reviewers")
    verified_count = _json_file_count(run_dir / "verified-reviewers")
    observed = max(raw_count, verified_count)
    if not observed:
        return {"reviewer_runs_total": planned, "reviewer_runs_completed": 0}

    total = planned
    if not total or observed >= total:
        total = observed
    elif planned_bundle_ids.issubset(_reviewed_bundle_ids_from_reviewer_outputs(run_dir)):
        total = observed
    return {"reviewer_runs_total": total, "reviewer_runs_completed": min(observed, total)}


def intent_test_artifact_counts(run_dir: Path) -> dict[str, int]:
    plan_targets = read_json(run_dir / "intent" / "intent-test-plan.json", {}).get("test_targets", [])
    generated = read_json(run_dir / "intent" / "intent-test-source.json", {}).get("generated_tests", [])
    raw_runs = read_json(run_dir / "intent" / "intent-test-results.raw.json", {}).get("test_runs", [])
    analyzed_runs = read_json(run_dir / "intent" / "intent-test-results.json", {}).get("test_results", [])

    def logical_ids(
        records: Any,
        prefix: str,
        *,
        planned: set[str] | None = None,
        include_record: Any = None,
        related_ids_by_test_id: dict[str, set[str]] | None = None,
        allowed_test_ids: set[str] | None = None,
    ) -> set[str]:
        ids: set[str] = set()
        if not isinstance(records, list):
            return ids
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                continue
            if include_record is not None and not include_record(record):
                continue
            test_id = _intent_test_id(record, f"{prefix}-{index + 1:03d}")
            if allowed_test_ids is not None and test_id not in allowed_test_ids:
                continue
            candidates = [test_id, *_intent_related_test_ids(record)]
            if related_ids_by_test_id is not None:
                candidates.extend(sorted(related_ids_by_test_id.get(test_id, set())))
            candidates = list(dict.fromkeys(candidate for candidate in candidates if candidate))
            if planned is not None:
                ids.update(candidate for candidate in candidates if candidate in planned)
            elif test_id:
                ids.add(test_id)
        return ids

    executable_generated = _intent_generated_execution_records(generated if isinstance(generated, list) else [])
    planned_ids = logical_ids(plan_targets, "ITP")
    raw_plan_ids_by_test_id: dict[str, set[str]] = {}
    if isinstance(raw_runs, list):
        for index, record in enumerate(raw_runs):
            if not isinstance(record, dict):
                continue
            test_id = _intent_test_id(record, f'ITR-{index + 1:03d}')
            candidates = [test_id, *_intent_related_test_ids(record)]
            raw_plan_ids_by_test_id[test_id] = {
                candidate
                for candidate in candidates
                if candidate in planned_ids
            }
    written_ids = logical_ids(executable_generated, "ITV", planned=planned_ids)
    attempted_ids = logical_ids(raw_runs, "ITR", planned=planned_ids)
    run_ids = logical_ids(
        raw_runs,
        "ITR",
        planned=planned_ids,
        include_record=lambda record: str(record.get("status") or "").strip().lower() != "skipped"
        and bool(str(record.get("command") or record.get("sandbox_command") or "").strip()),
    )
    analyzed_ids = logical_ids(
        analyzed_runs,
        "ITA",
        planned=planned_ids,
        related_ids_by_test_id=raw_plan_ids_by_test_id,
        allowed_test_ids=set(raw_plan_ids_by_test_id),
    )
    assertion_ids = logical_ids(
        analyzed_runs,
        "ITA",
        planned=planned_ids,
        related_ids_by_test_id=raw_plan_ids_by_test_id,
        allowed_test_ids=set(raw_plan_ids_by_test_id),
        include_record=lambda record: str(record.get("classification") or "").strip()
        in {"confirmed_bug", "plausible_bug", "test_oracle_wrong", "passed_no_bug_reproduced"},
    )
    all_ids = planned_ids
    total = len(all_ids)
    return {
        "intent_tests_total": total,
        "intent_tests_planned": len(planned_ids),
        "intent_tests_written": min(len(written_ids), total) if total else 0,
        "intent_tests_attempted": min(len(attempted_ids), total) if total else 0,
        "intent_tests_run": min(len(run_ids), total) if total else 0,
        "intent_tests_asserted": min(len(assertion_ids), total) if total else 0,
        "intent_tests_analyzed": min(len(analyzed_ids), total) if total else 0,
    }


def phase_progress_data(run_dir: Path, phase: str, artifact_dir: Path | None = None) -> dict[str, Any]:
    if phase == "reviewer_fanout":
        return reviewer_fanout_artifact_counts(run_dir)
    if phase == "intent_test_validation":
        return intent_test_artifact_counts(run_dir)
    if phase == "upload_artifacts":
        manifest_dir = artifact_dir or run_dir
        manifest = artifact_manifest_items(read_json(manifest_dir / "artifact-manifest.json", {}))
        uploadable = 0
        for item in manifest:
            name = str(item.get("name") or "").strip()
            if name and (manifest_dir / name).is_file():
                uploadable += 1
        snapshot_path = uploaded_artifact_manifest_path(manifest_dir)
        if snapshot_path.is_file():
            return {
                'artifacts_total': uploadable,
                'artifacts_uploaded': len(uploaded_artifact_manifest_items(manifest_dir)),
            }
        return {"artifacts_total": uploadable, "artifacts_uploaded": uploadable}
    return {}


def refresh_coverage_intent_counters(run_dir: Path) -> None:
    coverage = read_json(run_dir / "coverage.json", {})
    if not isinstance(coverage, dict) or coverage.get("schema_version") != "coverage/v1":
        return
    counts = intent_test_artifact_counts(run_dir)
    analyzed_results = read_json(run_dir / "intent" / "intent-test-results.json", {}).get("test_results", [])
    coverage["intent_tests_planned"] = counts["intent_tests_planned"]
    coverage["intent_tests_attempted"] = counts["intent_tests_attempted"]
    coverage["intent_tests_run"] = counts["intent_tests_run"]
    coverage["intent_tests_asserted"] = counts["intent_tests_asserted"]
    supporting_ids: set[str] = set()
    if isinstance(analyzed_results, list):
        for result in analyzed_results:
            if not isinstance(result, dict):
                continue
            for finding_id in result.get("linked_finding_ids") or []:
                if str(finding_id).strip():
                    supporting_ids.add(str(finding_id).strip())
    coverage["intent_tests_supporting_findings"] = len(supporting_ids)
    write_json(run_dir / "coverage.json", coverage)

def phase_output_path(run_dir: Path, artifact_dir: Path | None, output: str) -> Path:
    if output.startswith("artifact:"):
        if artifact_dir is None:
            raise RuntimeError("artifact output validation requires artifact_dir")
        return artifact_dir / output.removeprefix("artifact:")
    return run_dir / output


def parse_required_json_output(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise RuntimeError(f"required phase output is missing: {path.name}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"required phase output is not valid JSON: {path.name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"required phase output must be a JSON object: {path.name}")
    return payload


def validate_phase_outputs(run_dir: Path, phase: str, artifact_dir: Path | None = None) -> None:
    for rel, expected_schema in PHASE_JSON_OUTPUTS.get(phase, ()):
        path = phase_output_path(run_dir, artifact_dir, rel)
        if rel == "risk-routing.json" and path.is_file():
            normalize_risk_routing_artifact(path)
        if rel == "clusters.json" and path.is_file():
            normalize_cluster_output_artifact(path)
        payload = parse_required_json_output(path)
        if expected_schema and str(payload.get("schema_version") or "").strip() != expected_schema:
            raise RuntimeError(f"required phase output {path.name} must use schema_version {expected_schema}")
        if rel == "bootstrap_helper_scripts.summary.json":
            if str(payload.get("status") or "").strip() != "completed":
                raise RuntimeError("bootstrap helper summary status must be completed")
            review_root = run_dir.parent.parent
            asset_groups = (
                ("tools", review_root / "tools", REQUIRED_TOOL_FILES),
                ("schemas", review_root / "schemas", REQUIRED_SCHEMA_FILES),
                ("prompts", review_root / "prompts", REQUIRED_PROMPT_FILES),
            )
            for label, root, names in asset_groups:
                missing = [name for name in names if not (root / name).is_file() or (root / name).is_symlink()]
                if missing:
                    raise RuntimeError(
                        f"bootstrap helper asset {label} missing: {', '.join(missing)}"
                    )
                required_key = f"required_{label}"
                materialized_key = f"materialized_{label}"
                if payload.get(required_key) != len(names) or payload.get(materialized_key) != len(names):
                    raise RuntimeError(
                        f"bootstrap helper summary {label} counts must equal {len(names)}"
                    )
        if rel == "report.agent.json":
            errors = agent_report_contract_errors(payload)
            if errors:
                raise RuntimeError(errors[0])
        if rel == "risk-routing.json":
            errors = risk_routing_contract_errors(payload, run_dir)
            if errors:
                raise RuntimeError(errors[0])
        if rel == "clusters.json":
            errors = cluster_output_contract_errors(payload)
            if errors:
                raise RuntimeError(errors[0])
        if rel == "bundle-grouping.json":
            errors = bundle_grouping_contract_errors(run_dir, payload)
            if errors:
                raise RuntimeError("; ".join(errors))
        if rel == "validated-findings.json":
            errors = validation_output_errors(payload)
            if errors:
                raise RuntimeError(errors[0])
        if rel == "json-errors.json":
            errors = payload.get("errors") if isinstance(payload.get("errors"), list) else []
            if errors:
                first = errors[0]
                if isinstance(first, dict):
                    raise RuntimeError(f"reviewer JSON validation failed for {first.get('file')}: {first.get('error')}")
                raise RuntimeError(f"reviewer JSON validation failed: {first}")
        if rel == "intent/intent-map.json":
            errors = intent_map_errors(payload)
            if errors:
                raise RuntimeError(errors[0])
        if rel == "intent/intent-test-plan.json":
            errors = intent_test_plan_errors(run_dir, payload)
            if errors:
                raise RuntimeError(errors[0])
        if rel == "intent/intent-test-source.json":
            errors = intent_test_source_errors(run_dir, payload)
            if errors:
                raise RuntimeError(errors[0])
        if rel == "intent/intent-test-results.raw.json":
            errors = intent_test_raw_run_errors(run_dir, payload)
            if errors:
                raise RuntimeError(errors[0])
        if rel == "intent/intent-test-results.json":
            errors = intent_test_result_errors(payload, run_dir)
            if errors:
                raise RuntimeError(errors[0])
    for rel in PHASE_PATH_OUTPUTS.get(phase, ()):
        path = phase_output_path(run_dir, artifact_dir, rel)
        if not path.exists():
            raise RuntimeError(f"required phase output is missing: {path.name}")
        if phase == "reviewer_fanout" and path.name == "raw-reviewers":
            raw_files = sorted(path.glob("*.json")) if path.is_dir() else []
            if not raw_files:
                raise RuntimeError("reviewer_fanout produced no raw reviewer JSON outputs")
            for raw_file in raw_files:
                if not raw_file.is_file() or raw_file.stat().st_size == 0:
                    raise RuntimeError(f"raw reviewer output {raw_file.name} is empty")
            expected_assignments = _planned_reviewer_assignments(run_dir)
            if expected_assignments:
                covered_assignments = _reviewer_assignments_from_outputs(run_dir, expected_assignments)
                missing_assignments = sorted(expected_assignments - covered_assignments)
                if missing_assignments:
                    missing_text = ", ".join(
                        f"{bundle_id}:{reviewer_id}"
                        for bundle_id, reviewer_id in missing_assignments
                    )
                    raise RuntimeError(
                        f"reviewer_fanout missing planned reviewer assignments: {missing_text}"
                    )
                unexpected_assignments = sorted(covered_assignments - expected_assignments)
                if unexpected_assignments:
                    unexpected_text = ", ".join(
                        f"{bundle_id}:{reviewer_id}"
                        for bundle_id, reviewer_id in unexpected_assignments
                    )
                    raise RuntimeError(
                        f"reviewer_fanout produced unplanned reviewer assignments: {unexpected_text}"
                    )
        if path.is_file() and path.name == "artifact-manifest.json":
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("artifact-manifest.json must be valid JSON") from exc
            if not isinstance(manifest, dict):
                raise RuntimeError("artifact-manifest.json must be an object")
            if manifest.get("schema_version") != "artifact-manifest/v1":
                raise RuntimeError("artifact-manifest.json must use schema_version artifact-manifest/v1")
            if not isinstance(manifest.get("items"), list):
                raise RuntimeError("artifact-manifest.json items must be a list")


def phase_completion_data(run_dir: Path, phase: str, artifact_dir: Path | None = None) -> dict[str, Any]:
    progress_data = phase_progress_data(run_dir, phase, artifact_dir)
    if progress_data:
        return progress_data
    if phase == "inventory_repository":
        inventory_payload = read_json(run_dir / "inventory.json", {})
        if not isinstance(inventory_payload, dict) or not (run_dir / "inventory.json").is_file():
            return {}
        summary = inventory_payload.get("summary") if isinstance(inventory_payload.get("summary"), dict) else {}
        files = inventory_payload.get("files") if isinstance(inventory_payload.get("files"), list) else []
        source_like = sum(1 for item in files if isinstance(item, dict) and item.get("is_source_like") is True)
        try:
            source_like = max(source_like, int(summary.get("source_like_files") or 0))
        except (TypeError, ValueError):
            pass
        return {"source_like_files_total": source_like}
    if phase == "risk_routing":
        inventory_payload = read_json(run_dir / "inventory.json", {})
        routing_payload = read_json(run_dir / "risk-routing.json", {})
        if not isinstance(inventory_payload, dict) or not isinstance(routing_payload, dict):
            return {}
        files = inventory_payload.get("files") if isinstance(inventory_payload.get("files"), list) else []
        source_paths = {
            str(item.get("path") or "").strip()
            for item in files
            if isinstance(item, dict) and item.get("is_source_like") is True and str(item.get("path") or "").strip()
        }
        profile = read_json(run_dir / "repo-profile.json", {})
        normalized_routing = effective_routing(routing_payload, profile, inventory_payload)
        classified_paths = {
            str(route.get("path") or "").strip()
            for route in normalized_routing.get("routes", [])
            if isinstance(route, dict) and str(route.get("path") or "").strip()
        }
        return {
            "source_like_files_total": len(source_paths),
            "source_like_files_classified": len(source_paths.intersection(classified_paths)),
        }
    if phase == "bundle_planning":
        bundles = read_json(run_dir / "bundle-plan.json", {}).get("bundles", [])
        return {"bundles_total": len(bundles) if isinstance(bundles, list) else 0}
    if phase == "bundle_packing":
        plan_path = run_dir / "bundle-plan.json"
        if not plan_path.is_file():
            return {}
        bundles = read_json(plan_path, {}).get("bundles", [])
        bundles = bundles if isinstance(bundles, list) else []
        planned_ids = {
            str(bundle.get("bundle_id") or bundle.get("id") or "").strip()
            for bundle in bundles
            if isinstance(bundle, dict) and str(bundle.get("bundle_id") or bundle.get("id") or "").strip()
        }
        packed_ids = {path.stem for path in (run_dir / "bundles").glob("*.md")} if (run_dir / "bundles").is_dir() else set()
        return {
            "bundles_total": len(bundles),
            "bundles_packed": len(planned_ids.intersection(packed_ids)),
        }
    if phase == "intent_test_planning":
        return {"intent_tests_total": intent_test_artifact_counts(run_dir)["intent_tests_total"]}
    if phase == "intent_test_writing":
        counts = intent_test_artifact_counts(run_dir)
        return {
            "intent_tests_total": counts["intent_tests_total"],
            "intent_tests_written": counts["intent_tests_written"],
        }
    if phase == "intent_test_running":
        counts = intent_test_artifact_counts(run_dir)
        return {"intent_tests_total": counts["intent_tests_total"], "intent_tests_run": counts["intent_tests_run"]}
    if phase == "intent_test_failure_analysis":
        counts = intent_test_artifact_counts(run_dir)
        return {
            "intent_tests_total": counts["intent_tests_total"],
            "intent_tests_run": counts["intent_tests_run"],
            "intent_tests_analyzed": counts["intent_tests_analyzed"],
        }
    if phase == "validator_disproof":
        input_path = run_dir / "validation-input.json"
        output_path = run_dir / "validated-findings.json"
        if not input_path.is_file() and not output_path.is_file():
            return {}
        validation_input = read_json(input_path, {})
        validation_output = read_json(output_path, {})

        def first_list_size(payload: Any, keys: tuple[str, ...]) -> int:
            if not isinstance(payload, dict):
                return 0
            for key in keys:
                value = payload.get(key)
                if isinstance(value, list):
                    return len(value)
            return 0

        total = first_list_size(
            validation_input,
            ("candidates", "candidate_findings", "findings", "clusters", "validation_candidates"),
        )
        canonical_collections = (
            validation_output.get("validated_findings"),
            validation_output.get("weak_findings"),
            validation_output.get("disproven_findings"),
        ) if isinstance(validation_output, dict) else ()
        if canonical_collections and all(isinstance(collection, list) for collection in canonical_collections):
            completed = sum(len(collection) for collection in canonical_collections)
        else:
            completed = first_list_size(
                validation_output,
                ("validated", "findings", "results", "validation_results", "validated_findings"),
            )
            completed += first_list_size(validation_output, ("weak", "weak_findings"))
            completed += first_list_size(validation_output, ("disproven", "disproven_findings"))
        return {
            "validator_candidates_total": max(total, completed),
            "validator_candidates_completed": completed,
        }
    return {}


def artifact_backed_progress_counters(run_dir: Path) -> dict[str, int]:
    counters: dict[str, int] = {}
    phase_paths = (
        ("inventory_repository", run_dir / "inventory.json"),
        ("risk_routing", run_dir / "risk-routing.json"),
        ("bundle_planning", run_dir / "bundle-plan.json"),
        ("bundle_packing", run_dir / "bundle-plan.json"),
        ("reviewer_fanout", run_dir / "bundle-plan.json"),
        ("intent_test_writing", run_dir / "intent" / "intent-test-source.json"),
        ("intent_test_running", run_dir / "intent" / "intent-test-results.raw.json"),
        ("validator_disproof", run_dir / "validated-findings.json"),
    )
    for phase, evidence_path in phase_paths:
        if not evidence_path.is_file():
            continue
        data = phase_completion_data(run_dir, phase)
        for key in PROGRESS_COUNTER_KEYS:
            if key not in data:
                continue
            try:
                counters[key] = max(0, int(data[key]))
            except (TypeError, ValueError):
                continue
    return counters


def write_review_instruction_tree(repo_dir: Path) -> None:
    review_root = repo_dir / ".codex-review"
    (review_root / "tools").mkdir(parents=True, exist_ok=True)
    (review_root / "schemas").mkdir(parents=True, exist_ok=True)
    (review_root / "prompts" / "reviewers").mkdir(parents=True, exist_ok=True)
    (review_root / "prompts" / "intent").mkdir(parents=True, exist_ok=True)
    (review_root / "AGENTS.review.md").write_text(REVIEW_AGENTS_TEXT, encoding="utf-8")
    tool_body = (
        "#!/usr/bin/env python3\n"
        "\"\"\"Pullwise review helper.\n\n"
        "Generated helpers must use only the Python standard library and perform mechanical tasks only.\n"
        "\"\"\"\n"
        "from __future__ import annotations\n\n"
        "import json\n"
        "import sys\n\n"
        "def main() -> int:\n"
        "    json.dump({'ok': True, 'tool': __file__}, sys.stdout, sort_keys=True)\n"
        "    sys.stdout.write('\\n')\n"
        "    return 0\n\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )
    for name in REQUIRED_TOOL_FILES:
        path = review_root / "tools" / name
        path.write_text(tool_body, encoding="utf-8")
        path.chmod(0o700)
    for name in REQUIRED_SCHEMA_FILES:
        path = review_root / "schemas" / name
        schema_id = name.removesuffix(".schema.json")
        write_json(path, {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{schema_id}/v1", "type": "object", "required": ["schema_version"], "additionalProperties": True})
    for name in REQUIRED_PROMPT_FILES:
        path = review_root / "prompts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prompt_template_for_name(name), encoding="utf-8")


def prompt_template_for_name(name: str) -> str:
    templates = {
        "00_repo_mapper.md": "You are the Repo Mapper. Produce repo-map.json. Do not report bugs. Return JSON only using repo-map/v1.\n",
        "01_risk_router.md": (
            "You are the Risk Router. Classify files and directories into P0/P1/P2/P3/SKIP. Cover every "
            "non-hard-skipped inventory path with an explicit route, or set an intentional default_depth for "
            "every unmatched path. The Worker will not infer missing tiers from paths, profiles, suffixes, or "
            "risk hints. Return JSON only using risk-routing/v1.\n"
        ),
        "02_bundle_planner.md": (
            "You are the Semantic Bundle Planner. Read bundle-planning-input.json and group every eligible path "
            "exactly once by feature, entrypoint, trust boundary, state flow, and implementation/test affinity. "
            "Every group must include a stable lowercase group_id, a non-empty title, a non-empty grouping_reasons "
            "list, its tier, and a non-empty paths list. Keep groups tier-homogeneous. Treat each output group as a final semantic bundle boundary: the Worker "
            "will not merge separate groups. Honor constraints.max_bundles and constraints.max_reviewer_assignments; "
            "reviewer cost per bundle is P0=3, P1=2, and P2=1. Minimize both group count and weighted reviewer cost "
            "without losing semantic cohesion, and avoid tiny or singleton groups unless they are truly isolated. "
            "If a hard limit appears impossible, preserve exact coverage and tiers and return the most compact safe "
            "grouping so the Worker can reject it before fanout; never omit paths or reduce reviewer coverage. Write "
            "only bundle-grouping.json using bundle-grouping/v1. Do not assign reviewers, ranges, final bundle ids, or final "
            "size limits; group_id is the semantic-group identifier and the Worker owns final bundle ids, validation, and bounded splitting.\n"
        ),
        "reviewers/security.md": (
            "You are the Security Reviewer. Report only concrete security issues with realistic abuse paths. "
            "Demonstrate an end-to-end attacker-controlled path, account for producer-side validation and containment, "
            "and classify unproven reachability as defense-in-depth rather than high/critical. "
            f"{REVIEWER_CONFIDENCE_PROMPT_CONTRACT} Return JSON only using codex-reviewer-output/v1.\n"
        ),
        "reviewers/correctness.md": (
            "You are the Correctness Reviewer. Focus on incorrect behavior, state, boundaries, idempotency, and concurrency. "
            f"{REVIEWER_CONFIDENCE_PROMPT_CONTRACT} Return JSON only using codex-reviewer-output/v1.\n"
        ),
        "reviewers/test_gap.md": (
            "You are the Test Gap Reviewer. Report missing or weak tests only for important P0/P1 behavior. "
            f"{REVIEWER_CONFIDENCE_PROMPT_CONTRACT} Return JSON only using codex-reviewer-output/v1.\n"
        ),
        "reviewers/correctness_lite.md": (
            "You are the Correctness Lite Reviewer. Only report clear bugs or user-visible behavior problems. "
            f"{REVIEWER_CONFIDENCE_PROMPT_CONTRACT} Return JSON only using codex-reviewer-output/v1.\n"
        ),
        "03_clusterer.md": (
            'You are the Finding Clusterer and Vote Aggregator. If every verified reviewer findings array is empty, '
            'immediately write canonical clusters.json with {"schema_version": "cluster-output/v1", "clusters": []} '
            'and validation-input.json with {"schema_version": "validation-input/v1", "candidates": []}; do not inspect '
            'or rescan application source. Otherwise merge duplicates and suppress vague findings. Merge test-gap '
            'evidence into the underlying defect when contract, sink, and fix match. Do not create new findings. Return JSON only.\n'
        ),
        "intent/04_intent_miner.md": (
            "You are the Intent Miner. Extract behavioral contracts from docs, API specs, types, tests, route "
            'definitions, and error messages. Do not infer intent only from implementation code. Return JSON only '
            'using intent-map/v1 with a top-level "bundle_id" and a "behavioral_contracts" array, even when empty.\n'
        ),
        "intent/05_intent_test_planner.md": (
            "You are the Intent Test Planner. Select only high-value P0/P1 candidates for temporary tests. Return "
            'JSON only using intent-test-plan/v1 with a top-level "test_targets" array. Every target must include '
            "test_id, title, linked_finding_ids, contract_ids, and expected_result_before_fix set to fail, pass, or unknown. "
            "Read intent/execution-capabilities.json as observed evidence, not a fixed framework menu. Propose one or "
            "more execution_candidates with command and cwd; each is an Agent hypothesis that Worker policy and "
            "preflight must verify. Prefer candidates that execute real repository behavior and preserve the oracle.\n"
        ),
        "intent/06_intent_test_writer.md": (
            "You are the Intent Test Writer. Write generated test source only under intent/generated-tests/** in the writable phase output directory; "
            "the Worker owns validation-workspace materialization and execution. Return JSON only using intent-test-source/v1, with every executable "
            'test record in the top-level "generated_tests" array rather than only in aliases such as '
            "generated_test_files, created_test_files, or test_sources. Every record must include path, command, "
            "and target_test_ids. Verify expected outcomes against AGENTS instructions, documentation, types, API "
            "contracts, and existing tests; when intended behavior remains uncertain, do not turn it into an asserted "
            "oracle. For Python unittest entry points, do not expose imported TestCase subclasses at module scope "
            "where unittest.main() can discover unrelated repository suites; import a module alias or explicitly load "
            "only the generated test class or method. Read intent/execution-capabilities.json, then remain free to use "
            "a different safe agent-proposed command, cwd, runtime, or contained harness when it faithfully executes "
            "real repository code. An unchanged repository test may be selected with reuse_existing true, but never "
            "claim an application/source file as generated. Do not copy or reimplement application logic to manufacture a passing test. If no "
            "faithful runnable strategy exists, record a precise top-level skip_reason. Do not modify the main repo workspace.\n"
        ),
        "intent/07_intent_test_failure_analyzer.md": (
            "You are the Test Failure Analyzer. A failing test is not automatically a bug. Return JSON only using "
            'intent-test-result/v1 with a top-level "test_results" array. Every result must include test_id, status, '
            "classification, confidence in 0..1, evidence, and artifacts; status must be one of "
            '"passed", "failed", "skipped", "timeout", or "error". Use only the documented intent-test classifications.\n'
        ),
        "08_validator.md": (
            'You are the Validation Reviewer. If the validation input contains no candidates, immediately write '
            'canonical validated-findings.json with {"schema_version": "validation-output/v1", '
            '"validated_findings": [], "weak_findings": [], "disproven_findings": []}; do not inspect or rescan '
            'application source. Otherwise try to disprove each candidate finding using evidence, location verification, '
            'related code, existing tests, and intent test results. An unknown cross-service producer is unresolved '
            'controllability, not proof of attacker control. dependency_missing is absence of dynamic evidence, not '
            'disproof; static source and contract evidence can still support plausible. Return JSON only.\n'
        ),
        "09_reporter.md": (
            'You are the Final Reporter. If the validated and weak finding collections are empty, immediately write '
            'a canonical no-findings report.agent.json with "findings": [] and "appendix_findings": [], preserving '
            'required summary, coverage, language, and artifact fields; do not inspect or rescan application source. '
            'Otherwise include only confirmed/plausible actionable findings in main findings; weak findings go to the '
            'top-level appendix_findings list. Do not inherit reviewer severity without calibrating reachability, '
            'control, impact, and containment. Return JSON only.\n'
        ),
    }
    discipline = (
        "\nRequired discipline:\n"
        "- Do not modify application source files.\n"
        "- Do not install dependencies.\n"
        "- Do not call external review/scanning services.\n"
        "- Do not include Markdown prose outside JSON for schema-bound phases.\n"
    )
    return "# Pullwise Codex Full Repository Review Phase\n\n" + templates.get(name, "Follow .codex-review/AGENTS.review.md. Return the requested artifact.\n") + discipline


def write_bootstrap_helper_summary(run_dir: Path) -> None:
    review_root = run_dir.parent.parent
    tools_dir = review_root / "tools"
    schemas_dir = review_root / "schemas"
    prompts_dir = review_root / "prompts"
    write_json(
        run_dir / "bootstrap_helper_scripts.summary.json",
        {
            "schema_version": "bootstrap-helper-summary/v1",
            "status": "completed",
            "required_tools": len(REQUIRED_TOOL_FILES),
            "materialized_tools": sum(1 for name in REQUIRED_TOOL_FILES if (tools_dir / name).is_file()),
            "required_schemas": len(REQUIRED_SCHEMA_FILES),
            "materialized_schemas": sum(1 for name in REQUIRED_SCHEMA_FILES if (schemas_dir / name).is_file()),
            "required_prompts": len(REQUIRED_PROMPT_FILES),
            "materialized_prompts": sum(1 for name in REQUIRED_PROMPT_FILES if (prompts_dir / name).is_file()),
        },
    )


def fallback_semantic_artifact(run_dir: Path, job: dict[str, Any], phase: str) -> None:
    ensure_intent_directories(run_dir)
    if phase == "bootstrap_helper_scripts":
        write_bootstrap_helper_summary(run_dir)
    elif phase == "bundle_planning":
        materialize_agent_bundle_plan(run_dir, job)
    elif phase == "clustering_and_voting":
        clusters_path = run_dir / 'clusters.json'
        if clusters_path.exists():
            normalize_cluster_output_artifact(clusters_path)
    elif phase == "intent_mining":
        intent_map_path = run_dir / "intent" / "intent-map.json"
        if intent_map_path.exists():
            repair_intent_map_artifact(intent_map_path)
    elif phase == "intent_test_planning":
        intent_plan_path = run_dir / "intent" / "intent-test-plan.json"
        if intent_plan_path.exists():
            repair_intent_test_plan_artifact(intent_plan_path, run_dir)
    elif phase == "intent_test_writing":
        intent_source_path = run_dir / "intent" / "intent-test-source.json"
        if intent_source_path.exists():
            repair_intent_test_source_artifact(intent_source_path, run_dir)
    elif phase == "intent_test_failure_analysis":
        intent_results_path = run_dir / "intent" / "intent-test-results.json"
        if intent_results_path.exists():
            repair_intent_test_results_artifact(intent_results_path, run_dir)
    elif phase == "validator_disproof":
        validation_path = run_dir / "validated-findings.json"
        if validation_path.exists():
            repair_validation_output_artifact(validation_path)
    elif phase == "final_report_json":
        repair_agent_report_artifact(run_dir, job)


def default_agent_report(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": "codex-full-repo-review",
        "schema_version": "v1",
        "run_id": safe_id(job.get("run_id") or f"run_{job.get('job_id')}", "run"),
        "commit_sha": str(job.get("commit") or "pending"),
        "output_language": output_language_for_job(job),
        "summary": {"overall_risk": "unknown", "result_status": "complete"},
        "coverage": {},
        "findings": [],
        "appendix_findings": [],
        "disproven_findings": [],
        "intent_test_validation": {"schema_version": "intent-test-result/v1", "test_results": []},
        "next_agent_tasks": [],
        "raw_artifact_refs": [],
    }


def agent_report_contract_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report.agent.json must be an object"]
    if report.get("schema_id") != "codex-full-repo-review":
        errors.append("report.agent.json must use schema_id codex-full-repo-review")
    findings = report.get("findings")
    if findings is not None and not isinstance(findings, list):
        errors.append("report.agent.json findings must be a list")
    if isinstance(findings, list):
        for index, finding in enumerate(findings):
            if not isinstance(finding, dict):
                errors.append(f"finding[{index}] must be an object")
                continue
            locations = finding.get("locations")
            if locations is None and isinstance(finding.get("location"), dict):
                locations = [finding.get("location")]
            if locations is not None and not isinstance(locations, list):
                errors.append(f"finding[{index}].locations must be a list")
    return errors


def agent_report_line_range(value: object) -> tuple[int, int]:
    if isinstance(value, (list, tuple)) and len(value) >= 1:
        start = _qa_int(value[0])
        end = _qa_int(value[1] if len(value) > 1 else value[0], start)
        return (start, end if end > 0 else start)
    text = str(value or "").strip()
    if not text:
        return (0, 0)
    numbers = [int(match) for match in re.findall(r"\d+", text)]
    if not numbers:
        return (0, 0)
    start = numbers[0]
    end = numbers[1] if len(numbers) > 1 else start
    return (start, end if end > 0 else start)


def agent_report_location(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    path = str(
        value.get("path")
        or value.get("file")
        or value.get("filename")
        or value.get("file_path")
        or value.get("filePath")
        or value.get("source_path")
        or value.get("sourcePath")
        or value.get("primary_path")
        or value.get("primaryPath")
        or ""
    ).strip()
    start = _qa_int(
        value.get("start_line")
        or value.get("line_start")
        or value.get("startLine")
        or value.get("lineStart")
        or value.get("line")
        or value.get("line_number")
        or value.get("lineNumber")
        or value.get("primary_line")
        or value.get("primaryLine")
        or value.get("start")
    )
    end = _qa_int(
        value.get("end_line")
        or value.get("line_end")
        or value.get("endLine")
        or value.get("lineEnd")
        or value.get("end")
        or start
    )
    if start <= 0:
        start, end = agent_report_line_range(
            value.get("line_range")
            or value.get("lineRange")
            or value.get("range")
            or value.get("lines")
        )
    if end <= 0:
        end = start
    if end < start:
        start, end = end, start
    if not path or start <= 0:
        return None
    location = dict(value)
    location["path"] = path
    for key in (
        "file",
        "filename",
        "file_path",
        "filePath",
        "source_path",
        "sourcePath",
        "line",
        "line_start",
        "line_end",
        "lineStart",
        "lineEnd",
        "startLine",
        "endLine",
        "line_number",
        "lineNumber",
        "primary_line",
        "primaryLine",
        "primary_path",
        "primaryPath",
        "start",
        "end",
        "line_range",
        "lineRange",
        "range",
        "lines",
    ):
        location.pop(key, None)
    location["start_line"] = start
    location["end_line"] = end
    return location


def agent_report_finding_location(finding: dict[str, Any]) -> dict[str, Any] | None:
    return agent_report_location(
        {
            "path": (
                finding.get("path")
                or finding.get("file")
                or finding.get("filename")
                or finding.get("file_path")
                or finding.get("filePath")
                or finding.get("source_path")
                or finding.get("sourcePath")
                or finding.get("primary_path")
                or finding.get("primaryPath")
            ),
            "start_line": (
                finding.get("start_line")
                or finding.get("line_start")
                or finding.get("startLine")
                or finding.get("lineStart")
                or finding.get("line")
                or finding.get("line_number")
                or finding.get("lineNumber")
                or finding.get("primary_line")
                or finding.get("primaryLine")
            ),
            "end_line": (
                finding.get("end_line")
                or finding.get("line_end")
                or finding.get("endLine")
                or finding.get("lineEnd")
                or finding.get("line")
                or finding.get("primary_line")
                or finding.get("primaryLine")
            ),
            "line_range": finding.get("line_range") or finding.get("lineRange") or finding.get("range") or finding.get("lines"),
        }
    )


def agent_report_location_candidates(value: object, *, default_path: object = "") -> list[dict[str, Any]]:
    if isinstance(value, (list, tuple)):
        candidates: list[dict[str, Any]] = []
        for item in value:
            candidates.extend(agent_report_location_candidates(item, default_path=default_path))
        return candidates
    if not isinstance(value, dict):
        return []
    location_fields = {
        "path",
        "file",
        "filename",
        "file_path",
        "filePath",
        "source_path",
        "sourcePath",
        "primary_path",
        "primaryPath",
        "line",
        "start_line",
        "startLine",
        "line_start",
        "lineStart",
        "primary_line",
        "primaryLine",
        "line_range",
        "lineRange",
        "range",
        "lines",
        "start",
        "end",
    }
    if location_fields.intersection(value):
        candidate = dict(value)
        if default_path and not any(candidate.get(key) for key in ("path", "file", "filename", "file_path", "filePath", "source_path", "sourcePath", "primary_path", "primaryPath")):
            candidate["path"] = default_path
        return [candidate]

    candidates = []
    for path, location_value in value.items():
        if isinstance(location_value, dict):
            candidate = dict(location_value)
            if not any(candidate.get(key) for key in ("path", "file", "filename", "file_path", "filePath", "source_path", "sourcePath", "primary_path", "primaryPath")):
                candidate["path"] = path
            candidates.append(candidate)
        elif isinstance(location_value, (int, float, str, list, tuple)):
            candidates.append({"path": path, "line_range": location_value})
    return candidates


def agent_report_locations(finding: dict[str, Any]) -> list[dict[str, Any]]:
    raw_locations = finding.get("locations")
    if not isinstance(raw_locations, list):
        raw_locations = []
    if not raw_locations:
        for key in ("location", "code_location", "codeLocation", "source_location", "sourceLocation"):
            if isinstance(finding.get(key), dict):
                raw_locations = [finding[key]]
                break
    if not raw_locations:
        for key in ("affected_locations", "affectedLocations", "code_locations", "codeLocations", "source_locations", "sourceLocations"):
            if isinstance(finding.get(key), list):
                raw_locations = finding[key]
                break
    default_path = (
        finding.get("path")
        or finding.get("file")
        or finding.get("filename")
        or finding.get("file_path")
        or finding.get("filePath")
        or finding.get("source_path")
        or finding.get("sourcePath")
        or finding.get("primary_path")
        or finding.get("primaryPath")
    )
    if not raw_locations:
        for key in ("line_evidence", "lineEvidence", "path_line_evidence", "pathLineEvidence", "paths"):
            raw_locations = agent_report_location_candidates(finding.get(key), default_path=default_path)
            if raw_locations:
                break
    if not raw_locations:
        finding_location = agent_report_finding_location(finding)
        if finding_location is not None:
            raw_locations = [finding_location]
    if not raw_locations and isinstance(finding.get("evidence"), list):
        raw_locations = [item for item in finding["evidence"] if isinstance(item, dict)]
    locations = []
    for raw_location in raw_locations:
        location = agent_report_location(raw_location)
        if location is not None:
            locations.append(location)
    if not locations:
        finding_location = agent_report_finding_location(finding)
        if finding_location is not None:
            locations.append(finding_location)
    return locations


def agent_report_confidence(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        confidence = float(value)
        if not math.isfinite(confidence):
            return 0.0
        if 0 <= confidence <= 1:
            return confidence
        if 1 < confidence <= 100:
            return confidence / 100
        return 0.0
    text = str(value or "").strip().lower()
    if not text:
        return 0.0
    named = {
        "very high": 0.95,
        "high": 0.9,
        "medium": 0.6,
        "moderate": 0.6,
        "low": 0.3,
        "very low": 0.1,
        "none": 0.0,
        "unknown": 0.0,
    }
    if text in named:
        return named[text]
    numeric = text.removesuffix("%").strip()
    try:
        confidence = float(numeric)
    except ValueError:
        return 0.0
    if text.endswith("%"):
        confidence = confidence / 100
    if 0 <= confidence <= 1:
        return confidence
    if 1 < confidence <= 100:
        return confidence / 100
    return 0.0


def agent_report_evidence(finding: dict[str, Any]) -> list[object]:
    raw_evidence: object = finding.get("evidence")
    if not raw_evidence:
        for key in ("supporting_evidence", "supportingEvidence", "path_line_evidence", "pathLineEvidence"):
            if finding.get(key):
                raw_evidence = finding[key]
                break
    if not raw_evidence:
        raw_evidence = finding.get("evidence_summary") or finding.get("evidenceSummary")
        label = "Evidence summary"
    else:
        label = "Evidence"

    if isinstance(raw_evidence, (str, dict)):
        raw_items: list[object] = [raw_evidence]
    elif isinstance(raw_evidence, list):
        raw_items = raw_evidence
    else:
        raw_items = []

    evidence: list[object] = []
    for item in raw_items:
        if isinstance(item, str):
            summary = item.strip()
            if summary:
                evidence.append({"type": "code", "label": label, "summary": summary})
            continue
        if not isinstance(item, dict):
            continue
        record = dict(item)
        if not str(record.get("summary") or "").strip():
            for key in ("evidence", "text", "reason", "description"):
                summary = str(record.get(key) or "").strip()
                if summary:
                    record["summary"] = summary
                    break
        record.setdefault("type", "code")
        evidence.append(record)
    return evidence


def normalized_agent_report_finding(finding: object) -> dict[str, Any] | None:
    if not isinstance(finding, dict):
        return None
    normalized = dict(finding)
    if not str(normalized.get("id") or "").strip():
        for key in ("finding_id", "local_id", "cluster_id"):
            candidate = str(finding.get(key) or "").strip()
            if candidate:
                normalized["id"] = candidate
                break
    normalized["locations"] = agent_report_locations(finding)
    normalized.pop("affected_locations", None)
    normalized.pop("affectedLocations", None)
    normalized["severity"] = normalized_finding_severity(finding.get("severity"))
    normalized["confidence"] = agent_report_confidence(finding.get("confidence"))
    if not str(normalized.get("recommendation") or "").strip():
        for key in ("recommended_fix", "recommended_action", "remediation"):
            recommendation = str(finding.get(key) or "").strip()
            if recommendation:
                normalized["recommendation"] = recommendation
                break
    for key in ("recommended_fix", "recommended_action", "remediation"):
        normalized.pop(key, None)
    normalized["evidence"] = agent_report_evidence(finding)
    for key in ("supporting_evidence", "supportingEvidence", "evidence_summary", "evidenceSummary"):
        normalized.pop(key, None)
    return normalized


def appendix_finding_identity(finding: dict[str, Any]) -> tuple[Any, ...]:
    ids = finding_binding_ids(finding)
    if ids:
        return ("ids", *sorted(ids))
    fallback = finding_fallback_binding_key(finding)
    if fallback is not None:
        return ("location", *fallback)
    return (
        "content",
        str(finding.get("title") or "").strip().lower(),
        json.dumps(finding.get("locations") or [], ensure_ascii=False, sort_keys=True, default=str),
    )


def canonical_appendix_findings(report: dict[str, Any], validation: object) -> list[dict[str, Any]]:
    sources: list[tuple[object, bool]] = []
    for value in report.get("appendix_findings") if isinstance(report.get("appendix_findings"), list) else []:
        sources.append((value, False))
    appendix = report.get("appendix") if isinstance(report.get("appendix"), dict) else {}
    for value in appendix.get("weak_findings") if isinstance(appendix.get("weak_findings"), list) else []:
        sources.append((value, True))
    for value in report.get("weak_findings") if isinstance(report.get("weak_findings"), list) else []:
        sources.append((value, True))
    if isinstance(validation, dict):
        for value in validation.get("weak_findings") if isinstance(validation.get("weak_findings"), list) else []:
            sources.append((value, True))

    findings: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    seen_id_sets: list[set[str]] = []
    for raw_finding, force_weak in sources:
        finding = normalized_agent_report_finding(raw_finding)
        if finding is None:
            continue
        if force_weak and not finding_validation_status(finding):
            finding["validator_status"] = "weak"
        ids = finding_binding_ids(finding)
        if ids and any(ids.intersection(existing_ids) for existing_ids in seen_id_sets):
            continue
        identity = appendix_finding_identity(finding)
        if identity in seen:
            continue
        seen.add(identity)
        if ids:
            seen_id_sets.append(ids)
        findings.append(finding)
    return findings



def _non_pending_text(value: object) -> str:
    text = str(value or "").strip()
    return "" if not text or text.lower() == "pending" else text


def resolved_report_commit_sha(run_dir: Path, report: dict[str, Any] | None, job: dict[str, Any] | None = None) -> str:
    report = report if isinstance(report, dict) else {}
    job = job if isinstance(job, dict) else {}
    for value in (report.get("commit_sha"), report.get("commit"), job.get("commit")):
        commit = _non_pending_text(value)
        if commit:
            return commit
    repo_root = _repo_root_for_run_dir(run_dir)
    if repo_root is not None:
        commit = _non_pending_text(git_commit(repo_root))
        if commit:
            return commit
    return "pending"

def enrich_finding_intent_evidence(run_dir: Path, finding: dict[str, Any]) -> None:
    validation_sources = (
        dict(finding.get("validation_sources"))
        if isinstance(finding.get("validation_sources"), dict)
        else {}
    )
    intent_signal = (
        dict(validation_sources.get("intent_test"))
        if isinstance(validation_sources.get("intent_test"), dict)
        else {}
    )
    test_id = str(intent_signal.get("test_id") or intent_signal.get("id") or "").strip()
    if not test_id:
        return
    analyzed_payload = read_json(run_dir / "intent" / "intent-test-results.json", {})
    raw_payload = read_json(run_dir / "intent" / "intent-test-results.raw.json", {})
    source_payload = read_json(run_dir / "intent" / "intent-test-source.json", {})
    analyzed = next(
        (
            item
            for item in analyzed_payload.get("test_results", [])
            if isinstance(item, dict)
            and test_id in {_intent_test_id(item, ""), *_intent_related_test_ids(item)}
        ),
        {},
    ) if isinstance(analyzed_payload, dict) else {}
    raw_result = next(
        (
            item
            for item in raw_payload.get("test_runs", [])
            if isinstance(item, dict)
            and test_id in {_intent_test_id(item, ""), *_intent_related_test_ids(item)}
        ),
        {},
    ) if isinstance(raw_payload, dict) else {}
    generated = next(
        (
            item
            for item in source_payload.get("generated_tests", [])
            if isinstance(item, dict)
            and test_id in {_intent_test_id(item, ""), *_intent_related_test_ids(item)}
        ),
        {},
    ) if isinstance(source_payload, dict) else {}
    if isinstance(analyzed, dict):
        for key in ("classification", "status", "confidence", "finding_confidence_impact"):
            if analyzed.get(key) is not None:
                intent_signal[key] = analyzed.get(key)
        artifact_refs = analyzed.get("artifact_refs") or analyzed.get("artifacts")
        if isinstance(artifact_refs, list):
            intent_signal["artifact_refs"] = artifact_refs
    command = str(raw_result.get("command") or "").strip() if isinstance(raw_result, dict) else ""
    if command:
        intent_signal["command"] = command
    output_texts: list[str] = []
    log_paths: list[str] = []
    for key in ("stdout_path", "stderr_path"):
        raw_path = str(raw_result.get(key) or "").strip() if isinstance(raw_result, dict) else ""
        if not raw_path:
            continue
        path = Path(raw_path)
        log_paths.append(f"intent/test-output/{path.name}")
        try:
            output = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            output = ""
        if output:
            output_texts.append(output[-4000:])
    if log_paths:
        intent_signal["log_path"] = log_paths[-1]
    if output_texts:
        intent_signal["output"] = "\n".join(output_texts)[-4000:]
    validation_sources["intent_test"] = intent_signal
    finding["validation_sources"] = validation_sources

    reproduction = dict(finding.get("reproduction")) if isinstance(finding.get("reproduction"), dict) else {}
    if command and not reproduction.get("commands"):
        reproduction["commands"] = [command]
    generated_path = _intent_source_path_from_entry(generated) if isinstance(generated, dict) else ""
    if generated_path and not reproduction.get("testFile"):
        reproduction["testFile"] = generated_path
    if log_paths and not reproduction.get("logPath"):
        reproduction["logPath"] = log_paths[-1]
    if output_texts and not reproduction.get("actual"):
        reproduction["actual"] = output_texts[-1]
    if reproduction:
        finding["reproduction"] = reproduction

    if command or log_paths or output_texts:
        evidence = list(finding.get("evidence")) if isinstance(finding.get("evidence"), list) else []
        evidence.append(
            {
                "type": "test",
                "label": f"Intent test {test_id}",
                "summary": f"Intent-test classification: {intent_signal.get('classification') or 'unknown'}.",
                "command": command,
                "logPath": log_paths[-1] if log_paths else "",
                "output": output_texts[-1] if output_texts else "",
            }
        )
        finding["evidence"] = evidence


def repair_agent_report_artifact(run_dir: Path, job: dict[str, Any]) -> None:
    path = run_dir / "report.agent.json"
    if not path.is_file():
        return
    raw = read_json(path, {})
    report = default_agent_report(job)
    if isinstance(raw, dict):
        report.update(raw)
    report["schema_id"] = "codex-full-repo-review"
    report["schema_version"] = "v1"
    report["run_id"] = str(report.get("run_id") or default_agent_report(job)["run_id"])
    report["commit_sha"] = resolved_report_commit_sha(run_dir, report, job)
    report["output_language"] = output_language_for_job(job)
    summary = report.get("summary")
    if not isinstance(summary, dict):
        summary = {"overall_risk": "unknown", "result_status": "complete"}
    summary.setdefault("result_status", "complete")
    report["summary"] = summary
    coverage = read_json(run_dir / "coverage.json", {})
    if isinstance(coverage, dict):
        report["coverage"] = coverage
    intent = read_json(
        run_dir / "intent" / "intent-test-results.json",
        {"schema_version": "intent-test-result/v1", "test_results": []},
    )
    if isinstance(intent, dict):
        report["intent_test_validation"] = intent
    raw_findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    if not raw_findings and isinstance(report.get("main_findings"), list):
        raw_findings = report["main_findings"]

    _valid_validation, accepted_validation = validation_binding_entries(run_dir)
    findings: list[dict[str, Any]] = []
    demoted_findings: list[dict[str, Any]] = []
    used_validation_entries: set[int] = set()
    for raw_finding in raw_findings:
        finding = normalized_agent_report_finding(raw_finding)
        if finding is None:
            continue
        validation_entry = matching_validation_entry(finding, accepted_validation)
        if validation_entry is not None and id(validation_entry) not in used_validation_entries:
            used_validation_entries.add(id(validation_entry))
            if not str(finding.get("id") or "").strip():
                finding["id"] = f"finding-{len(findings) + 1:03d}"
            finding["validator_status"] = validation_entry_status(validation_entry)
            enrich_finding_intent_evidence(run_dir, finding)
            findings.append(finding)
            continue
        demoted = dict(finding)
        demoted["demoted_from_main_findings"] = True
        demoted["demoted_reason"] = "missing_confirmed_or_plausible_validation"
        demoted_findings.append(demoted)

    validation = read_json(run_dir / "validated-findings.json", {})
    appendix_findings = canonical_appendix_findings(report, validation)
    seen_appendix = {appendix_finding_identity(finding) for finding in appendix_findings}
    for demoted in demoted_findings:
        identity = appendix_finding_identity(demoted)
        if identity in seen_appendix:
            continue
        seen_appendix.add(identity)
        appendix_findings.append(demoted)
    report["appendix_findings"] = appendix_findings
    report["findings"] = findings
    for key in ("disproven_findings", "raw_artifact_refs"):
        if not isinstance(report.get(key), list):
            report[key] = []
    report["next_agent_tasks"] = list(
        dict.fromkeys(
            str(finding.get("next_agent_task") or "").strip()
            for finding in findings
            if str(finding.get("next_agent_task") or "").strip()
        )
    )
    summary["overall_risk"] = highest_finding_risk(findings)
    summary["main_finding_count"] = len(findings)
    summary["confirmed_count"] = sum(1 for finding in findings if finding_validation_status(finding) != "plausible")
    summary["plausible_count"] = sum(1 for finding in findings if finding_validation_status(finding) == "plausible")
    summary["weak_count"] = sum(1 for finding in appendix_findings if finding_validation_status(finding) == "weak")
    summary["appendix_finding_count"] = len(appendix_findings)
    report["summary"] = summary
    write_json(path, report)


def agent_report_payload(run_dir: Path, job: dict[str, Any]) -> dict[str, Any]:
    report = default_agent_report(job)
    coverage = read_json(run_dir / "coverage.json", {})
    intent = read_json(run_dir / "intent" / "intent-test-results.json", {"schema_version": "intent-test-result/v1", "test_results": []})
    report["coverage"] = coverage if isinstance(coverage, dict) else {}
    report["intent_test_validation"] = intent if isinstance(intent, dict) else {"schema_version": "intent-test-result/v1", "test_results": []}
    report["raw_artifact_refs"] = [
        "inventory.json",
        "repo-map.json",
        "risk-routing.json",
        "bundle-plan.json",
        "clusters.json",
        "validated-findings.json",
        "intent-test-results.json",
    ]
    return report


def _markdown_text(value: object, fallback: str = "") -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    return " ".join(text.split())


def _markdown_location(location: object) -> str:
    if not isinstance(location, dict):
        return ""
    path = _markdown_text(location.get("path") or location.get("file") or location.get("filename"))
    start = _qa_int(location.get("start_line") or location.get("line_start") or location.get("line"))
    end = _qa_int(location.get("end_line") or location.get("line_end") or start)
    if not path:
        return ""
    if start > 0 and end > 0 and end != start:
        return f"`{path}:{start}-{end}`"
    if start > 0:
        return f"`{path}:{start}`"
    return f"`{path}`"


def _markdown_counts(items: list[Any], key: str) -> str:
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        label = _markdown_text(item.get(key), "unknown").lower()
        counts[label] = counts.get(label, 0) + 1
    if not counts:
        return "none"
    return ", ".join(f"{label} {count}" for label, count in sorted(counts.items()))


def _confidence_label(value: object) -> str:
    confidence = agent_report_confidence(value)
    if confidence <= 0:
        return "unknown"
    return f"{round(confidence * 100)}%"


def _coverage_summary(coverage: object) -> str:
    if not isinstance(coverage, dict) or not coverage:
        return "not recorded"
    total = _qa_int(coverage.get("source_like_files_total") or coverage.get("source_files_total"))
    deep = _qa_int(coverage.get("deep_reviewed_files"))
    standard = _qa_int(coverage.get("standard_reviewed_files"))
    light = _qa_int(coverage.get("light_reviewed_files"))
    skipped = _qa_int(coverage.get("skipped_files"))
    parts = []
    if total > 0:
        parts.append(f"{total} source-like files")
    reviewed_parts = []
    if deep > 0:
        reviewed_parts.append(f"{deep} deep")
    if standard > 0:
        reviewed_parts.append(f"{standard} standard")
    if light > 0:
        reviewed_parts.append(f"{light} light")
    if reviewed_parts:
        parts.append("reviewed " + ", ".join(reviewed_parts))
    if skipped > 0:
        parts.append(f"{skipped} skipped")
    return "; ".join(parts) if parts else "recorded without file counts"


def intent_test_process_started(record: object) -> bool:
    if not isinstance(record, dict):
        return False
    explicit = record.get('process_started')
    if isinstance(explicit, bool):
        return explicit
    status = str(record.get('status') or '').strip().lower()
    return status not in {
        '',
        'skipped',
        'not_run',
        'not_started',
        'unavailable',
    }


def _append_markdown_secondary_findings(
    lines: list[str],
    title: str,
    raw_findings: object,
) -> None:
    findings = raw_findings if isinstance(raw_findings, list) else []
    if not findings:
        return
    lines.extend([f'## {title}', ''])
    for index, finding in enumerate(findings, start=1):
        if not isinstance(finding, dict):
            continue
        finding_title = _markdown_text(finding.get('title'), 'Untitled finding')
        severity = _markdown_text(finding.get('severity'), 'unknown')
        lines.extend([f'### {index}. [{severity}] {finding_title}', ''])
        status = _markdown_text(
            finding.get('validator_status')
            or finding.get('validation_status')
            or finding.get('status')
        )
        if status:
            lines.append(f'- Validation status: {status}')
        locations = finding.get('locations') if isinstance(finding.get('locations'), list) else []
        location_text = ', '.join(
            item for item in (_markdown_location(location) for location in locations[:3]) if item
        )
        if location_text:
            lines.append(f'- Location: {location_text}')
        for field, label in (
            ('impact', 'Impact'),
            ('recommendation', 'Recommendation'),
            ('demoted_reason', 'Appendix reason'),
            ('disproof_reason', 'Disproof reason'),
        ):
            detail = _markdown_text(finding.get(field))
            if detail:
                lines.append(f'- {label}: {detail}')
        lines.append('')


def _zh_cn_markdown(lines: list[str]) -> list[str]:
    exact = {
        "# Codex Full Repository Review Report": "# Codex 全仓库审查报告",
        "## Summary": "## 摘要",
        "## Top Findings": "## 主要问题",
        "## Intent Test Validation Summary": "## 意图测试验证摘要",
        "## Recommended Follow-up": "## 建议后续工作",
        "## Machine-readable Sources": "## 机器可读来源",
        "No confirmed findings.": "没有已确认的问题。",
        "No intent tests were run or recorded for this review.": "本次审查没有运行或记录意图测试。",
        "Use the recommendations in the validated main findings above to plan the next fix pass.": "请根据以上已验证主要问题中的建议安排下一轮修复。",
        "No immediate follow-up task was generated by this review. If this run was meant to verify a specific risky change, inspect the coverage and intent test artifacts before treating the result as complete assurance.": "本次审查没有生成需要立即执行的后续任务。如果本次运行用于验证特定高风险变更，请先检查覆盖率和意图测试产物，再将结果视为完整保障。",
        "- `report.agent.json` contains the normalized findings and follow-up task list.": "- `report.agent.json` 包含规范化问题和后续任务列表。",
        "- `intent-test-results.json` contains the detailed intent-test validation records when tests were generated.": "- 生成测试时，`intent-test-results.json` 包含详细的意图测试验证记录。",
        "- `artifact-manifest.json` lists the complete set of uploaded worker artifacts.": "- `artifact-manifest.json` 列出所有已上传的 worker 产物。",
        "This review completed without confirmed findings in the validated report. That means the worker did not confirm an actionable issue from this run; it is not a proof that the repository has no defects.": "本次审查的验证报告中没有已确认问题。这表示 worker 在本次运行中没有确认可行动问题，但不代表仓库不存在缺陷。",
    }
    prefixes = {
        "- Mode: ": "- 模式：",
        "- Commit: ": "- 提交：",
        "- Result status: ": "- 结果状态：",
        "- Overall risk: ": "- 总体风险：",
        "- Confirmed findings: ": "- 已确认问题：",
        "- Plausible findings: ": "- 可能问题：",
        "- Intent tests run: ": "- 已运行意图测试：",
        "- Coverage: ": "- 覆盖率：",
        "- ID: ": "- ID：",
        "- Category: ": "- 类别：",
        "- Confidence: ": "- 置信度：",
        "- Location: ": "- 位置：",
        "- Impact: ": "- 影响：",
        "- Recommendation: ": "- 建议：",
        "- Next agent task: ": "- 下一项 agent 任务：",
        "- Evidence:": "- 证据：",
        "- Status counts: ": "- 状态统计：",
        "- Classification counts: ": "- 分类统计：",
    }
    localized: list[str] = []
    for line in lines:
        if line == '## Appendix Findings':
            localized.append('## 附录问题')
            continue
        if line == '## Disproven Findings':
            localized.append('## 已排除问题')
            continue
        if line.startswith("This review completed with"):
            localized.append("本次审查已完成；已确认和可能问题的数量及最高风险见上方摘要。")
            continue
        if line.startswith("Showing ") or line.startswith("- Showing "):
            localized.append("完整列表请查看对应的机器可读 JSON 产物。")
            continue
        if line.startswith("- See `report.agent.json` for "):
            localized.append("- 其他后续任务请查看 `report.agent.json`。")
            continue
        if line in exact:
            localized.append(exact[line])
            continue
        replacement = line
        for prefix, translated in prefixes.items():
            if line.startswith(prefix):
                replacement = translated + line[len(prefix) :]
                break
        replacement = replacement.replace("[plausible]", "[可能]")
        replacement = replacement.replace("full repository scan", "全仓库扫描")
        replacement = replacement.replace("source-like files", "个类源代码文件")
        replacement = replacement.replace("reviewed ", "已审查 ")
        replacement = replacement.replace(" deep", " 深度")
        replacement = replacement.replace(" standard", " 标准")
        replacement = replacement.replace(" light", " 轻量")
        replacement = replacement.replace(" skipped", " 跳过")
        if replacement.startswith("- `"):
            replacement = replacement.replace(": status ", "：状态 ")
            replacement = replacement.replace("; classification ", "；分类 ")
            replacement = replacement.replace("; finding impact ", "；问题置信度影响 ")
            replacement = replacement.replace("; skip reason ", "；跳过原因 ")
        localized.append(replacement)
    return localized


LOCALIZED_MARKDOWN_LABELS: dict[str, dict[str, str]] = {
    "ja": {
        "title": "Codex 全リポジトリレビュー報告",
        "summary": "概要",
        "findings": "主な指摘",
        "intent": "インテントテスト検証概要",
        "follow_up": "推奨フォローアップ",
        "sources": "機械可読ソース",
        "mode": "モード",
        "commit": "コミット",
        "status": "結果ステータス",
        "risk": "総合リスク",
        "confirmed": "確認済み指摘",
        "plausible": "可能性のある指摘",
        "tests": "実行済みインテントテスト",
        "coverage": "カバレッジ",
        "category": "カテゴリ",
        "confidence": "確信度",
        "location": "場所",
        "impact": "影響",
        "recommendation": "推奨対応",
        "next_task": "次の agent タスク",
        "evidence": "証拠",
        "none": "確認済みの指摘はありません。",
        "no_tests": "実行または記録されたインテントテストはありません。",
    },
    "ko": {
        "title": "Codex 전체 저장소 검토 보고서", "summary": "요약", "findings": "주요 발견",
        "intent": "의도 테스트 검증 요약", "follow_up": "권장 후속 작업", "sources": "기계 판독 가능 소스",
        "mode": "모드", "commit": "커밋", "status": "결과 상태", "risk": "전체 위험", "confirmed": "확인된 발견",
        "plausible": "가능성 있는 발견", "tests": "실행된 의도 테스트", "coverage": "커버리지", "category": "범주",
        "confidence": "신뢰도", "location": "위치", "impact": "영향", "recommendation": "권장 사항",
        "next_task": "다음 agent 작업", "evidence": "증거", "none": "확인된 발견이 없습니다.",
        "no_tests": "실행되거나 기록된 의도 테스트가 없습니다.",
    },
    "es": {
        "title": "Informe de revisión completa del repositorio de Codex", "summary": "Resumen", "findings": "Hallazgos principales",
        "intent": "Resumen de validación de pruebas de intención", "follow_up": "Seguimiento recomendado", "sources": "Fuentes legibles por máquina",
        "mode": "Modo", "commit": "Commit", "status": "Estado del resultado", "risk": "Riesgo general", "confirmed": "Hallazgos confirmados",
        "plausible": "Hallazgos plausibles", "tests": "Pruebas de intención ejecutadas", "coverage": "Cobertura", "category": "Categoría",
        "confidence": "Confianza", "location": "Ubicación", "impact": "Impacto", "recommendation": "Recomendación",
        "next_task": "Siguiente tarea del agent", "evidence": "Evidencia", "none": "No hay hallazgos confirmados.",
        "no_tests": "No se ejecutaron ni registraron pruebas de intención.",
    },
    "fr": {
        "title": "Rapport de revue complète du dépôt Codex", "summary": "Résumé", "findings": "Principaux constats",
        "intent": "Résumé de validation des tests d’intention", "follow_up": "Suivi recommandé", "sources": "Sources lisibles par machine",
        "mode": "Mode", "commit": "Commit", "status": "Statut du résultat", "risk": "Risque global", "confirmed": "Constats confirmés",
        "plausible": "Constats plausibles", "tests": "Tests d’intention exécutés", "coverage": "Couverture", "category": "Catégorie",
        "confidence": "Confiance", "location": "Emplacement", "impact": "Impact", "recommendation": "Recommandation",
        "next_task": "Prochaine tâche de l’agent", "evidence": "Preuves", "none": "Aucun constat confirmé.",
        "no_tests": "Aucun test d’intention n’a été exécuté ou enregistré.",
    },
    "de": {
        "title": "Codex-Bericht zur vollständigen Repository-Prüfung", "summary": "Zusammenfassung", "findings": "Wichtigste Befunde",
        "intent": "Zusammenfassung der Intent-Test-Validierung", "follow_up": "Empfohlene Folgemaßnahmen", "sources": "Maschinenlesbare Quellen",
        "mode": "Modus", "commit": "Commit", "status": "Ergebnisstatus", "risk": "Gesamtrisiko", "confirmed": "Bestätigte Befunde",
        "plausible": "Plausible Befunde", "tests": "Ausgeführte Intent-Tests", "coverage": "Abdeckung", "category": "Kategorie",
        "confidence": "Konfidenz", "location": "Ort", "impact": "Auswirkung", "recommendation": "Empfehlung",
        "next_task": "Nächste Agent-Aufgabe", "evidence": "Nachweise", "none": "Keine bestätigten Befunde.",
        "no_tests": "Es wurden keine Intent-Tests ausgeführt oder aufgezeichnet.",
    },
    "pt-BR": {
        "title": "Relatório de revisão completa do repositório Codex", "summary": "Resumo", "findings": "Principais achados",
        "intent": "Resumo da validação de testes de intenção", "follow_up": "Acompanhamento recomendado", "sources": "Fontes legíveis por máquina",
        "mode": "Modo", "commit": "Commit", "status": "Status do resultado", "risk": "Risco geral", "confirmed": "Achados confirmados",
        "plausible": "Achados plausíveis", "tests": "Testes de intenção executados", "coverage": "Cobertura", "category": "Categoria",
        "confidence": "Confiança", "location": "Local", "impact": "Impacto", "recommendation": "Recomendação",
        "next_task": "Próxima tarefa do agent", "evidence": "Evidências", "none": "Nenhum achado confirmado.",
        "no_tests": "Nenhum teste de intenção foi executado ou registrado.",
    },
    "it": {
        "title": "Rapporto di revisione completa del repository Codex", "summary": "Riepilogo", "findings": "Risultati principali",
        "intent": "Riepilogo della validazione dei test di intento", "follow_up": "Azioni successive consigliate", "sources": "Fonti leggibili dalla macchina",
        "mode": "Modalità", "commit": "Commit", "status": "Stato del risultato", "risk": "Rischio complessivo", "confirmed": "Risultati confermati",
        "plausible": "Risultati plausibili", "tests": "Test di intento eseguiti", "coverage": "Copertura", "category": "Categoria",
        "confidence": "Confidenza", "location": "Posizione", "impact": "Impatto", "recommendation": "Raccomandazione",
        "next_task": "Prossima attività dell’agent", "evidence": "Evidenze", "none": "Nessun risultato confermato.",
        "no_tests": "Non sono stati eseguiti o registrati test di intento.",
    },
}


def _localized_markdown(lines: list[str], language: str) -> list[str]:
    labels = LOCALIZED_MARKDOWN_LABELS.get(language)
    if labels is None:
        return lines
    exact = {
        "# Codex Full Repository Review Report": f"# {labels['title']}",
        "## Summary": f"## {labels['summary']}",
        "## Top Findings": f"## {labels['findings']}",
        "## Intent Test Validation Summary": f"## {labels['intent']}",
        "## Recommended Follow-up": f"## {labels['follow_up']}",
        "## Machine-readable Sources": f"## {labels['sources']}",
        "No confirmed findings.": labels["none"],
        "No intent tests were run or recorded for this review.": labels["no_tests"],
    }
    prefixes = {
        "- Mode: ": f"- {labels['mode']}: ", "- Commit: ": f"- {labels['commit']}: ",
        "- Result status: ": f"- {labels['status']}: ", "- Overall risk: ": f"- {labels['risk']}: ",
        "- Confirmed findings: ": f"- {labels['confirmed']}: ", "- Plausible findings: ": f"- {labels['plausible']}: ",
        "- Intent tests run: ": f"- {labels['tests']}: ", "- Coverage: ": f"- {labels['coverage']}: ",
        "- Category: ": f"- {labels['category']}: ", "- Confidence: ": f"- {labels['confidence']}: ",
        "- Location: ": f"- {labels['location']}: ", "- Impact: ": f"- {labels['impact']}: ",
        "- Recommendation: ": f"- {labels['recommendation']}: ", "- Next agent task: ": f"- {labels['next_task']}: ",
        "- Evidence:": f"- {labels['evidence']}:",
    }
    localized: list[str] = []
    for line in lines:
        if line.startswith("This review completed") or line.startswith("Showing ") or line.startswith("- Showing "):
            continue
        if line.startswith("Use the recommendations") or line.startswith("No immediate follow-up") or line.startswith("- See `report.agent.json` for "):
            continue
        if line.startswith("- `report.agent.json` contains"):
            localized.append("- `report.agent.json`")
            continue
        if line.startswith("- `intent-test-results.json` contains"):
            localized.append("- `intent-test-results.json`")
            continue
        if line.startswith("- `artifact-manifest.json` lists"):
            localized.append("- `artifact-manifest.json`")
            continue
        if line in exact:
            localized.append(exact[line])
            continue
        replacement = line
        for prefix, translated in prefixes.items():
            if line.startswith(prefix):
                replacement = translated + line[len(prefix) :]
                break
        replacement = replacement.replace("full repository scan", "full_repo")
        if line.startswith("- Coverage: "):
            replacement = f"- {labels['coverage']}: `coverage.json`"
        localized.append(replacement)
    return localized


def render_markdown(report: dict[str, Any], *, output_language: str = "") -> str:
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    confirmed_findings = [
        finding
        for finding in findings
        if isinstance(finding, dict) and finding_validation_status(finding) != "plausible"
    ]
    plausible_findings = [
        finding
        for finding in findings
        if isinstance(finding, dict) and finding_validation_status(finding) == "plausible"
    ]
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    intent = report.get("intent_test_validation") if isinstance(report.get("intent_test_validation"), dict) else {}
    tests = intent.get("test_results") if isinstance(intent.get("test_results"), list) else []
    recorded_tests = tests
    tests = [test for test in recorded_tests if intent_test_process_started(test)]
    lines = [
        # The summary reports processes that actually started. Keep all
        # records for the detailed section so skipped attempts remain visible.
        "# Codex Full Repository Review Report",
        "",
        "## Summary",
        "",
        "- Mode: full repository scan",
        f"- Commit: {_markdown_text(report.get('commit_sha'), 'pending')}",
        f"- Result status: {_markdown_text(summary.get('result_status'), 'unknown')}",
        f"- Overall risk: {_markdown_text(summary.get('overall_risk'), 'unknown')}",
        f"- Confirmed findings: {len(confirmed_findings)} ({_markdown_counts(confirmed_findings, 'severity')})",
        f"- Plausible findings: {len(plausible_findings)} ({_markdown_counts(plausible_findings, 'severity')})",
        f"- Intent tests run: {len(tests)} ({_markdown_counts(tests, 'status')})",
        f"- Coverage: {_coverage_summary(report.get('coverage'))}",
        "",
    ]
    tests = recorded_tests
    if confirmed_findings:
        highest = _markdown_text(findings[0].get("severity") if isinstance(findings[0], dict) else "", "unknown")
        finding_sentence = (
            f"This review completed with {len(confirmed_findings)} confirmed and {len(plausible_findings)} plausible actionable finding(s)."
            if plausible_findings
            else f"This review completed with {len(confirmed_findings)} confirmed finding(s)."
        )
        lines.extend(
            [
                f"{finding_sentence} The highest-priority finding in the report is {highest}. Each finding below includes the observed impact, supporting evidence, and the recommended next fix or validation step.",
                "",
            ]
        )
    elif plausible_findings:
        highest = _markdown_text(plausible_findings[0].get("severity"), "unknown")
        lines.extend(
            [
                f"This review completed with {len(plausible_findings)} plausible actionable finding(s) and no confirmed findings. The highest-priority plausible finding is {highest}; validate it in the target environment before treating it as confirmed.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "This review completed without confirmed findings in the validated report. That means the worker did not confirm an actionable issue from this run; it is not a proof that the repository has no defects.",
                "",
            ]
        )

    lines.extend(["## Top Findings", ""])
    if not findings:
        lines.extend(["No confirmed findings.", ""])
    else:
        for index, finding in enumerate(findings[:10], start=1):
            if not isinstance(finding, dict):
                continue
            title = _markdown_text(finding.get("title"), "Untitled finding")
            severity = _markdown_text(finding.get("severity"), "unknown")
            validation_label = "[plausible] " if finding_validation_status(finding) == "plausible" else ""
            lines.extend([f"### {index}. {validation_label}[{severity}] {title}", ""])
            finding_id = _markdown_text(finding.get("id") or finding.get("cluster_id"))
            category = _markdown_text(finding.get("category"))
            if finding_id:
                lines.append(f"- ID: `{finding_id}`")
            if category:
                lines.append(f"- Category: {category}")
            lines.append(f"- Confidence: {_confidence_label(finding.get('confidence'))}")
            locations = finding.get("locations") if isinstance(finding.get("locations"), list) else []
            location_text = ", ".join(item for item in (_markdown_location(location) for location in locations[:3]) if item)
            if location_text:
                lines.append(f"- Location: {location_text}")
            impact = _markdown_text(finding.get("impact"))
            recommendation = _markdown_text(finding.get("recommendation"))
            next_task = _markdown_text(finding.get("next_agent_task"))
            if impact:
                lines.append(f"- Impact: {impact}")
            if recommendation:
                lines.append(f"- Recommendation: {recommendation}")
            if next_task:
                lines.append(f"- Next agent task: {next_task}")
            evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
            if evidence:
                lines.append("- Evidence:")
                for evidence_item in evidence[:3]:
                    if isinstance(evidence_item, dict):
                        evidence_location = _markdown_location(evidence_item)
                        detail = _markdown_text(evidence_item.get("detail") or evidence_item.get("summary"))
                        if evidence_location and detail:
                            lines.append(f"  - {evidence_location}: {detail}")
                        elif evidence_location:
                            lines.append(f"  - {evidence_location}")
                        elif detail:
                            lines.append(f"  - {detail}")
                    else:
                        detail = _markdown_text(evidence_item)
                        if detail:
                            lines.append(f"  - {detail}")
            lines.append("")
        if len(findings) > 10:
            lines.extend([f"Showing 10 of {len(findings)} main findings. See `report.agent.json` for the full machine-readable list.", ""])

    lines.extend(["## Intent Test Validation Summary", ""])
    if not tests:
        lines.extend(["No intent tests were run or recorded for this review.", ""])
    else:
        lines.extend(
            [
                f"- Status counts: {_markdown_counts(tests, 'status')}",
                f"- Classification counts: {_markdown_counts(tests, 'classification')}",
                "",
            ]
        )
        for test in tests[:10]:
            if not isinstance(test, dict):
                continue
            test_id = _markdown_text(test.get("test_id") or test.get("id"), "unnamed-test")
            status = _markdown_text(test.get("status"), "unknown")
            classification = _markdown_text(test.get("classification"))
            confidence_impact = _markdown_text(test.get("finding_confidence_impact"))
            parts = [f"status {status}"]
            if classification:
                parts.append(f"classification {classification}")
            if confidence_impact:
                parts.append(f"finding impact {confidence_impact}")
            skip_reason = _markdown_text(test.get("skip_reason"))
            if skip_reason:
                parts.append(f"skip reason {skip_reason}")
            lines.append(f"- `{test_id}`: " + "; ".join(parts))
        if len(tests) > 10:
            lines.append(f"- Showing 10 of {len(tests)} intent test results. See `intent-test-results.json` for the full list.")
        lines.append("")

    tasks = report.get("next_agent_tasks") if isinstance(report.get("next_agent_tasks"), list) else []
    _append_markdown_secondary_findings(
        lines,
        'Appendix Findings',
        report.get('appendix_findings'),
    )
    _append_markdown_secondary_findings(
        lines,
        'Disproven Findings',
        report.get('disproven_findings'),
    )
    task_texts = [_markdown_text(task) for task in tasks]
    if not task_texts:
        task_texts = [_markdown_text(finding.get("next_agent_task")) for finding in findings if isinstance(finding, dict)]
    task_texts = list(dict.fromkeys(task for task in task_texts if task))
    lines.extend(["## Recommended Follow-up", ""])
    if task_texts:
        for task in task_texts[:10]:
            lines.append(f"- {task}")
        if len(task_texts) > 10:
            lines.append(f"- See `report.agent.json` for {len(task_texts) - 10} additional follow-up task(s).")
    elif findings:
        lines.append("Use the recommendations in the validated main findings above to plan the next fix pass.")
    else:
        lines.append("No immediate follow-up task was generated by this review. If this run was meant to verify a specific risky change, inspect the coverage and intent test artifacts before treating the result as complete assurance.")
    lines.append("")
    lines.extend(
        [
            "## Machine-readable Sources",
            "",
            "- `report.agent.json` contains the normalized findings and follow-up task list.",
            "- `intent-test-results.json` contains the detailed intent-test validation records when tests were generated.",
            "- `artifact-manifest.json` lists the complete set of uploaded worker artifacts.",
            "",
        ]
    )
    language = output_language or str(report.get("output_language") or "en").strip() or "en"
    if language == "zh-CN":
        lines = _zh_cn_markdown(lines)
    elif language != "en":
        lines = _localized_markdown(lines, language)
    return "\n".join(lines)

def materialize_terminal_artifacts(run_dir: Path, artifact_dir: Path, status: str, *, error: str = "") -> None:
    clear_uploaded_artifact_manifest(artifact_dir, source_run_dir=run_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("worker.log.jsonl", ""),
        ("codex-events.jsonl", ""),
        ("progress.log.jsonl", ""),
    ):
        src = run_dir / name
        if src.is_symlink():
            raise RuntimeError(f"artifact source must be a contained regular file in run directory: {name}")
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content, encoding="utf-8")
        _copy_run_artifact_source(run_dir, name, artifact_dir, destination_name=name)
    qa_status = "warn" if status in {"cancelled", "partial_completed"} else "fail"
    qa_message = (
        "Run was cancelled before full repository scan completed."
        if status == "cancelled"
        else "Run produced a partial result before all required review phases completed."
        if status == "partial_completed"
        else "Run failed before full repository scan completed."
    )
    qa_payload = {"schema_version": "qa/v1", "status": qa_status, "errors": [], "warnings": []}
    if qa_status == "fail":
        qa_payload["errors"].append(qa_message)
    else:
        qa_payload["warnings"].append(qa_message)
    write_json(run_dir / "qa.json", qa_payload)
    _copy_run_artifact_source(run_dir, "qa.json", artifact_dir, destination_name="qa.json")
    error_report = {
        "status": status,
        "error": error,
        "created_at": iso_time(time.time()),
    }
    write_json(run_dir / "error-report.json", error_report)
    _copy_run_artifact_source(run_dir, "error-report.json", artifact_dir, destination_name="error-report.json")
    terminal_report_path = run_dir / "report.agent.json"
    include_terminal_report = False
    if terminal_report_path.exists():
        _contained_run_artifact_source(run_dir, "report.agent.json")
        report = read_json(terminal_report_path, {})
        if isinstance(report, dict):
            summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
            summary = dict(summary)
            summary.setdefault("overall_risk", highest_finding_risk(report.get("findings") if isinstance(report.get("findings"), list) else []))
            summary["result_status"] = "incomplete"
            report["summary"] = summary
            write_json(terminal_report_path, report)
            _copy_run_artifact_source(run_dir, "report.agent.json", artifact_dir, destination_name="report.agent.json")
            include_terminal_report = True
    manifest = [
        artifact_item(artifact_dir / "worker.log.jsonl", "worker_log", "application/jsonl", "worker-log", True),
        artifact_item(artifact_dir / "qa.json", "qa", "application/json", "qa-gate", True),
        artifact_item(artifact_dir / "error-report.json", "error_report", "application/json", "error-report", True),
        artifact_item(artifact_dir / "codex-events.jsonl", "codex_event_log", "application/jsonl", "codex-events", False),
        artifact_item(artifact_dir / "progress.log.jsonl", "progress_log", "application/jsonl", "progress-log", False),
    ]
    if include_terminal_report:
        manifest.append(artifact_item(artifact_dir / "report.agent.json", "report.agent", "application/json", "codex-full-repo-review", False))
    manifest = append_debug_bundle_artifact(manifest, run_dir, artifact_dir, status=status, error=error)
    manifest_payload = artifact_manifest_payload(artifact_dir.name, manifest)
    write_json(artifact_dir / "artifact-manifest.json", manifest_payload)
    write_json(run_dir / "artifact-manifest.json", manifest_payload)


def _refresh_manifest_item(item: dict[str, Any], path: Path) -> None:
    data = path.read_bytes() if path.exists() else b""
    item["sha256"] = hashlib.sha256(data).hexdigest()
    item["size_bytes"] = len(data)


def _contained_run_artifact_source(run_dir: Path, relative: str | Path) -> Path:
    run_root = run_dir.resolve(strict=True)
    candidate = run_dir / Path(relative)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"artifact source is missing from run directory: {relative}") from exc
    try:
        resolved.relative_to(run_root)
    except ValueError as exc:
        raise RuntimeError(f"artifact source escapes run directory: {relative}") from exc
    if candidate.is_symlink() or not _is_regular_file_no_follow(candidate):
        raise RuntimeError(f"artifact source must be a contained regular file in run directory: {relative}")
    return candidate


def _copy_run_artifact_source(
    run_dir: Path,
    relative: str | Path,
    artifact_dir: Path,
    *,
    destination_name: str | None = None,
) -> Path:
    source = _contained_run_artifact_source(run_dir, relative)
    name = destination_name or source.name
    if not name or Path(name).is_absolute() or Path(name).name != name:
        raise RuntimeError(f"artifact destination name is invalid: {name}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    destination = artifact_dir / name
    if destination.is_symlink():
        raise RuntimeError(f"artifact destination must not be a symlink: {name}")
    try:
        destination.resolve(strict=False).relative_to(artifact_dir.resolve(strict=True))
    except ValueError as exc:
        raise RuntimeError(f"artifact destination escapes artifact directory: {name}") from exc
    shutil.copy2(source, destination, follow_symlinks=False)
    return destination



def _debug_bundle_files(directory: Path, prefix: str) -> list[tuple[Path, str]]:
    if not directory.is_dir():
        return []
    directory_root = directory.resolve(strict=True)
    files: list[tuple[Path, str]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or not _is_regular_file_no_follow(path) or path.name == DEBUG_BUNDLE_NAME:
            continue
        try:
            rel = path.relative_to(directory).as_posix()
            path.resolve(strict=True).relative_to(directory_root)
        except (OSError, ValueError):
            continue
        if prefix == "run" and (rel == "bundles" or rel.startswith("bundles/")):
            continue
        files.append((path, f"{prefix}/{rel}"))
    return files


def _diagnostic_list(payload: object, *keys: str) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _semantic_output_repair_count(run_dir: Path) -> int:
    path = run_dir / "worker.log.jsonl"
    if not path.is_file():
        return 0
    count = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    for line in lines:
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) and event.get("event") in {
            "semantic_phase_output_repair",
            "reviewer_json_output_repair",
        }:
            count += 1
    return count


def pipeline_diagnostics_payload(run_dir: Path) -> dict[str, Any]:
    raw_files = sorted((run_dir / "raw-reviewers").glob("*.json"))
    verified_files = sorted((run_dir / "verified-reviewers").glob("*.json"))
    planned_assignments = _planned_reviewer_assignments(run_dir)
    reviewer_execution = read_json(run_dir / "reviewer-execution.json", {})
    if not isinstance(reviewer_execution, dict):
        reviewer_execution = {}

    def valid_reviewer_payload(payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("schema_version") != REVIEWER_OUTPUT_SCHEMA_VERSION:
            return False
        if not str(payload.get("bundle_id") or "").strip():
            return False
        if _normalized_reviewer_id(payload.get("reviewer")) not in {
            "security",
            "correctness",
            "test_gap",
            "correctness_lite",
        }:
            return False
        reviewed_paths = payload.get("reviewed_paths")
        if (
            not isinstance(reviewed_paths, list)
            or not reviewed_paths
            or any(not isinstance(item, str) or not item.strip() for item in reviewed_paths)
        ):
            return False
        review_summary = payload.get("review_summary")
        summary_present = (
            isinstance(review_summary, str) and bool(review_summary.strip())
        ) or (
            isinstance(review_summary, (dict, list)) and bool(review_summary)
        )
        findings = payload.get("findings")
        return (
            summary_present
            and isinstance(payload.get("uncertainties"), list)
            and isinstance(findings, list)
            and all(isinstance(finding, dict) for finding in findings)
        )

    def reviewer_file_stats(
        paths: list[Path],
    ) -> tuple[int, int, int, int, set[tuple[str, str]]]:
        findings = 0
        empty_outputs = 0
        malformed_outputs = 0
        valid_outputs = 0
        covered_assignments: set[tuple[str, str]] = set()
        for path in paths:
            payload = read_json(path, {})
            output_findings = _diagnostic_list(payload, "findings")
            findings += len(output_findings)
            if not output_findings:
                empty_outputs += 1
            if not valid_reviewer_payload(payload):
                malformed_outputs += 1
                continue
            valid_outputs += 1
            covered_assignments.update(
                _reviewer_output_assignments(payload, path, planned_assignments)
            )
        return (
            findings,
            empty_outputs,
            malformed_outputs,
            valid_outputs,
            covered_assignments,
        )

    (
        raw_findings,
        empty_raw_outputs,
        malformed_raw_outputs,
        valid_raw_outputs,
        raw_assignments,
    ) = reviewer_file_stats(raw_files)
    (
        verified_findings,
        _empty_verified_outputs,
        malformed_verified_outputs,
        valid_verified_outputs,
        verified_assignments,
    ) = reviewer_file_stats(verified_files)

    planned_count = len(planned_assignments)
    execution_complete = (
        planned_count > 0
        and reviewer_execution.get("strategy") == "one_turn_per_assignment"
        and _qa_int(reviewer_execution.get("assignments_total")) == planned_count
        and _qa_int(reviewer_execution.get("assignments_completed")) == planned_count
    )
    clean_empty_reviewer_completion = (
        execution_complete
        and malformed_raw_outputs == 0
        and malformed_verified_outputs == 0
        and valid_raw_outputs > 0
        and valid_verified_outputs > 0
        and raw_assignments == planned_assignments
        and verified_assignments == planned_assignments
        and raw_findings == 0
        and verified_findings == 0
    )

    locations = read_json(run_dir / "location-verification.json", {})
    location_items = _diagnostic_list(locations, "items", "locations", "results", "verified_locations")
    location_summary = locations.get("summary") if isinstance(locations, dict) and isinstance(locations.get("summary"), dict) else {}
    locations_total = max(
        len(location_items),
        _qa_int(location_summary.get("locations_total") or location_summary.get("total_locations")),
    )

    clusters = read_json(run_dir / "clusters.json", {})
    cluster_items = _diagnostic_list(clusters, "clusters", "candidate_findings", "candidates")
    validation_input = read_json(run_dir / "validation-input.json", {})
    validation_candidates = _diagnostic_list(validation_input, "candidates", "candidate_findings")

    validation = read_json(run_dir / "validated-findings.json", {})
    validated_main = _diagnostic_list(validation, "validated_findings", "validated")
    weak_findings = _diagnostic_list(validation, "weak_findings")
    disproven_findings = _diagnostic_list(validation, "disproven_findings")
    if isinstance(validation, dict) and not weak_findings:
        weak_findings = [
            finding
            for finding in _diagnostic_list(validation, "findings", "results")
            if validation_entry_status(finding) == "weak"
        ]
    if isinstance(validation, dict) and not validated_main:
        validated_main = [
            finding
            for finding in _diagnostic_list(validation, "findings", "results")
            if validation_entry_status(finding) in MAIN_FINDING_VALIDATION_STATUSES
        ]

    report = read_json(run_dir / "report.agent.json", {})
    report_main = _diagnostic_list(report, "findings", "main_findings", "mainFindings")
    report_appendix = _diagnostic_list(report, "appendix_findings", "weak_findings")
    if not report_appendix and isinstance(report, dict) and isinstance(report.get("appendix"), dict):
        report_appendix = _diagnostic_list(report["appendix"], "weak_findings", "findings")
    report_disproven = _diagnostic_list(report, "disproven_findings")
    report_weak = [finding for finding in report_appendix if validation_entry_status(finding) == "weak"]

    def weak_report_matches(entry: dict[str, Any]) -> list[dict[str, Any]]:
        ids = finding_binding_ids(entry)
        matches = [
            finding
            for finding in report_weak
            if ids and ids.intersection(finding_binding_ids(finding))
        ]
        if matches:
            return matches
        key = finding_fallback_binding_key(entry)
        return [
            finding
            for finding in report_weak
            if key is not None and finding_fallback_binding_key(finding) == key
        ]

    weak_match_counts = [len(weak_report_matches(entry)) for entry in weak_findings if isinstance(entry, dict)]

    intent_plan = read_json(run_dir / "intent" / "intent-test-plan.json", {})
    intent_targets = _diagnostic_list(intent_plan, "test_targets", "targets", "tests")
    intent_results = read_json(run_dir / "intent" / "intent-test-results.json", {})
    analyzed_results = _diagnostic_list(intent_results, "test_results", "results")
    classifications: dict[str, int] = {}
    executed = 0
    skipped = 0
    for result in analyzed_results:
        if not isinstance(result, dict):
            continue
        classification = str(result.get("classification") or "unknown").strip().lower() or "unknown"
        classifications[classification] = classifications.get(classification, 0) + 1
        raw_result = result.get("raw_result") if isinstance(result.get("raw_result"), dict) else {}
        result_status = str(result.get("status") or raw_result.get("status") or "").strip().lower()
        if result_status in {"passed", "failed", "completed", "error", "timeout", "timed_out"}:
            executed += 1
        else:
            skipped += 1

    repair_count = _semantic_output_repair_count(run_dir)
    blocker_codes: list[str] = []
    if planned_assignments and (not raw_files or not verified_files):
        blocker_codes.append("reviewer_outputs_missing")
    if malformed_raw_outputs or malformed_verified_outputs:
        blocker_codes.append("reviewer_outputs_malformed")
    if planned_assignments and (
        not execution_complete
        or raw_assignments != planned_assignments
        or verified_assignments != planned_assignments
    ):
        blocker_codes.append("reviewer_assignments_incomplete")
    if raw_files and raw_findings == 0 and not clean_empty_reviewer_completion:
        blocker_codes.append("all_reviewer_outputs_empty")
    if len(planned_assignments) > 1 and not reviewer_execution:
        blocker_codes.append("reviewer_execution_untracked")
    elif reviewer_execution and reviewer_execution.get("strategy") != "one_turn_per_assignment":
        blocker_codes.append("reviewer_assignments_batched")
    if intent_targets and executed == 0:
        blocker_codes.append("intent_tests_not_executed")
    if len(report_main) < len(validated_main):
        blocker_codes.append("validated_main_missing_from_report")
    if weak_findings and not report_main:
        blocker_codes.append("weak_findings_excluded_from_main")
    if any(count == 0 for count in weak_match_counts):
        blocker_codes.append("weak_findings_missing_from_report_appendix")
    if any(count > 1 for count in weak_match_counts):
        blocker_codes.append("weak_findings_duplicated_in_report_appendix")
    if repair_count:
        blocker_codes.append("semantic_output_repairs_required")

    return {
        "schema_version": "pipeline-diagnostics/v1",
        "reviewer": {
            "strategy": str(reviewer_execution.get("strategy") or "unknown"),
            "assignments_planned": len(planned_assignments),
            "assignments_completed": _qa_int(reviewer_execution.get("assignments_completed")),
            "raw_outputs": len(raw_files),
            "valid_raw_outputs": valid_raw_outputs,
            "malformed_raw_outputs": malformed_raw_outputs,
            "empty_raw_outputs": empty_raw_outputs,
            "raw_findings": raw_findings,
            "verified_outputs": len(verified_files),
            "valid_verified_outputs": valid_verified_outputs,
            "malformed_verified_outputs": malformed_verified_outputs,
            "verified_findings": verified_findings,
            "execution_artifact": "run/reviewer-execution.json" if reviewer_execution else "",
        },
        "location_verification": {"locations_total": locations_total},
        "clustering": {
            "clusters": len(cluster_items),
            "validation_candidates": len(validation_candidates),
        },
        "validation": {
            "main": len(validated_main),
            "weak": len(weak_findings),
            "disproven": len(disproven_findings),
        },
        "intent_tests": {
            "planned": len(intent_targets),
            "analyzed": len(analyzed_results),
            "executed": executed,
            "skipped": skipped,
            "classifications": dict(sorted(classifications.items())),
        },
        "report": {
            "main": len(report_main),
            "appendix": len(report_appendix),
            "disproven": len(report_disproven),
        },
        "semantic_output_repairs": repair_count,
        "blocker_codes": blocker_codes,
    }


def write_debug_bundle(run_dir: Path, artifact_dir: Path, *, status: str = "", error: str = "") -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = artifact_dir / DEBUG_BUNDLE_NAME
    summary = {
        "schema_version": "pullwise-debug-bundle/v1",
        "created_at": iso_time(time.time()),
        "run_id": artifact_dir.name,
        "status": status,
        "error": error,
        "included_roots": ["run", "artifacts"],
        "pipeline_diagnostics": pipeline_diagnostics_payload(run_dir),
        "notes": [
            "This bundle is uploaded by the worker for live-environment debugging.",
            "Repository source files are not included; run artifacts, phase outputs, and logs are included.",
        ],
    }
    runtime = read_json(run_dir / "codex-runtime.json", {})
    if isinstance(runtime, dict) and runtime.get("schema_version") == "codex-runtime/v1":
        summary["codex_runtime"] = runtime
    with zipfile.ZipFile(bundle_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("debug-summary.json", json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        seen: set[str] = {"debug-summary.json"}
        for source, arcname in [*_debug_bundle_files(run_dir, "run"), *_debug_bundle_files(artifact_dir, "artifacts")]:
            if arcname in seen:
                continue
            seen.add(arcname)
            archive.write(source, arcname)
    return bundle_path


def append_debug_bundle_artifact(
    manifest: list[dict[str, Any]],
    run_dir: Path,
    artifact_dir: Path,
    *,
    status: str = "",
    error: str = "",
) -> list[dict[str, Any]]:
    manifest = [item for item in manifest if item.get("artifact_id") != DEBUG_BUNDLE_ARTIFACT_ID]
    interim_payload = artifact_manifest_payload(artifact_dir.name, manifest)
    write_json(artifact_dir / "artifact-manifest.json", interim_payload)
    write_json(run_dir / "artifact-manifest.json", interim_payload)
    bundle_path = write_debug_bundle(run_dir, artifact_dir, status=status, error=error)
    manifest.append(
        artifact_item(
            bundle_path,
            "debug_bundle",
            "application/zip",
            "pullwise-debug-bundle",
            False,
            artifact_id=DEBUG_BUNDLE_ARTIFACT_ID,
        )
    )
    return manifest


def refresh_log_artifacts(
    run_dir: Path,
    artifact_dir: Path,
    manifest_payload: dict[str, Any] | None = None,
    *,
    status: str = "completed",
    error: str = "",
) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for name in LOG_ARTIFACT_NAMES:
        src = run_dir / name
        if src.is_symlink():
            raise RuntimeError(f"artifact source must be a contained regular file in run directory: {name}")
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("", encoding="utf-8")
        _copy_run_artifact_source(run_dir, name, artifact_dir, destination_name=name)
    payload = manifest_payload if isinstance(manifest_payload, dict) else read_json(artifact_dir / "artifact-manifest.json", {})
    if not isinstance(payload, dict):
        return
    manifest_items = artifact_manifest_items(payload)
    changed = False
    debug_item: dict[str, Any] | None = None
    for item in manifest_items:
        name = str(item.get("name") or "")
        if name in LOG_ARTIFACT_NAMES:
            _refresh_manifest_item(item, artifact_dir / name)
            changed = True
        if item.get("artifact_id") == DEBUG_BUNDLE_ARTIFACT_ID:
            debug_item = item
    if changed:
        write_json(artifact_dir / "artifact-manifest.json", payload)
        write_json(run_dir / "artifact-manifest.json", payload)
    if debug_item is not None:
        write_debug_bundle(run_dir, artifact_dir, status=status, error=error)
        _refresh_manifest_item(debug_item, artifact_dir / DEBUG_BUNDLE_NAME)
        changed = True
    if changed:
        write_json(artifact_dir / "artifact-manifest.json", payload)
        write_json(run_dir / "artifact-manifest.json", payload)
def materialize_artifacts(run_dir: Path, artifact_dir: Path) -> None:
    clear_uploaded_artifact_manifest(artifact_dir, source_run_dir=run_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    required_files = ("report.md", "report.agent.json", "coverage.json", "qa.json", "token-budget.json")
    missing = [name for name in required_files if not (run_dir / name).is_file()]
    if missing:
        raise RuntimeError("required completed artifact source is missing: " + ", ".join(missing))
    optional_defaults = {
        "codex-events.jsonl": "",
        "worker.log.jsonl": "",
        "progress.log.jsonl": "",
    }
    for name, content in optional_defaults.items():
        src = run_dir / name
        if src.is_symlink():
            raise RuntimeError(f"artifact source must be a contained regular file in run directory: {name}")
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content, encoding="utf-8")
    for name in (*required_files, *optional_defaults.keys()):
        _copy_run_artifact_source(run_dir, name, artifact_dir, destination_name=name)
    manifest = []
    for name, kind, media_type, schema_id, required in (
        ("report.md", "report.human", "text/markdown", "human-markdown-report", True),
        ("report.agent.json", "report.agent", "application/json", "codex-full-repo-review", True),
        ("coverage.json", "coverage", "application/json", "coverage", True),
        ("qa.json", "qa", "application/json", "qa-gate", True),
        ("token-budget.json", "token_budget", "application/json", "token-budget", True),
        ("codex-events.jsonl", "codex_event_log", "application/jsonl", "codex-events", False),
        ("worker.log.jsonl", "worker_log", "application/jsonl", "worker-log", False),
        ("progress.log.jsonl", "progress_log", "application/jsonl", "progress-log", False),
    ):
        path = artifact_dir / name
        manifest.append(artifact_item(path, kind, media_type, schema_id, required))
    optional_artifacts = (
        ("inventory.json", "repo_inventory", "application/json", "inventory"),
        ("repo-map.json", "repo_map", "application/json", "repo-map"),
        ("risk-routing.json", "risk_routing", "application/json", "risk-routing"),
        ("bundle-plan.json", "bundle_plan", "application/json", "bundle-plan"),
        ("location-verification.json", "validation_result", "application/json", "location-verification"),
        ("clusters.json", "cluster_result", "application/json", "cluster-output"),
        ("validated-findings.json", "validation_result", "application/json", "validation-output"),
        ("intent/intent-map.json", "intent_map", "application/json", "intent-map"),
        ("intent/intent-test-plan.json", "intent_test_plan", "application/json", "intent-test-plan"),
        ("intent/intent-test-source.json", "intent_test_source", "application/json", "intent-test-source"),
        ("intent/intent-test-results.json", "intent_test_result", "application/json", "intent-test-result"),
        ("intent/intent-test-results.raw.json", "intent_test_output", "application/json", "project-test-run"),
        ("intent/execution-capabilities.json", "intent_execution_capabilities", "application/json", "agentic-execution-capabilities"),
        ("intent/intent-test-preflight.json", "intent_test_preflight", "application/json", "intent-test-preflight"),
        ("intent/intent-test-runtime-diagnostics.json", "intent_test_runtime_diagnostics", "application/json", "intent-test-runtime-diagnostics"),
        ("intent/intent-test-execution-history.json", "intent_test_execution_history", "application/json", "intent-test-execution-history"),
        ("intent/validation-workspace-integrity.json", "intent_workspace_integrity", "application/json", "intent-validation-workspace-integrity"),
    )
    for rel, kind, media_type, schema_id in optional_artifacts:
        src = run_dir / rel
        if not src.exists() and not src.is_symlink():
            continue
        dest = _copy_run_artifact_source(run_dir, rel, artifact_dir, destination_name=src.name)
        manifest.append(
            artifact_item(
                dest,
                kind,
                media_type,
                schema_id,
                False,
                artifact_id=(
                    LOCATION_VERIFICATION_ARTIFACT_ID
                    if rel == "location-verification.json"
                    else None
                ),
            )
        )
    for source_dir, name_prefix, artifact_prefix, kind in (
        (run_dir / "raw-reviewers", "raw-reviewer", "art_raw_reviewer_output", "raw_reviewer_output"),
        (run_dir / "verified-reviewers", "verified-reviewer", "art_verified_reviewer_output", "verified_reviewer_output"),
    ):
        for src in sorted(source_dir.glob("*.json")) if source_dir.is_dir() else []:
            rel = src.relative_to(run_dir)
            dest = _copy_run_artifact_source(
                run_dir,
                rel,
                artifact_dir,
                destination_name=f"{name_prefix}-{src.name}",
            )
            manifest.append(
                artifact_item(
                    dest,
                    kind,
                    "application/json",
                    "reviewer-output",
                    False,
                    artifact_id=f"{artifact_prefix}_{safe_artifact_suffix(src.name)}",
                )
            )
    for src in sorted((run_dir / "intent" / "test-output").glob("*.log")):
        if not src.is_file() and not src.is_symlink():
            continue
        rel = src.relative_to(run_dir)
        dest = _copy_run_artifact_source(
            run_dir,
            rel,
            artifact_dir,
            destination_name=f"intent-test-output-{src.name}",
        )
        manifest.append(
            artifact_item(
                dest,
                "intent_test_output",
                "text/plain",
                "project-test-output",
                False,
                artifact_id=_intent_output_artifact_id(src.name),
            )
        )
    manifest = append_debug_bundle_artifact(manifest, run_dir, artifact_dir, status="completed")
    manifest_payload = artifact_manifest_payload(artifact_dir.name, manifest)
    write_json(artifact_dir / "artifact-manifest.json", manifest_payload)
    write_json(run_dir / "artifact-manifest.json", manifest_payload)


def artifact_item(
    path: Path,
    kind: str,
    media_type: str,
    schema_id: str,
    required: bool,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    data = path.read_bytes() if path.exists() else b""
    artifact_id = artifact_id or "art_" + kind.replace(".", "_")
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "name": path.name,
        "media_type": media_type,
        "schema_id": schema_id,
        "schema_version": "v1",
        "encoding": "utf-8",
        "compression": "none",
        "required": required,
        "storage": {"type": "server_artifact", "url": f"/v1/review-runs/{path.parent.name}/artifacts/{artifact_id}"},
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }



def refresh_terminal_run_snapshot(run_dir: Path, run_id: str, status: str) -> dict[str, Any]:
    snapshot = progress_final_payload(run_dir, run_id, status)
    write_json(run_dir / "progress.json", snapshot)
    run_state = read_json(run_dir / "run-state.json", {})
    if not isinstance(run_state, dict):
        run_state = {}
    run_state["progress"] = snapshot
    active_job = run_state.get("active_job") if isinstance(run_state.get("active_job"), dict) else {}
    if active_job:
        active_job["current_phase"] = snapshot.get("current_phase")
        active_job["state"] = "completed" if status == "completed" else status
        active_job["message"] = snapshot.get("message")
        run_state["active_job"] = active_job
    write_json(run_dir / "run-state.json", run_state)
    return snapshot
def summary_payload(run_dir: Path, status: str) -> dict[str, Any]:
    agent = read_json(run_dir / "report.agent.json", {})
    coverage = read_json(run_dir / "coverage.json", {})
    findings = agent.get("findings") if isinstance(agent.get("findings"), list) else []
    confirmed_findings = [
        finding
        for finding in findings
        if isinstance(finding, dict) and finding_validation_status(finding) != "plausible"
    ]
    plausible_findings = [
        finding
        for finding in findings
        if isinstance(finding, dict) and finding_validation_status(finding) == "plausible"
    ]
    agent_summary = agent.get("summary") if isinstance(agent.get("summary"), dict) else {}
    overall_risk = str((agent_summary or {}).get("overall_risk") or "unknown").strip().lower() or "unknown"
    if overall_risk in {"unknown", "none"}:
        overall_risk = highest_finding_risk(findings)
    return {
        "overall_risk": overall_risk,
        "result_status": "complete" if status == "completed" else "incomplete",
        "finding_counts": {
            "confirmed_critical": count_findings(confirmed_findings, "critical"),
            "confirmed_high": count_findings(confirmed_findings, "high"),
            "confirmed_medium": count_findings(confirmed_findings, "medium"),
            "confirmed_low": count_findings(confirmed_findings, "low"),
            "plausible": len(plausible_findings),
            "weak_appendix": _json_list_len(agent.get("appendix_findings")),
            "disproven": _json_list_len(agent.get("disproven_findings")),
            "suppressed": _qa_int(agent_summary.get("suppressed_count") or agent_summary.get("suppressed")),
        },
        "coverage": coverage if isinstance(coverage, dict) else {},
        "top_findings": findings[:10],
    }


def progress_final_payload(run_dir: Path, run_id: str, status: str) -> dict[str, Any]:
    snapshot = read_json(run_dir / "progress.json", {})
    if not isinstance(snapshot, dict):
        snapshot = {}
    try:
        overall_percent = float(snapshot.get("overall_percent"))
    except (TypeError, ValueError):
        overall_percent = 100.0 if status == "completed" else 0.0
    if status == "completed":
        overall_percent = 100.0
    overall_percent = max(0.0, min(100.0, round(overall_percent, 2)))
    current_phase = str(snapshot.get("current_phase") or "").strip()
    if status == "completed":
        current_phase = "cleanup_active_job"
    elif not current_phase:
        current_phase = "failure_handling"
    message = str(snapshot.get("message") or "").strip()
    if status == "completed":
        message = "Run completed and active job cleaned up."
    elif not message:
        message = "Run completed and result accepted by server." if status == "completed" else f"Run ended with status {status}."
    payload = {
        "run_id": run_id,
        "overall_percent": overall_percent,
        "current_phase": current_phase,
        "status": status,
        "message": message,
    }
    for key in ("steps", "counters", "active_unit", "last_event_sequence", "updated_at"):
        value = snapshot.get(key)
        if key == "steps" and isinstance(value, list):
            payload[key] = value
        elif key == "counters" and isinstance(value, dict):
            reconciled = dict(value)
            reconciled.update(artifact_backed_progress_counters(run_dir))
            payload[key] = reconciled
        elif key == "active_unit" and isinstance(value, dict):
            payload[key] = value
        elif key in {"last_event_sequence", "updated_at"} and value is not None:
            payload[key] = value
    if status == "completed":
        snapshot_steps = snapshot.get("steps") if isinstance(snapshot.get("steps"), list) else []
        steps = snapshot_steps or default_progress_steps()
        completed_steps = []
        seen_cleanup = False
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id") or "").strip()
            if not step_id:
                continue
            seen_cleanup = seen_cleanup or step_id == "cleanup_active_job"
            completed_steps.append(
                {
                    "id": step_id,
                    "index": int(step.get("index") or index),
                    "label": str(step.get("label") or progress_step_label(step_id)),
                    "description": str(step.get("description") or ""),
                    "target_percent": step.get("target_percent"),
                    "status": "completed",
                    "percent": 100.0,
                }
            )
        if not seen_cleanup:
            cleanup_index = len(completed_steps) + 1
            completed_steps.append(
                {
                    "id": "cleanup_active_job",
                    "index": cleanup_index,
                    "label": progress_step_label("cleanup_active_job"),
                    "description": "",
                    "target_percent": 100,
                    "status": "completed",
                    "percent": 100.0,
                }
            )
        payload["steps"] = completed_steps
    return payload


def count_findings(findings: list[Any], severity: str) -> int:
    return sum(1 for item in findings if isinstance(item, dict) and normalized_finding_severity(item.get("severity")) == severity)


def normalized_finding_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "p0": "critical",
        "blocker": "critical",
        "critical": "critical",
        "p1": "high",
        "high": "high",
        "p2": "medium",
        "medium": "medium",
        "moderate": "medium",
        "p3": "low",
        "low": "low",
        "p4": "info",
        "informational": "info",
        "info": "info",
    }
    return aliases.get(text, text)


def highest_finding_risk(findings: list[Any]) -> str:
    ranked = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
    best = "unknown"
    best_rank = -1
    for item in findings:
        if not isinstance(item, dict):
            continue
        severity = normalized_finding_severity(item.get("severity"))
        rank = ranked.get(severity, -1)
        if rank > best_rank:
            best = severity
            best_rank = rank
    return best


def repository_payload(job: dict[str, Any], run_dir: Path | None = None) -> dict[str, Any]:
    repo = str(job.get("repo") or "")
    owner, _, name = repo.partition("/")
    report = read_json(run_dir / "report.agent.json", {}) if run_dir is not None else {}
    commit_sha = resolved_report_commit_sha(run_dir, report, job) if run_dir is not None else (_non_pending_text(job.get("commit")) or "pending")
    return {
        "provider": "github",
        "owner": owner,
        "name": name or repo,
        "commit_sha": commit_sha,
    }
def result_human_report(source_dir: Path | None) -> dict[str, str]:
    if source_dir is None:
        return {"summaryMarkdown": ""}
    report_path = source_dir / "report.md"
    try:
        markdown = report_path.read_text(encoding="utf-8")
    except OSError:
        markdown = ""
    markdown = markdown.strip()
    return {"summaryMarkdown": markdown}


def result_payload(active: ActiveJob, envelope: dict[str, Any], status: str, source_dir: Path | None = None) -> dict[str, Any]:
    agent_report = {}
    repository = envelope.get("repository") if isinstance(envelope.get("repository"), dict) else {}
    resolved_commit = str(repository.get("commit_sha") or repository.get("commit") or "").strip()
    for item in envelope.get("artifact_manifest") or []:
        if item.get("name") == "report.agent.json":
            break
    return {
        "status": status,
        "attempt_id": active.attempt_id,
        "result_checksum": hashlib.sha256(json.dumps(envelope, sort_keys=True).encode("utf-8")).hexdigest(),
        **({"resolved_commit": resolved_commit} if resolved_commit and resolved_commit.lower() != "pending" else {}),
        "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        "reviewWorkerProtocol": envelope,
        "humanReport": result_human_report(source_dir),
        "agentReport": agent_report,
        "readingGuide": {"forAgentDeep": "reviewWorkerProtocol.artifact_manifest"},
        "duration_ms": envelope["execution"].get("duration_ms", 0),
        "error": (envelope.get("error") or {}).get("message", ""),
        "error_code": (envelope.get("error") or {}).get("code", ""),
        "preflight": envelope.get("preflight") or {},
    }


def codex_error_code(error: object) -> str:
    if not error:
        return "CODEX_UNKNOWN_ERROR"
    if isinstance(error, dict):
        candidates = [
            error.get("codexErrorInfo"),
            error.get("codex_error_info"),
            error.get("code"),
            error.get("type"),
            error.get("message"),
        ]
    else:
        text = str(error)
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            candidates = [text]
        else:
            if isinstance(parsed, dict):
                return codex_error_code(parsed)
            candidates = [text]
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        code = CODEX_ERROR_CODES.get(normalized)
        if code:
            return code
        public_code = normalized.replace("-", "_").upper()
        if public_code == "CODEX_QUOTA_EXHAUSTED":
            return "CODEX_QUOTA_EXHAUSTED"
        lowered = normalized.lower()
        if (
            any(marker in lowered for marker in CODEX_QUOTA_ERROR_MARKERS)
            or lowered.strip() == "429"
            or CODEX_QUOTA_HTTP_429_RE.search(normalized) is not None
        ):
            return "CODEX_QUOTA_EXHAUSTED"
    return "CODEX_UNKNOWN_ERROR"


def failure_payload_for_error(error: object, *, status: str = "", phase: str = "") -> dict[str, Any]:
    status_text = str(status or "").strip().lower()
    phase_text = str(phase or "").strip().lower()
    message = str(error or "").strip()
    lowered = message.lower()
    code = codex_error_code(error)

    category = "codex_turn_failure"
    action = "fail_job_terminal"
    if isinstance(error, RepositoryLimitExceeded) or "repositorylimits.max" in lowered:
        code = "REPOSITORY_TOO_LARGE"
        category, action = "repository_limit_exceeded", "fail_job_terminal"
    elif status_text == "cancelled" or "cancel" in lowered:
        category, action = "job_cancelled", "cancel_job"
    elif status_text == "partial_completed":
        category, action = "qa_failure", "partial_result"
    elif phase_text == "start_codex_app_server":
        category, action = "codex_app_server_failure", "disable_worker"
    elif phase_text == "check_codex_auth" or code == "CODEX_UNAUTHORIZED":
        category, action = "codex_auth_failure", "fail_job_terminal"
    elif code == "CODEX_QUOTA_EXHAUSTED":
        category, action = "codex_usage_limit_exceeded", "fail_job_terminal"
    elif code == "CODEX_CONTEXT_WINDOW_EXCEEDED":
        category, action = "context_budget_failure", "fail_job_terminal"
    elif code == "CODEX_SANDBOX_ERROR":
        category, action = "worker_environment_failure", "fail_job_terminal"
    elif "artifact" in lowered and "upload" in lowered:
        category, action = "artifact_upload_failure", "fail_job_terminal"
    elif "result submit" in lowered:
        category, action = "result_submit_failure", "fail_job_terminal"
    elif "server unavailable" in lowered or "connection" in lowered:
        category, action = "server_connection_failure", "fail_job_terminal"
    elif phase_text in {"reviewer_json_validation"} or "json" in lowered or "schema" in lowered:
        category, action = "json_schema_failure", "repair_output"
    elif phase_text == "location_validation":
        category, action = "location_validation_failure", "degrade_scope"
    elif phase_text == "intent_test_planning":
        category, action = "intent_test_planning_failure", "skip_intent_test"
    elif phase_text == "intent_test_writing":
        category, action = "intent_test_generation_failure", "skip_intent_test"
    elif phase_text == "intent_test_running":
        category, action = "intent_test_runtime_failure", "degrade_scope"
    elif phase_text == "intent_test_failure_analysis":
        category, action = "intent_test_oracle_failure", "degrade_scope"
    elif phase_text == "qa_gate" or "qa" in lowered:
        category, action = "qa_failure", "partial_result"

    return {
        "code": code,
        "category": category,
        "message": message,
        "failure_action": action,
    }


def _is_regular_file_no_follow(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except OSError:
        return False


def copy_tree(
    source: Path,
    dest: Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    check_lifecycle_cancelled(cancel_requested)
    remaining_wall_time_seconds(deadline_monotonic)
    try:
        source_mode = source.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise RuntimeError(f"repository source is not readable: {source}") from exc
    if not stat.S_ISDIR(source_mode):
        raise RuntimeError(f"repository source must be a real directory: {source}")

    raise_repository_limit_if_exceeded(
        repository_scan_stats(
            source,
            context="copying checkout",
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        ),
        max_files=max_files,
        max_bytes=max_bytes,
        context="copying checkout",
    )

    for root, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        check_lifecycle_cancelled(cancel_requested)
        remaining_wall_time_seconds(deadline_monotonic)
        root_path = Path(root)
        if ".git" in root_path.parts or ".codex-review" in root_path.parts:
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if name not in {".git", ".codex-review"} and not (root_path / name).is_symlink()
        ]
        rel_root = root_path.relative_to(source)
        if rel_root != Path("."):
            (dest / rel_root).mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            check_lifecycle_cancelled(cancel_requested)
            remaining_wall_time_seconds(deadline_monotonic)
            path = root_path / filename
            if not _is_regular_file_no_follow(path):
                continue
            rel = path.relative_to(source)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)


def repository_file_count(
    repo_dir: Path,
    *,
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> int:
    files_seen = 0
    for root, dirnames, filenames in os.walk(repo_dir, topdown=True, followlinks=False):
        check_lifecycle_cancelled(cancel_requested)
        remaining_wall_time_seconds(deadline_monotonic)
        root_path = Path(root)
        if ".git" in root_path.parts or ".codex-review" in root_path.parts:
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if name not in {".git", ".codex-review"} and not (root_path / name).is_symlink()
        ]
        for filename in filenames:
            check_lifecycle_cancelled(cancel_requested)
            remaining_wall_time_seconds(deadline_monotonic)
            if _is_regular_file_no_follow(root_path / filename):
                files_seen += 1
    return files_seen


def job_clone_url(job: dict[str, Any]) -> str:
    repository = job.get("repository") if isinstance(job.get("repository"), dict) else {}
    for value in (
        job.get("clone_url"),
        job.get("cloneUrl"),
        repository.get("clone_url"),
        repository.get("cloneUrl"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    raise RuntimeError("claimed job must include checkout_dir or repository.clone_url")


def job_clone_token(job: dict[str, Any]) -> str:
    token_payload = job.get("clone_token") or job.get("cloneToken")
    if isinstance(token_payload, dict):
        return str(token_payload.get("token") or "").strip()
    return str(token_payload or "").strip()


def terminate_polled_process(process: Any) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=1)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        # Cancellation/deadline remains the primary failure even when the OS
        # cannot synchronously reap a stubborn child.
        pass


def poll_process_communicate(
    process: Any,
    args: list[str],
    *,
    deadline_monotonic: float | None,
    cancel_requested: Callable[[], bool] | None,
    timeout_deadline_monotonic: float | None = None,
) -> tuple[Any, Any]:
    while True:
        try:
            check_lifecycle_cancelled(cancel_requested)
            remaining = remaining_wall_time_seconds(deadline_monotonic)
        except (JobCancelled, JobPartialCompleted):
            terminate_polled_process(process)
            raise
        if timeout_deadline_monotonic is not None:
            timeout_remaining = timeout_deadline_monotonic - time.monotonic()
            if timeout_remaining <= 0:
                terminate_polled_process(process)
                raise subprocess.TimeoutExpired(args, 0)
        else:
            timeout_remaining = None
        waits = [0.1]
        if remaining is not None:
            waits.append(remaining)
        if timeout_remaining is not None:
            waits.append(timeout_remaining)
        try:
            return process.communicate(timeout=max(0.001, min(waits)))
        except subprocess.TimeoutExpired:
            continue


def run_git(
    args: list[str],
    *,
    env: dict[str, str],
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    check_lifecycle_cancelled(cancel_requested)
    remaining_wall_time_seconds(deadline_monotonic)
    try:
        process = subprocess.Popen(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        stdout, stderr = poll_process_communicate(
            process,
            args,
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        )
        if int(process.returncode or 0) != 0:
            raise subprocess.CalledProcessError(
                int(process.returncode),
                args,
                output=stdout,
                stderr=stderr,
            )
    except subprocess.CalledProcessError as exc:
        stderr = str(exc.stderr or "").strip()
        detail = f": {stderr[-500:]}" if stderr else ""
        raise RuntimeError(f"repository checkout git command failed{detail}") from exc


def write_git_askpass(parent: Path) -> Path:
    path = parent / "git-askpass.sh"
    script = (
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' 'x-access-token' ;;\n"
        "  *Password*) printf '%s\\n' \"$PULLWISE_GIT_TOKEN\" ;;\n"
        "  *) printf '\\n' ;;\n"
        "esac\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    return path


def repository_mirror_identity_url(clone_url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(clone_url)
        hostname = parsed.hostname or ""
        if not parsed.scheme or not hostname:
            return clone_url.split("?", 1)[0].split("#", 1)[0]
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urllib.parse.urlunsplit((parsed.scheme.lower(), host.lower(), parsed.path, "", ""))
    except ValueError:
        return clone_url.split("?", 1)[0].split("#", 1)[0]


def repository_mirror_dir(cache_root: Path, job: dict[str, Any], clone_url: str) -> Path:
    repo = str(job.get("repo") or "repository").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "__", repo).strip("._-") or "repository"
    identity_url = repository_mirror_identity_url(clone_url)
    digest = hashlib.sha256(f"{repo}\n{identity_url}".encode("utf-8")).hexdigest()[:16]
    root = cache_root.resolve(strict=False)
    mirror_dir = root / f"{slug}-{digest}.git"
    try:
        common = os.path.commonpath([str(root), str(mirror_dir)])
    except ValueError as exc:
        raise RuntimeError("repository mirror cache path must stay inside cache root") from exc
    if os.path.normcase(common) != os.path.normcase(str(root)) or mirror_dir == root:
        raise RuntimeError("repository mirror cache path must stay inside cache root")
    return mirror_dir


def ensure_repository_mirror(
    mirror_dir: Path,
    clone_url: str,
    *,
    env: dict[str, str],
    deadline_monotonic: float | None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    check_lifecycle_cancelled(cancel_requested)
    clone_url = repository_mirror_identity_url(clone_url)
    cache_root = mirror_dir.parent
    if cache_root.is_symlink():
        raise RuntimeError("repository mirror cache root must not be a symlink")
    if cache_root.exists() and not cache_root.is_dir():
        raise RuntimeError("repository mirror cache root must be a directory")
    cache_root.mkdir(parents=True, exist_ok=True)
    if mirror_dir.is_symlink():
        raise RuntimeError("repository mirror directory must not be a symlink")
    if mirror_dir.exists() and not mirror_dir.is_dir():
        raise RuntimeError("repository mirror directory must be a directory")
    if not (mirror_dir / "HEAD").is_file():
        run_git(
            ["git", "init", "--bare", str(mirror_dir)],
            env=env,
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        )
        run_git(
            ["git", "-C", str(mirror_dir), "remote", "add", "origin", clone_url],
            env=env,
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        )
    else:
        run_git(
            ["git", "-C", str(mirror_dir), "remote", "set-url", "origin", clone_url],
            env=env,
            deadline_monotonic=deadline_monotonic,
            cancel_requested=cancel_requested,
        )


def repository_fetch_ref(job: dict[str, Any], mirror_dir: Path) -> tuple[str, str]:
    commit = str(job.get("commit") or "").strip()
    commit = "" if commit.lower() == "pending" else commit
    if commit:
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
            raise RuntimeError("claimed job commit must be a Git commit hash or pending")
        target_ref = f"refs/pullwise/commits/{hashlib.sha256(commit.lower().encode('utf-8')).hexdigest()[:24]}"
        return commit, target_ref

    branch = str(job.get("branch") or "main").strip() or "main"
    if (
        branch.startswith(("/", "-"))
        or branch.endswith(("/", "."))
        or any(marker in branch for marker in ("..", "@{", "\\"))
        or any(ord(char) < 32 or char in " ~^:?*[" for char in branch)
        or any(part in {"", ".", ".."} or part.endswith(".lock") for part in branch.split("/"))
    ):
        raise RuntimeError("claimed job branch name is invalid")
    target_ref = f"refs/pullwise/branches/{hashlib.sha256(branch.encode('utf-8')).hexdigest()[:24]}"
    return f"refs/heads/{branch}", target_ref


def fetch_repository_mirror(
    job: dict[str, Any],
    mirror_dir: Path,
    *,
    env: dict[str, str],
    deadline_monotonic: float | None,
    cancel_requested: Callable[[], bool] | None = None,
) -> str:
    source_ref, target_ref = repository_fetch_ref(job, mirror_dir)
    force_prefix = "+" if source_ref.startswith("refs/heads/") else ""
    run_git(
        [
            "git",
            "-C",
            str(mirror_dir),
            "fetch",
            "--depth",
            "1",
            "--no-tags",
            "origin",
            f"{force_prefix}{source_ref}:{target_ref}",
        ],
        env=env,
        deadline_monotonic=deadline_monotonic,
        cancel_requested=cancel_requested,
    )
    return target_ref


def clone_checkout_from_mirror(
    mirror_dir: Path,
    repo_dir: Path,
    mirror_ref: str,
    *,
    env: dict[str, str],
    deadline_monotonic: float | None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    run_git(
        ["git", "clone", "--shared", "--no-checkout", str(mirror_dir), str(repo_dir)],
        env=env,
        deadline_monotonic=deadline_monotonic,
        cancel_requested=cancel_requested,
    )
    checkout_ref = "refs/pullwise/checkout"
    run_git(
        ["git", "-C", str(repo_dir), "fetch", "--depth", "1", "origin", f"{mirror_ref}:{checkout_ref}"],
        env=env,
        deadline_monotonic=deadline_monotonic,
        cancel_requested=cancel_requested,
    )
    run_git(
        ["git", "-C", str(repo_dir), "checkout", "--detach", checkout_ref],
        env=env,
        deadline_monotonic=deadline_monotonic,
        cancel_requested=cancel_requested,
    )
    run_git(
        ["git", "-C", str(repo_dir), "remote", "remove", "origin"],
        env=env,
        deadline_monotonic=deadline_monotonic,
        cancel_requested=cancel_requested,
    )


def remove_repository_mirror(mirror_dir: Path) -> None:
    if mirror_dir.is_symlink():
        raise RuntimeError("repository mirror directory must not be a symlink")
    if mirror_dir.exists():
        shutil.rmtree(mirror_dir)


def clone_repository_checkout(
    job: dict[str, Any],
    repo_dir: Path,
    *,
    mirror_cache_root: Path,
    deadline_monotonic: float | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    check_lifecycle_cancelled(cancel_requested)
    remaining_wall_time_seconds(deadline_monotonic)
    clone_url = job_clone_url(job)
    token = job_clone_token(job)
    mirror_dir = repository_mirror_dir(mirror_cache_root, job, clone_url)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    askpass_path: Path | None = None
    if token:
        askpass_path = write_git_askpass(repo_dir.parent)
        env["GIT_ASKPASS"] = str(askpass_path)
        env["PULLWISE_GIT_TOKEN"] = token
    try:
        for attempt in range(2):
            try:
                ensure_repository_mirror(
                    mirror_dir,
                    clone_url,
                    env=env,
                    deadline_monotonic=deadline_monotonic,
                    cancel_requested=cancel_requested,
                )
                mirror_ref = fetch_repository_mirror(
                    job,
                    mirror_dir,
                    env=env,
                    deadline_monotonic=deadline_monotonic,
                    cancel_requested=cancel_requested,
                )
                clone_checkout_from_mirror(
                    mirror_dir,
                    repo_dir,
                    mirror_ref,
                    env=env,
                    deadline_monotonic=deadline_monotonic,
                    cancel_requested=cancel_requested,
                )
                try:
                    os.utime(mirror_dir, None, follow_symlinks=False)
                except (NotImplementedError, OSError):
                    pass
                break
            except JobCancelled:
                raise
            except JobPartialCompleted:
                raise
            except RuntimeError:
                if repo_dir.exists():
                    shutil.rmtree(repo_dir)
                if attempt > 0:
                    raise
                remove_repository_mirror(mirror_dir)
    finally:
        env.pop("PULLWISE_GIT_TOKEN", None)
        if askpass_path is not None:
            try:
                askpass_path.unlink()
            except OSError:
                pass


def ensure_json(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob("*.json"):
        read_json(path, {})


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(read_text_no_follow(path, encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {} if default is None else default


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temp_path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, value: Any) -> None:
    append_text_no_follow(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_worker_config(path: Path, job: dict[str, Any], config: Any) -> None:
    write_json(
        path,
        {
            "protocol_version": PROTOCOL_VERSION,
            "worker_id": str(config.worker_id),
            "job_id": safe_id(job.get("job_id"), "job"),
            "full_repo_scan": True,
            "max_active_jobs": 1,
            "maintains_local_queue": False,
        },
    )


def active_attempt_id(config: Any, job: dict[str, Any]) -> str:
    try:
        attempt = int(job.get("attempt") or 1)
    except (TypeError, ValueError):
        attempt = 1
    return f"{config.worker_id}-{attempt}"


def _contained_artifact_upload_path(artifact_dir: Path, name: str, *, final_refresh: bool = False) -> Path:
    raw_name = Path(name)
    prefix = "final refresh artifact" if final_refresh else "artifact"
    if not name or raw_name.is_absolute() or raw_name.name != name:
        raise RuntimeError(f"{prefix} path escapes artifact directory before upload: {name}")
    path = artifact_dir / raw_name
    try:
        path.resolve(strict=False).relative_to(artifact_dir.resolve(strict=True))
    except ValueError as exc:
        raise RuntimeError(f"{prefix} path escapes artifact directory before upload: {name}") from exc
    if not path.exists():
        raise RuntimeError(f"{prefix} listed in manifest is missing or not a regular file before upload: {name}")
    try:
        path.resolve(strict=True).relative_to(artifact_dir.resolve(strict=True))
    except OSError as exc:
        raise RuntimeError(f"{prefix} listed in manifest is missing or not a regular file before upload: {name}") from exc
    except ValueError as exc:
        raise RuntimeError(f"{prefix} path escapes artifact directory before upload: {name}") from exc
    if path.is_symlink() or not _is_regular_file_no_follow(path):
        raise RuntimeError(f"{prefix} listed in manifest is missing or not a regular file before upload: {name}")
    return path


def upload_log_artifacts_best_effort(client: Any, job_id: str, attempt_id: str, run_dir: Path, artifact_dir: Path) -> str:
    try:
        upload_log_artifacts(client, job_id, attempt_id, run_dir, artifact_dir)
    except Exception as exc:
        append_jsonl(run_dir / "worker.log.jsonl", {"event": "final_log_artifact_upload_failed", "error": str(exc), "time": iso_time(time.time())})
        return str(exc)
    return ""


def _unique_uploaded_artifact_items_by_id(artifact_dir: Path) -> dict[str, dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    duplicate_ids: set[str] = set()
    for item in uploaded_artifact_manifest_items(artifact_dir):
        artifact_id = str(item.get("artifact_id") or "").strip()
        if not artifact_id:
            continue
        if artifact_id in unique:
            duplicate_ids.add(artifact_id)
            continue
        unique[artifact_id] = item
    for artifact_id in duplicate_ids:
        unique.pop(artifact_id, None)
    return unique


def upload_log_artifacts(client: Any, job_id: str, attempt_id: str, run_dir: Path, artifact_dir: Path) -> None:
    manifest_payload = read_json(artifact_dir / "artifact-manifest.json", {})
    if not isinstance(manifest_payload, dict):
        raise RuntimeError("artifact manifest must be an object before final log upload")
    refresh_log_artifacts(run_dir, artifact_dir, manifest_payload, status="completed")
    manifest = artifact_manifest_items(manifest_payload)
    if not manifest:
        raise RuntimeError("artifact manifest must contain artifact items before final log upload")
    accepted_items = _unique_uploaded_artifact_items_by_id(artifact_dir)
    candidates = 0
    for item in manifest:
        name = str(item.get("name") or "").strip()
        if name not in FINAL_REFRESH_ARTIFACT_NAMES:
            continue
        candidates += 1
        artifact_id = str(item.get("artifact_id") or "").strip()
        if not artifact_id:
            raise RuntimeError(f"final refresh artifact manifest entry requires artifact_id: {name}")
        path = _contained_artifact_upload_path(artifact_dir, name, final_refresh=True)
        data = path.read_bytes()
        if str(item.get("sha256") or "").lower() != hashlib.sha256(data).hexdigest():
            raise RuntimeError(f"final refresh artifact sha256 mismatch before upload: {name}")
        if int(item.get("size_bytes") if item.get("size_bytes") is not None else -1) != len(data):
            raise RuntimeError(f"final refresh artifact size mismatch before upload: {name}")
        if accepted_items.get(artifact_id) == item:
            continue
        client.artifact(
            job_id,
            artifact_id,
            {
                "protocol_version": PROTOCOL_VERSION,
                "attempt_id": attempt_id,
                "run_id": artifact_dir.name,
                "artifact": item,
                "content_base64": base64.b64encode(data).decode("ascii"),
                "final_log_upload": True,
            },
        )
    if candidates == 0:
        raise RuntimeError("artifact manifest contains no final refresh artifacts before final log upload")

def upload_artifacts_best_effort(client: Any, job_id: str, attempt_id: str, artifact_dir: Path) -> str:
    try:
        upload_artifacts(client, job_id, attempt_id, artifact_dir)
    except Exception as exc:
        return str(exc)
    return ""


def upload_artifacts(
    client: Any,
    job_id: str,
    attempt_id: str,
    artifact_dir: Path,
    *,
    progress_callback: Any | None = None,
    source_run_dir: Path | None = None,
) -> None:
    manifest_payload = read_json(artifact_dir / "artifact-manifest.json", {})
    if source_run_dir is not None and isinstance(manifest_payload, dict):
        refresh_log_artifacts(source_run_dir, artifact_dir, manifest_payload, status="completed")
    manifest = artifact_manifest_items(manifest_payload)
    if not isinstance(manifest_payload, dict) or not manifest:
        raise RuntimeError("artifact manifest must contain artifact items before upload")
    if manifest_payload.get("schema_version") != "artifact-manifest/v1":
        raise RuntimeError("artifact manifest must use schema_version artifact-manifest/v1 before upload")
    if str(manifest_payload.get("run_id") or "").strip() != artifact_dir.name:
        raise RuntimeError("artifact manifest run_id does not match upload run before upload")
    clear_uploaded_artifact_manifest(artifact_dir, source_run_dir=source_run_dir)
    uploadable: list[tuple[dict[str, Any], Path]] = []
    seen_artifact_ids: set[str] = set()
    for item in manifest:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not artifact_id or not name:
            raise RuntimeError("artifact manifest entries require artifact_id and name")
        if artifact_id in seen_artifact_ids:
            raise RuntimeError(f"artifact manifest contains duplicate artifact_id before upload: {artifact_id}")
        seen_artifact_ids.add(artifact_id)
        storage = item.get("storage") if isinstance(item.get("storage"), dict) else {}
        expected_storage_url = f"/v1/review-runs/{artifact_dir.name}/artifacts/{artifact_id}"
        if storage.get("type") != "server_artifact" or str(storage.get("url") or "") != expected_storage_url:
            raise RuntimeError(f"artifact manifest storage does not match upload run before upload: {artifact_id}")
        path = _contained_artifact_upload_path(artifact_dir, name)
        uploadable.append((item, path))
    uploadable.sort(key=lambda pair: 1 if pair[0].get("artifact_id") == DEBUG_BUNDLE_ARTIFACT_ID else 0)
    total = len(uploadable)
    uploaded_manifest_items: list[dict[str, Any]] = []
    optional_upload_errors: list[str] = []
    uploaded_count = 0
    for item, path in uploadable:
        artifact_id = str(item.get("artifact_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if source_run_dir is not None and artifact_id == DEBUG_BUNDLE_ARTIFACT_ID:
            for log_name in LOG_ARTIFACT_NAMES:
                src = source_run_dir / log_name
                if src.is_symlink():
                    raise RuntimeError(f"artifact source must be a contained regular file in run directory: {log_name}")
                if not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_text("", encoding="utf-8")
                _copy_run_artifact_source(source_run_dir, log_name, artifact_dir, destination_name=log_name)
            write_debug_bundle(source_run_dir, artifact_dir, status="completed")
            _refresh_manifest_item(item, artifact_dir / DEBUG_BUNDLE_NAME)
            write_json(artifact_dir / "artifact-manifest.json", manifest_payload)
            write_json(source_run_dir / "artifact-manifest.json", manifest_payload)
        data = path.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest()
        if str(item.get("sha256") or "").lower() != actual_sha:
            raise RuntimeError(f"artifact sha256 mismatch before upload: {name}")
        if int(item.get("size_bytes") if item.get("size_bytes") is not None else -1) != len(data):
            raise RuntimeError(f"artifact size mismatch before upload: {name}")
        try:
            client.artifact(
                job_id,
                artifact_id,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "attempt_id": attempt_id,
                    "run_id": artifact_dir.name,
                    "artifact": item,
                    "content_base64": base64.b64encode(data).decode("ascii"),
                },
            )
        except Exception as exc:
            if item.get("required") is True:
                raise
            optional_upload_errors.append(f"{artifact_id}: {exc}")
            continue
        uploaded_manifest_items.append(copy.deepcopy(item))
        uploaded_count += 1
        write_uploaded_artifact_manifest(artifact_dir, manifest_payload, uploaded_manifest_items, source_run_dir=source_run_dir)
        if progress_callback is not None:
            progress_callback(uploaded_count, total, item)
    if optional_upload_errors:
        warnings = manifest_payload.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        for message in optional_upload_errors:
            warnings.append(f"optional artifact upload failed: {message}")
        manifest_payload["warnings"] = warnings
        write_json(artifact_dir / "artifact-manifest.json", manifest_payload)
        if source_run_dir is not None:
            write_json(source_run_dir / "artifact-manifest.json", manifest_payload)
        if uploaded_manifest_items:
            write_uploaded_artifact_manifest(artifact_dir, manifest_payload, uploaded_manifest_items, source_run_dir=source_run_dir)
