from __future__ import annotations

import base64
import hashlib
import json
import os
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
from pathlib import Path
from typing import Any, Callable

from ._main_part_01_bootstrap import worker_machine_metrics_payload

try:
    import fcntl
except ImportError:  # pragma: no cover - runtime is Linux only; import stays testable elsewhere.
    fcntl = None

PROTOCOL_VERSION = "review-worker-protocol/v1"
WORKER_VERSION = "0.1.0"
TERMINAL_STATES = {"completed", "failed", "cancelled", "partial_completed"}
ACTIVE_HEARTBEAT_STATUSES = {"busy", "leased", "cancelling", "finishing", "failure_handling"}
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
    "RateLimitReached": "CODEX_QUOTA_EXHAUSTED",
    "rate_limit_reached": "CODEX_QUOTA_EXHAUSTED",
    "workspace_owner_credits_depleted": "CODEX_QUOTA_EXHAUSTED",
    "workspace_member_credits_depleted": "CODEX_QUOTA_EXHAUSTED",
    "workspace_owner_usage_limit_reached": "CODEX_QUOTA_EXHAUSTED",
    "workspace_member_usage_limit_reached": "CODEX_QUOTA_EXHAUSTED",
    "ContextWindowExceeded": "CODEX_CONTEXT_WINDOW_EXCEEDED",
    "Unauthorized": "CODEX_UNAUTHORIZED",
    "SandboxError": "CODEX_SANDBOX_ERROR",
    "HttpConnectionFailed": "CODEX_UPSTREAM_CONNECTION_FAILED",
    "InternalServerError": "CODEX_INTERNAL_SERVER_ERROR",
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
DEBUG_BUNDLE_NAME = "debug-bundle.zip"
DEBUG_BUNDLE_ARTIFACT_ID = "art_debug_bundle"
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
        command = str(worker_root / ".local" / "bin" / "codex")
    command_path = Path(command).expanduser()
    if not command_path.is_absolute():
        raise RuntimeError(f"Codex command must be an absolute path inside worker_root: {command}")
    resolved_command = command_path.resolve(strict=False)
    allowed_roots = [worker_root.resolve(strict=False), service_home.resolve(strict=False)]
    for root in allowed_roots:
        try:
            resolved_command.relative_to(root)
            return str(command_path)
        except ValueError:
            continue
    raise RuntimeError(f"Codex command must be inside worker_root {worker_root}: {command}")


class JsonRpcAppServer:
    def __init__(
        self,
        command: str,
        env: dict[str, str],
        cwd: Path,
        events_path: Path,
        rate_limit_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.command = command or "codex"
        self.env = env
        self.cwd = cwd
        self.events_path = events_path
        self.process: subprocess.Popen[str] | None = None
        self._next_id = 1
        self._pending: dict[int, dict[str, Any]] = {}
        self._turns: dict[str, threading.Event] = {}
        self._turn_errors: dict[str, str] = {}
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.rate_limit_callback = rate_limit_callback
        self.thread_sandbox_mode = "workspace-write"
        self.read_only_sandbox_policy_type = "readOnly"
        self.workspace_write_sandbox_policy_type = "workspaceWrite"

    def start(self) -> None:
        if self.is_running():
            return
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [self.command, "app-server", "--listen", "stdio://"],
            cwd=str(self.cwd),
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_repo_review_worker",
                    "title": "Codex Repo Review Worker",
                    "version": WORKER_VERSION,
                },
                "capabilities": {"experimentalApi": False},
            },
        )
        self.notify("initialized", {})

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def set_events_path(self, events_path: Path) -> None:
        self.events_path = events_path
        self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def start_thread(self, repo_dir: Path, model: str) -> str:
        last_error: RuntimeError | None = None
        for sandbox_mode in self._thread_sandbox_mode_candidates():
            try:
                result = self.request(
                    "thread/start",
                    {
                        "cwd": str(repo_dir),
                        "approvalPolicy": "never",
                        "sandbox": sandbox_mode,
                        "serviceName": "codex_repo_review_worker",
                        "model": model or None,
                    },
                )
            except RuntimeError as exc:
                if not codex_enum_variant_error(exc):
                    raise
                last_error = exc
                continue
            self.thread_sandbox_mode = sandbox_mode
            return str(((result.get("thread") or {}).get("id")) or result.get("threadId") or "")
        if last_error is not None:
            raise last_error
        return ""

    def _thread_sandbox_mode_candidates(self) -> tuple[str, ...]:
        preferred = self.thread_sandbox_mode or "workspace-write"
        fallback = "workspaceWrite" if preferred == "workspace-write" else "workspace-write"
        return (preferred, fallback)

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
        last_error: RuntimeError | None = None
        result: dict[str, Any] = {}
        for sandbox in self._sandbox_policy_candidates(repo_dir, read_only=read_only):
            try:
                result = self.request(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                        "cwd": str(repo_dir),
                        "approvalPolicy": "never",
                        "sandboxPolicy": sandbox,
                        "effort": effort,
                        "summary": "concise",
                    },
                )
            except RuntimeError as exc:
                if not codex_enum_variant_error(exc):
                    raise
                last_error = exc
                continue
            self._remember_sandbox_policy_type(sandbox)
            break
        else:
            if last_error is not None:
                raise last_error
        turn_id = str(((result.get("turn") or {}).get("id")) or result.get("turnId") or "")
        if not turn_id:
            return
        event = threading.Event()
        with self._lock:
            self._turns[turn_id] = event
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.interrupt(thread_id, turn_id)
                raise TimeoutError(f"codex turn timed out: {turn_id}")
            if event.wait(min(0.5, remaining)):
                break
            if cancel_requested is not None and cancel_requested():
                self.interrupt(thread_id, turn_id)
                raise JobCancelled("cancel requested")
        error = self._turn_errors.get(turn_id)
        if error:
            raise RuntimeError(error)

    def _sandbox_policy_candidates(self, repo_dir: Path, *, read_only: bool) -> tuple[dict[str, Any], ...]:
        preferred = self.read_only_sandbox_policy_type if read_only else self.workspace_write_sandbox_policy_type
        if read_only:
            fallback = "read-only" if preferred == "readOnly" else "readOnly"
        else:
            fallback = "workspace-write" if preferred == "workspaceWrite" else "workspaceWrite"
        return (
            self._sandbox_policy(repo_dir, read_only=read_only, policy_type=preferred),
            self._sandbox_policy(repo_dir, read_only=read_only, policy_type=fallback),
        )

    def _sandbox_policy(self, repo_dir: Path, *, read_only: bool, policy_type: str) -> dict[str, Any]:
        if read_only:
            return {"type": policy_type, "networkAccess": False}
        writable_roots = [str(repo_dir / ".codex-review")]
        validation_repo = repo_dir.parent / "validation-repo"
        writable_roots.append(str(validation_repo))
        return {
            "type": policy_type,
            "networkAccess": False,
            "writableRoots": writable_roots,
        }

    def _remember_sandbox_policy_type(self, sandbox: dict[str, Any]) -> None:
        policy_type = str(sandbox.get("type") or "")
        if policy_type in {"readOnly", "read-only"}:
            self.read_only_sandbox_policy_type = policy_type
        elif policy_type in {"workspaceWrite", "workspace-write"}:
            self.workspace_write_sandbox_policy_type = policy_type

    def interrupt(self, thread_id: str, turn_id: str) -> None:
        try:
            self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout_seconds=5)
        except Exception:
            pass

    def request(self, method: str, params: dict[str, Any] | None = None, timeout_seconds: int = 30) -> dict[str, Any]:
        request_id = self._next_request_id()
        event = threading.Event()
        with self._lock:
            self._pending[request_id] = {"event": event, "response": None}
        self._send({"method": method, "id": request_id, "params": params or {}})
        if not event.wait(timeout_seconds):
            raise TimeoutError(f"codex app-server request timed out: {method}")
        response = self._pending.pop(request_id, {}).get("response") or {}
        if isinstance(response.get("error"), dict):
            raise RuntimeError(str(response["error"].get("message") or response["error"]))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _reader(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            append_jsonl(self.events_path, message)
            if "id" in message and ("result" in message or "error" in message):
                request_id = int(message["id"])
                with self._lock:
                    pending = self._pending.get(request_id)
                if pending is not None:
                    pending["response"] = message
                    pending["event"].set()
                continue
            if message.get("method") == "turn/completed":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                turn_id = str(turn.get("id") or params.get("turnId") or "")
                error = turn.get("error") or turn.get("lastError")
                if turn_id:
                    if error:
                        self._turn_errors[turn_id] = json.dumps(error, ensure_ascii=False) if isinstance(error, (dict, list)) else str(error)
                    with self._lock:
                        event = self._turns.get(turn_id)
                    if event is not None:
                        event.set()
            elif message.get("method") == "account/rateLimits/updated":
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                if self.rate_limit_callback is not None:
                    self.rate_limit_callback(params)
            elif "id" in message and "method" in message:
                method = str(message.get("method") or "")
                if method in CODEX_APPROVAL_RESPONSE_METHODS:
                    self._send({"id": message.get("id"), "result": approval_response_for_request(message, self.cwd)})
                else:
                    self._send({"id": message.get("id"), "error": {"code": -32601, "message": "unsupported server request"}})

    def _next_request_id(self) -> int:
        with self._lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _send(self, message: dict[str, Any]) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("codex app-server is not running")
        with self._send_lock:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()


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


def codex_rate_limit_snapshot(response: dict[str, Any]) -> dict[str, Any]:
    by_limit = response.get("rateLimitsByLimitId") if isinstance(response.get("rateLimitsByLimitId"), dict) else None
    if by_limit:
        for key, value in by_limit.items():
            if quota_text(key).lower() == "codex" and isinstance(value, dict):
                return value
        for key, value in by_limit.items():
            if "codex" in quota_text(key).lower() and isinstance(value, dict):
                return value
    rate_limits = response.get("rateLimits")
    return rate_limits if isinstance(rate_limits, dict) else {}


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
) -> dict[str, Any]:
    snapshot = codex_rate_limit_snapshot(response)
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
        app_server_provider: Callable[[], JsonRpcAppServer] | None = None,
    ) -> None:
        self.config = config
        self.isolation = isolation
        self.app_server_provider = app_server_provider
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
        server: JsonRpcAppServer | None = None
        close_server = False
        try:
            if self.app_server_provider is not None:
                server = self.app_server_provider()
            else:
                self.isolation.runtime.mkdir(parents=True, exist_ok=True)
                self.isolation.logs.mkdir(parents=True, exist_ok=True)
                server = JsonRpcAppServer(
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

READ_ONLY_COMMANDS = {"git", "find", "wc", "cat", "sed", "awk", "grep", "rg"}
PROJECT_TEST_COMMANDS = {"npm", "pnpm", "yarn", "pytest", "python", "python3", "go", "cargo", "mvn", "gradle", "make"}
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
    argv = [str(part) for part in command] if isinstance(command, list) else shlex.split(str(command or ""))
    if not argv:
        return False
    executable = normalized_executable_name(argv[0])
    lowered = {part.lower() for part in argv}
    if lowered.intersection(DENIED_COMMAND_TOKENS):
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
        if path_is_under_codex_review(workspace, argv[1]) and "/tools/" in Path(argv[1]).as_posix():
            return True
    if executable in PROJECT_TEST_COMMANDS and cwd_in_validation:
        lowered_text = " ".join(part.lower() for part in argv)
        if any(token in lowered_text for token in (" install", " add ", " publish", " curl", " wget")):
            return False
        return any(token in lowered for token in {"test", "pytest", "go", "cargo", "mvn", "gradle", "make"}) or executable in {"pytest", "make"}
    return executable in READ_ONLY_COMMANDS


def normalized_executable_name(value: object) -> str:
    name = Path(str(value or "")).name.lower()
    return name[:-4] if name.endswith(".exe") else name


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


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
    if executable in {"python", "python3", "py"}:
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
        self.app_server: JsonRpcAppServer | None = None
        self.quota_monitor = CodexQuotaMonitor(config, self.isolation, self.ensure_app_server)
        self.lock = WorkerLock(self.isolation.worker_root, str(config.worker_id), self.isolation.codex_home)
        self._machine_metrics_payload: dict[str, Any] | None = None
        self._machine_metrics_collected_at = 0.0

    def default_app_server_events_path(self) -> Path:
        return self.isolation.logs / "codex-app-server-events.jsonl"

    def ensure_app_server(self, events_path: Path | None = None) -> JsonRpcAppServer:
        self.isolation.runtime.mkdir(parents=True, exist_ok=True)
        self.isolation.logs.mkdir(parents=True, exist_ok=True)
        target_events_path = events_path or self.default_app_server_events_path()
        if self.app_server is not None and not self.app_server.is_running():
            self.app_server.close()
            self.app_server = None
        if self.app_server is None:
            self.app_server = JsonRpcAppServer(
                scoped_codex_command(self.config),
                self.isolation.env(self.config),
                self.isolation.runtime,
                target_events_path,
                rate_limit_callback=self.quota_monitor.apply_rate_limit_update,
            )
            self.app_server.start()
        else:
            self.app_server.set_events_path(target_events_path)
        return self.app_server

    def close_app_server(self) -> None:
        if self.app_server is None:
            return
        self.app_server.close()
        self.app_server = None

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
            self.recover_pending_submissions()
            while True:
                active = self.state.active_job
                if active is not None and active.state == "finishing":
                    self.retry_pending_submission_for_active(active)
                self.heartbeat()
                if self.state.can_lease():
                    job = self.client.claim()
                    if job:
                        self.run_job(job)
                if once:
                    return
                time.sleep(max(1, int(getattr(self.config, "poll_seconds", 5) or 5)))
        finally:
            self.close_app_server()
            self.lock.release()

    def heartbeat(self) -> dict[str, Any]:
        active = self.state.active_job
        app_server_error = ""
        if active is None or self.app_server is None or not self.app_server.is_running():
            try:
                self.ensure_app_server()
            except Exception as exc:
                app_server_error = quota_text(exc, 500)
        quota = self.quota_monitor.snapshot_if_due(active=active is not None)
        quota_ready = bool((quota or {}).get("ready", True))
        app_server_ready = self.app_server is not None and self.app_server.is_running()
        provider_ready = app_server_ready and quota_ready
        self.state.provider_ready = provider_ready
        readiness_reason = quota_text((quota or {}).get("reason") or (quota or {}).get("status"), 160)
        if app_server_error:
            readiness_reason = f"codex_app_server_unavailable: {app_server_error}"
        elif not app_server_ready:
            readiness_reason = readiness_reason or "codex_app_server_not_running"
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
                "status": "ready" if app_server_ready else "needs_attention",
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
        return response if isinstance(response, dict) else {}

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
                append_jsonl(run_dir / "worker.log.jsonl", {"event": "progress_event_post_failed", "phase": phase, "time": iso_time(time.time())})
        return event

    def request_cancel(self, active: ActiveJob, *, reason: str = "server_cancelled") -> None:
        reason_text = str(reason or "server_cancelled").strip() or "server_cancelled"
        active.cancel_requested = True
        active.cancel_reason = reason_text
        active.state = "cancelling"
        active.message = "Cancellation requested."
        if active.run_dir is not None:
            self.emit_cancel_requested(active, active.run_dir)

    def emit_cancel_requested(self, active: ActiveJob, run_dir: Path) -> None:
        if active.cancel_requested_reported:
            return
        reason = active.cancel_reason or "server_cancelled"
        active.cancel_requested = True
        active.state = "cancelling"
        active.cancel_requested_reported = True
        append_jsonl(run_dir / "worker.log.jsonl", {"event": "run_cancel_requested", "reason": reason, "time": iso_time(time.time())})
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
        active.state = "finishing" if phase in {"upload_artifacts", "submit_result_envelope", "cleanup_active_job"} else "busy"
        append_jsonl(run_dir / "worker.log.jsonl", {"event": "phase_started", "phase": phase, "progress": progress, "time": iso_time(time.time())})
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
            "retryable": failure.get("retryable"),
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
        terminal_state = "failed"
        app_server: JsonRpcAppServer | None = None
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
            events_path = artifact_dir / "codex-events.jsonl"
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
                        app_server = self.ensure_app_server(events_path)
                    elif phase == "initialize_codex_connection":
                        thread_id = app_server.start_thread(repo_dir, model_for_job(job)) if app_server else ""
                        active.thread_id = thread_id
                        run_state = read_json(run_dir / "run-state.json", {})
                        if not isinstance(run_state, dict):
                            run_state = {}
                        run_state.update({"thread_id": thread_id, "active_job": active.heartbeat_payload()})
                        write_json(run_dir / "run-state.json", run_state)
                    elif phase == "check_codex_auth":
                        self.run_codex_auth_check(app_server, repo_dir, run_dir, job)
                    elif phase == "submit_result_envelope":
                        pass
                    elif phase == "cleanup_active_job":
                        pass
                    elif phase in SEMANTIC_PHASES:
                        self.run_semantic_phase(app_server, repo_dir, run_dir, job, phase)
                    elif phase == "reviewer_json_validation":
                        self.run_reviewer_json_validation_phase(app_server, repo_dir, run_dir, job)
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
                            app_server,
                            repo_dir,
                            run_dir,
                            job,
                            phase,
                            validation_exc,
                        )
                        validate_phase_outputs(run_dir, phase, artifact_dir)
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
                        if not self.submit_result_or_mark_pending(active, job_id, result_payload(active, envelope, "done"), artifact_dir, envelope):
                            terminal_state = "result_submit_pending"
                            return
                        terminal_state = "completed"
                        self.emit_event(
                            active,
                            run_dir,
                            "run_completed",
                            "submit_result_envelope",
                            status="completed",
                            progress=100,
                            current_phase_percent=100,
                            message="Run completed.",
                        )
                        upload_log_artifacts_best_effort(self.client, job_id, active.attempt_id, run_dir, artifact_dir)
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
            if upload_error:
                envelope.setdefault("extensions", {}).setdefault("worker_internal", {})["artifact_upload_error"] = upload_error
            if self.submit_result_or_mark_pending(active, job_id, result_payload(active, envelope, "cancelled"), artifact_dir, envelope):
                terminal_state = "cancelled"
            else:
                terminal_state = "result_submit_pending"
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
            if upload_error:
                envelope.setdefault("extensions", {}).setdefault("worker_internal", {})["artifact_upload_error"] = upload_error
            if self.submit_result_or_mark_pending(active, job_id, result_payload(active, envelope, "partial_completed"), artifact_dir, envelope):
                terminal_state = "partial_completed"
            else:
                terminal_state = "result_submit_pending"
                return
        except Exception as exc:
            if codex_error_code(str(exc)) == "CODEX_QUOTA_EXHAUSTED":
                self.quota_monitor.mark_exhausted(str(exc))
            artifact_dir = artifact_dir or self.isolation.artifacts / run_id
            run_dir = run_dir or self.isolation.workspaces / run_id / "repo" / ".codex-review" / "runs" / run_id
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_failed", "error": str(exc), "time": iso_time(time.time())})
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
            if upload_error:
                envelope.setdefault("extensions", {}).setdefault("worker_internal", {})["artifact_upload_error"] = upload_error
            if self.submit_result_or_mark_pending(active, job_id, result_payload(active, envelope, "failed"), artifact_dir, envelope):
                terminal_state = "failed"
            else:
                terminal_state = "result_submit_pending"
                return
        finally:
            if app_server is not None:
                app_server.set_events_path(self.default_app_server_events_path())
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

    def submit_result_or_mark_pending(
        self,
        active: ActiveJob,
        job_id: str,
        payload: dict[str, Any],
        artifact_dir: Path,
        envelope: dict[str, Any],
    ) -> bool:
        try:
            self.client.result(job_id, payload)
            return True
        except Exception as exc:
            active.state = "finishing"
            active.current_phase = "submit_result_envelope"
            active.current_phase_status = "retrying"
            active.message = f"Result submit pending: {exc}"
            write_json(artifact_dir / "result-envelope.json", envelope)
            write_json(
                artifact_dir / "pending-submit.json",
                {
                    "run_id": active.run_id,
                    "job_id": active.job_id,
                    "lease_id": active.lease_id,
                    "attempt_id": active.attempt_id,
                    "result_status": result_status_from_envelope(envelope),
                    "status": "result_submit_pending",
                    "created_at": iso_time(time.time()),
                    "retry_count": 0,
                    "next_retry_after": None,
                    "result_envelope_path": "result-envelope.json",
                    "error": str(exc),
                },
            )
            return False


    def pending_submission_files(self) -> list[Path]:
        if not self.isolation.artifacts.exists():
            return []
        return sorted(
            self.isolation.artifacts.glob("*/pending-submit.json"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
        )

    def active_job_from_pending_submission(
        self,
        pending_path: Path,
        pending: dict[str, Any],
        envelope: dict[str, Any],
    ) -> ActiveJob:
        job_info = envelope.get("job") if isinstance(envelope.get("job"), dict) else {}
        run_id = safe_id(pending.get("run_id") or job_info.get("run_id") or pending_path.parent.name, "run")
        job_id = safe_id(pending.get("job_id") or job_info.get("job_id") or f"job_{run_id}", "job")
        lease_id = safe_id(pending.get("lease_id") or job_info.get("lease_id") or f"lease_{job_id}", "lease")
        attempt_id = str(pending.get("attempt_id") or f"{self.config.worker_id}-recovered")
        active = ActiveJob(job_id=job_id, run_id=run_id, lease_id=lease_id, attempt_id=attempt_id)
        active.state = "finishing"
        active.current_phase = "submit_result_envelope"
        active.current_phase_status = "retrying"
        active.current_phase_percent = 100.0
        active.overall_percent = 100.0
        active.message = "Recovered pending result submission."
        return active

    def recover_pending_submissions(self) -> None:
        if self.state.active_job is not None:
            return
        for pending_path in self.pending_submission_files():
            pending = read_json(pending_path, {})
            if not isinstance(pending, dict):
                pending = {}
            envelope_path = pending_path.parent / str(pending.get("result_envelope_path") or "result-envelope.json")
            envelope = read_json(envelope_path, {})
            if not isinstance(envelope, dict):
                envelope = {}
            active = self.active_job_from_pending_submission(pending_path, pending, envelope)
            self.state.set_active(active)
            if not self.retry_pending_submission(active, pending_path):
                return

    def retry_pending_submission_for_active(self, active: ActiveJob) -> bool:
        pending_path = self.isolation.artifacts / active.run_id / "pending-submit.json"
        if not pending_path.exists():
            active.message = "Pending result submission metadata is missing."
            return False
        return self.retry_pending_submission(active, pending_path)

    def retry_pending_submission(self, active: ActiveJob, pending_path: Path) -> bool:
        pending = read_json(pending_path, {})
        if not isinstance(pending, dict):
            pending = {}
        next_retry_after = pending.get("next_retry_after")
        if isinstance(next_retry_after, (int, float)) and next_retry_after > time.time():
            return False
        envelope_path = pending_path.parent / str(pending.get("result_envelope_path") or "result-envelope.json")
        envelope = read_json(envelope_path, {})
        if not isinstance(envelope, dict) or not envelope:
            pending["error"] = "result envelope is missing or invalid"
            pending["retry_count"] = int(pending.get("retry_count") or 0) + 1
            pending["next_retry_after"] = int(time.time() + 60)
            write_json(pending_path, pending)
            active.message = "Result submit pending: result envelope is missing or invalid"
            return False
        status = str(pending.get("result_status") or result_status_from_envelope(envelope))
        payload = result_payload(active, envelope, status)
        try:
            self.client.result(active.job_id, payload)
        except Exception as exc:
            retry_count = int(pending.get("retry_count") or 0) + 1
            pending.update(
                {
                    "run_id": active.run_id,
                    "job_id": active.job_id,
                    "lease_id": active.lease_id,
                    "attempt_id": active.attempt_id,
                    "result_status": status,
                    "status": "result_submit_pending",
                    "retry_count": retry_count,
                    "next_retry_after": int(time.time() + min(300, 2 ** min(retry_count, 8))),
                    "result_envelope_path": str(pending.get("result_envelope_path") or "result-envelope.json"),
                    "error": str(exc),
                }
            )
            write_json(pending_path, pending)
            active.state = "finishing"
            active.current_phase = "submit_result_envelope"
            active.current_phase_status = "retrying"
            active.message = f"Result submit pending: {exc}"
            return False
        try:
            pending_path.unlink()
        except FileNotFoundError:
            pass
        if self.state.active_job is active:
            self.state.clear_active(terminal_state_from_result_status(status))
        return True
    def run_codex_auth_check(self, app_server: JsonRpcAppServer | None, repo_dir: Path, run_dir: Path, job: dict[str, Any]) -> None:
        if app_server is None:
            raise RuntimeError("Codex app-server is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        app_server.run_turn(
            thread_id=thread_id,
            repo_dir=repo_dir,
            prompt='Codex auth check: return only JSON {"ok": true}.',
            effort="medium",
            read_only=True,
            timeout_seconds=turn_timeout_for_job(job),
            cancel_requested=self.poll_cancel_requested,
        )

    def run_semantic_phase(self, app_server: JsonRpcAppServer | None, repo_dir: Path, run_dir: Path, job: dict[str, Any], phase: str) -> None:
        if app_server is None:
            raise RuntimeError("Codex app-server is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        effort = effort_for_phase(job, phase)
        prompt = phase_prompt(phase, run_dir)
        app_server.run_turn(
            thread_id=thread_id,
            repo_dir=repo_dir,
            prompt=prompt,
            effort=effort,
            read_only=phase not in {"bootstrap_helper_scripts", "intent_test_writing", "final_report_json"},
            timeout_seconds=turn_timeout_for_job(job),
            cancel_requested=self.poll_cancel_requested,
        )

    def repair_semantic_phase_outputs(
        self,
        app_server: JsonRpcAppServer | None,
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
        if app_server is None:
            raise RuntimeError("Codex app-server is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        app_server.run_turn(
            thread_id=thread_id,
            repo_dir=repo_dir,
            prompt=phase_repair_prompt(phase, run_dir, validation_error),
            effort=effort_for_phase(job, phase),
            read_only=phase not in {"bootstrap_helper_scripts", "intent_test_writing", "final_report_json"},
            timeout_seconds=turn_timeout_for_job(job),
            cancel_requested=self.poll_cancel_requested,
        )

    def run_reviewer_json_validation_phase(self, app_server: JsonRpcAppServer | None, repo_dir: Path, run_dir: Path, job: dict[str, Any]) -> None:
        try:
            validate_reviewer_outputs(run_dir)
        except RuntimeError as validation_exc:
            self.repair_reviewer_outputs(app_server, repo_dir, run_dir, job, validation_exc)
            validate_reviewer_outputs(run_dir)

    def repair_reviewer_outputs(
        self,
        app_server: JsonRpcAppServer | None,
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
        if app_server is None:
            raise RuntimeError("Codex app-server is missing")
        state = read_json(run_dir / "run-state.json")
        thread_id = str(state.get("thread_id") or "")
        if not thread_id:
            raise RuntimeError("Codex thread is missing")
        app_server.run_turn(
            thread_id=thread_id,
            repo_dir=repo_dir,
            prompt=reviewer_json_repair_prompt(run_dir, validation_error),
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
            write_json(
                run_dir / "inventory.json",
                inventory(
                    repo_dir,
                    max_files=int(limits.get("maxFiles")) if limits.get("maxFiles") is not None else None,
                    max_bytes=int(limits.get("maxBytes")) if limits.get("maxBytes") is not None else None,
                    deadline_monotonic=scan_deadline,
                ),
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
        elif phase == "render_markdown_report":
            report = read_json(run_dir / "report.agent.json", default_agent_report(job))
            (run_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
        elif phase == "qa_gate":
            artifact_dir = self.isolation.artifacts / safe_id(job.get("run_id") or f"run_{job.get('job_id')}", "run")
            write_json(run_dir / "qa.json", qa_gate_payload(repo_dir, run_dir))
            for _attempt in range(2):
                materialize_artifacts(run_dir, artifact_dir)
                write_json(run_dir / "qa.json", qa_gate_payload(repo_dir, run_dir, artifact_dir))
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
        refresh_log_artifacts(run_dir, artifact_dir)
        manifest = artifact_manifest_items(read_json(artifact_dir / "artifact-manifest.json", {}))
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
            "repository": repository_payload(job),
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
            "artifact_manifest": manifest,
            "extensions": {"worker_internal": {"bundle_count": 1}},
        }


class JobCancelled(RuntimeError):
    pass


class JobPartialCompleted(RuntimeError):
    pass


def safe_id(value: Any, prefix: str) -> str:
    text = str(value or "").strip().replace("/", "_").replace("\\", "_")
    return text or f"{prefix}_{int(time.time())}"


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


def _job_review_policy(job: dict[str, Any]) -> dict[str, Any]:
    request = _job_review_request(job)
    return request.get("policy") if isinstance(request.get("policy"), dict) else {}


def _job_review_budget(job: dict[str, Any]) -> dict[str, Any]:
    request = _job_review_request(job)
    return request.get("budget") if isinstance(request.get("budget"), dict) else {}


def _clean_effort(value: object, *, field: str) -> str:
    effort = str(value or "").strip().lower()
    if effort not in {"low", "medium", "high", "xhigh"}:
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
        "inputs": ["v1.2 worker spec", ".codex-review/AGENTS.review.md"],
        "outputs": [".codex-review/tools/*.py", ".codex-review/schemas/*.schema.json", ".codex-review/prompts/*.md"],
        "instructions": [
            "Create or repair only review helper tools, schemas, and prompt templates.",
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
            "Weak findings go to appendix; disproven findings are excluded from main findings.",
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


def phase_prompt(phase: str, run_dir: Path) -> str:
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
    if inputs:
        lines.append("Inputs:")
        lines.extend(f"- {item}" for item in inputs)
    if outputs:
        lines.append("Required outputs:")
        lines.extend(f"- {item}" for item in outputs)
    if instructions:
        lines.append("Phase instructions:")
        lines.extend(f"- {item}" for item in instructions)
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


def phase_repair_prompt(phase: str, run_dir: Path, validation_error: object) -> str:
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
            phase_prompt(phase, run_dir).rstrip(),
        ]
    ) + "\n"


def reviewer_json_repair_prompt(run_dir: Path, validation_error: object) -> str:
    return "\n".join(
        [
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
    ) + "\n"


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


def inventory(
    repo_dir: Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
    deadline_monotonic: float | None = None,
) -> dict[str, Any]:
    files = []
    files_seen = 0
    bytes_seen = 0
    for path in sorted(repo_dir.rglob("*")):
        if ".git" in path.parts or ".codex-review" in path.parts:
            continue
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            raise RuntimeError("repository scan deadline exceeded while inventorying checkout")
        if not _is_regular_file_no_follow(path):
            continue
        rel = path.relative_to(repo_dir).as_posix()
        stat_result = path.stat(follow_symlinks=False)
        files_seen += 1
        bytes_seen += stat_result.st_size
        if max_files is not None and files_seen > max_files:
            raise RuntimeError("repositoryLimits.maxFiles exceeded while inventorying checkout")
        if max_bytes is not None and bytes_seen > max_bytes:
            raise RuntimeError("repositoryLimits.maxBytes exceeded while inventorying checkout")
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


def file_tier(item: dict[str, Any]) -> str:
    if item.get("is_binary") or item.get("is_generated_candidate"):
        return "SKIP"
    if item.get("risk_hints"):
        return "P0"
    path = str(item.get("path") or "").lower()
    if item.get("is_source_like") and any(part in path for part in ("src/", "app/", "server/", "api/", "lib/")):
        return "P1"
    if item.get("is_source_like"):
        return "P2"
    return "P3"


def bundle_plan_payload(run_dir: Path) -> dict[str, Any]:
    inv = read_json(run_dir / "inventory.json", {})
    files = inv.get("files") if isinstance(inv.get("files"), list) else []
    grouped: dict[str, list[dict[str, Any]]] = {"P0": [], "P1": [], "P2": [], "P3": [], "SKIP": []}
    for item in files:
        if isinstance(item, dict):
            grouped[file_tier(item)].append(item)
    bundles = []
    for tier in ("P0", "P1", "P2"):
        tier_files = grouped[tier]
        chunk: list[dict[str, Any]] = []
        token_count = 0
        for item in tier_files:
            item_tokens = int(item.get("estimated_tokens") or 0)
            if chunk and (len(chunk) >= 25 or token_count + item_tokens > 60000):
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
        "deep_reviewed_files": len(grouped["P0"]),
        "standard_reviewed_files": len(grouped["P1"]),
        "light_reviewed_files": len(grouped["P2"]),
        "inventory_only_files": len(grouped["P3"]),
        "skipped_files": len(grouped["SKIP"]),
        "intent_tests_planned": 0,
        "intent_tests_run": 0,
        "intent_tests_supporting_findings": 0,
        "skipped_scope": [item.get("path") for item in grouped["SKIP"][:100]],
    }
    write_json(run_dir / "coverage.json", coverage)
    return {"schema_version": "bundle-plan/v1", "run_id": run_dir.name, "bundles": bundles}


def bundle_payload(tier: str, index: int, files: list[dict[str, Any]], estimated_tokens: int) -> dict[str, Any]:
    reviewers = {
        "P0": ["security", "correctness", "test_gap"],
        "P1": ["correctness", "test_gap"],
        "P2": ["correctness_lite"],
    }[tier]
    return {
        "bundle_id": f"{tier.lower()}-bundle-{index:03d}",
        "tier": tier,
        "title": f"{tier} review bundle {index}",
        "estimated_tokens": estimated_tokens,
        "paths": [str(item.get("path")) for item in files if item.get("path")],
        "reviewers": reviewers,
        "validator_required": tier == "P0",
        "intent_test_eligible": tier in {"P0", "P1"},
        "risk_reasons": sorted({hint for item in files for hint in item.get("risk_hints", [])})[:12],
    }


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
        for rel in bundle.get("paths") or []:
            path = repo_dir / str(rel)
            lines.append(f"### {rel}")
            lines.append("")
            if not path.is_file():
                lines.append("```text")
                lines.append("<missing>")
                lines.append("```")
                lines.append("")
                continue
            suffix = path.suffix.lstrip(".") or "text"
            lines.append(f"```{suffix}")
            for index, source_line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
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
    raw_files = sorted(raw_dir.glob("*.json"))
    if not raw_files:
        errors.append({"file": "raw-reviewers", "error": "no reviewer JSON outputs were produced"})
    for path in raw_files:
        payload = read_json(path, None)
        if not isinstance(payload, dict):
            errors.append({"file": path.name, "error": "not an object"})
            continue
        schema_version = str(payload.get("schema_version") or "").strip()
        if schema_version != "codex-reviewer-output/v1":
            errors.append({"file": path.name, "error": "schema_version must be codex-reviewer-output/v1"})
            continue
        if not isinstance(payload.get("findings"), list):
            errors.append({"file": path.name, "error": "findings must be a list"})
            continue
        valid_outputs += 1
        write_json(verified_dir / path.name, payload)
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
            finding_id = str(finding.get("local_id") or finding.get("id") or "")
            for location in finding.get("locations") or []:
                if not isinstance(location, dict):
                    continue
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


def _intent_generated_command(generated: dict[str, Any], target: dict[str, Any]) -> list[str]:
    for key in ("command", "test_command", "testCommand", "run_command", "runCommand"):
        command = _intent_command(generated.get(key))
        if command:
            return command
    for key in ("command", "test_command", "testCommand", "run_command", "runCommand"):
        command = _intent_command(target.get(key))
        if command:
            return command
    return []


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

def run_intent_tests(run_dir: Path) -> dict[str, Any]:
    ensure_intent_directories(run_dir)
    validation = read_json(run_dir / "intent" / "validation-workspace.json", {})
    validation_root = str(validation.get("validation_repo_root") or "").strip() if isinstance(validation, dict) else ""
    validation_repo = Path(validation_root) if validation_root else None
    config = read_json(run_dir / "intent" / "intent-test-validation.json", {})
    if isinstance(config, dict) and config.get("enabled") is False:
        return {"schema_version": "intent-test-run-results/v1", "run_id": run_dir.name, "test_runs": []}
    plan = read_json(run_dir / "intent" / "intent-test-plan.json", {})
    targets = plan.get("test_targets") if isinstance(plan.get("test_targets"), list) else []
    source = read_json(run_dir / "intent" / "intent-test-source.json", {})
    generated_tests = source.get("generated_tests") if isinstance(source.get("generated_tests"), list) else []
    generated_by_id = {
        _intent_test_id(generated, f"ITV-{index + 1:03d}"): generated
        for index, generated in enumerate(generated_tests)
        if isinstance(generated, dict)
    }
    target_by_id = {
        _intent_test_id(target, f"ITV-{index + 1:03d}"): target
        for index, target in enumerate(targets)
        if isinstance(target, dict)
    }
    ordered_ids = []
    for collection in (targets, generated_tests):
        for index, item in enumerate(collection):
            if not isinstance(item, dict):
                continue
            test_id = _intent_test_id(item, f"ITV-{index + 1:03d}")
            if test_id and test_id not in ordered_ids:
                ordered_ids.append(test_id)
    max_tests = max(0, int((config if isinstance(config, dict) else {}).get("max_tests_per_run") or 20))
    total_deadline = time.monotonic() + max(0, int((config if isinstance(config, dict) else {}).get("max_total_test_run_seconds") or 900))
    raw_results = []
    for test_id in ordered_ids[:max_tests]:
        generated = generated_by_id.get(test_id) if isinstance(generated_by_id.get(test_id), dict) else {}
        target = target_by_id.get(test_id) if isinstance(target_by_id.get(test_id), dict) else {}
        command = _intent_generated_command(generated, target)
        base_result = {"schema_version": "project-test-run/v1", "test_id": test_id}
        if not validation_repo:
            raw_results.append({**base_result, "status": "skipped", "exit_code": None, "duration_ms": 0, "timed_out": False, "skip_reason": "validation workspace was not prepared"})
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
                completed = subprocess.run(
                    command,
                    cwd=str(cwd),
                    env=_intent_test_env(validation_repo, sandboxed=False),
                    text=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    check=False,
                )
                duration_ms = int((time.monotonic() - started) * 1000)
                stdout_path.write_text(completed.stdout or "", encoding="utf-8")
                stderr_path.write_text(completed.stderr or "", encoding="utf-8")
                raw_results.append(
                    {
                        **base_result,
                        "status": "passed" if completed.returncode == 0 else "failed",
                        "command": " ".join(shlex.quote(part) for part in command),
                        "sandbox_command": " ".join(shlex.quote(part) for part in sandbox_command),
                        "cwd": str(cwd),
                        "exit_code": int(completed.returncode),
                        "duration_ms": duration_ms,
                        "timed_out": False,
                        "stdout_path": str(stdout_path),
                        "stderr_path": str(stderr_path),
                        "sandbox_fallback_reason": "intent test sandbox runner failed to initialize",
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

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 5 or not isinstance(value, dict):
            return
        for field in ("cluster_id", "id", "finding_id", "local_id"):
            item_id = str(value.get(field) or "").strip()
            if item_id:
                ids.add(item_id)
        for field in ("clusters", "candidate_findings", "findings", "items", "candidates", "source_findings", "merged_findings"):
            children = value.get(field)
            if isinstance(children, list):
                for child in children:
                    visit(child, depth + 1)

    visit(payload)
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


def qa_gate_payload(repo_dir: Path, run_dir: Path, artifact_dir: Path | None = None) -> dict[str, Any]:
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
            start = _qa_int(location.get("start_line") or location.get("line"))
            end = _qa_int(location.get("end_line") or start)
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


def phase_progress_data(run_dir: Path, phase: str, artifact_dir: Path | None = None) -> dict[str, Any]:
    if phase == "reviewer_fanout":
        bundles = read_json(run_dir / "bundle-plan.json", {}).get("bundles", [])
        raw_dir = run_dir / "raw-reviewers"
        completed = len(list(raw_dir.glob("*.json"))) if raw_dir.is_dir() else 0
        total = len(bundles) if isinstance(bundles, list) else completed
        return {"reviewer_runs_total": total, "reviewer_runs_completed": min(completed, total) if total else completed}
    if phase == "intent_test_validation":
        plan_targets = read_json(run_dir / "intent" / "intent-test-plan.json", {}).get("test_targets", [])
        generated = read_json(run_dir / "intent" / "intent-test-source.json", {}).get("generated_tests", [])
        raw_runs = read_json(run_dir / "intent" / "intent-test-results.raw.json", {}).get("test_runs", [])
        analyzed_runs = read_json(run_dir / "intent" / "intent-test-results.json", {}).get("test_results", [])
        return {
            "intent_tests_total": len(plan_targets) if isinstance(plan_targets, list) else 0,
            "intent_tests_written": len(generated) if isinstance(generated, list) else 0,
            "intent_tests_run": len(raw_runs) if isinstance(raw_runs, list) else len(analyzed_runs) if isinstance(analyzed_runs, list) else 0,
        }
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
        payload = parse_required_json_output(path)
        if expected_schema and str(payload.get("schema_version") or "").strip() != expected_schema:
            raise RuntimeError(f"required phase output {path.name} must use schema_version {expected_schema}")
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
                payload = parse_required_json_output(raw_file)
                if str(payload.get("schema_version") or "").strip() != "codex-reviewer-output/v1":
                    raise RuntimeError(f"raw reviewer output {raw_file.name} must use schema_version codex-reviewer-output/v1")
                if not isinstance(payload.get("findings"), list):
                    raise RuntimeError(f"raw reviewer output {raw_file.name} findings must be a list")
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
        summary = read_json(run_dir / "inventory.json", {}).get("summary", {})
        return summary if isinstance(summary, dict) else {}
    if phase == "bundle_planning":
        bundles = read_json(run_dir / "bundle-plan.json", {}).get("bundles", [])
        return {"bundles_total": len(bundles) if isinstance(bundles, list) else 0}
    if phase == "intent_test_planning":
        targets = read_json(run_dir / "intent" / "intent-test-plan.json", {}).get("test_targets", [])
        return {"intent_tests_total": len(targets) if isinstance(targets, list) else 0}
    if phase == "intent_test_running":
        runs = read_json(run_dir / "intent" / "intent-test-results.raw.json", {}).get("test_runs", [])
        return {"intent_tests_run": len(runs) if isinstance(runs, list) else 0}
    return {}


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
        if not path.exists():
            path.write_text(tool_body, encoding="utf-8")
            path.chmod(0o700)
    for name in REQUIRED_SCHEMA_FILES:
        path = review_root / "schemas" / name
        if not path.exists():
            schema_id = name.removesuffix(".schema.json")
            write_json(path, {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"{schema_id}/v1", "type": "object", "required": ["schema_version"], "additionalProperties": True})
    for name in REQUIRED_PROMPT_FILES:
        path = review_root / "prompts" / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prompt_template_for_name(name), encoding="utf-8")


def prompt_template_for_name(name: str) -> str:
    templates = {
        "00_repo_mapper.md": "You are the Repo Mapper. Produce repo-map.json. Do not report bugs. Return JSON only using repo-map/v1.\n",
        "01_risk_router.md": "You are the Risk Router. Classify files and directories into P0/P1/P2/P3/SKIP. Return JSON only using risk-routing/v1.\n",
        "02_bundle_planner.md": "You may adjust mechanical bundle boundaries without changing the review policy. Return JSON only using bundle-plan/v1.\n",
        "reviewers/security.md": "You are the Security Reviewer. Report only concrete security issues with realistic abuse paths. Return JSON only using codex-reviewer-output/v1.\n",
        "reviewers/correctness.md": "You are the Correctness Reviewer. Focus on incorrect behavior, state, boundaries, idempotency, and concurrency. Return JSON only.\n",
        "reviewers/test_gap.md": "You are the Test Gap Reviewer. Report missing or weak tests only for important P0/P1 behavior. Return JSON only.\n",
        "reviewers/correctness_lite.md": "You are the Correctness Lite Reviewer. Only report clear bugs or user-visible behavior problems. Return JSON only.\n",
        "03_clusterer.md": "You are the Finding Clusterer and Vote Aggregator. Merge duplicates and suppress vague findings. Do not create new findings. Return JSON only.\n",
        "intent/04_intent_miner.md": "You are the Intent Miner. Extract behavioral contracts from docs, API specs, types, tests, route definitions, and error messages. Do not infer intent only from implementation code. Return JSON only using intent-map/v1.\n",
        "intent/05_intent_test_planner.md": "You are the Intent Test Planner. Select only high-value P0/P1 candidates for temporary tests. Return JSON only using intent-test-plan/v1.\n",
        "intent/06_intent_test_writer.md": "You are the Intent Test Writer. Write temporary tests only in the disposable validation workspace or .codex-review/generated-tests/**. Do not modify the main repo workspace. Return JSON describing created test files.\n",
        "intent/07_intent_test_failure_analyzer.md": "You are the Test Failure Analyzer. A failing test is not automatically a bug. Classify each result using intent-test-result/v1. Return JSON only.\n",
        "08_validator.md": "You are the Validation Reviewer. Try to disprove each candidate finding using evidence, location verification, related code, existing tests, and intent test results. Return JSON only.\n",
        "09_reporter.md": "You are the Final Reporter. Include only confirmed/plausible actionable findings in main findings; weak findings go to appendix. Return JSON only.\n",
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
    if phase == "repo_map" and not (run_dir / "repo-map.json").exists():
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
    elif phase == "intent_mining" and not (run_dir / "intent" / "intent-map.json").exists():
        write_json(run_dir / "intent" / "intent-map.json", {"schema_version": "intent-map/v1", "bundle_id": "all", "behavioral_contracts": [], "unknowns": ["No high-value intent targets were materialized."]})
    elif phase == "intent_test_planning" and not (run_dir / "intent" / "intent-test-plan.json").exists():
        write_json(run_dir / "intent" / "intent-test-plan.json", {"schema_version": "intent-test-plan/v1", "test_targets": []})
    elif phase == "intent_test_writing" and not (run_dir / "intent" / "intent-test-source.json").exists():
        write_json(run_dir / "intent" / "intent-test-source.json", {"schema_version": "intent-test-source/v1", "generated_tests": []})
    elif phase == "intent_test_failure_analysis" and not (run_dir / "intent" / "intent-test-results.json").exists():
        write_json(run_dir / "intent" / "intent-test-results.json", fallback_intent_test_results(run_dir))
    elif phase == "validator_disproof" and not (run_dir / "validated-findings.json").exists():
        write_json(run_dir / "validated-findings.json", {"schema_version": "validation-output/v1", "validated_findings": [], "disproven_findings": []})
    elif phase == "final_report_json" and not (run_dir / "report.agent.json").exists():
        write_json(run_dir / "report.agent.json", agent_report_payload(run_dir, job))


def default_agent_report(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_id": "codex-full-repo-review",
        "schema_version": "v1",
        "run_id": safe_id(job.get("run_id") or f"run_{job.get('job_id')}", "run"),
        "commit_sha": str(job.get("commit") or "pending"),
        "summary": {"overall_risk": "unknown", "result_status": "complete"},
        "coverage": {},
        "findings": [],
        "appendix_findings": [],
        "disproven_findings": [],
        "intent_test_validation": {"schema_version": "intent-test-result/v1", "test_results": []},
        "next_agent_tasks": [],
        "raw_artifact_refs": [],
    }


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


def render_markdown(report: dict[str, Any]) -> str:
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    intent = report.get("intent_test_validation") if isinstance(report.get("intent_test_validation"), dict) else {}
    tests = intent.get("test_results") if isinstance(intent.get("test_results"), list) else []
    return "\n".join(
        [
            "# Codex Full Repository Review Report",
            "",
            "## Summary",
            "",
            f"- Mode: full repository scan",
            f"- Commit: {report.get('commit_sha') or 'pending'}",
            f"- Confirmed findings: {len(findings)}",
            f"- Intent tests run: {len(tests)}",
            "",
            "## Top Findings",
            "",
            "No confirmed findings." if not findings else "",
            "",
            "## Intent Test Validation Summary",
            "",
            "No intent tests were run." if not tests else "",
        ]
    )


def materialize_terminal_artifacts(run_dir: Path, artifact_dir: Path, status: str, *, error: str = "") -> None:
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
    manifest = [
        artifact_item(artifact_dir / "worker.log.jsonl", "worker_log", "application/jsonl", "worker-log", True),
        artifact_item(artifact_dir / "qa.json", "qa", "application/json", "qa-gate", True),
        artifact_item(artifact_dir / "error-report.json", "error_report", "application/json", "error-report", True),
        artifact_item(artifact_dir / "codex-events.jsonl", "codex_event_log", "application/jsonl", "codex-events", False),
        artifact_item(artifact_dir / "progress.log.jsonl", "progress_log", "application/jsonl", "progress-log", False),
    ]
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
        "notes": [
            "This bundle is uploaded by the worker for live-environment debugging.",
            "Repository source files are not included; run artifacts, phase outputs, and logs are included.",
        ],
    }
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


def refresh_log_artifacts(run_dir: Path, artifact_dir: Path, manifest_payload: dict[str, Any] | None = None) -> None:
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
    changed = False
    for item in artifact_manifest_items(payload):
        name = str(item.get("name") or "")
        if name in LOG_ARTIFACT_NAMES:
            _refresh_manifest_item(item, artifact_dir / name)
            changed = True
        if item.get("artifact_id") == DEBUG_BUNDLE_ARTIFACT_ID:
            write_debug_bundle(run_dir, artifact_dir)
            _refresh_manifest_item(item, artifact_dir / DEBUG_BUNDLE_NAME)
            changed = True
    if changed:
        write_json(artifact_dir / "artifact-manifest.json", payload)
        write_json(run_dir / "artifact-manifest.json", payload)

def materialize_artifacts(run_dir: Path, artifact_dir: Path) -> None:
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


def summary_payload(run_dir: Path, status: str) -> dict[str, Any]:
    agent = read_json(run_dir / "report.agent.json", {})
    coverage = read_json(run_dir / "coverage.json", {})
    findings = agent.get("findings") if isinstance(agent.get("findings"), list) else []
    return {
        "overall_risk": (agent.get("summary") or {}).get("overall_risk", "unknown") if isinstance(agent.get("summary"), dict) else "unknown",
        "result_status": "complete" if status == "completed" else "incomplete",
        "finding_counts": {
            "confirmed_critical": count_findings(findings, "critical"),
            "confirmed_high": count_findings(findings, "high"),
            "confirmed_medium": count_findings(findings, "medium"),
            "confirmed_low": count_findings(findings, "low"),
            "plausible": 0,
            "weak_appendix": 0,
            "disproven": 0,
            "suppressed": 0,
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
    if not current_phase:
        current_phase = "submit_result_envelope" if status == "completed" else "failure_handling"
    message = str(snapshot.get("message") or "").strip()
    if not message:
        message = "Run completed and result accepted by server." if status == "completed" else f"Run ended with status {status}."
    return {
        "run_id": run_id,
        "overall_percent": overall_percent,
        "current_phase": current_phase,
        "status": status,
        "message": message,
    }


def count_findings(findings: list[Any], severity: str) -> int:
    return sum(1 for item in findings if isinstance(item, dict) and str(item.get("severity")).lower() == severity)


def repository_payload(job: dict[str, Any]) -> dict[str, Any]:
    repo = str(job.get("repo") or "")
    owner, _, name = repo.partition("/")
    return {
        "provider": "github",
        "owner": owner,
        "name": name or repo,
        "commit_sha": str(job.get("commit") or "pending"),
    }


def result_payload(active: ActiveJob, envelope: dict[str, Any], status: str) -> dict[str, Any]:
    agent_report = {}
    for item in envelope.get("artifact_manifest") or []:
        if item.get("name") == "report.agent.json":
            break
    return {
        "status": status,
        "attempt_id": active.attempt_id,
        "result_checksum": hashlib.sha256(json.dumps(envelope, sort_keys=True).encode("utf-8")).hexdigest(),
        "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
        "reviewWorkerProtocol": envelope,
        "humanReport": {"summaryMarkdown": "# Codex Full Repository Review Report\n"},
        "agentReport": agent_report,
        "readingGuide": {"forAgentDeep": "reviewWorkerProtocol.artifact_manifest"},
        "duration_ms": envelope["execution"].get("duration_ms", 0),
        "error": (envelope.get("error") or {}).get("message", ""),
        "error_code": (envelope.get("error") or {}).get("code", ""),
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
    action = "fail_job_retryable"
    retryable = True

    if status_text == "cancelled" or "cancel" in lowered:
        category, action, retryable = "job_cancelled", "cancel_job", False
    elif status_text == "partial_completed":
        category, action, retryable = "qa_failure", "partial_result", False
    elif phase_text == "start_codex_app_server":
        category, action, retryable = "codex_app_server_failure", "disable_worker", False
    elif phase_text == "check_codex_auth" or code == "CODEX_UNAUTHORIZED":
        category, action, retryable = "codex_auth_failure", "fail_job_retryable", True
    elif code == "CODEX_QUOTA_EXHAUSTED":
        category, action, retryable = "codex_usage_limit_exceeded", "fail_job_retryable", True
    elif code == "CODEX_CONTEXT_WINDOW_EXCEEDED":
        category, action, retryable = "context_budget_failure", "split_bundle_and_retry", True
    elif code == "CODEX_SANDBOX_ERROR":
        category, action, retryable = "worker_environment_failure", "fail_job_terminal", False
    elif "artifact" in lowered and "upload" in lowered:
        category, action, retryable = "artifact_upload_failure", "retry_phase", True
    elif "result submit" in lowered or "pending-submit" in lowered:
        category, action, retryable = "result_submit_failure", "retry_phase", True
    elif "server unavailable" in lowered or "connection" in lowered:
        category, action, retryable = "server_connection_failure", "retry_phase", True
    elif phase_text in {"reviewer_json_validation"} or "json" in lowered or "schema" in lowered:
        category, action, retryable = "json_schema_failure", "repair_output", True
    elif phase_text == "location_validation":
        category, action, retryable = "location_validation_failure", "degrade_scope", False
    elif phase_text == "intent_test_planning":
        category, action, retryable = "intent_test_planning_failure", "skip_intent_test", False
    elif phase_text == "intent_test_writing":
        category, action, retryable = "intent_test_generation_failure", "skip_intent_test", False
    elif phase_text == "intent_test_running":
        category, action, retryable = "intent_test_runtime_failure", "degrade_scope", False
    elif phase_text == "intent_test_failure_analysis":
        category, action, retryable = "intent_test_oracle_failure", "degrade_scope", False
    elif phase_text == "qa_gate" or "qa" in lowered:
        category, action, retryable = "qa_failure", "partial_result", False

    return {
        "code": code,
        "category": category,
        "message": message,
        "retryable": retryable,
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

    files_seen = 0
    bytes_seen = 0
    for root, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        if ".git" in root_path.parts:
            dirnames[:] = []
            continue
        dirnames[:] = [name for name in dirnames if name != ".git" and not (root_path / name).is_symlink()]
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
            file_size = path.stat(follow_symlinks=False).st_size
            files_seen += 1
            bytes_seen += file_size
            if max_files is not None and files_seen > max_files:
                raise RuntimeError("repositoryLimits.maxFiles exceeded while copying checkout")
            if max_bytes is not None and bytes_seen > max_bytes:
                raise RuntimeError("repositoryLimits.maxBytes exceeded while copying checkout")
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
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


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
    refresh_log_artifacts(run_dir, artifact_dir, manifest_payload)
    manifest = artifact_manifest_items(manifest_payload)
    if not manifest:
        raise RuntimeError("artifact manifest must contain artifact items before final log upload")
    uploaded = 0
    for item in manifest:
        name = str(item.get("name") or "").strip()
        if name not in LOG_ARTIFACT_NAMES:
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        if not artifact_id:
            raise RuntimeError(f"log artifact manifest entry requires artifact_id: {name}")
        path = artifact_dir / name
        try:
            path.resolve(strict=False).relative_to(artifact_dir.resolve(strict=False))
        except ValueError as exc:
            raise RuntimeError(f"log artifact path escapes artifact directory before upload: {name}") from exc
        if not path.is_file():
            raise RuntimeError(f"log artifact listed in manifest is missing before upload: {name}")
        data = path.read_bytes()
        if str(item.get("sha256") or "").lower() != hashlib.sha256(data).hexdigest():
            raise RuntimeError(f"log artifact sha256 mismatch before upload: {name}")
        if int(item.get("size_bytes") if item.get("size_bytes") is not None else -1) != len(data):
            raise RuntimeError(f"log artifact size mismatch before upload: {name}")
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
        raise RuntimeError("artifact manifest contains no log artifacts before final log upload")

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
    manifest = artifact_manifest_items(manifest_payload)
    if not isinstance(manifest_payload, dict) or not manifest:
        raise RuntimeError("artifact manifest must contain artifact items before upload")
    if manifest_payload.get("schema_version") != "artifact-manifest/v1":
        raise RuntimeError("artifact manifest must use schema_version artifact-manifest/v1 before upload")
    if str(manifest_payload.get("run_id") or "").strip() != artifact_dir.name:
        raise RuntimeError("artifact manifest run_id does not match upload run before upload")
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
    total = len(uploadable)
    for uploaded, (item, path) in enumerate(uploadable, start=1):
        artifact_id = str(item.get("artifact_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if source_run_dir is not None and name in LOG_ARTIFACT_NAMES:
            refresh_log_artifacts(source_run_dir, artifact_dir, manifest_payload)
        data = path.read_bytes()
        actual_sha = hashlib.sha256(data).hexdigest()
        if str(item.get("sha256") or "").lower() != actual_sha:
            raise RuntimeError(f"artifact sha256 mismatch before upload: {name}")
        if int(item.get("size_bytes") if item.get("size_bytes") is not None else -1) != len(data):
            raise RuntimeError(f"artifact size mismatch before upload: {name}")
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
        if progress_callback is not None:
            progress_callback(uploaded, total, item)
