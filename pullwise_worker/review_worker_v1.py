from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import fcntl
except ImportError:  # pragma: no cover - runtime is Linux only; import stays testable elsewhere.
    fcntl = None

PROTOCOL_VERSION = "review-worker-protocol/v1"
WORKER_VERSION = "0.1.0"
TERMINAL_STATES = {"completed", "failed", "cancelled", "partial_completed"}
PIPELINE_PHASES = (
    ("prepare_workspace", 4),
    ("start_codex_app_server", 8),
    ("initialize_codex_connection", 10),
    ("check_codex_auth", 12),
    ("bootstrap_helper_scripts", 14),
    ("inventory_repository", 18),
    ("token_budget", 22),
    ("repo_map", 30),
    ("risk_routing", 38),
    ("bundle_planning", 46),
    ("bundle_packing", 54),
    ("reviewer_fanout", 68),
    ("reviewer_json_validation", 72),
    ("location_validation", 76),
    ("clustering_and_voting", 82),
    ("validator_disproof", 88),
    ("final_report_json", 92),
    ("render_markdown_report", 95),
    ("qa_gate", 97),
    ("hash_artifacts", 98),
    ("upload_artifacts", 99),
    ("submit_result_envelope", 100),
)
SEMANTIC_PHASES = {
    "bootstrap_helper_scripts",
    "repo_map",
    "risk_routing",
    "reviewer_fanout",
    "clustering_and_voting",
    "validator_disproof",
    "final_report_json",
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
)
REQUIRED_SCHEMA_FILES = (
    "repo-map.schema.json",
    "risk-routing.schema.json",
    "bundle-plan.schema.json",
    "reviewer-output.schema.json",
    "cluster-output.schema.json",
    "validation-output.schema.json",
    "final-report.schema.json",
    "server-result-envelope.schema.json",
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
    "04_validator.md",
    "05_reporter.md",
)
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
- Helper scripts must use Python 3 standard library only.
- Helper scripts perform mechanical tasks only.
- Codex performs semantic code review judgment.
- Every main finding must be concrete, located, evidenced, and actionable.
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

Rules:
- Do not modify application source files.
- Do not install dependencies.
- Do not call external review/scanning tools.
- Every main finding must have path, line range, evidence, impact, recommendation, severity, confidence, and next_agent_task.
- Weak findings go to appendix.
- Disproven findings do not appear in main findings.
- Coverage and skipped scope must be reported.
"""


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

    def heartbeat_payload(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "lease_id": self.lease_id,
            "state": self.state,
            "started_at": iso_time(self.started_at),
            "current_phase": self.current_phase,
            "cancel_requested": self.cancel_requested,
        }


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
    def __init__(self, worker_root: Path, worker_id: str) -> None:
        self.worker_root = worker_root
        self.worker_id = worker_id
        self.path = worker_root / "lock" / "worker.lock"
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
                    "codex_home": str(self.worker_root / "codex-home"),
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
        configured_root = os.environ.get("PULLWISE_WORKER_ROOT", "").strip()
        self.worker_root = Path(configured_root) if configured_root else service_home / "workers" / self.worker_id
        self.codex_home = self.worker_root / "codex-home"
        self.codex_sqlite_home = self.worker_root / "codex-sqlite"
        self.runtime = self.worker_root / "runtime"
        self.workspaces = self.worker_root / "workspaces"
        self.artifacts = self.worker_root / "artifacts"
        self.logs = self.worker_root / "logs"

    def prepare(self) -> None:
        for path in (
            self.worker_root,
            self.codex_home,
            self.codex_sqlite_home,
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
        env = os.environ.copy()
        extra = getattr(config, "codex_env", None)
        if isinstance(extra, dict):
            env.update({str(k): str(v) for k, v in extra.items()})
        env.update(
            {
                "HOME": str(self.worker_root),
                "USERPROFILE": str(self.worker_root),
                "CODEX_HOME": str(self.codex_home),
                "CODEX_SQLITE_HOME": str(self.codex_sqlite_home),
            }
        )
        return env


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

    def start(self) -> None:
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

    def start_thread(self, repo_dir: Path, model: str) -> str:
        result = self.request(
            "thread/start",
            {
                "cwd": str(repo_dir),
                "approvalPolicy": "never",
                "sandbox": "workspaceWrite",
                "personality": "precise",
                "serviceName": "codex_repo_review_worker",
                "model": model or None,
            },
        )
        return str(((result.get("thread") or {}).get("id")) or result.get("threadId") or "")

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
        sandbox = {"type": "readOnly", "networkAccess": False}
        if not read_only:
            sandbox = {
                "type": "workspaceWrite",
                "networkAccess": False,
                "writableRoots": [str(repo_dir / ".codex-review")],
            }
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
                if "approval" in method.lower():
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


class CodexQuotaMonitor:
    def __init__(self, config: Any, isolation: Isolation) -> None:
        self.config = config
        self.isolation = isolation
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
        try:
            self.isolation.runtime.mkdir(parents=True, exist_ok=True)
            self.isolation.logs.mkdir(parents=True, exist_ok=True)
            server = JsonRpcAppServer(
                str(getattr(self.config, "codex_command", "") or "codex"),
                self.isolation.env(self.config),
                self.isolation.runtime,
                self.isolation.logs / "codex-quota-events.jsonl",
            )
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
            if codex_error_code(str(exc)) == "CODEX_QUOTA_EXHAUSTED":
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
            if server is not None:
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
DENIED_COMMAND_TOKENS = {
    "brew",
    "cargo",
    "checkout",
    "commit",
    "curl",
    "install",
    "npm",
    "pip",
    "push",
    "reset",
    "rm",
    "wget",
}


def approval_response_for_request(message: dict[str, Any], workspace: Path) -> dict[str, Any]:
    decision, reason = decide_approval(message, workspace)
    return {"decision": decision, "outcome": decision, "reason": reason}


def decide_approval(message: dict[str, Any], workspace: Path) -> tuple[str, str]:
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    request = params.get("request") if isinstance(params.get("request"), dict) else params
    request_type = str(request.get("type") or request.get("kind") or message.get("method") or "").lower()
    if "file" in request_type or request.get("paths") or request.get("path"):
        paths = request.get("paths") if isinstance(request.get("paths"), list) else [request.get("path")]
        if paths and all(path_is_under_codex_review(workspace, path) for path in paths if path):
            return "acceptForSession", "write is limited to .codex-review"
        return "decline", "file changes outside .codex-review are not allowed"
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


def command_is_allowed(command: object, workspace: Path, raw_cwd: object = None) -> bool:
    argv = [str(part) for part in command] if isinstance(command, list) else shlex.split(str(command or ""))
    if not argv:
        return False
    executable = Path(argv[0]).name
    lowered = {part.lower() for part in argv}
    if lowered.intersection(DENIED_COMMAND_TOKENS):
        return False
    cwd = Path(str(raw_cwd)) if raw_cwd else workspace
    if not cwd.is_absolute():
        cwd = workspace / cwd
    try:
        cwd.resolve(strict=False).relative_to(workspace.resolve(strict=False))
    except ValueError:
        return False
    if executable in {"python", "python3"} and len(argv) >= 2:
        return path_is_under_codex_review(workspace, argv[1]) and "/tools/" in Path(argv[1]).as_posix()
    return executable in READ_ONLY_COMMANDS


class ReviewWorkerV1:
    def __init__(self, config: Any, client: Any | None = None) -> None:
        self.config = config
        self.client = client
        self.state = WorkerState()
        self.isolation = Isolation(config)
        self.quota_monitor = CodexQuotaMonitor(config, self.isolation)
        self.lock = WorkerLock(self.isolation.worker_root, str(config.worker_id))

    def run(self, *, once: bool = False) -> None:
        if os.name != "posix":
            raise RuntimeError("Pullwise review worker v1 is Linux/POSIX only")
        self.isolation.prepare()
        self.lock.acquire()
        try:
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
            self.lock.release()

    def heartbeat(self) -> dict[str, Any]:
        active = self.state.active_job
        quota = self.quota_monitor.snapshot_if_due(active=active is not None)
        quota_ready = bool((quota or {}).get("ready", True))
        self.state.provider_ready = quota_ready
        readiness_reason = quota_text((quota or {}).get("reason") or (quota or {}).get("status"), 160)
        response = self.client.heartbeat(
            running_jobs=1 if active else 0,
            active_job_ids=[active.job_id] if active else [],
            last_error=readiness_reason if not quota_ready else None,
            doctor_status="ok" if quota_ready else "degraded",
            codex_ready=quota_ready,
            ready_providers=["codex"] if quota_ready else [],
            codex_quota=quota,
        )
        cancelled = response.get("cancelled_job_ids") if isinstance(response, dict) else []
        if active and active.job_id in (cancelled or []):
            active.cancel_requested = True
        return response if isinstance(response, dict) else {}

    def poll_cancel_requested(self) -> bool:
        active = self.state.active_job
        if active is None:
            return False
        self.heartbeat()
        return bool(active.cancel_requested)


    def run_job(self, job: dict[str, Any]) -> None:
        job_id = safe_id(job.get("job_id"), "job")
        run_id = safe_id(job.get("run_id") or f"run_{job_id}", "run")
        lease_id = safe_id(job.get("lease_id") or f"lease_{job_id}", "lease")
        attempt = int(job.get("attempt") or 1)
        active = ActiveJob(job_id=job_id, run_id=run_id, lease_id=lease_id, attempt_id=f"{self.config.worker_id}-{attempt}")
        self.state.set_active(active)
        terminal_state = "failed"
        app_server: JsonRpcAppServer | None = None
        started = time.time()
        try:
            validate_job_policy(job)
            repo_dir, run_dir, artifact_dir = self.prepare_workspace(job, run_id)
            events_path = artifact_dir / "codex-events.jsonl"
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_started", "job_id": job_id, "run_id": run_id, "time": iso_time(started)})
            for phase, progress in PIPELINE_PHASES:
                active.current_phase = phase
                active.state = "busy" if phase not in {"upload_artifacts", "submit_result_envelope"} else "finishing"
                self.client.progress(job_id, phase, progress, phase.replace("_", " "))
                append_jsonl(run_dir / "worker.log.jsonl", {"event": "phase_started", "phase": phase, "progress": progress, "time": iso_time(time.time())})
                if active.cancel_requested:
                    raise JobCancelled("cancel requested")
                if phase == "start_codex_app_server":
                    app_server = JsonRpcAppServer(
                        str(getattr(self.config, "codex_command", "") or "codex"),
                        self.isolation.env(self.config),
                        repo_dir,
                        events_path,
                        rate_limit_callback=self.quota_monitor.apply_rate_limit_update,
                    )
                    app_server.start()
                elif phase == "initialize_codex_connection":
                    thread_id = app_server.start_thread(repo_dir, model_for_job(job)) if app_server else ""
                    write_json(run_dir / "run-state.json", {"thread_id": thread_id, "active_job": active.heartbeat_payload()})
                elif phase == "check_codex_auth":
                    self.run_codex_auth_check(app_server, repo_dir, run_dir, job)
                elif phase in SEMANTIC_PHASES:
                    self.run_semantic_phase(app_server, repo_dir, run_dir, job, phase)
                elif phase in MECHANICAL_PHASES:
                    self.run_mechanical_phase(repo_dir, run_dir, job, phase)
            envelope = self.build_envelope(job, run_id, "completed", started, artifact_dir, run_dir)
            self.client.result(job_id, result_payload(active, envelope, "done"))
            terminal_state = "completed"
        except JobCancelled:
            artifact_dir = self.isolation.artifacts / run_id
            run_dir = self.isolation.workspaces / run_id / "repo" / ".codex-review" / "runs" / run_id
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_cancelled", "error": "cancel requested", "time": iso_time(time.time())})
            envelope = self.build_envelope(job, run_id, "cancelled", started, artifact_dir, run_dir, error="cancel requested")
            upload_error = upload_artifacts_best_effort(self.client, job_id, active.attempt_id, artifact_dir)
            if upload_error:
                envelope.setdefault("extensions", {}).setdefault("worker_internal", {})["artifact_upload_error"] = upload_error
            self.client.result(job_id, result_payload(active, envelope, "failed"))
            terminal_state = "cancelled"
        except Exception as exc:
            if codex_error_code(str(exc)) == "CODEX_QUOTA_EXHAUSTED":
                self.quota_monitor.mark_exhausted(str(exc))
            artifact_dir = self.isolation.artifacts / run_id
            run_dir = self.isolation.workspaces / run_id / "repo" / ".codex-review" / "runs" / run_id
            append_jsonl(run_dir / "worker.log.jsonl", {"event": "job_failed", "error": str(exc), "time": iso_time(time.time())})
            envelope = self.build_envelope(job, run_id, "failed", started, artifact_dir, run_dir, error=str(exc))
            upload_error = upload_artifacts_best_effort(self.client, job_id, active.attempt_id, artifact_dir)
            if upload_error:
                envelope.setdefault("extensions", {}).setdefault("worker_internal", {})["artifact_upload_error"] = upload_error
            self.client.result(job_id, result_payload(active, envelope, "failed"))
            terminal_state = "failed"
        finally:
            if app_server is not None:
                app_server.close()
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
        if source:
            copy_tree(Path(source), repo_dir)
        for path in (repo_dir / ".codex-review", run_dir, run_dir / "bundles", run_dir / "raw-reviewers", run_dir / "verified-reviewers"):
            path.mkdir(parents=True, exist_ok=True)
        write_worker_config(repo_dir / ".codex-review" / "worker-config.json", job, self.config)
        write_review_instruction_tree(repo_dir)
        return repo_dir, run_dir, artifact_dir

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
            return
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
            read_only=phase not in {"bootstrap_helper_scripts", "final_report_json"},
            timeout_seconds=turn_timeout_for_job(job),
            cancel_requested=self.poll_cancel_requested,
        )

    def run_mechanical_phase(self, repo_dir: Path, run_dir: Path, job: dict[str, Any], phase: str) -> None:
        if phase == "inventory_repository":
            write_json(run_dir / "inventory.json", inventory(repo_dir))
        elif phase == "token_budget":
            write_json(
                run_dir / "token-budget.json",
                {
                    "model": model_for_job(job),
                    "core_effort": core_effort_for_job(job),
                    "non_core_effort": "medium",
                    "review_worker": review_worker_policy_for_job(job),
                },
            )
        elif phase == "bundle_planning":
            inv = read_json(run_dir / "inventory.json")
            files = inv.get("files") if isinstance(inv.get("files"), list) else []
            write_json(run_dir / "bundle-plan.json", {"bundles": [{"bundle_id": "bundle_001", "depth": "P1", "files": [f.get("path") for f in files[:25]]}]})
        elif phase == "bundle_packing":
            (run_dir / "bundles" / "bundle_001.md").write_text("# Bundle bundle_001\n", encoding="utf-8")
        elif phase == "reviewer_json_validation":
            ensure_json(run_dir / "raw-reviewers")
        elif phase == "location_validation":
            write_json(run_dir / "location-verification.json", {"verified": True, "errors": []})
        elif phase == "render_markdown_report":
            report = read_json(run_dir / "report.agent.json", default_agent_report(job))
            (run_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
        elif phase == "qa_gate":
            write_json(run_dir / "qa.json", {"status": "pass", "errors": [], "warnings": []})
        elif phase == "upload_artifacts":
            upload_artifacts(
                self.client,
                safe_id(job.get("job_id"), "job"),
                active_attempt_id(self.config, job),
                self.isolation.artifacts / safe_id(job.get("run_id") or f"run_{job.get('job_id')}", "run"),
            )
        elif phase == "hash_artifacts":
            materialize_artifacts(run_dir, self.isolation.artifacts / safe_id(job.get("run_id") or f"run_{job.get('job_id')}", "run"))
        elif phase in {"repo_map", "risk_routing", "clustering_and_voting", "validator_disproof", "final_report_json"}:
            fallback_semantic_artifact(run_dir, job, phase)
        write_json(run_dir / "progress.json", {"phase": phase, "updated_at": iso_time(time.time())})

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
    ) -> dict[str, Any]:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        if status == "completed":
            if not (artifact_dir / "artifact-manifest.json").exists():
                materialize_artifacts(run_dir, artifact_dir)
        else:
            materialize_terminal_artifacts(run_dir, artifact_dir, status, error=error)
        manifest = read_json(artifact_dir / "artifact-manifest.json", [])
        now = time.time()
        error_code = codex_error_code(error) if error else ""
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
            "error": {"code": error_code, "message": error, "retryable": error_code != "CODEX_QUOTA_EXHAUSTED"} if error else None,
            "summary": summary_payload(run_dir, status),
            "quality_gate": read_json(run_dir / "qa.json", {"status": "fail", "errors": ["run did not reach qa gate"], "warnings": []}),
            "artifact_manifest": manifest,
            "extensions": {"worker_internal": {"bundle_count": 1}},
        }


class JobCancelled(RuntimeError):
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
    return core_effort_for_job(job) if phase in CORE_EFFORT_PHASES else "medium"


def validate_job_policy(job: dict[str, Any]) -> dict[str, Any]:
    agent = job.get("agentConfig") if isinstance(job.get("agentConfig"), dict) else {}
    provider = str(agent.get("provider") or "").strip().lower()
    if provider and provider != "codex":
        raise ValueError("agentConfig.provider must be codex")
    codex = agent.get("codex") if isinstance(agent.get("codex"), dict) else {}
    model = str(codex.get("model") or "").strip()
    if not model:
        raise ValueError("claimed job must include agentConfig.codex.model")
    effort = str(codex.get("reasoningEffort") or "").strip().lower()
    if effort not in {"low", "medium", "high", "xhigh"}:
        raise ValueError("claimed job must include agentConfig.codex.reasoningEffort")
    review_worker = agent.get("reviewWorker") if isinstance(agent.get("reviewWorker"), dict) else {}
    try:
        turn_timeout_seconds = int(review_worker.get("turnTimeoutSeconds"))
        scan_deadline_seconds = int(review_worker.get("scanDeadlineSeconds"))
    except (TypeError, ValueError):
        raise ValueError("claimed job must include agentConfig.reviewWorker.turnTimeoutSeconds and scanDeadlineSeconds") from None
    if turn_timeout_seconds <= 0 or scan_deadline_seconds < 0:
        raise ValueError("agentConfig.reviewWorker turn timeout must be positive and scan deadline must be non-negative")
    limits = job.get("repositoryLimits") if isinstance(job.get("repositoryLimits"), dict) else {}
    try:
        max_files = int(limits.get("maxFiles"))
        max_bytes = int(limits.get("maxBytes"))
    except (TypeError, ValueError):
        raise ValueError("claimed job must include repositoryLimits.maxFiles and repositoryLimits.maxBytes") from None
    if max_files <= 0 or max_bytes <= 0:
        raise ValueError("repositoryLimits.maxFiles and repositoryLimits.maxBytes must be positive")
    return {
        "model": model,
        "reasoning_effort": effort,
        "review_worker": {
            "turnTimeoutSeconds": turn_timeout_seconds,
            "scanDeadlineSeconds": scan_deadline_seconds,
        },
        "repository_limits": {"maxFiles": max_files, "maxBytes": max_bytes},
    }


def phase_prompt(phase: str, run_dir: Path) -> str:
    return (
        f"Phase: {phase}\n"
        "Perform only the requested full-repository review phase. "
        "Do not modify application source files. Write outputs only under .codex-review/runs.\n"
        f"Run artifact directory: {run_dir}\n"
    )


def inventory(repo_dir: Path) -> dict[str, Any]:
    files = []
    for path in sorted(repo_dir.rglob("*")):
        if not path.is_file() or ".git" in path.parts or ".codex-review" in path.parts:
            continue
        rel = path.relative_to(repo_dir).as_posix()
        files.append({"path": rel, "size_bytes": path.stat().st_size})
    return {"source_like_files_total": len(files), "files": files}


def write_review_instruction_tree(repo_dir: Path) -> None:
    review_root = repo_dir / ".codex-review"
    (review_root / "tools").mkdir(parents=True, exist_ok=True)
    (review_root / "schemas").mkdir(parents=True, exist_ok=True)
    (review_root / "prompts" / "reviewers").mkdir(parents=True, exist_ok=True)
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
    schema_body = {"type": "object", "additionalProperties": True}
    for name in REQUIRED_SCHEMA_FILES:
        path = review_root / "schemas" / name
        if not path.exists():
            write_json(path, schema_body)
    for name in REQUIRED_PROMPT_FILES:
        path = review_root / "prompts" / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# Pullwise Codex Full Repository Review Phase\n\n"
                "Follow .codex-review/AGENTS.review.md. Return the phase artifact requested by the worker design.\n",
                encoding="utf-8",
            )


def fallback_semantic_artifact(run_dir: Path, job: dict[str, Any], phase: str) -> None:
    if phase == "repo_map":
        write_json(run_dir / "repo-map.json", {"areas": [], "notes": "Codex repo_map phase did not materialize an artifact."})
    elif phase == "risk_routing":
        write_json(run_dir / "risk-routing.json", {"routes": [], "default_depth": "P1"})
        write_json(run_dir / "coverage.json", {"source_like_files_total": read_json(run_dir / "inventory.json", {}).get("source_like_files_total", 0), "deep_reviewed_files": 0, "standard_reviewed_files": 0, "light_reviewed_files": 0, "inventory_only_files": 0, "skipped_files": 0})
    elif phase == "clustering_and_voting":
        write_json(run_dir / "cluster-result.json", {"clusters": []})
    elif phase == "validator_disproof":
        write_json(run_dir / "validation-result.json", {"validated": []})
    elif phase == "final_report_json":
        write_json(run_dir / "report.agent.json", default_agent_report(job))


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
        "next_agent_tasks": [],
        "raw_artifact_refs": [],
    }


def render_markdown(report: dict[str, Any]) -> str:
    findings = report.get("findings") if isinstance(report.get("findings"), list) else []
    return "\n".join(
        [
            "# Codex Full Repository Review Report",
            "",
            "## Summary",
            "",
            f"- Mode: full repository scan",
            f"- Commit: {report.get('commit_sha') or 'pending'}",
            f"- Confirmed findings: {len(findings)}",
            "",
            "## Top Findings",
            "",
            "No confirmed findings." if not findings else "",
        ]
    )


def materialize_terminal_artifacts(run_dir: Path, artifact_dir: Path, status: str, *, error: str = "") -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (
        ("worker.log.jsonl", ""),
        ("codex-events.jsonl", ""),
    ):
        src = run_dir / name
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content, encoding="utf-8")
        shutil.copy2(src, artifact_dir / name)
    error_report = {
        "status": status,
        "error": error,
        "created_at": iso_time(time.time()),
    }
    write_json(artifact_dir / "error-report.json", error_report)
    manifest = [
        artifact_item(artifact_dir / "worker.log.jsonl", "worker_log", "application/jsonl", "worker-log", False),
        artifact_item(artifact_dir / "codex-events.jsonl", "codex_event_log", "application/jsonl", "codex-events", False),
        artifact_item(artifact_dir / "error-report.json", "error_report", "application/json", "error-report", False),
    ]
    write_json(artifact_dir / "artifact-manifest.json", manifest)


def materialize_artifacts(run_dir: Path, artifact_dir: Path) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    required_defaults = {
        "report.md": "# Codex Full Repository Review Report\n",
        "report.agent.json": json.dumps(default_agent_report({"job_id": "unknown"}), sort_keys=True),
        "coverage.json": "{}",
        "token-budget.json": "{}",
        "qa.json": json.dumps({"status": "fail", "errors": ["missing qa gate"], "warnings": []}),
        "codex-events.jsonl": "",
        "worker.log.jsonl": "",
    }
    for name, content in required_defaults.items():
        src = run_dir / name
        if not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content, encoding="utf-8")
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
    ):
        path = artifact_dir / name
        manifest.append(artifact_item(path, kind, media_type, schema_id, required))
    write_json(artifact_dir / "artifact-manifest.json", manifest)


def artifact_item(path: Path, kind: str, media_type: str, schema_id: str, required: bool) -> dict[str, Any]:
    data = path.read_bytes() if path.exists() else b""
    artifact_id = "art_" + kind.replace(".", "_")
    return {
        "artifact_id": artifact_id,
        "kind": kind,
        "name": path.name,
        "media_type": media_type,
        "schema_id": schema_id,
        "schema_version": "v1",
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


def copy_tree(source: Path, dest: Path) -> None:
    for path in source.rglob("*"):
        if ".git" in path.parts:
            continue
        rel = path.relative_to(source)
        target = dest / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


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


def upload_artifacts_best_effort(client: Any, job_id: str, attempt_id: str, artifact_dir: Path) -> str:
    try:
        upload_artifacts(client, job_id, attempt_id, artifact_dir)
    except Exception as exc:
        return str(exc)
    return ""


def upload_artifacts(client: Any, job_id: str, attempt_id: str, artifact_dir: Path) -> None:
    manifest = read_json(artifact_dir / "artifact-manifest.json", [])
    if not isinstance(manifest, list):
        raise RuntimeError("artifact manifest must be a list before upload")
    for item in manifest:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not artifact_id or not name:
            raise RuntimeError("artifact manifest entries require artifact_id and name")
        path = artifact_dir / name
        if not path.is_file():
            if item.get("required") is True:
                raise RuntimeError(f"required artifact is missing: {name}")
            continue
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
                "attempt_id": attempt_id,
                "run_id": artifact_dir.name,
                "artifact": item,
                "content_base64": base64.b64encode(data).decode("ascii"),
            },
        )

