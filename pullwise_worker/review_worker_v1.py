from __future__ import annotations

import base64
import copy
import fnmatch
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import __version__
from ._main_part_01_bootstrap import worker_machine_metrics_payload

try:
    import fcntl
except ImportError:  # pragma: no cover - runtime is Linux only; import stays testable elsewhere.
    fcntl = None

PROTOCOL_VERSION = "review-worker-protocol/v1"
WORKER_VERSION = __version__
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
    "bootstrap_helper_scripts",
    "repo_map",
    "risk_routing",
    "reviewer_fanout",
    "clustering_and_voting",
    "intent_mining",
    "intent_test_planning",
    "intent_test_writing",
    "intent_test_failure_analysis",
    "validator_disproof",
    "final_report_json",
}
INTENT_VALIDATION_CHILD_PHASES = {
    "intent_mining",
    "intent_test_planning",
    "validation_workspace_prepare",
    "intent_test_writing",
    "intent_test_running",
    "intent_test_failure_analysis",
}
CORE_EFFORT_PHASES = SEMANTIC_PHASES - {"bootstrap_helper_scripts"}
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
    "bundle_planning": (("bundle-plan.json", "bundle-plan/v1"), ("coverage.json", "coverage/v1")),
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
    "429",
)
GLOBAL_AGENTS_TEXT = """# Codex Repo Review Worker Global Instructions

You are running inside an isolated Codex repo review worker.

Rules:
- Full repository scan, not diff review.
- Do not install dependencies.
- Do not call external review or scanning tools.
- Do not modify application source files.
- Write only under .codex-review/** when file writes are needed.
- For dynamic tests, write only to the disposable validation workspace or .codex-review/generated-tests/**.
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
- Every main finding must have path, line range, evidence, impact, recommendation, severity, confidence, and next_agent_task.
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
        return {
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


class Isolation:
    def __init__(self, config: Any) -> None:
        self.worker_id = str(config.worker_id)
        service_home = Path(str(getattr(config, "service_home", "") or "/var/lib/codex-review"))
        self.service_home = service_home
        configured_root = os.environ.get("PULLWISE_WORKER_ROOT", "").strip()
        self.worker_root = Path(configured_root) if configured_root else service_home / "workers" / self.worker_id
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
        if not agents.exists():
            agents.write_text(GLOBAL_AGENTS_TEXT, encoding="utf-8")
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
    service_home = Path(str(getattr(config, "service_home", "") or "/var/lib/pullwise-worker")).expanduser()
    worker_root = Path(str(getattr(config, "worker_root", "") or service_home / "workers" / str(getattr(config, "worker_id", "worker") or "worker"))).expanduser()
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


def load_codex_sdk_runtime() -> CodexSdkRuntime:
    try:
        from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox
    except ImportError as exc:  # pragma: no cover - exercised on hosts missing the runtime dependency.
        raise RuntimeError("openai-codex Python SDK is required; install the pullwise-worker package dependencies") from exc
    return CodexSdkRuntime(Codex=Codex, CodexConfig=CodexConfig, ApprovalMode=ApprovalMode, Sandbox=Sandbox)


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
        self.events_path = events_path
        self.rate_limit_callback = rate_limit_callback
        self._runtime: CodexSdkRuntime | None = None
        self._codex: Any | None = None
        self._client: Any | None = None
        self._threads: dict[str, Any] = {}
        self._approval_workspace = cwd

    def start(self) -> None:
        if self.is_running():
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
        return self._codex is not None

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
        self.events_path = events_path
        self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def start_thread(self, repo_dir: Path, model: str) -> str:
        if self._codex is None:
            self.start()
        if self._codex is None or self._runtime is None:
            raise RuntimeError("Codex SDK is not running")
        self._approval_workspace = repo_dir
        thread = self._codex.thread_start(
            approval_mode=self._runtime.ApprovalMode.deny_all,
            cwd=str(repo_dir),
            sandbox=self._runtime.Sandbox.workspace_write,
            service_name="codex_repo_review_worker",
            model=model or None,
        )
        thread_id = str(getattr(thread, "id", "") or "")
        if thread_id:
            self._threads[thread_id] = thread
        return thread_id

    def run_turn(
        self,
        *,
        thread_id: str,
        repo_dir: Path,
        prompt: str,
        effort: str,
        read_only: bool,
        timeout_seconds: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> None:
        client = self._sdk_client()
        self._approval_workspace = repo_dir
        params = {
            "cwd": str(repo_dir),
            "approvalPolicy": "never",
            "sandboxPolicy": self._sandbox_policy(repo_dir, read_only=read_only),
            "effort": effort,
            "summary": "concise",
        }
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
                raise TimeoutError("codex turn start timed out")
            if start_completed.wait(min(0.5, remaining)):
                break
            if cancel_requested is not None and cancel_requested():
                abandon_start()
                raise JobCancelled("cancel requested")
        start_error = start_state.get("error")
        if isinstance(start_error, BaseException):
            raise start_error
        started = start_state.get("started")
        turn = getattr(started, "turn", None)
        turn_id = str(getattr(turn, "id", "") or getattr(started, "turn_id", "") or "")
        if not turn_id:
            return

        completed = threading.Event()
        abandoned = threading.Event()
        error: dict[str, str] = {}

        def consume_turn() -> None:
            try:
                while True:
                    notification = client.next_turn_notification(turn_id)
                    if abandoned.is_set():
                        break
                    self._record_sdk_notification(notification)
                    method = str(getattr(notification, "method", "") or "")
                    payload = getattr(notification, "payload", None)
                    if method == "account/rateLimits/updated" and self.rate_limit_callback is not None:
                        params = self._model_to_dict(payload)
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
                        error["message"] = self._json_text(turn_error)
                        break
                    if method != "turn/completed":
                        continue
                    completed_turn = getattr(payload, "turn", None)
                    completed_turn_id = str(getattr(completed_turn, "id", "") or getattr(payload, "turn_id", "") or "")
                    if completed_turn_id and completed_turn_id != turn_id:
                        continue
                    turn_error = getattr(completed_turn, "error", None) or getattr(completed_turn, "last_error", None)
                    if turn_error:
                        error["message"] = self._json_text(turn_error)
                    break
            except BaseException as exc:  # noqa: BLE001 - surfaced to the worker phase as a Codex turn failure.
                error["message"] = str(exc)
            finally:
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
                self.interrupt(thread_id, turn_id)
                raise TimeoutError(f"codex turn timed out: {turn_id}")
            if completed.wait(min(0.5, remaining)):
                break
            if cancel_requested is not None and cancel_requested():
                abandoned.set()
                self.interrupt(thread_id, turn_id)
                raise JobCancelled("cancel requested")
        if error.get("message"):
            raise RuntimeError(error["message"])

    def _sandbox_policy(self, repo_dir: Path, *, read_only: bool) -> dict[str, Any]:
        if read_only:
            return {"type": "readOnly", "networkAccess": False}
        validation_repo = repo_dir.parent / "validation-repo"
        return {
            "type": "workspaceWrite",
            "networkAccess": False,
            "writableRoots": [str(repo_dir / ".codex-review"), str(validation_repo)],
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

    def request(self, method: str, params: dict[str, Any] | None = None, timeout_seconds: int = 30) -> dict[str, Any]:
        del timeout_seconds
        client = self._sdk_client()
        if hasattr(client, "_request_raw"):
            result = client._request_raw(method, params or {})
            return result if isinstance(result, dict) else {}
        raise RuntimeError(f"Codex SDK client does not expose raw request support: {method}")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        client = self._sdk_client()
        if hasattr(client, "notify"):
            client.notify(method, params or {})

    def login_chatgpt(self) -> Any:
        if self._codex is None:
            self.start()
        if self._codex is None:
            raise RuntimeError("Codex SDK is not running")
        return self._codex.login_chatgpt()

    def login_chatgpt_device_code(self) -> Any:
        if self._codex is None:
            self.start()
        if self._codex is None:
            raise RuntimeError("Codex SDK is not running")
        return self._codex.login_chatgpt_device_code()

    def login_api_key(self, api_key: str) -> None:
        if self._codex is None:
            self.start()
        if self._codex is None:
            raise RuntimeError("Codex SDK is not running")
        self._codex.login_api_key(api_key)

    def account(self, *, refresh_token: bool = False) -> Any:
        if self._codex is None:
            self.start()
        if self._codex is None:
            raise RuntimeError("Codex SDK is not running")
        return self._codex.account(refresh_token=refresh_token)

    def close(self) -> None:
        codex = self._codex
        self._codex = None
        self._client = None
        self._threads.clear()
        if codex is not None:
            codex.close()

    def _sdk_client(self) -> Any:
        if self._client is None:
            if self._codex is None:
                self.start()
            self._client = getattr(self._codex, "_client", None) if self._codex is not None else None
        if self._client is None:
            raise RuntimeError("Codex SDK client is not running")
        return self._client

    def _approval_handler(self, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        return approval_response_for_request({"method": method, "params": params or {}}, self._approval_workspace)

    def _record_sdk_notification(self, notification: Any) -> None:
        method = str(getattr(notification, "method", "") or "")
        payload = self._model_to_dict(getattr(notification, "payload", None))
        append_jsonl(self.events_path, {"method": method, "params": payload})

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

    def snapshot_if_due(self, *, active: bool = False) -> dict[str, Any] | None:
        current_time = int(time.time())
        if active and self.snapshot is not None:
            return self.snapshot
        if active or current_time < self.next_check_at:
            return self.snapshot
        return self.refresh(current_time)

    def refresh(self, current_time: int | None = None) -> dict[str, Any]:
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
                self.snapshot = {
                    "provider": "codex",
                    "status": "unavailable",
                    "ready": True,
                    "reason": "codex_quota_unavailable",
                    "checkedAt": checked_at,
                    "nextCheckAt": next_check_at,
                    "thresholdPercent": threshold,
                    "lastError": quota_text(exc, 500),
                }
        finally:
            if close_server and server is not None:
                server.close()
        self.next_check_at = int((self.snapshot or {}).get("nextCheckAt") or next_check_at)
        return self.snapshot or {}

    def apply_rate_limit_update(self, params: dict[str, Any]) -> None:
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
PROJECT_TEST_COMMANDS = {"npm", "pnpm", "yarn", "pytest", "python", "python3", "py", "go", "cargo", "mvn", "gradle", "make"}
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
        paths: list[object] = []
        if isinstance(request.get("paths"), list):
            paths.extend(request.get("paths") or [])
        elif request.get("path"):
            paths.append(request.get("path"))
        if request.get("grantRoot"):
            paths.append(request.get("grantRoot"))
        file_changes = request.get("fileChanges")
        if isinstance(file_changes, dict):
            paths.extend(file_changes.keys())
        if paths and all(path_is_under_allowed_write_root(workspace, path) for path in paths):
            return "acceptForSession", "write is limited to .codex-review or disposable validation workspace"
        return "decline", "file changes outside approved review workspaces are not allowed"
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
    return path_is_under_codex_review(workspace, raw_path) or path_is_under_validation_workspace(workspace, raw_path)


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
    cwd_in_validation = path_is_under_validation_workspace(workspace, cwd)
    if not cwd_in_workspace and not cwd_in_validation:
        return False
    if executable in {"python", "python3"} and len(argv) >= 2:
        if (
            path_is_under_codex_review(workspace, argv[1])
            and "/tools/" in Path(argv[1]).as_posix()
            and helper_command_arguments_are_contained(argv[2:], workspace, cwd)
        ):
            return True
    if executable in PROJECT_TEST_COMMANDS and cwd_in_validation:
        allowed, _reason = intent_test_command_policy(argv, cwd, workspace.parent / "validation-repo")
        return allowed
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


def helper_command_arguments_are_contained(argv: list[str], workspace: Path, cwd: Path) -> bool:
    for part in argv:
        value = str(part)
        candidate_value = value.split("=", 1)[1] if value.startswith("-") and "=" in value else value
        candidate = Path(candidate_value)
        looks_like_path = candidate.is_absolute() or candidate_value.startswith(".") or "/" in candidate_value or "\\" in candidate_value
        if not looks_like_path:
            local_candidate = cwd / candidate
            looks_like_path = local_candidate.exists() or local_candidate.is_symlink()
        if looks_like_path and not read_operand_is_contained(candidate_value, workspace, cwd):
            return False
    return True


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
    unsafe_options = {"--no-index", "--ext-diff", "--textconv", "--open-files-in-pager"}
    lowered = {str(part).lower() for part in argv[1:]}
    if lowered.intersection(unsafe_options) or any(str(part).lower().startswith("--output=") for part in argv[1:]):
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
        if part == "--no-pager" or part.startswith("--no-") or part in {"--paginate", "-p"}:
            index += 1
            continue
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


def _command_executable_available(command: list[str]) -> tuple[bool, str]:
    if not command:
        return False, "command is empty"
    executable = str(command[0])
    executable_name = normalized_executable_name(executable)
    path = Path(executable)
    if path.is_absolute() and path.exists():
        return True, ""
    if shutil.which(executable) is not None:
        return True, ""
    return False, f"dependency_missing: {executable_name} executable is not available"


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
    available, reason = _command_executable_available(argv)
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
        try:
            import importlib.util

            if importlib.util.find_spec("pytest") is None:
                return False, "dependency_missing: pytest is not available"
        except (ImportError, ValueError):
            return False, "dependency_missing: pytest is not available"
    if executable == "pytest":
        try:
            import importlib.util

            if importlib.util.find_spec("pytest") is None:
                return False, "dependency_missing: pytest is not available"
        except (ImportError, ValueError):
            return False, "dependency_missing: pytest is not available"
    return True, "runnable"


def _intent_preflight_classification(reason: str) -> str:
    if reason.startswith("dependency_missing:"):
        return "dependency_missing"
    if reason.startswith("environment_error:"):
        return "environment_error"
    return "skipped_not_runnable"


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
    return False, f"{executable} is not an allowed generated test command"


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

    def default_codex_events_path(self) -> Path:
        return self.isolation.logs / "codex-sdk-events.jsonl"

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

    def run(self, *, once: bool = False) -> None:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("Pullwise review worker v1 is Linux only")
        self.isolation.prepare()
        self.lock.acquire()
        try:
            if hasattr(self.client, "register"):
                self.client.register()
            self.state.state = "idle"
            while True:
                self.heartbeat()
                if self.state.can_lease():
                    job = self.client.claim()
                    if job:
                        self.run_job(job)
                if once:
                    return
                time.sleep(max(1, int(getattr(self.config, "poll_seconds", 5) or 5)))
        finally:
            self.close_codex_client()
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
        quota_ready = bool((quota or {}).get("ready", True))
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
        if active and active.job_id in (cancelled or []):
            self.request_cancel(active, reason="server_cancelled")
        commands = response.get("commands") if isinstance(response, dict) and isinstance(response.get("commands"), list) else []
        if active:
            for command in commands:
                if not isinstance(command, dict):
                    continue
                if command.get("type") == "cancel_run" and str(command.get("run_id") or "") == active.run_id:
                    self.request_cancel(active, reason=str(command.get("reason") or "server_cancelled"))
        if process_worker_command:
            self.handle_worker_command(response, active=active)
        return response if isinstance(response, dict) else {}

    def handle_worker_command(self, response: object, *, active: ActiveJob | None) -> bool:
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
            or active is not None
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
        self.heartbeat()
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
        with self._progress_lock:
            active.last_event_sequence += 1
            if progress is not None:
                active.overall_percent = round(float(progress), 2)
            active.current_phase = phase
            active.current_phase_status = status
            active.current_phase_percent = round(float(current_phase_percent), 2)
            active.message = message
            active.apply_progress_data(data)
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

    def request_cancel(self, active: ActiveJob, *, reason: str = "server_cancelled") -> None:
        with self._progress_lock:
            reason_text = str(reason or "server_cancelled").strip() or "server_cancelled"
            active.cancel_requested = True
            active.cancel_reason = reason_text
            active.state = "cancelling"
            active.message = "Cancellation requested."
            if active.run_dir is not None:
                self.emit_cancel_requested(active, active.run_dir)

    def emit_cancel_requested(self, active: ActiveJob, run_dir: Path) -> None:
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

    def start_phase(self, active: ActiveJob, run_dir: Path, phase: str, progress: int) -> None:
        with self._progress_lock:
            if active.cancel_requested:
                return
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

    def complete_phase(self, active: ActiveJob, run_dir: Path, phase: str, progress: int, *, data: dict[str, Any] | None = None) -> None:
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
        with self._progress_lock:
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
        job_id = safe_id(job.get("job_id"), "job")
        run_id = safe_id(job.get("run_id") or f"run_{job_id}", "run")
        lease_id = safe_id(job.get("lease_id") or f"lease_{job_id}", "lease")
        attempt = int(job.get("attempt") or 1)
        active = ActiveJob(job_id=job_id, run_id=run_id, lease_id=lease_id, attempt_id=f"{self.config.worker_id}-{attempt}")
        self.state.set_active(active)
        heartbeat_stop, heartbeat_thread = self.start_active_job_supervisor(active)
        terminal_state = "failed"
        codex_client: CodexSdkClient | None = None
        repo_dir: Path | None = None
        run_dir: Path | None = None
        artifact_dir: Path | None = None
        started = time.time()
        try:
            job_policy = validate_job_policy(job)
            scan_deadline_seconds = int(job_policy.get("review_worker", {}).get("scanDeadlineSeconds") or 0)
            wall_deadline = started + scan_deadline_seconds if scan_deadline_seconds > 0 else None
            repo_dir, run_dir, artifact_dir = self.prepare_workspace(job, run_id)
            active.run_dir = run_dir
            events_path = run_dir / "codex-events.jsonl"
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_started", "job_id": job_id, "run_id": run_id, "time": iso_time(started)})
            self.emit_event(active, run_dir, "run_started", "prepare_workspace", status="running", progress=0, message="Run started.")
            for phase, progress in PIPELINE_PHASES:
                if wall_deadline is not None and time.time() > wall_deadline:
                    raise JobPartialCompleted("review wall-time deadline exceeded")
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
                        thread_id = codex_client.start_thread(repo_dir, model_for_job(job)) if codex_client else ""
                        active.thread_id = thread_id
                        run_state = read_json(run_dir / "run-state.json", {})
                        if not isinstance(run_state, dict):
                            run_state = {}
                        run_state.update({"thread_id": thread_id, "active_job": active.heartbeat_payload()})
                        write_json(run_dir / "run-state.json", run_state)
                    elif phase == "check_codex_auth":
                        self.run_codex_auth_check(codex_client, repo_dir, run_dir, job)
                    elif phase == "submit_result_envelope":
                        pass
                    elif phase == "cleanup_active_job":
                        pass
                    elif phase == "reviewer_fanout":
                        self.run_reviewer_fanout_phase(
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            active=active,
                            progress=progress,
                        )
                    elif phase in SEMANTIC_PHASES:
                        self.run_semantic_phase(codex_client, repo_dir, run_dir, job, phase)
                        if phase == "validator_disproof":
                            repair_validation_output_artifact(run_dir / "validated-findings.json")
                        if phase == "final_report_json":
                            repair_agent_report_artifact(run_dir, job)
                    elif phase == "reviewer_json_validation":
                        self.run_reviewer_json_validation_phase(codex_client, repo_dir, run_dir, job)
                    elif phase in MECHANICAL_PHASES:
                        self.run_mechanical_phase(repo_dir, run_dir, job, phase, active=active, progress=progress)
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
                        validate_phase_outputs(run_dir, phase, artifact_dir)
                    except Exception as validation_exc:
                        if phase not in SEMANTIC_PHASES:
                            raise
                        self.repair_semantic_phase_outputs(
                            codex_client,
                            repo_dir,
                            run_dir,
                            job,
                            phase,
                            validation_exc,
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
                terminal_state = "failed"
            else:
                terminal_state = "result_submit_failed"
                return
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join()
            if codex_client is not None:
                codex_client.set_events_path(self.default_codex_events_path())
            if terminal_state in TERMINAL_STATES:
                self.state.clear_active(terminal_state)
            self.heartbeat()

    def prepare_workspace(self, job: dict[str, Any], run_id: str) -> tuple[Path, Path, Path]:
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
        scan_deadline_seconds = None
        try:
            scan_deadline_seconds = int(review_worker_policy_for_job(job).get("scanDeadlineSeconds") or 0)
        except (TypeError, ValueError):
            scan_deadline_seconds = None
        copy_deadline = time.monotonic() + scan_deadline_seconds if scan_deadline_seconds and scan_deadline_seconds > 0 else None
        if source:
            copy_tree(
                Path(source),
                repo_dir,
                max_files=limits.get("maxFiles") if limits else None,
                max_bytes=limits.get("maxBytes") if limits else None,
                deadline_monotonic=copy_deadline,
            )
        else:
            clone_repository_checkout(job, repo_dir, deadline_monotonic=copy_deadline)
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
            deadline_monotonic=copy_deadline,
        )
        if repository_file_count(repo_dir) <= 0:
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
            return False
        try:
            self.client.result(job_id, payload)
            return True
        except Exception as exc:
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
            return False


    def run_codex_auth_check(self, codex_client: CodexSdkClient | None, repo_dir: Path, run_dir: Path, job: dict[str, Any]) -> None:
        if codex_client is None:
            raise RuntimeError("Codex SDK client is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        codex_client.run_turn(
            thread_id=thread_id,
            repo_dir=repo_dir,
            prompt='Codex auth check: return only JSON {"ok": true}.',
            effort="medium",
            read_only=True,
            timeout_seconds=turn_timeout_for_job(job),
            cancel_requested=self.poll_cancel_requested,
        )

    def run_semantic_phase(self, codex_client: CodexSdkClient | None, repo_dir: Path, run_dir: Path, job: dict[str, Any], phase: str) -> None:
        if codex_client is None:
            raise RuntimeError("Codex SDK client is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        effort = effort_for_phase(job, phase)
        prompt = phase_prompt(phase, run_dir, job)
        codex_client.run_turn(
            thread_id=thread_id,
            repo_dir=repo_dir,
            prompt=prompt,
            effort=effort,
            read_only=False,
            timeout_seconds=turn_timeout_for_job(job),
            cancel_requested=self.poll_cancel_requested,
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
    ) -> None:
        if codex_client is None:
            raise RuntimeError("Codex SDK client is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        assignments = planned_reviewer_assignment_sequence(run_dir)
        if not assignments:
            raise RuntimeError("reviewer_fanout has no planned reviewer assignments")

        raw_dir = run_dir / "raw-reviewers"
        raw_dir.mkdir(parents=True, exist_ok=True)
        execution_path = run_dir / "reviewer-execution.json"
        expected_assignments = set(assignments)
        records: list[dict[str, Any]] = []
        completed = 0
        execution = {
            "schema_version": "reviewer-execution/v1",
            "strategy": "one_turn_per_assignment",
            "root_thread_id": thread_id,
            "assignments_total": len(assignments),
            "assignments_completed": 0,
            "assignments": records,
        }
        write_json(execution_path, execution)

        for index, (bundle_id, reviewer_id) in enumerate(assignments, start=1):
            output_name = reviewer_assignment_output_name(bundle_id, reviewer_id)
            output_path = raw_dir / output_name
            if output_path.exists():
                output_path.unlink()
            record = {
                "bundle_id": bundle_id,
                "reviewer_id": reviewer_id,
                "output": f"raw-reviewers/{output_name}",
                "status": "running",
                "started_at": iso_time(time.time()),
            }
            records.append(record)
            write_json(execution_path, execution)
            append_jsonl(
                run_dir / "worker.log.jsonl",
                {
                    "event": "reviewer_assignment_started",
                    "bundle_id": bundle_id,
                    "reviewer_id": reviewer_id,
                    "assignment_index": index,
                    "assignments_total": len(assignments),
                    "time": iso_time(time.time()),
                },
            )
            try:
                codex_client.run_turn(
                    thread_id=thread_id,
                    repo_dir=repo_dir,
                    prompt=reviewer_assignment_prompt(run_dir, bundle_id, reviewer_id, job),
                    effort=effort_for_phase(job, "reviewer_fanout"),
                    read_only=False,
                    timeout_seconds=turn_timeout_for_job(job),
                    cancel_requested=self.poll_cancel_requested,
                )
            except Exception as exc:
                record.update(
                    {
                        "status": "failed",
                        "completed_at": iso_time(time.time()),
                        "error": quota_text(exc, 500),
                    }
                )
                write_json(execution_path, execution)
                append_jsonl(
                    run_dir / "worker.log.jsonl",
                    {
                        "event": "reviewer_assignment_failed",
                        "bundle_id": bundle_id,
                        "reviewer_id": reviewer_id,
                        "error": quota_text(exc, 500),
                        "time": iso_time(time.time()),
                    },
                )
                raise

            payload = read_json(output_path, None)
            covered = _reviewer_output_assignments(payload, output_path, expected_assignments)
            valid_output = (
                isinstance(payload, dict)
                and isinstance(payload.get("findings"), list)
                and covered == {(bundle_id, reviewer_id)}
            )
            finding_count = len(payload.get("findings") or []) if isinstance(payload, dict) else 0
            record.update(
                {
                    "status": "completed" if valid_output else "invalid_output",
                    "completed_at": iso_time(time.time()),
                    "finding_count": finding_count,
                }
            )
            if valid_output:
                completed += 1
            else:
                record["error"] = "exact assignment output is missing, malformed, or covers a different assignment"
            execution["assignments_completed"] = completed
            write_json(execution_path, execution)
            append_jsonl(
                run_dir / "worker.log.jsonl",
                {
                    "event": "reviewer_assignment_completed" if valid_output else "reviewer_assignment_invalid_output",
                    "bundle_id": bundle_id,
                    "reviewer_id": reviewer_id,
                    "finding_count": finding_count,
                    "assignment_index": index,
                    "assignments_total": len(assignments),
                    "time": iso_time(time.time()),
                },
            )
            self.progress_phase(
                active,
                run_dir,
                "reviewer_fanout",
                progress,
                current_phase_percent=min(90.0, (index / len(assignments)) * 90.0),
                message=f"Completed reviewer assignment {index} of {len(assignments)}.",
                data={
                    "reviewer_runs_total": len(assignments),
                    "reviewer_runs_completed": completed,
                    "active_unit": {
                        "bundle_id": bundle_id,
                        "reviewer_id": reviewer_id,
                        "assignment_index": index,
                    },
                },
            )

    def repair_semantic_phase_outputs(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        phase: str,
        validation_error: object,
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
        codex_client.run_turn(
            thread_id=thread_id,
            repo_dir=repo_dir,
            prompt=phase_repair_prompt(phase, run_dir, validation_error, job),
            effort=effort_for_phase(job, phase),
            read_only=False,
            timeout_seconds=turn_timeout_for_job(job),
            cancel_requested=self.poll_cancel_requested,
        )
        fallback_semantic_artifact(run_dir, job, phase)

    def run_reviewer_json_validation_phase(self, codex_client: CodexSdkClient | None, repo_dir: Path, run_dir: Path, job: dict[str, Any]) -> None:
        try:
            validate_reviewer_outputs(run_dir)
        except RuntimeError as validation_exc:
            self.repair_reviewer_outputs(codex_client, repo_dir, run_dir, job, validation_exc)
            validate_reviewer_outputs(run_dir)

    def repair_reviewer_outputs(
        self,
        codex_client: CodexSdkClient | None,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        validation_error: object,
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
        codex_client.run_turn(
            thread_id=thread_id,
            repo_dir=repo_dir,
            prompt=reviewer_json_repair_prompt(run_dir, validation_error, job),
            effort=effort_for_phase(job, "reviewer_json_validation"),
            read_only=False,
            timeout_seconds=turn_timeout_for_job(job),
            cancel_requested=self.poll_cancel_requested,
        )

    def run_mechanical_phase(
        self,
        repo_dir: Path,
        run_dir: Path,
        job: dict[str, Any],
        phase: str,
        *,
        active: ActiveJob | None = None,
        progress: int = 0,
    ) -> None:
        if phase == "inventory_repository":
            policy = validate_job_policy(job)
            limits = policy["repository_limits"] if isinstance(policy.get("repository_limits"), dict) else {}
            scan_deadline_seconds = int(policy.get("review_worker", {}).get("scanDeadlineSeconds") or 0)
            scan_deadline = time.monotonic() + scan_deadline_seconds if scan_deadline_seconds > 0 else None
            inv = inventory(
                repo_dir,
                max_files=int(limits.get("maxFiles")) if limits.get("maxFiles") is not None else None,
                max_bytes=int(limits.get("maxBytes")) if limits.get("maxBytes") is not None else None,
                deadline_monotonic=scan_deadline,
            )
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
        elif phase == "bundle_planning":
            write_json(run_dir / "bundle-plan.json", bundle_plan_payload(run_dir))
        elif phase == "bundle_packing":
            pack_bundles(repo_dir, run_dir)
        elif phase == "reviewer_json_validation":
            validate_reviewer_outputs(run_dir)
        elif phase == "location_validation":
            write_json(run_dir / "location-verification.json", location_verification_payload(repo_dir, run_dir))
        elif phase == "validation_workspace_prepare":
            prepare_validation_workspace(repo_dir, run_dir)
        elif phase == "intent_test_running":
            write_json(run_dir / "intent" / "intent-test-results.raw.json", run_intent_tests(run_dir))
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
            "artifact_manifest": manifest,
            "extensions": {"worker_internal": {"bundle_count": 1}},
        }


class JobCancelled(RuntimeError):
    pass


class JobPartialCompleted(RuntimeError):
    pass


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


def iso_time(value: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def model_for_job(job: dict[str, Any]) -> str:
    return str(validate_job_policy(job)["model"])


def core_effort_for_job(job: dict[str, Any]) -> str:
    return str(validate_job_policy(job)["reasoning_effort"])


def review_worker_policy_for_job(job: dict[str, Any]) -> dict[str, int]:
    return dict(validate_job_policy(job)["review_worker"])


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
    try:
        scan_deadline_seconds = _policy_int(review_budget, "max_wall_time_seconds", "maxWallTimeSeconds", default=None)
    except (TypeError, ValueError):
        raise ValueError("claimed job must include review_request.budget.max_wall_time_seconds") from None
    if turn_timeout_seconds <= 0 or scan_deadline_seconds < 0:
        raise ValueError("review worker turn timeout must be positive and scan deadline must be non-negative")
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
        },
        "repository_limits": limits,
        "intent_test_validation": intent_validation_policy_for_job(job),
    }


SEMANTIC_PHASE_PROMPT_SPECS: dict[str, dict[str, Any]] = {
    "bootstrap_helper_scripts": {
        "role": "Bootstrap Helper Script Maintainer",
        "prompt_files": [],
        "inputs": [
            ".codex-review/AGENTS.review.md",
            "self-contained required helper, schema, and prompt files already materialized under .codex-review",
        ],
        "outputs": [".codex-review/tools/*.py", ".codex-review/schemas/*.schema.json", ".codex-review/prompts/*.md"],
        "instructions": [
            "Create or repair only review helper tools, schemas, and prompt templates.",
            "Use only the self-contained .codex-review contract in this checkout; do not search for or depend on a parent-workspace specification file.",
            "Helper scripts must use Python 3 standard library only and perform mechanical tasks only.",
            "Return a concise implementation summary; do not include secrets.",
        ],
    },
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
        "outputs": ["risk-routing.json", "coverage.json when coverage is refined"],
        "instructions": [
            "Classify files and directories into P0/P1/P2/P3/SKIP using role, entrypoint, trust boundary, auth/payment/data/upload/config/concurrency signals.",
            "Do not report findings in this phase.",
            "Write JSON only using risk-routing/v1.",
        ],
    },
    "reviewer_fanout": {
        "role": "Sequential Logical Reviewer Fanout",
        "prompt_files": [
            "reviewers/security.md",
            "reviewers/correctness.md",
            "reviewers/test_gap.md",
            "reviewers/correctness_lite.md",
        ],
        "inputs": ["bundles/*.md", "repo-map.json", "risk-routing.json", "reviewer prompts"],
        "outputs": ["raw-reviewers/*.json"],
        "instructions": [
            "Review bundles in tier order using security, correctness, test-gap, and correctness-lite perspectives as applicable.",
            "Every finding must be concrete, located, evidenced, actionable, and include false-positive risk.",
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
            "Write JSON only using intent-map/v1.",
        ],
    },
    "intent_test_planning": {
        "role": "Intent Test Planner",
        "prompt_files": ["intent/05_intent_test_planner.md"],
        "inputs": ["clusters.json", "intent/intent-map.json", "validation-input.json"],
        "outputs": ["intent/intent-test-plan.json"],
        "instructions": [
            "Select only high-value P0/P1 candidate findings for temporary tests.",
            "Every test target must link to finding IDs and behavioral contract IDs.",
            "Write JSON only using intent-test-plan/v1.",
        ],
    },
    "intent_test_writing": {
        "role": "Intent Test Writer",
        "prompt_files": ["intent/06_intent_test_writer.md"],
        "inputs": ["intent/intent-test-plan.json", "target snippets", "existing tests", "disposable validation workspace"],
        "outputs": ["intent/intent-test-source.json", "intent/generated-tests/** or disposable validation workspace tests"],
        "instructions": [
            "Write temporary tests only in the disposable validation workspace or .codex-review/generated-tests/**.",
            "Every generated test must include target_test_ids linking it to the intent-test-plan target(s) it implements.",
            "Do not modify the main repo workspace, install dependencies, use production secrets, or use network.",
            "Return JSON describing created test files.",
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
            "Write JSON only using intent-test-result/v1.",
        ],
    },
    "validator_disproof": {
        "role": "Validation Reviewer",
        "prompt_files": ["08_validator.md"],
        "inputs": ["clusters.json", "location-verification.json", "intent/intent-test-results.json", "related snippets"],
        "outputs": ["validated-findings.json"],
        "instructions": [
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

def phase_prompt(phase: str, run_dir: Path, job: dict[str, Any] | None = None) -> str:
    spec = SEMANTIC_PHASE_PROMPT_SPECS.get(phase, {})
    role = str(spec.get("role") or phase.replace("_", " ").title())
    inputs = [str(item) for item in spec.get("inputs", []) if str(item).strip()]
    outputs = [str(item) for item in spec.get("outputs", []) if str(item).strip()]
    instructions = [str(item) for item in spec.get("instructions", []) if str(item).strip()]
    prompt_files = [str(item) for item in spec.get("prompt_files", []) if str(item).strip()]
    lines = [
        f"Phase: {phase}",
        f"Role: {role}",
        "Perform only this full-repository review phase.",
        "Do not modify application source files.",
        "Do not install dependencies.",
        "Do not call external review/scanning services.",
        "Write phase outputs only under the active .codex-review tree.",
        f"Run artifact directory: {run_dir}",
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
        lines.append(f"- Paths are relative to the run artifact directory: {run_dir}")
        lines.extend(f"- {item}" for item in outputs)
    if instructions:
        lines.append("Phase instructions:")
        lines.extend(f"- {item}" for item in instructions)
    adaptive_context = _adaptive_prompt_context(run_dir)
    if adaptive_context:
        lines.extend(adaptive_context)
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
    lines = [
        "Phase: reviewer_fanout",
        "Role: Independent Bundle Reviewer",
        "Perform exactly one logical reviewer assignment in this turn.",
        f"Bundle assignment: {bundle_id}",
        f"Reviewer assignment: {reviewer_id}",
        f"Read the packed bundle at: {run_dir / 'bundles' / f'{bundle_id}.md'}",
        f"Exact output path: {run_dir / 'raw-reviewers' / output_name}",
        f"Output path relative to the run artifact directory: raw-reviewers/{output_name}",
        "Do not review or emit output for any other bundle or reviewer assignment in this turn.",
        "Treat this assignment as an independent review; do not inherit an earlier reviewer's empty conclusion.",
        "Inspect the concrete source in the packed bundle and follow referenced repository call sites when needed.",
        "Existing tests are contract evidence, not proof that the implementation is correct.",
        "Actively look for concrete failure scenarios before concluding that findings is empty.",
        "Before reporting an async UI race or duplicate mutation, inspect disabled state, synchronous ref/lock guards, event ordering, and server idempotency; prove that the second action reaches a harmful non-idempotent operation.",
        "Every finding must include id, title, severity, confidence, path/line evidence, impact, recommendation, false_positive_risk, and next_agent_task.",
        "If findings is empty, review_summary must document concrete areas examined, checks performed, and rejected candidates with source-backed reasons.",
        "Write one JSON object only using schema_version codex-reviewer-output/v1.",
        "The object must include bundle_id, reviewer, reviewed_paths, findings, review_summary, and uncertainties.",
        "Do not modify application source files, install dependencies, use network, or call external scanning services.",
        "Write only the exact output file under the active .codex-review tree; do not rely on prose in the turn response.",
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
            phase_prompt(phase, run_dir, job).rstrip(),
        ]
    ) + "\n"


def reviewer_json_repair_prompt(
    run_dir: Path,
    validation_error: object,
    job: dict[str, Any] | None = None,
) -> str:
    lines = [
            "Reviewer JSON output repair",
            f"Local validation failed: {validation_error}",
            "Repair only malformed files under .codex-review/runs/*/raw-reviewers/.",
            "Each repaired file must be JSON using schema_version codex-reviewer-output/v1 with a findings array.",
            "Preserve valid reviewer evidence, locations, severity, confidence, and false-positive context.",
            "Do not add unrelated findings.",
            "Do not modify application source files.",
            "Do not install dependencies or call external review/scanning services.",
            "",
            f"Run artifact directory: {run_dir}",
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
) -> dict[str, int]:
    files_seen = 0
    bytes_seen = 0
    for path in sorted(repo_dir.rglob("*")):
        if ".git" in path.parts or ".codex-review" in path.parts:
            continue
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            raise RuntimeError(f"repository scan deadline exceeded while {context}")
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
) -> None:
    raise_repository_limit_if_exceeded(
        repository_scan_stats(repo_dir, context=context, deadline_monotonic=deadline_monotonic),
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
) -> dict[str, Any]:
    raise_repository_limit_if_exceeded(
        repository_scan_stats(repo_dir, context="inventorying checkout", deadline_monotonic=deadline_monotonic),
        max_files=max_files,
        max_bytes=max_bytes,
        context="inventorying checkout",
    )
    files = []
    for path in sorted(repo_dir.rglob("*")):
        if ".git" in path.parts or ".codex-review" in path.parts:
            continue
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            raise RuntimeError("repository scan deadline exceeded while inventorying checkout")
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
    for key in ("tier", "depth", "review_depth", "reviewDepth", "priority", "classification"):
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
    if not isinstance(payload, dict) or isinstance(payload.get("routes"), list):
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
    inventory = read_json(run_dir / "inventory.json", {})
    source_like = [
        item
        for item in inventory.get("files", [])
        if isinstance(item, dict) and item.get("is_source_like") is True
    ] if isinstance(inventory, dict) else []
    if source_like and not routes:
        errors.append("risk-routing.json routes must not be empty for a non-empty source inventory")
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


def generic_file_tier(item: dict[str, Any], routing: dict[str, Any] | None = None) -> str:
    if isinstance(routing, dict) and routing:
        default_tier = risk_routing_default_tier(routing)
        if default_tier:
            return default_tier
    if item.get("risk_hints"):
        return "P0"
    path = str(item.get("path") or "").lower()
    if item.get("is_source_like") and any(part in path for part in ("src/", "app/", "server/", "api/", "lib/")):
        return "P1"
    if item.get("is_source_like"):
        return "P2"
    return "P3"


def profile_fallback_route_for_item(item: dict[str, Any], profile: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(profile, dict) or profile.get("schema_version") != "repo-profile/v1":
        return None
    path = str(item.get("path") or "").replace("\\", "/").lower().lstrip("./")
    adapters = set(str(adapter) for adapter in profile.get("adapter_ids", []) if isinstance(adapter, str))
    primary_languages = set(str(language) for language in profile.get("primary_languages", []) if isinstance(language, str))
    reasons: list[str] = []
    tier = ""
    if "python-backend" in adapters or "python" in primary_languages:
        if any(part in path for part in ("auth", "session", "permission", "tenant", "webhook", "migration", "migrations")):
            tier = "P0"
            if "auth" in path or "session" in path or "permission" in path:
                reasons.append("python_backend_auth_path")
            if "webhook" in path:
                reasons.append("python_backend_webhook_path")
            if "migration" in path:
                reasons.append("python_backend_migration_path")
    if "frontend" in adapters or "node" in adapters:
        if any(part in path for part in ("/api/", "api/", "route.ts", "route.js", "server-action", "middleware", ".env")):
            tier = "P1" if tier != "P0" else tier
            reasons.append("frontend_server_boundary_path")
    if not tier:
        return None
    return {"path": str(item.get("path") or ""), "tier": tier, "source": "profile_fallback", "reasons": sorted(set(reasons)) or ["profile_fallback_path"]}


def profile_fallback_file_tier(item: dict[str, Any], profile: dict[str, Any] | None) -> str:
    route = profile_fallback_route_for_item(item, profile)
    return str(route.get("tier") or "") if isinstance(route, dict) else ""


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
    routing = semantic_routing if isinstance(semantic_routing, dict) and semantic_routing.get("schema_version") == "risk-routing/v1" else {}
    inv = inventory_payload if isinstance(inventory_payload, dict) else {}
    files = inv.get("files") if isinstance(inv.get("files"), list) else []
    routes: list[dict[str, Any]] = []
    sources = {"semantic_routes": 0, "profile_fallback_routes": 0, "hard_skip_routes": 0}
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
        fallback_route = profile_fallback_route_for_item(item, profile)
        if fallback_route:
            routes.append(fallback_route)
            sources["profile_fallback_routes"] += 1
            continue
        routes.append({"path": path, "tier": generic_file_tier(item, routing), "source": "generic", "reasons": ["generic_default"]})
    return {"schema_version": "effective-risk-routing/v1", "run_id": "", "sources": sources, "routes": routes}


def file_tier(item: dict[str, Any], routing: dict[str, Any] | None = None, profile: dict[str, Any] | None = None) -> str:
    if is_hard_skip_item(item):
        return "SKIP"
    if isinstance(routing, dict) and routing:
        semantic_tier = semantic_file_tier(item, routing)
        if semantic_tier:
            return semantic_tier
    fallback_tier = profile_fallback_file_tier(item, profile)
    if fallback_tier:
        return fallback_tier
    return generic_file_tier(item, routing)


def bundle_plan_payload(run_dir: Path) -> dict[str, Any]:
    inv = read_json(run_dir / "inventory.json", {})
    files = inv.get("files") if isinstance(inv.get("files"), list) else []
    semantic_routing = read_json(run_dir / "risk-routing.json", {})
    semantic_routing = semantic_routing if isinstance(semantic_routing, dict) and semantic_routing.get("schema_version") == "risk-routing/v1" else {}
    profile = read_json(run_dir / "repo-profile.json", {})
    profile = profile if isinstance(profile, dict) and profile.get("schema_version") == "repo-profile/v1" else {}
    routing = effective_routing(semantic_routing, profile, inv)
    routing["run_id"] = run_dir.name
    write_json(run_dir / "effective-risk-routing.json", routing)
    route_source_by_path = {
        str(route.get("path") or ""): str(route.get("source") or "")
        for route in routing.get("routes", [])
        if isinstance(route, dict)
    }
    grouped: dict[str, list[dict[str, Any]]] = {"P0": [], "P1": [], "P2": [], "P3": [], "SKIP": []}
    for item in files:
        if isinstance(item, dict):
            item["_routing_source"] = route_source_by_path.get(str(item.get("path") or ""), "")
            grouped[file_tier(item, routing, profile)].append(item)
    source_like_by_tier = {
        tier: [item for item in items if isinstance(item, dict) and item.get("is_source_like")]
        for tier, items in grouped.items()
    }
    bundles = []
    for tier in ("P0", "P1", "P2"):
        tier_files = sorted(
            (
                segment
                for item in grouped[tier]
                for segment in split_oversized_bundle_item(item)
            ),
            key=lambda item: (
                _bundle_component_key(str(item.get("path") or "")),
                str(item.get("path") or ""),
                int(item.get("_segment_start_line") or 0),
            ),
        )
        chunk: list[dict[str, Any]] = []
        token_count = 0
        for item in tier_files:
            item_tokens = int(item.get("estimated_tokens") or 0)
            if chunk and (len(chunk) >= 25 or token_count + item_tokens > MAX_BUNDLE_ESTIMATED_TOKENS):
                bundles.append(bundle_payload(tier, len(bundles) + 1, chunk, token_count))
                chunk = []
                token_count = 0
            chunk.append(item)
            token_count += item_tokens
        if chunk:
            bundles.append(bundle_payload(tier, len(bundles) + 1, chunk, token_count))
    coverage = {
        "schema_version": "coverage/v1",
        "source_like_files_total": sum(1 for item in files if isinstance(item, dict) and item.get("is_source_like")),
        "deep_reviewed_files": len(source_like_by_tier["P0"]),
        "standard_reviewed_files": len(source_like_by_tier["P1"]),
        "light_reviewed_files": len(source_like_by_tier["P2"]),
        "inventory_only_files": len(source_like_by_tier["P3"]),
        "skipped_files": len(source_like_by_tier["SKIP"]),
        "intent_tests_planned": 0,
        "intent_tests_run": 0,
        "intent_tests_supporting_findings": 0,
        "skipped_scope": [item.get("path") for item in source_like_by_tier["SKIP"][:100]],
    }
    if source_like_by_tier["SKIP"]:
        coverage["skipped_reasons"] = {"semantic_or_inventory_skip": coverage["skipped_scope"]}
    write_json(run_dir / "coverage.json", coverage)
    return {"schema_version": "bundle-plan/v1", "run_id": run_dir.name, "routing_sources": routing.get("sources", {}), "bundles": bundles}


def _bundle_component_key(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("./")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"app", "src", "server", "lib"}:
        return f"{parts[0]}/{parts[1]}"
    if len(parts) >= 2 and parts[0] in {"test", "tests", "__tests__"}:
        return f"app/{parts[1]}"
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return normalized


def split_oversized_bundle_item(item: dict[str, Any]) -> list[dict[str, Any]]:
    estimated_tokens = max(0, int(item.get("estimated_tokens") or 0))
    line_count = max(0, int(item.get("line_count") or 0))
    if estimated_tokens <= MAX_BUNDLE_ESTIMATED_TOKENS:
        return [item]
    segment_count = max(2, math.ceil(estimated_tokens / MAX_BUNDLE_ESTIMATED_TOKENS))
    if line_count <= 1:
        estimated_characters = max(1, estimated_tokens * 4)
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
        if segment_tokens > MAX_BUNDLE_ESTIMATED_TOKENS:
            raise RuntimeError(
                f"source segment exceeds hard bundle limit: {item.get('path')}:{start_line}-{end_line}"
            )
        segment = dict(item)
        segment["estimated_tokens"] = segment_tokens
        segment["_segment_start_line"] = start_line
        segment["_segment_end_line"] = end_line
        segments.append(segment)
    return segments


def _bundle_metadata(files: list[dict[str, Any]]) -> dict[str, Any]:
    paths = [str(item.get("path") or "") for item in files if item.get("path")]
    component_keys = sorted({_bundle_component_key(path) for path in paths if _bundle_component_key(path)})
    related_tests = sorted(
        path
        for path in paths
        if path.startswith(("test/", "tests/", "__tests__/")) or "/tests/" in f"/{path}" or Path(path).name.startswith("test_") or ".test." in Path(path).name
    )
    grouping_reasons: list[str] = []
    if len(paths) > 1 and len(component_keys) == 1:
        grouping_reasons.append("path_affinity")
    if related_tests:
        grouping_reasons.append("test_affinity")
    metadata: dict[str, Any] = {}
    if len(component_keys) == 1:
        metadata["component_key"] = component_keys[0]
    if grouping_reasons:
        metadata["grouping_reasons"] = grouping_reasons
    if related_tests:
        metadata["related_tests"] = related_tests
    routing_sources = sorted({str(item.get("_routing_source") or "") for item in files if str(item.get("_routing_source") or "")})
    if routing_sources:
        metadata["routing_sources"] = routing_sources
    return metadata

def bundle_payload(tier: str, index: int, files: list[dict[str, Any]], estimated_tokens: int) -> dict[str, Any]:
    reviewers = {
        "P0": ["security", "correctness", "test_gap"],
        "P1": ["correctness", "test_gap"],
        "P2": ["correctness_lite"],
    }[tier]
    payload = {
        "bundle_id": f"{tier.lower()}-bundle-{index:03d}",
        "tier": tier,
        "title": f"{tier} review bundle {index}",
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
    payload.update(_bundle_metadata(files))
    return payload


def pack_bundles(repo_dir: Path, run_dir: Path) -> None:
    plan = read_json(run_dir / "bundle-plan.json", {})
    bundles = plan.get("bundles") if isinstance(plan.get("bundles"), list) else []
    bundle_dir = run_dir / "bundles"
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
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
        file_ranges = bundle.get("file_ranges") if isinstance(bundle.get("file_ranges"), list) else []
        entries = file_ranges or [{"path": rel} for rel in bundle.get("paths") or []]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            rel = str(entry.get("path") or "")
            path = repo_dir / str(rel)
            start_line = max(1, int(entry.get("start_line") or 1))
            end_line = max(start_line, int(entry.get("end_line") or 0)) if entry.get("end_line") else 0
            start_char = max(0, int(entry.get("start_char") or 0))
            end_char = max(start_char, int(entry.get("end_char") or 0)) if entry.get("end_char") is not None else 0
            range_label = f":{start_line}-{end_line}" if end_line else ""
            if entry.get("start_char") is not None and entry.get("end_char") is not None:
                range_label += f" chars {start_char}-{end_char}"
            lines.append(f"### {rel}{range_label}")
            lines.append("")
            if not path.is_file():
                lines.append("```text")
                lines.append("<missing>")
                lines.append("```")
                lines.append("")
                continue
            suffix = path.suffix.lstrip(".") or "text"
            lines.append(f"```{suffix}")
            source_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            selected_lines = source_lines[start_line - 1 : end_line or None]
            if entry.get("start_char") is not None and entry.get("end_char") is not None and selected_lines:
                selected_lines = [selected_lines[0][start_char:end_char]]
            for index, source_line in enumerate(selected_lines, start=start_line):
                lines.append(f"{index} | {source_line}")
            lines.append("```")
            lines.append("")
        (bundle_dir / f"{bundle_id}.md").write_text("\n".join(lines), encoding="utf-8")



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
        if not isinstance(payload.get("findings"), list):
            errors.append({"file": path.name, "error": "findings must be a list"})
            continue
        normalized_findings: list[dict[str, Any]] = []
        finding_error = ""
        for index, raw_finding in enumerate(payload["findings"]):
            if not isinstance(raw_finding, dict):
                finding_error = f"findings[{index}] must be an object"
                break
            locations = agent_report_locations(raw_finding)
            if not locations:
                finding_error = f"findings[{index}].locations is missing or invalid"
                break
            finding = dict(raw_finding)
            finding["locations"] = locations
            for alias in (
                "location",
                "affected_locations",
                "affectedLocations",
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
                file_path = repo_dir / rel
                line_count = len(file_path.read_text(encoding="utf-8", errors="replace").splitlines()) if file_path.is_file() else 0
                status = "valid" if rel and file_path.is_file() and 1 <= start <= max(line_count, 1) and start <= max(end, start) else "invalid"
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


def prepare_validation_workspace(repo_dir: Path, run_dir: Path) -> dict[str, Any]:
    ensure_intent_directories(run_dir)
    validation_repo = repo_dir.parent / "validation-repo"
    if validation_repo.exists():
        shutil.rmtree(validation_repo)
    copy_tree(repo_dir, validation_repo)
    payload = {
        "schema_version": "validation-workspace/v1",
        "validation_repo_root": str(validation_repo),
        "source_repo_root": str(repo_dir),
        "commit_sha": git_commit(repo_dir),
        "created_at": iso_time(time.time()),
    }
    write_json(run_dir / "intent" / "validation-workspace.json", payload)
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
    if framework in {"vitest", "jest", "node", "npm"} or suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
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
    for key in ("command", "test_command", "testCommand", "run_command", "runCommand"):
        command = _intent_command(generated.get(key))
        if command:
            return command
    if isinstance(source, dict):
        command = _intent_command(
            _intent_source_command_value(source, generated, generated_index, generated_total)
        )
        if command:
            return command
    for key in ("command", "test_command", "testCommand", "run_command", "runCommand"):
        command = _intent_command(target.get(key))
        if command:
            return command
    return _intent_inferred_command(generated, target, validation_repo)


def _intent_generated_execution_records(generated_tests: list[Any]) -> list[dict[str, Any]]:
    records = [item for item in generated_tests if isinstance(item, dict)]
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


def _intent_normalized_execution_command(command: list[str]) -> list[str]:
    if len(command) != 4:
        return command
    executable, module_flag, framework, raw_path = command
    if module_flag != "-m" or framework != "unittest" or not raw_path.lower().endswith(".py"):
        return command
    path = PurePosixPath(raw_path.replace("\\", "/"))
    start_dir = path.parent.as_posix()
    if start_dir in {"", "."}:
        return command
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


def _intent_output_path(run_dir: Path, test_id: str, suffix: str) -> Path:
    safe = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in test_id).strip("._")
    return run_dir / "intent" / "test-output" / f"{safe or 'intent-test'}.{suffix}.log"


def _intent_output_artifact_id(name: str) -> str:
    return f"art_intent_test_output_{safe_artifact_suffix(name, fallback='log')}"


def safe_artifact_suffix(name: str, *, fallback: str = "artifact") -> str:
    return "".join(char if char.isalnum() else "_" for char in name).strip("_") or fallback


def _intent_test_env(validation_repo: Path, *, sandboxed: bool = False) -> dict[str, str]:
    env: dict[str, str] = {}
    passthrough = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
    }
    for key, value in os.environ.items():
        normalized = key.upper()
        if normalized in passthrough or normalized.startswith("LC_"):
            env[key] = value
    sandbox_home = validation_repo / ".intent-test-home"
    sandbox_tmp = sandbox_home / "tmp"
    sandbox_tmp.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "CI": "true",
            "HOME": "/tmp" if sandboxed else str(sandbox_home),
            "USERPROFILE": "/tmp" if sandboxed else str(sandbox_home),
            "TMPDIR": "/tmp" if sandboxed else str(sandbox_tmp),
            "TMP": "/tmp" if sandboxed else str(sandbox_tmp),
            "TEMP": "/tmp" if sandboxed else str(sandbox_tmp),
            "NO_PROXY": "*",
            "PYTHONPATH": "/workspace" if sandboxed else str(validation_repo),
            "PULLWISE_INTENT_TEST": "1",
            "PULLWISE_INTENT_TEST_NETWORK_DISABLED": "1",
        }
    )
    return env


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
    for host_path in ("/usr", "/bin", "/lib", "/lib64", "/opt"):
        if Path(host_path).exists():
            argv.extend(["--ro-bind", host_path, host_path])
    argv.extend(["--", *command])
    return argv, sandbox_cwd, ""


def _intent_sandbox_setup_failed(command: list[str], completed: subprocess.CompletedProcess[str]) -> bool:
    executable = Path(command[0]).name.lower() if command else ""
    if executable not in {"bwrap", "bubblewrap"}:
        return False
    stderr = str(completed.stderr or "").lower()
    markers = (
        "bwrap:",
        "bubblewrap:",
        "operation not permitted",
        "permission denied",
        "creating new namespace",
        "unshare",
        "namespace",
    )
    return completed.returncode != 0 and any(marker in stderr for marker in markers)


def materialize_generated_intent_test_sources(
    run_dir: Path,
    validation_repo: Path | None,
    validation: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, str]:
    generated_tests = source.get("generated_tests") if isinstance(source.get("generated_tests"), list) else []
    if validation_repo is None:
        return {}
    source_root_value = str(validation.get("source_repo_root") or "").strip()
    source_root = Path(source_root_value) if source_root_value else None
    source_generation_root = source_root / ".codex-review" / "generated-tests" if source_root is not None else None
    run_generation_root = run_dir / "intent" / "generated-tests"
    errors: dict[str, str] = {}
    for index, generated in enumerate(generated_tests):
        if not isinstance(generated, dict):
            continue
        test_id = _intent_test_id(generated, f"ITV-{index + 1:03d}")
        raw_path = str(
            generated.get("path")
            or generated.get("artifact_path")
            or generated.get("artifactPath")
            or ""
        ).strip()
        if not raw_path:
            continue
        declared_path = Path(raw_path)
        validation_candidate = declared_path if declared_path.is_absolute() else validation_repo / declared_path
        if path_is_under(validation_candidate, validation_repo) and validation_candidate.is_file():
            continue
        source_candidate = declared_path if declared_path.is_absolute() else (source_root / declared_path if source_root is not None else None)
        source_is_canonical = (
            source_candidate is not None
            and source_generation_root is not None
            and not source_candidate.is_symlink()
            and source_candidate.is_file()
            and path_is_under(source_candidate, source_generation_root)
        )
        if source_is_canonical:
            try:
                relative_path = source_candidate.resolve(strict=True).relative_to(source_root.resolve(strict=True))
            except (OSError, ValueError):
                errors[test_id] = "generated test source escapes the source repository"
                continue
        else:
            run_candidate = declared_path if declared_path.is_absolute() else run_dir / declared_path
            if run_candidate.is_symlink() or not run_candidate.is_file() or not path_is_under(run_candidate, run_generation_root):
                errors[test_id] = "generated test source is missing or outside .codex-review/generated-tests"
                continue
            try:
                relative_path = run_candidate.resolve(strict=True).relative_to(run_dir.resolve(strict=True))
            except (OSError, ValueError):
                errors[test_id] = "generated test source escapes the run intent directory"
                continue
            source_candidate = run_candidate
        destination = validation_repo / relative_path
        if not path_is_under(destination, validation_repo):
            errors[test_id] = "generated test destination escapes the validation workspace"
            continue
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_candidate, destination, follow_symlinks=False)
        except OSError as exc:
            errors[test_id] = f"generated test source could not be materialized: {exc}"
    return errors


def run_intent_tests(run_dir: Path) -> dict[str, Any]:
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
    materialization_errors = materialize_generated_intent_test_sources(
        run_dir,
        validation_repo,
        validation if isinstance(validation, dict) else {},
        source if isinstance(source, dict) else {},
    )
    target_by_id = {
        _intent_test_id(target, f"ITV-{index + 1:03d}"): target
        for index, target in enumerate(targets)
        if isinstance(target, dict)
    }
    generated_records = _intent_generated_execution_records(generated_tests)
    execution_records: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    if generated_records:
        for index, generated in enumerate(generated_records):
            test_id = _intent_test_id(generated, f"ITV-{index + 1:03d}")
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
    total_deadline = time.monotonic() + max(0, int((config if isinstance(config, dict) else {}).get("max_total_test_run_seconds") or 900))
    raw_results = []
    limited_records = execution_records[:max_tests]
    for generated_index, (test_id, generated, target) in enumerate(limited_records):
        command = _intent_generated_command(
            generated,
            target,
            validation_repo,
            source=source,
            generated_index=generated_index,
            generated_total=len(execution_records),
        )
        command = _intent_normalized_execution_command(command)
        base_result = {"schema_version": "project-test-run/v1", "test_id": test_id}
        related_ids = _intent_related_test_ids(generated)
        if related_ids:
            base_result["target_test_ids"] = related_ids
        if not validation_repo:
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": "validation workspace was not prepared"})
            continue
        if materialization_errors.get(test_id):
            raw_results.append(
                {
                    **base_result,
                    "status": "skipped",
                    "classification": "test_harness_error",
                    "exit_code": None,
                    "duration_ms": 0,
                    "timed_out": False,
                    "skip_reason": materialization_errors[test_id],
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
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": "no generated test command was produced"})
            continue
        cwd = _intent_test_cwd(validation_repo, generated, target)
        if cwd is None:
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": "generated test cwd escapes validation workspace"})
            continue
        if not cwd.is_dir():
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": "generated test cwd does not exist"})
            continue
        allowed, policy_reason = intent_test_command_policy(command, cwd, validation_repo)
        if not allowed:
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": f"generated test command is not allowed by worker policy: {policy_reason}"})
            continue
        runnable, runnable_reason = intent_command_is_runnable_for_repo(command, cwd, validation_repo, profile)
        if not runnable:
            raw_results.append({**base_result, "status": "skipped", "classification": _intent_preflight_classification(runnable_reason), "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": runnable_reason.split(": ", 1)[-1]})
            continue
        remaining_total = total_deadline - time.monotonic()
        if remaining_total <= 0:
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": "intent test total timeout budget exhausted"})
            continue
        timeout_seconds = min(_intent_test_timeout(config if isinstance(config, dict) else {}, generated, target), max(1, int(remaining_total)))
        started = time.monotonic()
        stdout_path = _intent_output_path(run_dir, test_id, "stdout")
        stderr_path = _intent_output_path(run_dir, test_id, "stderr")
        sandbox_command, sandbox_cwd, sandbox_skip_reason = _intent_test_sandbox_command(command, cwd, validation_repo)
        if sandbox_skip_reason:
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": sandbox_skip_reason})
            continue
        try:
            completed = subprocess.run(
                sandbox_command,
                cwd=str(cwd),
                env=_intent_test_env(validation_repo, sandboxed=sys.platform.startswith("linux")),
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout_path.write_text(completed.stdout or "", encoding="utf-8")
            stderr_path.write_text(completed.stderr or "", encoding="utf-8")
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
            raw_results.append(
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
                }
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout_path.write_text(str(exc.stdout or ""), encoding="utf-8")
            stderr_path.write_text(str(exc.stderr or ""), encoding="utf-8")
            raw_results.append(
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
                }
            )
        except OSError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            stdout_path.write_text("", encoding="utf-8")
            stderr_path.write_text(str(exc), encoding="utf-8")
            raw_results.append(
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
                }
            )
    return {"schema_version": "intent-test-run-results/v1", "run_id": run_dir.name, "test_runs": raw_results}


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
    if not isinstance(generated, list):
        generated = tests if tests else payload.get("created_files") or payload.get("createdFiles") or []
    generated_items = generated if isinstance(generated, list) else []
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
    for payload in (
        config,
        read_json(run_dir / "intent" / "intent-test-plan.json", {}),
        read_json(run_dir / "intent" / "intent-test-source.json", {}),
    ):
        if _intent_skip_reason_from_payload(payload):
            return ""
    raw_runs = read_json(run_dir / "intent" / "intent-test-results.raw.json", {}).get("test_runs", [])
    if isinstance(raw_runs, list):
        for raw_run in raw_runs:
            if _intent_skip_reason_from_payload(raw_run):
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
    current = [copy.deepcopy(item) for item in artifact_manifest_items(read_json(artifact_dir / "artifact-manifest.json", {}))]
    uploaded = uploaded_artifact_manifest_items(artifact_dir)
    if not uploaded:
        return current
    uploaded_by_id = {str(item.get("artifact_id") or "").strip(): item for item in uploaded if str(item.get("artifact_id") or "").strip()}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in current:
        artifact_id = str(item.get("artifact_id") or "").strip()
        if artifact_id and artifact_id in uploaded_by_id:
            merged.append(copy.deepcopy(uploaded_by_id[artifact_id]))
            seen.add(artifact_id)
        else:
            merged.append(item)
    for item in uploaded:
        artifact_id = str(item.get("artifact_id") or "").strip()
        if artifact_id and artifact_id not in seen:
            merged.append(copy.deepcopy(item))
            seen.add(artifact_id)
    return merged


def reconcile_envelope_artifact_manifest_with_uploads(envelope: dict[str, Any], artifact_dir: Path) -> None:
    manifest = result_artifact_manifest_items(artifact_dir)
    if manifest:
        envelope["artifact_manifest"] = manifest


def result_manifest_uploaded_snapshot_mismatches(envelope: dict[str, Any], artifact_dir: Path) -> list[str]:
    uploaded = uploaded_artifact_manifest_items(artifact_dir)
    if not uploaded:
        return []
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
        if not artifact_id or artifact_id not in uploaded_by_id:
            continue
        if item != uploaded_by_id[artifact_id]:
            mismatches.append(artifact_id)
    return mismatches


def validate_result_manifest_matches_uploaded_snapshot(envelope: dict[str, Any], artifact_dir: Path) -> None:
    mismatches = result_manifest_uploaded_snapshot_mismatches(envelope, artifact_dir)
    if mismatches:
        raise RuntimeError("result artifact manifest differs from uploaded artifact snapshot: " + ", ".join(mismatches[:10]))


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
            if not rel or start <= 0 or end < start or not location_path.is_file():
                errors.append(f"finding[{index}] has invalid location")
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
                    matched_validation_entries.add(id(matching_entry))
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
            candidates = [test_id, *_intent_related_test_ids(record)]
            candidates = list(dict.fromkeys(candidate for candidate in candidates if candidate))
            planned_matches = [candidate for candidate in candidates if planned and candidate in planned]
            if planned_matches:
                ids.update(planned_matches)
            elif test_id:
                ids.add(test_id)
        return ids

    executable_generated = _intent_generated_execution_records(generated if isinstance(generated, list) else [])
    planned_ids = logical_ids(plan_targets, "ITP")
    written_ids = logical_ids(executable_generated, "ITV", planned=planned_ids)
    attempted_ids = logical_ids(raw_runs, "ITR", planned=planned_ids)
    run_ids = logical_ids(
        raw_runs,
        "ITR",
        planned=planned_ids,
        include_record=lambda record: str(record.get("status") or "").strip().lower() != "skipped"
        and bool(str(record.get("command") or record.get("sandbox_command") or "").strip()),
    )
    analyzed_ids = logical_ids(analyzed_runs, "ITA", planned=planned_ids)
    assertion_ids = logical_ids(
        analyzed_runs,
        "ITA",
        planned=planned_ids,
        include_record=lambda record: str(record.get("classification") or "").strip()
        in {"confirmed_bug", "plausible_bug", "test_oracle_wrong", "passed_no_bug_reproduced"},
    )
    all_ids = planned_ids or (written_ids | attempted_ids | analyzed_ids)
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
        "01_risk_router.md": "You are the Risk Router. Classify files and directories into P0/P1/P2/P3/SKIP. Return JSON only using risk-routing/v1.\n",
        "02_bundle_planner.md": "You may adjust mechanical bundle boundaries without changing the review policy. Return JSON only using bundle-plan/v1.\n",
        "reviewers/security.md": "You are the Security Reviewer. Report only concrete security issues with realistic abuse paths. Demonstrate an end-to-end attacker-controlled path, account for producer-side validation and containment, and classify unproven reachability as defense-in-depth rather than high/critical. Return JSON only using codex-reviewer-output/v1.\n",
        "reviewers/correctness.md": "You are the Correctness Reviewer. Focus on incorrect behavior, state, boundaries, idempotency, and concurrency. Return JSON only using codex-reviewer-output/v1.\n",
        "reviewers/test_gap.md": "You are the Test Gap Reviewer. Report missing or weak tests only for important P0/P1 behavior. Return JSON only using codex-reviewer-output/v1.\n",
        "reviewers/correctness_lite.md": "You are the Correctness Lite Reviewer. Only report clear bugs or user-visible behavior problems. Return JSON only using codex-reviewer-output/v1.\n",
        "03_clusterer.md": "You are the Finding Clusterer and Vote Aggregator. Merge duplicates and suppress vague findings. Merge test-gap evidence into the underlying defect when contract, sink, and fix match. Do not create new findings. Return JSON only.\n",
        "intent/04_intent_miner.md": "You are the Intent Miner. Extract behavioral contracts from docs, API specs, types, tests, route definitions, and error messages. Do not infer intent only from implementation code. Return JSON only using intent-map/v1.\n",
        "intent/05_intent_test_planner.md": "You are the Intent Test Planner. Select only high-value P0/P1 candidates for temporary tests. Return JSON only using intent-test-plan/v1.\n",
        "intent/06_intent_test_writer.md": "You are the Intent Test Writer. Write temporary tests only in the disposable validation workspace or .codex-review/generated-tests/**. Every generated test must include target_test_ids linking it to the intent-test-plan target(s) it implements. Do not modify the main repo workspace. Return JSON describing created test files.\n",
        "intent/07_intent_test_failure_analyzer.md": "You are the Test Failure Analyzer. A failing test is not automatically a bug. Classify each result using intent-test-result/v1. Return JSON only.\n",
        "08_validator.md": "You are the Validation Reviewer. Try to disprove each candidate finding using evidence, location verification, related code, existing tests, and intent test results. An unknown cross-service producer is unresolved controllability, not proof of attacker control. dependency_missing is absence of dynamic evidence, not disproof; static source and contract evidence can still support plausible. Return JSON only.\n",
        "09_reporter.md": "You are the Final Reporter. Include only confirmed/plausible actionable findings in main findings; weak findings go to the top-level appendix_findings list. Do not inherit reviewer severity without calibrating reachability, control, impact, and containment. Return JSON only.\n",
    }
    discipline = (
        "\nRequired discipline:\n"
        "- Do not modify application source files.\n"
        "- Do not install dependencies.\n"
        "- Do not call external review/scanning services.\n"
        "- Do not include Markdown prose outside JSON for schema-bound phases.\n"
    )
    return "# Pullwise Codex Full Repository Review Phase\n\n" + templates.get(name, "Follow .codex-review/AGENTS.review.md. Return the requested artifact.\n") + discipline


def fallback_semantic_artifact(run_dir: Path, job: dict[str, Any], phase: str) -> None:
    ensure_intent_directories(run_dir)
    if phase == "bootstrap_helper_scripts":
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
    elif phase == "repo_map" and not (run_dir / "repo-map.json").exists():
        write_json(run_dir / "repo-map.json", {"schema_version": "repo-map/v1", "areas": [], "notes": "Codex repo_map phase did not materialize an artifact."})
    elif phase == "risk_routing" and not (run_dir / "risk-routing.json").exists():
        write_json(run_dir / "risk-routing.json", {"schema_version": "risk-routing/v1", "routes": [], "default_depth": "P1"})
        if not (run_dir / "coverage.json").exists():
            inv = read_json(run_dir / "inventory.json", {})
            summary = inv.get("summary") if isinstance(inv.get("summary"), dict) else {}
            write_json(
                run_dir / "coverage.json",
                {
                    "schema_version": "coverage/v1",
                    "source_like_files_total": int(summary.get("source_like_files") or 0),
                    "deep_reviewed_files": 0,
                    "standard_reviewed_files": 0,
                    "light_reviewed_files": 0,
                    "inventory_only_files": 0,
                    "skipped_files": 0,
                },
            )
    elif phase == "reviewer_fanout":
        (run_dir / "raw-reviewers").mkdir(parents=True, exist_ok=True)
    elif phase == "clustering_and_voting" and not (run_dir / "clusters.json").exists():
        write_json(run_dir / "clusters.json", {"schema_version": "cluster-output/v1", "clusters": [], "candidate_findings": []})
        write_json(run_dir / "validation-input.json", {"schema_version": "validation-input/v1", "candidates": []})
    elif phase == "intent_mining":
        intent_map_path = run_dir / "intent" / "intent-map.json"
        if intent_map_path.exists():
            repair_intent_map_artifact(intent_map_path)
        else:
            write_json(intent_map_path, {"schema_version": "intent-map/v1", "bundle_id": "all", "behavioral_contracts": [], "unknowns": ["No high-value intent targets were materialized."]})
    elif phase == "intent_test_planning":
        intent_plan_path = run_dir / "intent" / "intent-test-plan.json"
        if intent_plan_path.exists():
            repair_intent_test_plan_artifact(intent_plan_path, run_dir)
        else:
            write_json(intent_plan_path, {"schema_version": "intent-test-plan/v1", "test_targets": []})
    elif phase == "intent_test_writing":
        intent_source_path = run_dir / "intent" / "intent-test-source.json"
        if intent_source_path.exists():
            repair_intent_test_source_artifact(intent_source_path, run_dir)
        else:
            write_json(intent_source_path, {"schema_version": "intent-test-source/v1", "generated_tests": []})
    elif phase == "intent_test_failure_analysis":
        intent_results_path = run_dir / "intent" / "intent-test-results.json"
        if intent_results_path.exists():
            repair_intent_test_results_artifact(intent_results_path, run_dir)
        else:
            write_json(intent_results_path, fallback_intent_test_results(run_dir))
    elif phase == "validator_disproof":
        validation_path = run_dir / "validated-findings.json"
        if not validation_path.exists():
            write_json(
                validation_path,
                {
                    "schema_version": "validation-output/v1",
                    "validated_findings": [],
                    "weak_findings": [],
                    "disproven_findings": [],
                },
            )
        repair_validation_output_artifact(validation_path)
    elif phase == "final_report_json":
        if not (run_dir / "report.agent.json").exists():
            write_json(run_dir / "report.agent.json", agent_report_payload(run_dir, job))
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
    path = str(value.get("path") or value.get("file") or value.get("filename") or "").strip()
    start = _qa_int(
        value.get("start_line")
        or value.get("line_start")
        or value.get("startLine")
        or value.get("lineStart")
        or value.get("line")
        or value.get("line_number")
        or value.get("lineNumber")
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
        "line",
        "line_start",
        "line_end",
        "lineStart",
        "lineEnd",
        "startLine",
        "endLine",
        "line_number",
        "lineNumber",
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
            "path": finding.get("path") or finding.get("file") or finding.get("filename"),
            "start_line": (
                finding.get("start_line")
                or finding.get("line_start")
                or finding.get("startLine")
                or finding.get("lineStart")
                or finding.get("line")
                or finding.get("line_number")
                or finding.get("lineNumber")
            ),
            "end_line": (
                finding.get("end_line")
                or finding.get("line_end")
                or finding.get("endLine")
                or finding.get("lineEnd")
                or finding.get("line")
            ),
            "line_range": finding.get("line_range") or finding.get("lineRange") or finding.get("range") or finding.get("lines"),
        }
    )


def agent_report_locations(finding: dict[str, Any]) -> list[dict[str, Any]]:
    raw_locations = finding.get("locations")
    if not isinstance(raw_locations, list):
        raw_locations = []
    if not raw_locations and isinstance(finding.get("location"), dict):
        raw_locations = [finding["location"]]
    if not raw_locations:
        for key in ("affected_locations", "affectedLocations"):
            if isinstance(finding.get(key), list):
                raw_locations = finding[key]
                break
    if not raw_locations and isinstance(finding.get("line_evidence"), dict):
        raw_locations = [
            {
                **finding["line_evidence"],
                "path": finding.get("path") or finding.get("file") or finding.get("filename"),
            }
        ]
    if not raw_locations and isinstance(finding.get("lineEvidence"), dict):
        raw_locations = [
            {
                **finding["lineEvidence"],
                "path": finding.get("path") or finding.get("file") or finding.get("filename"),
            }
        ]
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
    if "evidence" not in normalized:
        normalized["evidence"] = []
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
    raw = read_json(path, {}) if path.exists() else {}
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
    for raw_finding in raw_findings:
        finding = normalized_agent_report_finding(raw_finding)
        if finding is None:
            continue
        validation_entry = matching_validation_entry(finding, accepted_validation)
        if validation_entry is not None:
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
    lines = [
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
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content, encoding="utf-8")
        shutil.copy2(src, artifact_dir / name)
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
    shutil.copy2(run_dir / "qa.json", artifact_dir / "qa.json")
    error_report = {
        "status": status,
        "error": error,
        "created_at": iso_time(time.time()),
    }
    write_json(run_dir / "error-report.json", error_report)
    shutil.copy2(run_dir / "error-report.json", artifact_dir / "error-report.json")
    terminal_report_path = run_dir / "report.agent.json"
    include_terminal_report = False
    if terminal_report_path.exists():
        report = read_json(terminal_report_path, {})
        if isinstance(report, dict):
            summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
            summary = dict(summary)
            summary.setdefault("overall_risk", highest_finding_risk(report.get("findings") if isinstance(report.get("findings"), list) else []))
            summary["result_status"] = "incomplete"
            report["summary"] = summary
            write_json(terminal_report_path, report)
            shutil.copy2(terminal_report_path, artifact_dir / "report.agent.json")
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



def _debug_bundle_files(directory: Path, prefix: str) -> list[tuple[Path, str]]:
    if not directory.is_dir():
        return []
    files: list[tuple[Path, str]] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name == DEBUG_BUNDLE_NAME:
            continue
        try:
            rel = path.relative_to(directory).as_posix()
        except ValueError:
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

    def reviewer_finding_count(paths: list[Path]) -> tuple[int, int]:
        findings = 0
        empty_outputs = 0
        for path in paths:
            payload = read_json(path, {})
            output_findings = _diagnostic_list(payload, "findings")
            findings += len(output_findings)
            if not output_findings:
                empty_outputs += 1
        return findings, empty_outputs

    raw_findings, empty_raw_outputs = reviewer_finding_count(raw_files)
    verified_findings, _empty_verified_outputs = reviewer_finding_count(verified_files)
    planned_assignments = _planned_reviewer_assignments(run_dir)
    reviewer_execution = read_json(run_dir / "reviewer-execution.json", {})
    if not isinstance(reviewer_execution, dict):
        reviewer_execution = {}

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
    if raw_files and raw_findings == 0:
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
            "empty_raw_outputs": empty_raw_outputs,
            "raw_findings": raw_findings,
            "verified_outputs": len(verified_files),
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
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("", encoding="utf-8")
        shutil.copy2(src, artifact_dir / name)
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
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content, encoding="utf-8")
    for name in (*required_files, *optional_defaults.keys()):
        src = run_dir / name
        shutil.copy2(src, artifact_dir / name)
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
        ("clusters.json", "cluster_result", "application/json", "cluster-output"),
        ("validated-findings.json", "validation_result", "application/json", "validation-output"),
        ("intent/intent-map.json", "intent_map", "application/json", "intent-map"),
        ("intent/intent-test-plan.json", "intent_test_plan", "application/json", "intent-test-plan"),
        ("intent/intent-test-source.json", "intent_test_source", "application/json", "intent-test-source"),
        ("intent/intent-test-results.json", "intent_test_result", "application/json", "intent-test-result"),
        ("intent/intent-test-results.raw.json", "intent_test_output", "application/json", "project-test-run"),
    )
    for rel, kind, media_type, schema_id in optional_artifacts:
        src = run_dir / rel
        if not src.exists():
            continue
        dest = artifact_dir / src.name
        shutil.copy2(src, dest)
        manifest.append(artifact_item(dest, kind, media_type, schema_id, False))
    for source_dir, name_prefix, artifact_prefix, kind in (
        (run_dir / "raw-reviewers", "raw-reviewer", "art_raw_reviewer_output", "raw_reviewer_output"),
        (run_dir / "verified-reviewers", "verified-reviewer", "art_verified_reviewer_output", "verified_reviewer_output"),
    ):
        for src in sorted(source_dir.glob("*.json")) if source_dir.is_dir() else []:
            dest = artifact_dir / f"{name_prefix}-{src.name}"
            shutil.copy2(src, dest)
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
        if not src.is_file():
            continue
        dest = artifact_dir / f"intent-test-output-{src.name}"
        shutil.copy2(src, dest)
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
        if any(marker in lowered for marker in CODEX_QUOTA_ERROR_MARKERS):
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
) -> None:
    try:
        source_mode = source.stat(follow_symlinks=False).st_mode
    except OSError as exc:
        raise RuntimeError(f"repository source is not readable: {source}") from exc
    if not stat.S_ISDIR(source_mode):
        raise RuntimeError(f"repository source must be a real directory: {source}")

    raise_repository_limit_if_exceeded(
        repository_scan_stats(source, context="copying checkout", deadline_monotonic=deadline_monotonic),
        max_files=max_files,
        max_bytes=max_bytes,
        context="copying checkout",
    )

    for root, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
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
            if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
                raise RuntimeError("repository scan deadline exceeded while copying checkout")
            path = root_path / filename
            if not _is_regular_file_no_follow(path):
                continue
            rel = path.relative_to(source)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)


def repository_file_count(repo_dir: Path) -> int:
    files_seen = 0
    for root, dirnames, filenames in os.walk(repo_dir, topdown=True, followlinks=False):
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


def git_command_timeout(deadline_monotonic: float | None) -> int | None:
    if deadline_monotonic is None:
        return None
    remaining = int(deadline_monotonic - time.monotonic())
    return max(1, remaining)


def run_git(args: list[str], *, env: dict[str, str], deadline_monotonic: float | None = None) -> None:
    try:
        subprocess.run(
            args,
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            timeout=git_command_timeout(deadline_monotonic),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("repository scan deadline exceeded while cloning checkout") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
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


def clone_repository_checkout(job: dict[str, Any], repo_dir: Path, *, deadline_monotonic: float | None = None) -> None:
    clone_url = job_clone_url(job)
    branch = str(job.get("branch") or "main").strip() or "main"
    commit = str(job.get("commit") or "").strip()
    commit = "" if commit.lower() == "pending" else commit
    token = job_clone_token(job)
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    askpass_path: Path | None = None
    if token:
        askpass_path = write_git_askpass(repo_dir.parent)
        env["GIT_ASKPASS"] = str(askpass_path)
        env["PULLWISE_GIT_TOKEN"] = token
    try:
        run_git(["git", "init", str(repo_dir)], env=env, deadline_monotonic=deadline_monotonic)
        run_git(["git", "-C", str(repo_dir), "remote", "add", "origin", clone_url], env=env, deadline_monotonic=deadline_monotonic)
        ref = commit or branch
        run_git(["git", "-C", str(repo_dir), "fetch", "--depth", "1", "--no-tags", "origin", ref], env=env, deadline_monotonic=deadline_monotonic)
        run_git(["git", "-C", str(repo_dir), "checkout", "--detach", "FETCH_HEAD"], env=env, deadline_monotonic=deadline_monotonic)
        run_git(["git", "-C", str(repo_dir), "remote", "remove", "origin"], env=env, deadline_monotonic=deadline_monotonic)
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
        return json.loads(path.read_text(encoding="utf-8"))
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
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


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


def upload_log_artifacts_best_effort(client: Any, job_id: str, attempt_id: str, run_dir: Path, artifact_dir: Path) -> str:
    try:
        upload_log_artifacts(client, job_id, attempt_id, run_dir, artifact_dir)
    except Exception as exc:
        append_jsonl(run_dir / "worker.log.jsonl", {"event": "final_log_artifact_upload_failed", "error": str(exc), "time": iso_time(time.time())})
        return str(exc)
    return ""


def upload_log_artifacts(client: Any, job_id: str, attempt_id: str, run_dir: Path, artifact_dir: Path) -> None:
    manifest_payload = read_json(artifact_dir / "artifact-manifest.json", {})
    if not isinstance(manifest_payload, dict):
        raise RuntimeError("artifact manifest must be an object before final log upload")
    refresh_log_artifacts(run_dir, artifact_dir, manifest_payload, status="completed")
    manifest = artifact_manifest_items(manifest_payload)
    if not manifest:
        raise RuntimeError("artifact manifest must contain artifact items before final log upload")
    uploaded = 0
    for item in manifest:
        name = str(item.get("name") or "").strip()
        if name not in FINAL_REFRESH_ARTIFACT_NAMES:
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        if not artifact_id:
            raise RuntimeError(f"final refresh artifact manifest entry requires artifact_id: {name}")
        path = artifact_dir / name
        try:
            path.resolve(strict=False).relative_to(artifact_dir.resolve(strict=False))
        except ValueError as exc:
            raise RuntimeError(f"final refresh artifact path escapes artifact directory before upload: {name}") from exc
        if not path.is_file():
            raise RuntimeError(f"final refresh artifact listed in manifest is missing before upload: {name}")
        data = path.read_bytes()
        if str(item.get("sha256") or "").lower() != hashlib.sha256(data).hexdigest():
            raise RuntimeError(f"final refresh artifact sha256 mismatch before upload: {name}")
        if int(item.get("size_bytes") if item.get("size_bytes") is not None else -1) != len(data):
            raise RuntimeError(f"final refresh artifact size mismatch before upload: {name}")
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
        uploaded += 1
    if uploaded == 0:
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
        path = artifact_dir / name
        try:
            path.resolve(strict=False).relative_to(artifact_dir.resolve(strict=False))
        except ValueError as exc:
            raise RuntimeError(f"artifact path escapes artifact directory before upload: {name}") from exc
        if not path.is_file():
            raise RuntimeError(f"artifact listed in manifest is missing before upload: {name}")
        uploadable.append((item, path))
    uploadable.sort(key=lambda pair: 1 if pair[0].get("artifact_id") == DEBUG_BUNDLE_ARTIFACT_ID else 0)
    total = len(uploadable)
    uploaded_manifest_items: list[dict[str, Any]] = []
    optional_upload_errors: list[str] = []
    for uploaded, (item, path) in enumerate(uploadable, start=1):
        artifact_id = str(item.get("artifact_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if source_run_dir is not None and artifact_id == DEBUG_BUNDLE_ARTIFACT_ID:
            for log_name in LOG_ARTIFACT_NAMES:
                src = source_run_dir / log_name
                if not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    src.write_text("", encoding="utf-8")
                shutil.copy2(src, artifact_dir / log_name)
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
        write_uploaded_artifact_manifest(artifact_dir, manifest_payload, uploaded_manifest_items, source_run_dir=source_run_dir)
        if progress_callback is not None:
            progress_callback(uploaded, total, item)
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
