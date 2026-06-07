from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import platform
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Lock

from . import __version__


PHASE_PROGRESS = {
    "clone": 10,
    "index": 25,
    "secrets": 40,
    "deps": 55,
    "ai": 80,
    "report": 95,
}
_SAFE_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_FAILED_CHECKOUT_MARKER_SUFFIX = ".failed-retain"
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")
_MIN_READY_DISK_BYTES = 1024 * 1024 * 1024
_MIN_NODE_MAJOR = 20
_CODEX_SKIP_GIT_REPO_CHECK_ARG = "--skip-git-repo-check"
_VERIFIER_HOME_DIR_NAME = ".verifier-home"
_VERIFIER_TMP_DIR_NAME = ".verifier-tmp"
_CHECKOUT_ROOT_SENTINEL_NAME = ".pullwise-checkout-root"
_CHECKOUT_RUNTIME_DIR_NAMES = {_VERIFIER_HOME_DIR_NAME, _VERIFIER_TMP_DIR_NAME}
DEFAULT_WORKER_PACKAGE_BASE_URL = "https://github.com/GoPullwise/pullwise-worker/releases/download"
SUPPORTED_REVIEW_PROVIDERS = {"codex", "opencode"}
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_CODEX_REASONING_EFFORT = "medium"
DEFAULT_OPENCODE_MODEL = "opencode/big-pickle"
DEFAULT_OPENCODE_VARIANT = "medium"
AUDIT_SWARM_PROTOCOL_VERSION = "audit-swarm/0.1"
CONVERGENCE_PROTOCOL_VERSION = "pullwise-convergence/0.1"
CONVERGENCE_MIN_VERIFIED_CONFIDENCE = 0.75
CONVERGENCE_MIN_UNVERIFIED_CONFIDENCE = 0.85
AUDIT_SWARM_EVIDENCE_BLOCK_KINDS = {
    "summary",
    "claim",
    "code_location",
    "evidence",
    "command",
    "verifier_verdict",
    "false_positive_check",
    "invariant",
    "risk",
}
CODEX_LOGIN_COMMAND = (
    "sudo -u pullwise-worker env HOME=/var/lib/pullwise-worker "
    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin "
    "codex login --device-auth"
)
_CODEX_AUTH_FAILURE_MARKERS = (
    "401 Unauthorized",
    "Failed to refresh token",
    "Please log out and sign in again",
    "access token could not be refreshed",
    "refresh token was already used",
)
_CODEX_EXEC_LOCK = Lock()
_CODEX_AUTH_FAILURE_LOCK = Lock()
_codex_auth_failure_until = 0.0
_codex_auth_failure_detail = ""


def parse_provider_chain(value: str | None, fallback: str = "codex") -> list[str]:
    raw = value if value is not None else fallback
    providers: list[str] = []
    for item in str(raw or "").split(","):
        provider = item.strip().lower()
        if provider in SUPPORTED_REVIEW_PROVIDERS and provider not in providers:
            providers.append(provider)
    return providers or ["codex"]


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in _VERIFIER_DISABLED_VALUES


def env_int(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return default


def parse_verifier_scripts(value: str | None) -> list[str]:
    raw_items = value.split(",") if value else _VERIFIER_DEFAULT_SCRIPTS
    scripts = []
    for item in raw_items:
        script = item.strip()
        if script in _PACKAGE_SCRIPT_NAMES and script not in scripts:
            scripts.append(script)
    return scripts or list(_VERIFIER_DEFAULT_SCRIPTS)


class WorkerConfig:
    def __init__(self, args: argparse.Namespace, *, require_worker_token: bool = True) -> None:
        self.server_url = (getattr(args, "server_url", None) or os.environ.get("PULLWISE_SERVER_URL") or "http://localhost:8080").rstrip("/")
        self.worker_token = getattr(args, "worker_token", None) or os.environ.get("PULLWISE_WORKER_TOKEN") or ""
        self.worker_id = getattr(args, "worker_id", None) or os.environ.get("PULLWISE_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
        self.provider = getattr(args, "provider", None) or os.environ.get("PULLWISE_PROVIDER") or "codex"
        self.provider_chain = parse_provider_chain(os.environ.get("PULLWISE_PROVIDER_CHAIN"), self.provider)
        self.max_concurrent_jobs = max(1, int(getattr(args, "max_concurrent_jobs", None) or os.environ.get("PULLWISE_MAX_CONCURRENT_JOBS") or 1))
        self.poll_seconds = max(1, int(getattr(args, "poll_seconds", None) or os.environ.get("PULLWISE_WORKER_POLL_SECONDS") or 5))
        self.poll_jitter_seconds = max(0.0, float(os.environ.get("PULLWISE_WORKER_POLL_JITTER_SECONDS") or 2))
        self.max_backoff_seconds = max(self.poll_seconds, int(os.environ.get("PULLWISE_WORKER_MAX_BACKOFF_SECONDS") or 60))
        checkout_root = getattr(args, "checkout_root", None) or os.environ.get("PULLWISE_CHECKOUT_ROOT")
        work_dir = getattr(args, "work_dir", None) or os.environ.get("PULLWISE_WORKER_WORK_DIR")
        self.work_dir = Path(checkout_root) if checkout_root else Path(work_dir or tempfile.gettempdir()) / "pullwise-worker"
        log_dir = getattr(args, "log_dir", None) or os.environ.get("PULLWISE_LOG_DIR")
        self.log_dir = Path(log_dir) if log_dir else Path(tempfile.gettempdir()) / "pullwise-worker-logs"
        self.codex_command = getattr(args, "codex_command", None) or os.environ.get("PULLWISE_CODEX_COMMAND") or "codex"
        self.codex_model = os.environ.get("PULLWISE_CODEX_MODEL", DEFAULT_CODEX_MODEL).strip() or DEFAULT_CODEX_MODEL
        self.codex_reasoning_effort = (
            os.environ.get("PULLWISE_CODEX_REASONING_EFFORT", DEFAULT_CODEX_REASONING_EFFORT).strip()
            or DEFAULT_CODEX_REASONING_EFFORT
        )
        self.opencode_command = os.environ.get("PULLWISE_OPENCODE_COMMAND", "opencode").strip() or "opencode"
        self.opencode_model = os.environ.get("PULLWISE_OPENCODE_MODEL", DEFAULT_OPENCODE_MODEL).strip() or DEFAULT_OPENCODE_MODEL
        self.opencode_variant = os.environ.get("PULLWISE_OPENCODE_VARIANT", DEFAULT_OPENCODE_VARIANT).strip() or DEFAULT_OPENCODE_VARIANT
        self.codex_timeout_seconds = max(60, int(getattr(args, "codex_timeout_seconds", None) or os.environ.get("PULLWISE_CODEX_TIMEOUT_SECONDS") or 1800))
        self.codex_doctor_timeout_seconds = max(10, int(os.environ.get("PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS") or 60))
        self.codex_auth_failure_cooldown_seconds = max(0, int(os.environ.get("PULLWISE_CODEX_AUTH_FAILURE_COOLDOWN_SECONDS") or 3600))
        self.readiness_check_seconds = max(10, int(os.environ.get("PULLWISE_READINESS_CHECK_SECONDS") or 60))
        self.result_upload_attempts = max(1, int(os.environ.get("PULLWISE_RESULT_UPLOAD_ATTEMPTS") or 5))
        self.failed_checkout_retention_seconds = max(0, int(os.environ.get("PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS") or 0))
        self.max_checkout_bytes = max(1, int(os.environ.get("PULLWISE_MAX_CHECKOUT_BYTES") or 20 * 1024 * 1024 * 1024))
        self.cleanup_interval_seconds = max(60, int(os.environ.get("PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS") or 3600))
        self.log_retention_seconds = max(0, int(os.environ.get("PULLWISE_LOG_RETENTION_SECONDS") or 14 * 24 * 60 * 60))
        self.max_log_bytes = max(1, int(os.environ.get("PULLWISE_MAX_LOG_BYTES") or 1024 * 1024 * 1024))
        self.scan_summary_log_max_bytes = max(1024, int(os.environ.get("PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES") or 10 * 1024 * 1024))
        self.verifier_enabled = env_bool("PULLWISE_WORKER_VERIFIER_ENABLED", False)
        self.verifier_install_deps = env_bool("PULLWISE_WORKER_VERIFIER_INSTALL_DEPS", True)
        self.verifier_confirm_failures = env_bool("PULLWISE_WORKER_VERIFIER_CONFIRM_FAILURES", True)
        self.verifier_host_execution_allowed = env_bool("PULLWISE_WORKER_VERIFIER_ALLOW_HOST_EXECUTION", False)
        self.verifier_timeout_seconds = max(10, int(os.environ.get("PULLWISE_WORKER_VERIFIER_TIMEOUT_SECONDS") or 120))
        self.verifier_max_commands = max(1, int(os.environ.get("PULLWISE_WORKER_VERIFIER_MAX_COMMANDS") or 5))
        self.verifier_scripts = parse_verifier_scripts(os.environ.get("PULLWISE_WORKER_VERIFIER_SCRIPTS"))
        if require_worker_token and not self.worker_token:
            raise ValueError("PULLWISE_WORKER_TOKEN is required")


class PullwiseRequestError(Exception):
    pass


class PullwiseHTTPError(PullwiseRequestError):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class PullwiseResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            parsed = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PullwiseRequestError(f"invalid JSON response: {exc}") from exc
        return parsed if isinstance(parsed, dict) else {}


class PullwiseClient:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.headers = {
            "Authorization": f"Bearer {config.worker_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post(self, path: str, payload: dict) -> PullwiseResponse:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.server_url}{path}",
            data=body,
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return PullwiseResponse(response.read())
        except urllib.error.HTTPError as exc:
            reason = getattr(exc, "reason", None) or getattr(exc, "msg", "") or "error"
            raise PullwiseHTTPError(f"HTTP {exc.code}: {reason}", exc.code) from exc
        except (OSError, TimeoutError, urllib.error.URLError) as exc:
            raise PullwiseRequestError(str(exc)) from exc


    def heartbeat(
        self,
        *,
        running_jobs: int = 0,
        last_error: str | None = None,
        doctor_status: str | None = None,
        codex_ready: bool | None = None,
        systemd_active: bool | None = None,
        doctor_checked_at: int | None = None,
    ) -> dict:
        response = self.post(
            "/worker/heartbeat",
            {
                "worker_id": self.config.worker_id,
                "version": __version__,
                "provider": self.config.provider,
                "max_concurrent_jobs": self.config.max_concurrent_jobs,
                "running_jobs": running_jobs,
                "free_slots": max(0, self.config.max_concurrent_jobs - running_jobs),
                "hostname": socket.gethostname(),
                "last_error": last_error,
                "doctor_status": doctor_status,
                "codex_ready": codex_ready,
                "systemd_active": systemd_active,
                "doctor_checked_at": doctor_checked_at,
            },
        )
        return response.json()

    def command_status(self, command_id: str, status: str, *, error: str | None = None) -> None:
        payload = {"worker_id": self.config.worker_id, "status": status}
        if error:
            payload["error"] = error
        self.post(f"/worker/commands/{command_id}/status", payload)

    def claim(self) -> dict | None:
        response = self.post(
            "/worker/jobs/claim",
            {"worker_id": self.config.worker_id, "max_jobs": self.config.max_concurrent_jobs},
        )
        return response.json().get("job")

    def claim_many(self, max_jobs: int) -> list[dict]:
        response = self.post("/worker/jobs/claim", {"worker_id": self.config.worker_id, "max_jobs": max_jobs})
        data = response.json()
        jobs = data.get("jobs")
        if isinstance(jobs, list):
            return [job for job in jobs if isinstance(job, dict)]
        job = data.get("job")
        return [job] if isinstance(job, dict) else []

    def progress(
        self,
        job_id: str,
        phase: str,
        progress: int,
        message: str = "",
        logs_summary: str = "",
        audit_swarm: dict | None = None,
    ) -> None:
        payload = {
            "phase": phase,
            "progress": progress,
            "message": message,
            "started_at": int(time.time()),
            "logs_summary": logs_summary,
        }
        if audit_swarm:
            payload["audit_swarm"] = audit_swarm
        self.post(f"/worker/jobs/{job_id}/progress", payload)

    def result(self, job_id: str, payload: dict) -> None:
        self.post(f"/worker/jobs/{job_id}/result", payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Pullwise pull worker.")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "doctor", "start", "stop", "status", "restart", "update", "uninstall", "cleanup"],
    )
    parser.add_argument("--server-url")
    parser.add_argument("--worker-id")
    parser.add_argument("--max-concurrent-jobs", type=int)
    parser.add_argument("--poll-seconds", type=int)
    parser.add_argument("--work-dir")
    parser.add_argument("--checkout-root")
    parser.add_argument("--log-dir")
    parser.add_argument("--provider")
    parser.add_argument("--codex-command")
    parser.add_argument("--codex-timeout-seconds", type=int)
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--remove-config", action="store_true")
    parser.add_argument("--remove-logs", action="store_true")
    args = parser.parse_args()

    if args.command in {"start", "stop", "status", "restart"}:
        raise SystemExit(service_action(args.command, dry_run=args.dry_run))
    if args.command == "uninstall":
        raise SystemExit(uninstall_worker(remove_config=args.remove_config, remove_logs=args.remove_logs, dry_run=args.dry_run))
    require_worker_token = args.command in {"run", "doctor"}
    try:
        config = WorkerConfig(args, require_worker_token=require_worker_token)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if args.command == "doctor":
        raise SystemExit(0 if run_doctor(config) else 1)
    if args.command == "update":
        raise SystemExit(update_worker(config, dry_run=args.dry_run))
    if args.command == "cleanup":
        cleanup_worker_resources(config)
        raise SystemExit(0)
    worker = Worker(config)
    worker.run(once=args.once)


class Worker:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.client = PullwiseClient(config)
        self.last_error: str | None = None
        self._readiness_checked_at = 0.0
        self._doctor_status = "not_ready"
        self._codex_ready = False
        self._empty_poll_count = 0
        self._error_poll_count = 0
        self._last_cleanup_at = 0.0

    def run(self, *, once: bool = False) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        self.cleanup_resources_if_due([], force=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.max_concurrent_jobs) as executor:
            running: dict[concurrent.futures.Future[None], dict] = {}
            while True:
                done = [future for future in running if future.done()]
                for future in done:
                    job = running.pop(future)
                    try:
                        future.result()
                    except Exception as exc:
                        self.last_error = f"job {job.get('job_id')} failed unexpectedly: {exc}"[:500]
                self.cleanup_resources_if_due(running.values())
                free_slots = max(0, self.config.max_concurrent_jobs - len(running))
                ready = self.refresh_readiness_if_due()
                loop_error = False
                claimed_jobs = 0
                heartbeat_payload: dict = {}
                try:
                    heartbeat_response = self.client.heartbeat(
                        running_jobs=len(running),
                        last_error=self.last_error,
                        doctor_status=self._doctor_status,
                        codex_ready=self._codex_ready,
                        doctor_checked_at=int(self._readiness_checked_at) if self._readiness_checked_at else None,
                    )
                    if isinstance(heartbeat_response, dict):
                        heartbeat_payload = heartbeat_response
                except PullwiseRequestError as exc:
                    self.last_error = f"heartbeat failed: {redact_secrets(str(exc), self.config)}"[:500]
                    loop_error = True
                worker_state = heartbeat_payload.get("worker") if isinstance(heartbeat_payload.get("worker"), dict) else {}
                command = heartbeat_payload.get("command") if isinstance(heartbeat_payload.get("command"), dict) else None
                if worker_state.get("status") == "disabled":
                    ready = False
                if command:
                    ready = False
                    if not running and not loop_error and self.handle_lifecycle_command(command):
                        return
                if ready and free_slots:
                    jobs = []
                    if not loop_error:
                        try:
                            claim_limit = 1 if once else free_slots
                            jobs = self.client.claim_many(claim_limit)
                        except PullwiseRequestError as exc:
                            self.last_error = f"job claim failed: {redact_secrets(str(exc), self.config)}"[:500]
                            loop_error = True
                    if once:
                        jobs = jobs[:1]
                    for job in jobs:
                        future = executor.submit(self.run_job, job)
                        running[future] = job
                    claimed_jobs = len(jobs)
                    if once:
                        concurrent.futures.wait(running)
                        return
                elif once:
                    concurrent.futures.wait(running)
                    return
                time.sleep(self.next_poll_sleep(claimed_jobs=claimed_jobs, loop_error=loop_error))

    def cleanup_resources_if_due(self, active_jobs: object, *, force: bool = False) -> None:
        current = time.monotonic()
        if not force and current - self._last_cleanup_at < self.config.cleanup_interval_seconds:
            return
        self._last_cleanup_at = current
        active_job_ids = set()
        for job in active_jobs or []:
            try:
                active_job_ids.add(safe_job_id(job.get("job_id") if isinstance(job, dict) else job))
            except ValueError:
                continue
        try:
            cleanup_worker_resources(self.config, active_job_ids=active_job_ids)
        except Exception as exc:
            self.last_error = f"worker cleanup failed: {redact_secrets(str(exc), self.config)}"[:500]

    def handle_lifecycle_command(self, command: dict) -> bool:
        command_id = str(command.get("id") or "").strip()
        action = str(command.get("command") or "").strip().lower()
        if not command_id or action not in {"stop", "uninstall"}:
            return False
        try:
            self.client.command_status(command_id, "running")
        except PullwiseRequestError as exc:
            self.last_error = f"command ack failed: {redact_secrets(str(exc), self.config)}"[:500]
            return False
        code = execute_lifecycle_command(action)
        if code == 0:
            try:
                self.client.command_status(command_id, "succeeded")
            except PullwiseRequestError as exc:
                self.last_error = f"command status failed: {redact_secrets(str(exc), self.config)}"[:500]
            return True
        error = f"{action} command exited {code}"
        try:
            self.client.command_status(command_id, "failed", error=error)
        except PullwiseRequestError as exc:
            self.last_error = f"command status failed: {redact_secrets(str(exc), self.config)}"[:500]
        return False

    def next_poll_sleep(self, *, claimed_jobs: int, loop_error: bool) -> float:
        if loop_error:
            self._error_poll_count += 1
            self._empty_poll_count = 0
            base = self.config.poll_seconds * (2 ** min(self._error_poll_count - 1, 6))
        elif claimed_jobs:
            self._error_poll_count = 0
            self._empty_poll_count = 0
            base = self.config.poll_seconds
        else:
            self._error_poll_count = 0
            self._empty_poll_count += 1
            base = self.config.poll_seconds * (2 ** min(self._empty_poll_count - 1, 6))
        jitter = random.uniform(0, self.config.poll_jitter_seconds) if self.config.poll_jitter_seconds else 0
        return min(self.config.max_backoff_seconds, base) + jitter

    def refresh_readiness_if_due(self) -> bool:
        current = time.time()
        if current - self._readiness_checked_at < self.config.readiness_check_seconds:
            return self._doctor_status == "ok"
        checks, _provider_ready = worker_readiness_checks(self.config)
        failed_check = first_failed_check(checks)
        self._codex_ready = readiness_check_ok(checks, "codex_ready")
        self._doctor_status = "degraded" if failed_check else "ok"
        self._readiness_checked_at = current
        self.last_error = None if failed_check is None else readiness_error_message(failed_check, self.config)
        return failed_check is None

    def upload_result_with_retry(self, job_id: str, payload: dict) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.config.result_upload_attempts + 1):
            try:
                self.client.result(job_id, payload)
                return
            except PullwiseHTTPError as exc:
                if exc.status_code < 500 or attempt >= self.config.result_upload_attempts:
                    raise
                last_error = exc
            except PullwiseRequestError as exc:
                last_error = exc
                if attempt >= self.config.result_upload_attempts:
                    raise
            if attempt < self.config.result_upload_attempts:
                time.sleep(min(30, 2 ** (attempt - 1)))
        if last_error:
            raise last_error

    def run_job(self, job: dict) -> None:
        job_id = safe_job_id(job.get("job_id"))
        attempt_id = f"{self.config.worker_id}-{job.get('attempt') or 1}"
        checkout_dir = checkout_dir_for_job(self.config.work_dir, job_id)
        started = time.monotonic()
        duration_ms = 0
        job_error = ""
        try:
            self.client.progress(
                job_id,
                "clone",
                PHASE_PROGRESS["clone"],
                "Cloning repository",
                audit_swarm=audit_swarm_scan_artifacts("clone", config=self.config, summary="Cloning repository."),
            )
            resolved_commit = clone_repository(job, checkout_dir)
            job["resolved_commit"] = resolved_commit
            job["commit"] = resolved_commit
            self.client.progress(
                job_id,
                "index",
                PHASE_PROGRESS["index"],
                "Repository ready",
                audit_swarm=audit_swarm_scan_artifacts("preflight", config=self.config, summary="Repository checkout is ready."),
            )
            preflight = collect_preflight_metadata(self.config, job, checkout_dir)
            try:
                verifier, verifier_findings, verifier_logs = run_verifier_commands(self.config, job, checkout_dir, preflight)
            except Exception as exc:
                verifier = {
                    "enabled": self.config.verifier_enabled,
                    "summary": f"Verifier failed before completing: {redact_secrets(str(exc), self.config)}"[:500],
                    "runs": [],
                }
                verifier_findings = []
                verifier_logs = verifier["summary"]
            preflight["verifier"] = verifier
            if verifier.get("enabled") and verifier.get("runs"):
                preflight["execution"] = "allowlisted_verifier_scripts"
                preflight["summary"] = (
                    "Static preflight captured repository metadata, then the verifier executed allowlisted "
                    "setup and project check commands with bounded timeouts and logs."
                )
            self.client.progress(
                job_id,
                "ai",
                PHASE_PROGRESS["ai"],
                "Running Codex review",
                verifier_logs,
                audit_swarm=audit_swarm_scan_artifacts(
                    "discovery",
                    config=self.config,
                    preflight=preflight,
                    summary="Repository preflight is complete; reviewer agents are discovering issue cards.",
                ),
            )
            audit_payload, summary, logs_summary = run_codex_review(self.config, job, checkout_dir)
            ai_usage = normalize_ai_usage(audit_payload.get("ai_usage"))
            if verifier_findings:
                audit_payload = merge_audit_swarm_payloads(
                    audit_swarm_payload_from_findings(verifier_findings, verifier_role="verifier"),
                    audit_payload,
                )
            projected_findings = audit_swarm_findings_from_payload(audit_payload) or []
            candidate_count = len(projected_findings)
            projected_findings, rejected_reasons, rejected_samples = filter_reportable_findings(projected_findings)
            projected_findings, convergence_rejected_reasons, convergence_rejected_samples, convergence_state = (
                apply_convergence_gate(job, checkout_dir, projected_findings)
            )
            for reason, count in convergence_rejected_reasons.items():
                rejected_reasons[reason] = rejected_reasons.get(reason, 0) + count
            rejected_samples = [*rejected_samples, *convergence_rejected_samples][:5]
            audit_payload = filter_audit_swarm_payload_by_findings(audit_payload, projected_findings)
            summary = summarize(projected_findings)
            verification_audit = verification_audit_payload(
                candidate_count=candidate_count,
                reported_findings=projected_findings,
                rejected_reasons=rejected_reasons,
                rejected_samples=rejected_samples,
            )
            if verifier_logs:
                logs_summary = "\n".join([verifier_logs, logs_summary])[-1000:]
            duration_ms = int((time.monotonic() - started) * 1000)
            audit_swarm = audit_swarm_scan_artifacts(
                "report",
                config=self.config,
                audit_payload=audit_payload,
                preflight=preflight,
                verification_audit=verification_audit,
                summary=verification_audit.get("summary") or "Audit Swarm result is ready.",
                logs_summary=logs_summary,
            )
            payload = {
                "status": "done",
                "commit": resolved_commit,
                "resolved_commit": resolved_commit,
                "audit_protocol": audit_payload.get("audit_protocol") or AUDIT_SWARM_PROTOCOL_VERSION,
                "issue_cards": audit_payload.get("issue_cards") if isinstance(audit_payload.get("issue_cards"), list) else [],
                "verification_results": (
                    audit_payload.get("verification_results")
                    if isinstance(audit_payload.get("verification_results"), list)
                    else []
                ),
                "summary": summary,
                "duration_ms": duration_ms,
                "attempt_id": attempt_id,
                "preflight": preflight,
                "verification_audit": verification_audit,
                "convergence_state": convergence_state,
                "audit_swarm": audit_swarm,
            }
            if ai_usage:
                payload["ai_usage"] = ai_usage
            payload["result_checksum"] = result_checksum(payload)
            self.client.progress(job_id, "report", 100, "Uploading result", logs_summary, audit_swarm=audit_swarm)
            try:
                self.upload_result_with_retry(job_id, payload)
            except Exception as exc:
                self.last_error = f"result upload failed for {job_id}: {redact_secrets(str(exc), self.config)}"[:500]
                job_error = self.last_error
                write_scan_summary(self.config, job_id, "upload_failed", duration_ms, self.last_error)
                return
            write_scan_summary(self.config, job_id, "done", duration_ms, "")
            self.last_error = None
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            error = redact_secrets(str(exc)[:500], self.config)
            error_payload = {
                "status": "failed",
                "audit_protocol": AUDIT_SWARM_PROTOCOL_VERSION,
                "issue_cards": [],
                "verification_results": [],
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "duration_ms": duration_ms,
                "error": error,
                "attempt_id": attempt_id,
                "audit_swarm": audit_swarm_scan_artifacts("failed", config=self.config, summary=error),
            }
            if job.get("resolved_commit"):
                error_payload["commit"] = job["resolved_commit"]
                error_payload["resolved_commit"] = job["resolved_commit"]
            error_payload["result_checksum"] = result_checksum(error_payload)
            try:
                self.upload_result_with_retry(job_id, error_payload)
            except Exception as upload_exc:
                self.last_error = f"failed result upload failed for {job_id}: {redact_secrets(str(upload_exc), self.config)}"[:500]
                job_error = self.last_error
                write_scan_summary(self.config, job_id, "upload_failed", duration_ms, self.last_error)
                return
            write_scan_summary(self.config, job_id, "failed", duration_ms, error)
            self.last_error = error
            job_error = error
        finally:
            if job_error and self.config.failed_checkout_retention_seconds > 0:
                marker = failed_checkout_marker(checkout_dir)
                marker.write_text(str(int(time.time()) + self.config.failed_checkout_retention_seconds), encoding="utf-8")
            else:
                shutil.rmtree(checkout_dir, ignore_errors=True)


def safe_job_id(value: object) -> str:
    job_id = str(value or "").strip()
    if not job_id or job_id in {".", ".."} or not _SAFE_JOB_ID_RE.match(job_id):
        raise ValueError("job_id contains unsafe path characters")
    return job_id


def checkout_dir_for_job(work_dir: Path, job_id: str) -> Path:
    root = work_dir.resolve(strict=False)
    checkout_dir = (work_dir / safe_job_id(job_id)).resolve(strict=False)
    try:
        common = os.path.commonpath([str(root), str(checkout_dir)])
    except ValueError as exc:
        raise ValueError("job checkout directory must stay inside work_dir") from exc
    if os.path.normcase(common) != os.path.normcase(str(root)) or checkout_dir == root:
        raise ValueError("job checkout directory must stay inside work_dir")
    return checkout_dir


def failed_checkout_marker(checkout_dir: Path) -> Path:
    return checkout_dir.parent / f"{checkout_dir.name}{_FAILED_CHECKOUT_MARKER_SUFFIX}"


def checkout_dir_from_failed_marker(marker: Path) -> Path:
    name = marker.name
    if not name.endswith(_FAILED_CHECKOUT_MARKER_SUFFIX):
        return marker.with_suffix("")
    return marker.parent / name[: -len(_FAILED_CHECKOUT_MARKER_SUFFIX)]


def checkout_root_sentinel(work_dir: Path) -> Path:
    return work_dir / _CHECKOUT_ROOT_SENTINEL_NAME


def checkout_root_is_owned(work_dir: Path) -> bool:
    sentinel = checkout_root_sentinel(work_dir)
    if sentinel.is_file():
        return True
    entries = [path for path in work_dir.iterdir() if path.name not in _CHECKOUT_RUNTIME_DIR_NAMES]
    if entries:
        return False
    try:
        sentinel.write_text("pullwise-worker checkout root\n", encoding="utf-8")
    except OSError:
        return False
    return True


def clone_repository(job: dict, checkout_dir: Path) -> str:
    shutil.rmtree(checkout_dir, ignore_errors=True)
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_url = str(job.get("clone_url") or "")
    if not clone_url:
        repo = str(job.get("repo") or "")
        clone_url = f"https://github.com/{repo}.git"
    git_env = git_auth_env(job.get("clone_token"))
    commit = str(job.get("commit") or "pending")
    clone_command = ["git", "clone"]
    if not commit or commit == "pending":
        clone_command.extend(["--depth", "1"])
    clone_command.extend(["--branch", str(job.get("branch") or "main"), clone_url, str(checkout_dir)])
    run_git_command(clone_command, phase="clone", env=git_env)
    if commit and commit != "pending":
        run_git_command(
            ["git", "-C", str(checkout_dir), "checkout", commit],
            phase="checkout",
        )
    return resolve_git_head(checkout_dir)


def resolve_git_head(checkout_dir: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout_dir), "rev-parse", "HEAD"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(git_error_message("rev-parse", exc)) from exc
    commit = (completed.stdout or "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise RuntimeError("git rev-parse HEAD did not return a commit SHA")
    return commit.lower()


def run_git_command(command: list[str], *, phase: str, env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600),
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {phase} timed out after {env_int('PULLWISE_GIT_TIMEOUT_SECONDS', 600)}s") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(git_error_message(phase, exc)) from exc


def git_error_message(phase: str, exc: subprocess.CalledProcessError) -> str:
    output = "\n".join(part for part in (exc.stderr, exc.stdout) if part)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    summary = " ".join(lines[:3])[:400]
    if not summary:
        summary = f"git exited with status {exc.returncode}"
    return f"git {phase} failed: {summary}"


def clone_token_value(clone_token: object) -> str:
    token = clone_token.get("token") if isinstance(clone_token, dict) else None
    return str(token or "")


def git_auth_env(clone_token: object) -> dict[str, str] | None:
    token = clone_token_value(clone_token)
    if not token:
        return None
    basic = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    env = os.environ.copy()
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
    )
    return env


_README_PACKAGE_SCRIPT_RE = re.compile(r"\b(npm|pnpm|yarn|bun)\s+run\s+([A-Za-z0-9:_-]+)\b")
_HIGH_SIGNAL_PACKAGE_SCRIPTS = {"dev", "start", "build", "test"}
_PACKAGE_SCRIPT_NAMES = ["dev", "start", "build", "test", "lint", "typecheck", "check"]
_VERIFIER_DEFAULT_SCRIPTS = ["build", "test", "lint", "typecheck", "check"]
_VERIFIER_DISABLED_VALUES = {"", "0", "false", "no", "off"}
_VERIFIER_MAX_OUTPUT_CHARS = 4000
_VERIFIER_OUTPUT_WITHHELD = "Verifier stdout/stderr is withheld from API responses and audit bundles."
_VERIFIER_ENV_PASSTHROUGH_KEYS = (
    "PATH",
    "Path",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "ComSpec",
    "PATHEXT",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)
_VERIFICATION_STATUSES = {"verified", "static_proof", "potential_risk", "unverified"}
_LOCKFILE_PACKAGE_MANAGERS = {
    "bun.lock": "bun",
    "bun.lockb": "bun",
    "package-lock.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
}
_MANIFEST_TYPES = {
    "Cargo.toml": "rust",
    "Pipfile": "python",
    "go.mod": "go",
    "package.json": "node",
    "poetry.lock": "python-lock",
    "pyproject.toml": "python",
    "requirements.txt": "python",
}
_CONFIG_MANIFEST_TYPES = {
    ".devcontainer/devcontainer.json": "devcontainer",
    "compose.yaml": "docker-compose",
    "compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
    "docker-compose.yml": "docker-compose",
    "Dockerfile": "dockerfile",
}
_DOCKERFILE_SCAN_MAX_FILES = 50
_DOCKERFILE_SKIP_DIRS = {
    ".git",
    "docs",
    "examples",
    "fixtures",
    "node_modules",
    "test",
    "tests",
    "vendor",
    "__tests__",
}
_SECRET_SCAN_MAX_BYTES = 256 * 1024
_SECRET_SCAN_MAX_FILES = 3000
_SECRET_SCAN_SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".tox",
    ".venv",
    "build",
    "coverage",
    "dist",
    "docs",
    "examples",
    "fixtures",
    "node_modules",
    "target",
    "test",
    "tests",
    "vendor",
    "venv",
    "__pycache__",
    "__tests__",
}
_SECRET_SCAN_SKIP_FILES = {
    "bun.lock",
    "bun.lockb",
    "Cargo.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "yarn.lock",
}
_SECRET_SCAN_TEXT_SUFFIXES = {
    ".bash",
    ".conf",
    ".config",
    ".cs",
    ".env",
    ".go",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".php",
    ".properties",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
    ".zsh",
}
_SECRET_PATTERNS = [
    {
        "kind": "github_token",
        "label": "GitHub token",
        "regex": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{36,255}\b"),
    },
    {
        "kind": "stripe_live_secret_key",
        "label": "Stripe live secret key",
        "regex": re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b"),
    },
    {
        "kind": "slack_token",
        "label": "Slack token",
        "regex": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    },
]


def collect_preflight_metadata(config: WorkerConfig, job: dict, checkout_dir: Path) -> dict:
    repository = repository_preflight_metadata(checkout_dir)
    tool_versions = worker_tool_versions(config, repository["packageManagers"])
    return {
        "mode": "static",
        "execution": "no_project_scripts",
        "summary": "Static preflight captured repository manifests, worker environment, and tool versions; no project scripts were executed.",
        "repo": str(job.get("repo") or ""),
        "branch": str(job.get("branch") or "main"),
        "commit": str(job.get("commit") or "pending"),
        "workerVersion": __version__,
        "providerChain": list(config.provider_chain),
        "environment": worker_environment_metadata(checkout_dir),
        "languages": repository["languages"],
        "packageManagers": repository["packageManagers"],
        "manifests": repository["manifests"],
        "availableScripts": repository["availableScripts"],
        "toolVersions": tool_versions,
        "limitations": [
            "Dependency installation, build, tests, lint, and typecheck were not executed in this preflight.",
            "Runtime verification requires a later sandboxed verifier stage with project dependencies available.",
        ],
    }


def worker_environment_metadata(checkout_dir: Path) -> dict:
    return {
        "os": platform.system(),
        "osRelease": platform.release(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "pythonVersion": platform.python_version(),
        "pythonExecutable": sys.executable,
        "checkoutRoot": str(checkout_dir),
    }


def repository_preflight_metadata(checkout_dir: Path) -> dict:
    manifests = repository_manifests(checkout_dir)
    package_json = read_package_json(checkout_dir / "package.json")
    scripts = package_json.get("scripts") if isinstance(package_json.get("scripts"), dict) else {}
    available_scripts = sorted(
        script for script in _PACKAGE_SCRIPT_NAMES if isinstance(scripts, dict) and script in scripts
    )
    package_managers = package_managers_for_repository(checkout_dir, package_json)
    languages = language_hints_for_repository(checkout_dir, manifests)
    return {
        "languages": languages,
        "packageManagers": package_managers,
        "manifests": manifests,
        "availableScripts": available_scripts,
    }


def repository_manifests(checkout_dir: Path) -> list[dict]:
    manifests: list[dict] = []
    for filename, manifest_type in sorted(_MANIFEST_TYPES.items()):
        path = checkout_dir / filename
        if path.is_file():
            manifests.append({"file": filename, "type": manifest_type})
    for filename, manifest_type in sorted(_CONFIG_MANIFEST_TYPES.items()):
        path = checkout_dir / filename
        if path.is_file():
            manifests.append({"file": filename, "type": manifest_type})
    for filename, manager in sorted(_LOCKFILE_PACKAGE_MANAGERS.items()):
        path = checkout_dir / filename
        if path.is_file():
            manifests.append({"file": filename, "type": f"{manager}-lock"})
    manifests.extend(github_actions_workflow_manifests(checkout_dir))
    manifests.extend(dockerfile_manifests(checkout_dir))
    return dedupe_manifests(manifests)[:50]


def github_actions_workflow_manifests(checkout_dir: Path) -> list[dict]:
    workflows_dir = checkout_dir / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return []
    manifests = []
    for path in sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")]):
        if path.is_file():
            manifests.append({"file": path.relative_to(checkout_dir).as_posix(), "type": "github-actions-workflow"})
    return manifests[:20]


def dockerfile_manifests(checkout_dir: Path) -> list[dict]:
    manifests = []
    for path in iter_dockerfiles(checkout_dir):
        manifests.append({"file": path.relative_to(checkout_dir).as_posix(), "type": "dockerfile"})
    return manifests[:20]


def dedupe_manifests(manifests: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for manifest in manifests:
        key = (manifest.get("file"), manifest.get("type"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(manifest)
    return deduped


def read_package_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def package_managers_for_repository(checkout_dir: Path, package_json: dict) -> list[str]:
    managers = []
    package_manager = str(package_json.get("packageManager") or "").strip()
    if package_manager:
        managers.append(package_manager.split("@", 1)[0])
    for filename, manager in sorted(_LOCKFILE_PACKAGE_MANAGERS.items()):
        if (checkout_dir / filename).is_file():
            managers.append(manager)
    if (checkout_dir / "package.json").is_file() and not managers:
        managers.append("npm")
    return list(dict.fromkeys(manager for manager in managers if manager))


def language_hints_for_repository(checkout_dir: Path, manifests: list[dict]) -> list[str]:
    hints = []
    manifest_types = {item.get("type") for item in manifests}
    if "node" in manifest_types:
        hints.append("JavaScript/TypeScript")
    if {"python", "python-lock"} & manifest_types:
        hints.append("Python")
    if "go" in manifest_types:
        hints.append("Go")
    if "rust" in manifest_types:
        hints.append("Rust")
    extension_hints = [
        ("*.ts", "TypeScript"),
        ("*.tsx", "TypeScript"),
        ("*.js", "JavaScript"),
        ("*.jsx", "JavaScript"),
        ("*.py", "Python"),
        ("*.go", "Go"),
        ("*.rs", "Rust"),
    ]
    for pattern, label in extension_hints:
        if label not in hints and any(checkout_dir.glob(pattern)):
            hints.append(label)
    return hints[:8]


def worker_tool_versions(config: WorkerConfig, package_managers: list[str] | None = None) -> list[dict]:
    checks = [
        ("git", ["git", "--version"]),
        ("node", ["node", "--version"]),
        ("python", [sys.executable, "--version"]),
    ]
    for package_manager in package_managers or []:
        if package_manager in {"npm", "pnpm", "yarn", "bun"}:
            checks.append((package_manager, [package_manager, "--version"]))
    if "codex" in config.provider_chain:
        checks.append(("codex", [config.codex_command, "--version"]))
    if "opencode" in config.provider_chain:
        checks.append(("opencode", [config.opencode_command, "--version"]))
    return [safe_tool_version(name, command) for name, command in checks]


def safe_tool_version(name: str, command: list[str]) -> dict:
    command_text = " ".join(command)
    try:
        completed = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "name": name,
            "command": command_text,
            "available": False,
            "exitCode": 127,
            "output": str(exc)[:200],
        }
    output = " ".join(part.strip() for part in (completed.stdout, completed.stderr) if part and part.strip())
    return {
        "name": name,
        "command": command_text,
        "available": completed.returncode == 0,
        "exitCode": completed.returncode,
        "output": output[:200],
    }


def run_verifier_commands(
    config: WorkerConfig,
    job: dict,
    checkout_dir: Path,
    preflight: dict,
) -> tuple[dict, list[dict], str]:
    if not config.verifier_enabled:
        return (
            {
                "enabled": False,
                "summary": "Verifier command execution is disabled for this worker.",
                "runs": [],
            },
            [],
            "verifier disabled",
        )

    if not config.verifier_host_execution_allowed:
        return (
            {
                "enabled": True,
                "summary": (
                    "Verifier command execution is enabled, but host execution is not allowed. "
                    "Run verifier commands only inside an external sandbox or set "
                    "PULLWISE_WORKER_VERIFIER_ALLOW_HOST_EXECUTION=true for trusted hosts."
                ),
                "runs": [],
            },
            [],
            "verifier host execution disabled",
        )

    package_managers = preflight.get("packageManagers") if isinstance(preflight.get("packageManagers"), list) else []
    package_manager = str(package_managers[0] if package_managers else "npm")
    available_scripts = preflight.get("availableScripts") if isinstance(preflight.get("availableScripts"), list) else []
    scripts = [script for script in config.verifier_scripts if script in available_scripts][: config.verifier_max_commands]
    install_command = package_install_command(package_manager, checkout_dir) if config.verifier_install_deps else []
    if not scripts and not install_command:
        return (
            {
                "enabled": True,
                "summary": "Verifier enabled, but no dependency install command or allowlisted package scripts were present.",
                "runs": [],
            },
            [],
            "verifier no runnable scripts",
        )

    runs = []
    if install_command:
        install_run = execute_verifier_command(
            config,
            job,
            checkout_dir,
            "install-deps",
            install_command,
        )
        runs.append(install_run)
        if install_run.get("status") not in {"passed", "flaky"}:
            findings = verifier_findings_from_results(job, checkout_dir, runs)
            summary = verifier_runs_summary(runs)
            return (
                {
                    "enabled": True,
                    "summary": summary,
                    "runs": runs,
                },
                findings,
                summary,
            )
    runs.extend(
        execute_verifier_script(config, job, checkout_dir, package_manager, script)
        for script in scripts
    )
    findings = verifier_findings_from_results(job, checkout_dir, runs)
    summary = verifier_runs_summary(runs)
    return (
        {
            "enabled": True,
            "summary": summary,
            "runs": runs,
        },
        findings,
        summary,
    )


def verifier_runs_summary(runs: list[dict]) -> str:
    failed_count = len([run for run in runs if run.get("status") == "failed"])
    flaky_count = len([run for run in runs if run.get("status") == "flaky"])
    passed_count = len([run for run in runs if run.get("status") == "passed"])
    skipped_count = len([run for run in runs if run.get("status") in {"skipped", "timeout"}])
    return (
        f"Verifier ran {len(runs)} allowlisted command(s): {passed_count} passed, "
        f"{failed_count} failed, {flaky_count} flaky, {skipped_count} skipped or timed out."
    )


def execute_verifier_script(
    config: WorkerConfig,
    job: dict,
    checkout_dir: Path,
    package_manager: str,
    script: str,
) -> dict:
    command = package_script_command(package_manager, script)
    return execute_verifier_command(config, job, checkout_dir, script, command)


def execute_verifier_command(
    config: WorkerConfig,
    job: dict,
    checkout_dir: Path,
    label: str,
    command: list[str],
) -> dict:
    started = time.monotonic()
    log_path, public_log_path = verifier_log_path(config, job, label)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = verifier_command_env(checkout_dir)
    attempts = [execute_verifier_attempt(config, checkout_dir, command, env, 1)]
    if attempts[0].get("status") == "failed" and config.verifier_confirm_failures:
        attempts.append(execute_verifier_attempt(config, checkout_dir, command, env, 2))

    confirmed_failure = attempts[0].get("status") == "failed" and (
        not config.verifier_confirm_failures
        or (len(attempts) > 1 and attempts[1].get("status") == "failed")
    )
    if confirmed_failure:
        status = "failed"
        result_attempt = attempts[-1]
    elif attempts[0].get("status") == "failed":
        status = "flaky"
        result_attempt = attempts[0]
    else:
        status = str(attempts[0].get("status") or "skipped")
        result_attempt = attempts[0]

    output = verifier_attempts_output(attempts)
    log_path.write_text(output, encoding="utf-8")
    public_attempts = [verifier_public_attempt(attempt) for attempt in attempts]
    return {
        "script": label,
        "command": " ".join(command),
        "status": status,
        "exitCode": int(result_attempt.get("exitCode") or 0),
        "durationMs": int((time.monotonic() - started) * 1000),
        "logPath": public_log_path,
        "outputRedacted": bool(output),
        "outputSummary": _VERIFIER_OUTPUT_WITHHELD if output else "",
        "attempts": public_attempts,
        "confirmedFailure": bool(confirmed_failure),
    }


def verifier_command_env(checkout_dir: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _VERIFIER_ENV_PASSTHROUGH_KEYS:
        if key in os.environ and os.environ[key]:
            env[key] = os.environ[key]
    sandbox_root = checkout_dir.parent
    home_dir = sandbox_root / _VERIFIER_HOME_DIR_NAME
    tmp_dir = sandbox_root / _VERIFIER_TMP_DIR_NAME
    home_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "CI": "true",
            "PULLWISE_VERIFY": "1",
            "NO_COLOR": "1",
            "HOME": str(home_dir),
            "USERPROFILE": str(home_dir),
            "TMPDIR": str(tmp_dir),
            "TMP": str(tmp_dir),
            "TEMP": str(tmp_dir),
        }
    )
    return env


def verifier_public_attempt(attempt: dict) -> dict:
    public = {
        "attempt": attempt.get("attempt"),
        "status": attempt.get("status"),
        "exitCode": attempt.get("exitCode"),
        "durationMs": attempt.get("durationMs"),
    }
    if attempt.get("output"):
        public["outputRedacted"] = True
        public["outputSummary"] = _VERIFIER_OUTPUT_WITHHELD
    return public


def execute_verifier_attempt(
    config: WorkerConfig,
    checkout_dir: Path,
    command: list[str],
    env: dict[str, str],
    attempt: int,
) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(checkout_dir),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.verifier_timeout_seconds,
            env=env,
        )
        output = verifier_output_text(completed.stdout, completed.stderr)
        status = "passed" if completed.returncode == 0 else "failed"
        return {
            "attempt": attempt,
            "status": status,
            "exitCode": completed.returncode,
            "durationMs": int((time.monotonic() - started) * 1000),
            "output": output[-_VERIFIER_MAX_OUTPUT_CHARS:],
        }
    except FileNotFoundError as exc:
        output = str(exc)
        return {
            "attempt": attempt,
            "status": "skipped",
            "exitCode": 127,
            "durationMs": int((time.monotonic() - started) * 1000),
            "output": output,
        }
    except subprocess.TimeoutExpired as exc:
        output = verifier_output_text(exc.stdout, exc.stderr) or f"Command timed out after {config.verifier_timeout_seconds}s."
        return {
            "attempt": attempt,
            "status": "timeout",
            "exitCode": 124,
            "durationMs": int((time.monotonic() - started) * 1000),
            "output": output[-_VERIFIER_MAX_OUTPUT_CHARS:],
        }


def verifier_attempts_output(attempts: list[dict]) -> str:
    if len(attempts) == 1:
        return str(attempts[0].get("output") or "")[-_VERIFIER_MAX_OUTPUT_CHARS:]
    parts = []
    for attempt in attempts:
        parts.append(
            f"--- attempt {attempt.get('attempt')} "
            f"({attempt.get('status')} exit {attempt.get('exitCode')}) ---"
        )
        output = str(attempt.get("output") or "").rstrip()
        if output:
            parts.append(output)
    return "\n".join(parts)[-_VERIFIER_MAX_OUTPUT_CHARS:]


def package_script_command(package_manager: str, script: str) -> list[str]:
    manager = package_manager if package_manager in {"npm", "pnpm", "yarn", "bun"} else "npm"
    return [manager, "run", script]


def package_install_command(package_manager: str, checkout_dir: Path) -> list[str]:
    if not (checkout_dir / "package.json").is_file():
        return []
    manager = package_manager if package_manager in {"npm", "pnpm", "yarn", "bun"} else "npm"
    if manager == "npm":
        command = ["npm", "ci"] if (checkout_dir / "package-lock.json").is_file() else ["npm", "install"]
        return [*command, "--ignore-scripts"]
    if manager == "pnpm":
        command = ["pnpm", "install"]
        if (checkout_dir / "pnpm-lock.yaml").is_file():
            command.append("--frozen-lockfile")
        return [*command, "--ignore-scripts"]
    if manager == "yarn":
        command = ["yarn", "install"]
        if (checkout_dir / "yarn.lock").is_file():
            command.append("--frozen-lockfile")
        return [*command, "--ignore-scripts"]
    if manager == "bun":
        command = ["bun", "install"]
        if (checkout_dir / "bun.lock").is_file() or (checkout_dir / "bun.lockb").is_file():
            command.append("--frozen-lockfile")
        return [*command, "--ignore-scripts"]
    return []


def verifier_log_path(config: WorkerConfig, job: dict, script: str) -> tuple[Path, str]:
    job_id = safe_job_id(job.get("job_id") or job.get("scan_id") or "scan")
    safe_script = re.sub(r"[^A-Za-z0-9_.-]+", "_", script).strip("._") or "script"
    relative = Path("verification") / job_id / f"{safe_script}.log"
    return config.log_dir / relative, relative.as_posix()


def verifier_output_text(stdout: object, stderr: object) -> str:
    parts = []
    for value in (stdout, stderr):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str) and value:
            parts.append(value)
    output = "\n".join(parts).replace("\x00", "")
    return output[-_VERIFIER_MAX_OUTPUT_CHARS:]


def verifier_findings_from_results(job: dict, checkout_dir: Path, runs: list[dict]) -> list[dict]:
    findings = []
    for run in runs:
        if run.get("status") != "failed":
            continue
        if run.get("confirmedFailure") is False:
            continue
        findings.append(verifier_command_failure_finding(job, checkout_dir, run))
    return findings


def verifier_command_failure_finding(job: dict, checkout_dir: Path, run: dict) -> dict:
    script = str(run.get("script") or "script")
    command = str(run.get("command") or "")
    exit_code = int(run.get("exitCode") or 1)
    log_path = str(run.get("logPath") or "")
    is_install = script == "install-deps"
    location_file = install_evidence_file(checkout_dir) if is_install else "package.json"
    line = 1 if is_install else package_script_line(checkout_dir, script)
    title = f"`{command}` fails during dependency installation" if is_install else f"`{command}` fails in verifier"
    severity = "high" if script in {"build", "test", "typecheck", "check", "install-deps"} else "medium"
    category = "Dependencies" if is_install else ("Tests" if script == "test" else "Quality")
    digest_input = f"{job.get('repo')}:{job.get('commit')}:{script}:{exit_code}:{log_path}".encode("utf-8")
    finding_id = f"verified_command_failure_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    code_label = "dependency manifest" if is_install else "package.json script"
    code_summary = (
        f"The verifier installs dependencies from `{location_file}` before running project checks."
        if is_install
        else f"The failing verifier command maps to the `{script}` script in package.json."
    )
    attempts = run.get("attempts") if isinstance(run.get("attempts"), list) else []
    attempt_count = len(attempts) or 1
    confirmed_text = " on two consecutive attempts" if attempt_count > 1 else ""
    impact = (
        "The project dependencies could not be installed in the verifier environment, so build/test reproduction is blocked before application checks run."
        if is_install
        else "The documented or standard project quality gate currently fails in the verifier environment."
    )
    limitation = (
        "Private registries, missing package manager credentials, or network restrictions can make dependency installation fail outside production."
        if is_install
        else "Private services or environment variables absent from the worker can make this fail outside production."
    )
    confidence_rationale = (
        "The command failure is directly observed and confirmed by a repeated verifier attempt; "
        "production impact depends on environment parity."
        if attempt_count > 1
        else "The command failure is directly observed; production impact depends on environment parity."
    )
    return {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "title": title[:120],
        "summary": f"The verifier ran `{command}` and it exited with code {exit_code}{confirmed_text}.",
        "impact": impact,
        "detectionReasoning": (
            "This finding comes from an executed allowlisted verifier command, not from model inference. "
            f"The command returned exit code {exit_code}{confirmed_text}; stdout/stderr is retained only in the worker-local log."
        ),
        "reproductionPath": f"At commit `{job.get('commit') or 'pending'}`, run `{command}` from the repository root.",
        "verificationStatus": "verified",
        "verificationSummary": (
            f"`{command}` was executed by the worker verifier and failed with exit code {exit_code}{confirmed_text}."
        ),
        "affectedLocations": [{"file": location_file, "startLine": line, "endLine": line}],
        "evidence": [
            {
                "type": "runtime_log",
                "label": "Verifier command output",
                "summary": f"`{command}` exited {exit_code}. Raw stdout/stderr is withheld from API and audit bundle payloads.",
                "file": "",
                "startLine": 0,
                "endLine": 0,
                "command": command,
                "exitCode": exit_code,
                "logPath": log_path,
                "outputRedacted": True,
                "url": "",
            },
            {
                "type": "code",
                "label": code_label,
                "summary": code_summary,
                "file": location_file,
                "startLine": line,
                "endLine": line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
        ],
        "reproduction": {
            "commands": [command],
            "input": f"Run `{command}` at repository root.",
            "expected": "Command exits 0.",
            "actual": f"Command exited {exit_code}; stdout/stderr is withheld from shared payloads.",
            "testFile": "",
            "logPath": log_path,
        },
        "whyNotFalsePositive": [
            (
                "The command was executed by the worker verifier and returned a non-zero exit code on two consecutive attempts."
                if attempt_count > 1
                else "The command was executed by the worker verifier and returned a non-zero exit code."
            ),
            (
                "Dependency installation is an explicit verifier setup step before running project checks."
                if is_install
                else "Only allowlisted project scripts are promoted to verifier findings."
            ),
        ],
        "limitations": [
            (
                "Dependency installation uses an allowlisted package-manager command with install scripts disabled."
                if is_install
                else "The verifier runs only allowlisted project scripts after any configured setup step."
            ),
            limitation,
        ],
        "file": location_file,
        "line": line,
        "confidence": 0.95,
        "confidenceRationale": confidence_rationale,
        "autoFix": False,
        "effort": "review required",
        "fixBenefits": (
            "Restores dependency installation so build and test reproduction can run from a clean checkout."
            if is_install
            else "Restores a failing quality gate and gives users a copyable command to verify the fix."
        ),
        "fixRisks": "The root cause may be missing verifier environment configuration rather than application code.",
        "tags": ["verified", "verifier", "command-failure", script],
        "steps": [
            f"Run `{command}` locally at the pinned commit.",
            "Inspect the captured output and fix the first failing error.",
            "Rerun the same command and the existing test suite.",
        ],
        "badCode": [],
        "goodCode": [],
        "references": [],
    }


def package_script_line(checkout_dir: Path, script: str) -> int:
    package_path = checkout_dir / "package.json"
    if not package_path.is_file():
        return 1
    try:
        package_text = package_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 1
    return first_matching_line(package_text, rf'"{re.escape(script)}"\s*:')


def install_evidence_file(checkout_dir: Path) -> str:
    for filename in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"):
        if (checkout_dir / filename).is_file():
            return filename
    return "package.json"


def run_deterministic_repository_checks(job: dict, checkout_dir: Path) -> list[dict]:
    findings: list[dict] = []
    findings.extend(readme_missing_package_script_findings(job, checkout_dir))
    findings.extend(workflow_missing_package_script_findings(job, checkout_dir))
    findings.extend(dockerfile_missing_source_findings(job, checkout_dir))
    findings.extend(committed_secret_findings(job, checkout_dir))
    return findings[:25]


def readme_missing_package_script_findings(job: dict, checkout_dir: Path) -> list[dict]:
    package_path = checkout_dir / "package.json"
    if not package_path.is_file():
        return []
    readme_path = first_existing_file(checkout_dir, ["README.md", "README.markdown", "README"])
    if readme_path is None:
        return []

    try:
        package_text = package_path.read_text(encoding="utf-8")
        package_data = json.loads(package_text)
        readme_text = readme_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []

    scripts = package_data.get("scripts") if isinstance(package_data, dict) else None
    if not isinstance(scripts, dict):
        return []
    defined_scripts = {str(name) for name in scripts if isinstance(name, str)}
    if not defined_scripts:
        return []

    readme_rel = readme_path.relative_to(checkout_dir).as_posix()
    package_rel = "package.json"
    scripts_line = first_matching_line(package_text, r'"scripts"\s*:')
    seen: set[str] = set()
    findings: list[dict] = []
    for line_number, line in enumerate(readme_text.splitlines(), start=1):
        for match in _README_PACKAGE_SCRIPT_RE.finditer(line):
            manager = match.group(1)
            script = match.group(2)
            if script in defined_scripts or script in seen:
                continue
            seen.add(script)
            findings.append(
                missing_package_script_finding(
                    job=job,
                    manager=manager,
                    script=script,
                    readme_file=readme_rel,
                    readme_line=line_number,
                    package_file=package_rel,
                    package_line=scripts_line,
                )
            )
    return findings


def workflow_missing_package_script_findings(job: dict, checkout_dir: Path) -> list[dict]:
    package_path = checkout_dir / "package.json"
    workflows_dir = checkout_dir / ".github" / "workflows"
    if not package_path.is_file() or not workflows_dir.is_dir():
        return []

    try:
        package_text = package_path.read_text(encoding="utf-8")
        package_data = json.loads(package_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []

    scripts = package_data.get("scripts") if isinstance(package_data, dict) else None
    defined_scripts = {str(name) for name in scripts if isinstance(name, str)} if isinstance(scripts, dict) else set()
    package_rel = "package.json"
    scripts_line = first_matching_line(package_text, r'"scripts"\s*:') if isinstance(scripts, dict) else 1
    seen: set[tuple[str, str]] = set()
    findings: list[dict] = []
    for workflow_path in sorted([*workflows_dir.glob("*.yml"), *workflows_dir.glob("*.yaml")]):
        try:
            workflow_text = workflow_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        workflow_rel = workflow_path.relative_to(checkout_dir).as_posix()
        for line_number, line in enumerate(workflow_text.splitlines(), start=1):
            for match in _README_PACKAGE_SCRIPT_RE.finditer(line):
                manager = match.group(1)
                script = match.group(2)
                if script in defined_scripts:
                    continue
                key = (workflow_rel, script)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    missing_workflow_package_script_finding(
                        job=job,
                        manager=manager,
                        script=script,
                        workflow_file=workflow_rel,
                        workflow_line=line_number,
                        package_file=package_rel,
                        package_line=scripts_line,
                    )
                )
                if len(findings) >= 10:
                    return findings
    return findings


def dockerfile_missing_source_findings(job: dict, checkout_dir: Path) -> list[dict]:
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for dockerfile_path in iter_dockerfiles(checkout_dir):
        try:
            dockerfile_text = dockerfile_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        dockerfile_rel = dockerfile_path.relative_to(checkout_dir).as_posix()
        for line_number, line in enumerate(dockerfile_text.splitlines(), start=1):
            parsed = dockerfile_copy_add_sources(line)
            if not parsed:
                continue
            instruction, sources = parsed
            for source in sources:
                if not docker_source_is_static_local(source):
                    continue
                if docker_source_exists(checkout_dir, source):
                    continue
                key = (dockerfile_rel, source)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(
                    missing_dockerfile_source_finding(
                        job=job,
                        dockerfile_file=dockerfile_rel,
                        dockerfile_line=line_number,
                        instruction=instruction,
                        source=source,
                    )
                )
                if len(findings) >= 10:
                    return findings
    return findings


def iter_dockerfiles(checkout_dir: Path):
    candidates = []
    root_dockerfile = checkout_dir / "Dockerfile"
    if root_dockerfile.is_file():
        candidates.append(root_dockerfile)
    candidates.extend(path for path in checkout_dir.rglob("Dockerfile") if path.is_file() and path != root_dockerfile)
    candidates.extend(path for path in checkout_dir.rglob("*.Dockerfile") if path.is_file())
    yielded = 0
    seen = set()
    for path in sorted(candidates):
        if yielded >= _DOCKERFILE_SCAN_MAX_FILES:
            return
        if path in seen or not dockerfile_scan_allowed(path, checkout_dir):
            continue
        seen.add(path)
        yielded += 1
        yield path


def dockerfile_scan_allowed(path: Path, checkout_dir: Path) -> bool:
    try:
        relative = path.relative_to(checkout_dir)
    except ValueError:
        return False
    parts = [part.lower() for part in relative.parts]
    return not any(part in _DOCKERFILE_SKIP_DIRS for part in parts[:-1])


def dockerfile_copy_add_sources(line: str) -> tuple[str, list[str]] | None:
    text = line.strip()
    if not text or text.startswith("#") or text.endswith("\\"):
        return None
    match = re.match(r"^(COPY|ADD)\s+(.+)$", text, flags=re.IGNORECASE)
    if not match:
        return None
    instruction = match.group(1).upper()
    body = dockerfile_strip_inline_comment(match.group(2).strip())
    body = dockerfile_strip_instruction_flags(body)
    if not body or "--from=" in match.group(2):
        return None
    if body.startswith("["):
        try:
            items = json.loads(body)
        except json.JSONDecodeError:
            return None
        if not isinstance(items, list) or len(items) < 2 or not all(isinstance(item, str) for item in items):
            return None
        return instruction, items[:-1]
    try:
        tokens = shlex.split(body, posix=True)
    except ValueError:
        return None
    if len(tokens) < 2:
        return None
    return instruction, tokens[:-1]


def dockerfile_strip_inline_comment(value: str) -> str:
    if " #" not in value:
        return value
    return value.split(" #", 1)[0].strip()


def dockerfile_strip_instruction_flags(value: str) -> str:
    remaining = value.strip()
    while remaining.startswith("--"):
        parts = remaining.split(maxsplit=1)
        if len(parts) < 2:
            return ""
        remaining = parts[1].strip()
    return remaining


def docker_source_is_static_local(source: str) -> bool:
    source = str(source or "").strip()
    if not source or source in {".", "./"}:
        return False
    lowered = source.lower()
    if lowered.startswith(("http://", "https://", "git://")):
        return False
    if "$" in source or any(char in source for char in "*?[]"):
        return False
    if source.startswith("/") or _WINDOWS_DRIVE_RE.match(source):
        return False
    parts = [part for part in source.replace("\\", "/").split("/") if part]
    return ".." not in parts


def docker_source_exists(checkout_dir: Path, source: str) -> bool:
    normalized = source.replace("\\", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    try:
        target = (checkout_dir / normalized).resolve(strict=False)
        root = checkout_dir.resolve(strict=False)
    except OSError:
        return False
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return target.exists()


def first_existing_file(root: Path, names: list[str]) -> Path | None:
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def first_matching_line(text: str, pattern: str) -> int:
    compiled = re.compile(pattern)
    for line_number, line in enumerate(text.splitlines(), start=1):
        if compiled.search(line):
            return line_number
    return 1


def committed_secret_findings(job: dict, checkout_dir: Path) -> list[dict]:
    findings: list[dict] = []
    seen_locations: set[tuple[str, str]] = set()
    for path in iter_secret_scan_files(checkout_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        file_path = path.relative_to(checkout_dir).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            for pattern in _SECRET_PATTERNS:
                match = pattern["regex"].search(line)
                if not match:
                    continue
                secret_value = match.group(0)
                if secret_match_is_placeholder(secret_value, line):
                    continue
                location_key = (str(pattern["kind"]), file_path)
                if location_key in seen_locations:
                    continue
                seen_locations.add(location_key)
                findings.append(
                    committed_secret_finding(
                        job=job,
                        secret_kind=str(pattern["kind"]),
                        secret_label=str(pattern["label"]),
                        safe_prefix=secret_safe_prefix(secret_value),
                        file_path=file_path,
                        line=line_number,
                    )
                )
                if len(findings) >= 10:
                    return findings
    return findings


def iter_secret_scan_files(checkout_dir: Path):
    scanned = 0
    for path in checkout_dir.rglob("*"):
        if scanned >= _SECRET_SCAN_MAX_FILES:
            return
        if not secret_scan_file_allowed(path, checkout_dir):
            continue
        scanned += 1
        yield path


def secret_scan_file_allowed(path: Path, checkout_dir: Path) -> bool:
    if not path.is_file():
        return False
    try:
        relative = path.relative_to(checkout_dir)
        size = path.stat().st_size
    except (OSError, ValueError):
        return False
    if size <= 0 or size > _SECRET_SCAN_MAX_BYTES:
        return False
    parts = [part.lower() for part in relative.parts]
    if any(part in _SECRET_SCAN_SKIP_DIRS for part in parts[:-1]):
        return False
    name = relative.name
    lower_name = name.lower()
    if name in _SECRET_SCAN_SKIP_FILES or lower_name in {item.lower() for item in _SECRET_SCAN_SKIP_FILES}:
        return False
    if any(marker in lower_name for marker in ("example", "sample", "fixture", "mock")):
        return False
    if lower_name in {"dockerfile", ".env", ".npmrc", ".pypirc"}:
        return True
    return path.suffix.lower() in _SECRET_SCAN_TEXT_SUFFIXES


def secret_match_is_placeholder(secret_value: str, line: str) -> bool:
    lowered_line = line.lower()
    if any(marker in lowered_line for marker in ("example", "sample", "dummy", "fake", "placeholder", "changeme")):
        return True
    body = re.sub(r"[^A-Za-z0-9]", "", secret_value)
    return len(set(body)) < 8


def secret_safe_prefix(secret_value: str) -> str:
    if secret_value.startswith("sk_live_"):
        return "sk_live_"
    if secret_value.startswith("xox") and "-" in secret_value:
        return secret_value.split("-", 1)[0] + "-"
    if secret_value.startswith("gh") and len(secret_value) >= 4:
        return secret_value[:4]
    return secret_value[:4]


def shell_quote(value: str) -> str:
    return "'" + str(value or "").replace("'", "'\"'\"'") + "'"


def committed_secret_finding(
    *,
    job: dict,
    secret_kind: str,
    secret_label: str,
    safe_prefix: str,
    file_path: str,
    line: int,
) -> dict:
    commit = str(job.get("commit") or "the scanned commit")
    digest_input = f"{job.get('repo')}:{commit}:{secret_kind}:{file_path}:{line}".encode("utf-8")
    finding_id = f"static_committed_secret_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    grep_command = f'git grep -n "{safe_prefix}" -- {shell_quote(file_path)}'
    return {
        "id": finding_id,
        "severity": "high",
        "category": "Security",
        "title": f"Committed {secret_label} detected",
        "summary": (
            f"`{file_path}` line {line} contains a value matching a vendor-specific {secret_label} pattern. "
            "The raw value is redacted from this report."
        ),
        "impact": (
            "A committed credential can be copied from repository history and used outside the application until it is revoked or rotated."
        ),
        "detectionReasoning": (
            f"A deterministic secret rule matched the {secret_label} prefix `{safe_prefix}` at commit `{commit}`. "
            "The scanner reports only the location and prefix so the report does not leak the full credential."
        ),
        "reproductionPath": (
            f"At commit `{commit}`, inspect `{file_path}` line {line} or run `{grep_command}` from the repository root."
        ),
        "verificationStatus": "static_proof",
        "verificationSummary": (
            "A deterministic scanner matched a high-confidence credential pattern in a repository file; no provider API validation was attempted."
        ),
        "affectedLocations": [{"file": file_path, "startLine": line, "endLine": line}],
        "evidence": [
            {
                "type": "code",
                "label": "redacted secret location",
                "summary": f"Line {line} contains a {secret_label}-shaped value with prefix `{safe_prefix}`; full value redacted.",
                "file": file_path,
                "startLine": line,
                "endLine": line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            }
        ],
        "reproduction": {
            "commands": [grep_command],
            "input": f"Inspect `{file_path}` line {line}.",
            "expected": "No live credential-like token is committed to the repository.",
            "actual": f"A {secret_label}-shaped token with prefix `{safe_prefix}` is present; full value redacted.",
            "testFile": "",
            "logPath": "",
        },
        "whyNotFalsePositive": [
            f"The value matches a vendor-specific {secret_label} token prefix and length.",
            f"The finding points to a concrete repository file and line: `{file_path}:{line}`.",
            "The scanner excludes common docs, examples, fixtures, tests, vendor directories, and lockfiles before reporting.",
        ],
        "limitations": [
            "The scanner does not call the provider API, so it cannot prove whether the credential is still active.",
            "If this was an intentionally revoked test credential, the immediate production impact may be lower.",
        ],
        "file": file_path,
        "line": line,
        "confidence": 0.95,
        "confidenceRationale": (
            "Vendor-specific live-token syntax plus an exact file and line gives high static confidence; active exploitability depends on whether the credential is still valid."
        ),
        "autoFix": False,
        "effort": "review required",
        "fixBenefits": "Removes a committed credential exposure and gives maintainers a precise location to inspect and rotate.",
        "fixRisks": "Removing the line is not enough if the secret was already exposed; rotate or revoke it with the provider.",
        "tags": ["deterministic", "static-proof", "secret", secret_kind],
        "steps": [
            f"Inspect `{file_path}` line {line} at the pinned commit.",
            "Revoke or rotate the credential in the provider console.",
            "Move the value to a secret manager or runtime environment variable.",
            "Remove the committed value and consider history cleanup if the repository was shared.",
        ],
        "badCode": [],
        "goodCode": [],
        "references": [],
    }


def missing_package_script_finding(
    *,
    job: dict,
    manager: str,
    script: str,
    readme_file: str,
    readme_line: int,
    package_file: str,
    package_line: int,
) -> dict:
    command = f"{manager} run {script}"
    severity = "medium" if script in _HIGH_SIGNAL_PACKAGE_SCRIPTS else "low"
    digest_input = f"{readme_file}:{readme_line}:{package_file}:{script}".encode("utf-8")
    finding_id = f"static_missing_script_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    repo = str(job.get("repo") or "repository")
    commit = str(job.get("commit") or "the scanned commit")
    return {
        "id": finding_id,
        "severity": severity,
        "category": "Docs",
        "title": f"README references missing package script `{script}`",
        "summary": (
            f"The README tells users to run `{command}`, but the root package.json scripts "
            f"object does not define `{script}`."
        ),
        "impact": (
            "Users following the documented setup or verification path can hit an immediate package-manager "
            "failure before the application starts."
        ),
        "detectionReasoning": (
            f"Static repository check compared `{readme_file}` with the root `{package_file}` scripts object "
            f"at commit `{commit}` and found no `{script}` entry."
        ),
        "reproductionPath": (
            f"At commit `{commit}`, inspect `{readme_file}` line {readme_line} and `{package_file}` line "
            f"{package_line}; then run `{command}` from the repository root to verify the documented command."
        ),
        "verificationStatus": "static_proof",
        "verificationSummary": (
            "The README command and package.json scripts were compared statically; no project scripts were executed."
        ),
        "affectedLocations": [
            {"file": readme_file, "startLine": readme_line, "endLine": readme_line},
            {"file": package_file, "startLine": package_line, "endLine": package_line},
        ],
        "evidence": [
            {
                "type": "documentation",
                "label": "README command",
                "summary": f"`{readme_file}` documents `{command}`.",
                "file": readme_file,
                "startLine": readme_line,
                "endLine": readme_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
            {
                "type": "code",
                "label": "package.json scripts",
                "summary": f"The root scripts object does not define `{script}`.",
                "file": package_file,
                "startLine": package_line,
                "endLine": package_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
        ],
        "reproduction": {
            "commands": [command],
            "input": f"README command in {repo}: `{command}`",
            "expected": f"`{package_file}` defines a `{script}` script or the README uses an existing command.",
            "actual": f"`{package_file}` has no `{script}` script in the root scripts object.",
            "testFile": "",
            "logPath": "",
        },
        "whyNotFalsePositive": [
            f"The command is explicitly documented in `{readme_file}`.",
            f"The root `{package_file}` scripts object was parsed as JSON and does not contain `{script}`.",
        ],
        "limitations": [
            "A monorepo package or external wrapper could provide this command outside the root package.json.",
            "The checker does not execute the command, so this is static proof of a documentation/config mismatch.",
        ],
        "file": readme_file,
        "line": readme_line,
        "confidence": 0.9,
        "confidenceRationale": (
            "High-confidence static comparison of README command text and root package.json scripts; production "
            "impact depends on whether users rely on this documented root command."
        ),
        "autoFix": False,
        "effort": "5 min",
        "fixBenefits": "Keeps documented setup and verification commands aligned with package.json.",
        "fixRisks": "Low; either add the missing script or update the README to the command the project actually supports.",
        "tags": ["deterministic", "static-proof", "docs", "package-json"],
        "steps": [
            f"Decide whether `{command}` should be supported at the repository root.",
            f"Add a `{script}` entry to `{package_file}` or change `{readme_file}` to an existing script.",
        ],
        "badCode": [],
        "goodCode": [],
        "references": [],
    }


def missing_workflow_package_script_finding(
    *,
    job: dict,
    manager: str,
    script: str,
    workflow_file: str,
    workflow_line: int,
    package_file: str,
    package_line: int,
) -> dict:
    command = f"{manager} run {script}"
    severity = "medium" if script in _HIGH_SIGNAL_PACKAGE_SCRIPTS else "low"
    digest_input = f"{workflow_file}:{workflow_line}:{package_file}:{script}".encode("utf-8")
    finding_id = f"static_ci_missing_script_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    commit = str(job.get("commit") or "the scanned commit")
    grep_command = f'git grep -n "{command}" -- {shell_quote(workflow_file)}'
    return {
        "id": finding_id,
        "severity": severity,
        "category": "CI",
        "title": f"GitHub Actions references missing package script `{script}`",
        "summary": (
            f"`{workflow_file}` runs `{command}`, but the root package.json scripts object does not define `{script}`."
        ),
        "impact": (
            "The CI workflow can fail before build or test logic runs, blocking reproducible verification for this commit."
        ),
        "detectionReasoning": (
            f"Static repository check compared `{workflow_file}` with `{package_file}` at commit `{commit}` and found "
            f"no `{script}` script."
        ),
        "reproductionPath": (
            f"At commit `{commit}`, inspect `{workflow_file}` line {workflow_line} and `{package_file}` line "
            f"{package_line}; then run `{command}` from the repository root."
        ),
        "verificationStatus": "static_proof",
        "verificationSummary": (
            "The GitHub Actions command and package.json scripts were compared statically; the workflow was not executed."
        ),
        "affectedLocations": [
            {"file": workflow_file, "startLine": workflow_line, "endLine": workflow_line},
            {"file": package_file, "startLine": package_line, "endLine": package_line},
        ],
        "evidence": [
            {
                "type": "tool",
                "label": "GitHub Actions command",
                "summary": f"`{workflow_file}` runs `{command}`.",
                "file": workflow_file,
                "startLine": workflow_line,
                "endLine": workflow_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
            {
                "type": "code",
                "label": "package.json scripts",
                "summary": f"The root scripts object does not define `{script}`.",
                "file": package_file,
                "startLine": package_line,
                "endLine": package_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
        ],
        "reproduction": {
            "commands": [command, grep_command],
            "input": f"GitHub Actions command in {workflow_file}: `{command}`",
            "expected": f"`{package_file}` defines a `{script}` script or the workflow uses an existing command.",
            "actual": f"`{package_file}` has no `{script}` script in the root scripts object.",
            "testFile": "",
            "logPath": "",
        },
        "whyNotFalsePositive": [
            f"The workflow command is explicitly present in `{workflow_file}`.",
            f"The root `{package_file}` scripts object was parsed as JSON and does not contain `{script}`.",
        ],
        "limitations": [
            "A workflow working-directory or monorepo package may intentionally run this command from another package.",
            "The checker does not execute the workflow, so this is static proof of a CI/package.json mismatch at the repository root.",
        ],
        "file": workflow_file,
        "line": workflow_line,
        "confidence": 0.9,
        "confidenceRationale": (
            "High-confidence static comparison of GitHub Actions command text and root package.json scripts; impact depends on workflow working-directory configuration."
        ),
        "autoFix": False,
        "effort": "5 min",
        "fixBenefits": "Keeps CI verification commands aligned with package.json so users and automation can reproduce checks.",
        "fixRisks": "Low; either add the missing script or update the workflow to an existing script or explicit working directory.",
        "tags": ["deterministic", "static-proof", "ci", "package-json"],
        "steps": [
            f"Decide whether `{command}` should run at the repository root.",
            f"Add a `{script}` entry to `{package_file}`, update `{workflow_file}`, or set the correct workflow working-directory.",
        ],
        "badCode": [],
        "goodCode": [],
        "references": [],
    }


def missing_dockerfile_source_finding(
    *,
    job: dict,
    dockerfile_file: str,
    dockerfile_line: int,
    instruction: str,
    source: str,
) -> dict:
    commit = str(job.get("commit") or "the scanned commit")
    build_command = f"docker build -f {shell_quote(dockerfile_file)} ."
    digest_input = f"{job.get('repo')}:{commit}:{dockerfile_file}:{dockerfile_line}:{source}".encode("utf-8")
    finding_id = f"static_docker_missing_source_{hashlib.sha1(digest_input).hexdigest()[:10]}"
    return {
        "id": finding_id,
        "severity": "medium",
        "category": "Build",
        "title": f"Dockerfile {instruction} source `{source}` is missing",
        "summary": (
            f"`{dockerfile_file}` line {dockerfile_line} uses `{instruction} {source}`, but `{source}` was not found in the repository checkout."
        ),
        "impact": (
            "A clean Docker build can fail before the application starts, blocking a reproducible environment for this commit."
        ),
        "detectionReasoning": (
            f"A deterministic Dockerfile check inspected `{dockerfile_file}` at commit `{commit}` and found a literal "
            f"repository-local `{instruction}` source path that does not exist."
        ),
        "reproductionPath": (
            f"At commit `{commit}`, inspect `{dockerfile_file}` line {dockerfile_line} and verify that `{source}` exists; "
            f"then run `{build_command}` from the repository root."
        ),
        "verificationStatus": "static_proof",
        "verificationSummary": (
            "The Dockerfile source path was checked against the repository tree; docker build was not executed."
        ),
        "affectedLocations": [{"file": dockerfile_file, "startLine": dockerfile_line, "endLine": dockerfile_line}],
        "evidence": [
            {
                "type": "code",
                "label": "Dockerfile copy source",
                "summary": f"`{instruction}` references missing repository path `{source}`.",
                "file": dockerfile_file,
                "startLine": dockerfile_line,
                "endLine": dockerfile_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
            {
                "type": "tool",
                "label": "Repository path check",
                "summary": f"The scanner looked for `{source}` in the repository root and did not find it.",
                "file": dockerfile_file,
                "startLine": dockerfile_line,
                "endLine": dockerfile_line,
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "url": "",
            },
        ],
        "reproduction": {
            "commands": [build_command],
            "input": f"Dockerfile instruction `{instruction} {source}`",
            "expected": f"`{source}` exists in the Docker build context.",
            "actual": f"`{source}` is absent from the repository checkout.",
            "testFile": "",
            "logPath": "",
        },
        "whyNotFalsePositive": [
            f"The source path `{source}` is a literal local path, not a URL, glob, variable, or multi-stage `--from` source.",
            f"The finding points to a concrete Dockerfile line: `{dockerfile_file}:{dockerfile_line}`.",
        ],
        "limitations": [
            "The check assumes `docker build` uses the repository root as build context.",
            "If the missing source is generated before docker build, this may be an environment/setup requirement rather than a committed file issue.",
        ],
        "file": dockerfile_file,
        "line": dockerfile_line,
        "confidence": 0.88,
        "confidenceRationale": (
            "A literal Dockerfile COPY/ADD source is missing from the repository tree; impact depends on build context and pre-build generation steps."
        ),
        "autoFix": False,
        "effort": "5 min",
        "fixBenefits": "Restores a Docker build path that users can reproduce from a clean checkout.",
        "fixRisks": "Low; add the missing file, correct the Dockerfile path, or document the required generation step.",
        "tags": ["deterministic", "static-proof", "dockerfile", "build"],
        "steps": [
            f"Confirm whether `{source}` should be committed or generated before Docker build.",
            f"Update `{dockerfile_file}` or add the missing source, then run `{build_command}`.",
        ],
        "badCode": [],
        "goodCode": [],
        "references": [],
    }


def run_codex_review(config: WorkerConfig, job: dict, checkout_dir: Path) -> tuple[dict, dict, str]:
    errors: list[str] = []
    try:
        deterministic_payload = audit_swarm_payload_from_findings(
            run_deterministic_repository_checks(job, checkout_dir),
            verifier_role="deterministic-check",
        )
    except Exception as exc:
        deterministic_payload = empty_audit_swarm_payload()
        errors.append(f"deterministic: {redact_secrets(str(exc), config)}"[:500])
    for provider in config.provider_chain:
        try:
            if provider == "codex":
                provider_result = run_codex_provider_review(config, job, checkout_dir)
            elif provider == "opencode":
                provider_result = run_opencode_provider_review(config, job, checkout_dir)
            else:
                raise RuntimeError(f"unsupported review provider: {provider}")
            audit_payload, _summary, logs_summary = provider_result[:3]
            ai_usage = normalize_ai_usage(provider_result[3] if len(provider_result) > 3 else {})
            audit_payload = normalize_audit_swarm_files_for_checkout(audit_payload, checkout_dir)
            audit_payload = merge_audit_swarm_payloads(deterministic_payload, audit_payload)
            if ai_usage:
                audit_payload["ai_usage"] = ai_usage
            summary = summarize(audit_swarm_findings_from_payload(audit_payload) or [])
            if errors:
                logs_summary = "\n".join([*errors, logs_summary])[-1000:]
            return audit_payload, summary, logs_summary
        except Exception as exc:
            errors.append(f"{provider}: {redact_secrets(str(exc), config)}"[:500])
    raise RuntimeError(f"all review providers failed: {'; '.join(errors)}")


def codex_auth_failure_error(config: WorkerConfig) -> str | None:
    with _CODEX_AUTH_FAILURE_LOCK:
        remaining = _codex_auth_failure_until - time.monotonic()
        detail = _codex_auth_failure_detail
    if remaining <= 0:
        return None
    clean_detail = redact_secrets(detail, config)
    return f"codex exec temporarily disabled after auth failure; retrying in {remaining:.0f}s: {clean_detail}"


def mark_codex_auth_failure(config: WorkerConfig, detail: str) -> None:
    global _codex_auth_failure_until, _codex_auth_failure_detail

    cooldown = max(0, int(config.codex_auth_failure_cooldown_seconds))
    if cooldown <= 0:
        return
    clipped = redact_secrets(str(detail or "").strip(), config)
    if len(clipped) > 500:
        clipped = clipped[-500:]
    with _CODEX_AUTH_FAILURE_LOCK:
        _codex_auth_failure_until = time.monotonic() + cooldown
        _codex_auth_failure_detail = clipped


def looks_like_codex_auth_failure(detail: str) -> bool:
    lowered = str(detail or "").lower()
    return any(marker.lower() in lowered for marker in _CODEX_AUTH_FAILURE_MARKERS)


def run_codex_provider_review(config: WorkerConfig, job: dict, checkout_dir: Path) -> tuple[dict, dict, str, dict]:
    prompt = review_prompt(job)
    with tempfile.TemporaryDirectory(prefix="pullwise-codex-") as tmpdir:
        schema_path = Path(tmpdir) / "audit-swarm.schema.json"
        output_path = Path(tmpdir) / "audit-swarm.json"
        schema_path.write_text(json.dumps(audit_swarm_output_schema()), encoding="utf-8")
        command = codex_review_command(config, str(schema_path), str(output_path), prompt)
        auth_failure = codex_auth_failure_error(config)
        if auth_failure:
            raise RuntimeError(auth_failure)
        with _CODEX_EXEC_LOCK:
            auth_failure = codex_auth_failure_error(config)
            if auth_failure:
                raise RuntimeError(auth_failure)
            completed = subprocess.run(
                command,
                cwd=str(checkout_dir),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=config.codex_timeout_seconds,
            )
        raw_logs = "\n".join([completed.stdout or "", completed.stderr or ""])
        logs_summary = redact_secrets(raw_logs[-1000:], config)
        if completed.returncode != 0:
            detail = codex_failure_detail(completed.stderr or completed.stdout, config)
            if looks_like_codex_auth_failure(detail):
                mark_codex_auth_failure(config, detail)
            raise RuntimeError(f"codex exec failed with exit code {completed.returncode}: {detail[:700]}")
        output = output_path.read_text(encoding="utf-8") if output_path.exists() else completed.stdout
    audit_payload = parse_audit_swarm_payload(output)
    return audit_payload, summarize(audit_swarm_findings_from_payload(audit_payload) or []), logs_summary, codex_ai_usage(raw_logs, config)


def codex_ai_usage(_raw_output: str, config: WorkerConfig) -> dict:
    return ai_usage_payload(config.codex_model)


def opencode_ai_usage(_raw_output: str, config: WorkerConfig) -> dict:
    return ai_usage_payload(config.opencode_model)


def ai_usage_payload(model: object) -> dict:
    clean_model = clean_protocol_text(model)
    return {"model": clean_model} if clean_model else {}


def normalize_ai_usage(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return ai_usage_payload(value.get("model") or value.get("modelName") or value.get("model_name"))


def codex_failure_detail(raw_output: str, config: WorkerConfig) -> str:
    structured = extract_codex_error_detail(raw_output)
    raw_detail = structured or (raw_output or "").strip()[-1000:] or "no stderr/stdout"
    return redact_secrets(raw_detail, config)


def extract_codex_error_detail(raw_output: str) -> str | None:
    text = raw_output or ""
    marker = "ERROR:"
    index = text.find(marker)
    decoder = json.JSONDecoder()
    while index >= 0:
        candidate = text[index + len(marker):].lstrip()
        try:
            payload, _end = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            index = text.find(marker, index + len(marker))
            continue
        if isinstance(payload, dict):
            code = payload.get("code")
            message = payload.get("message")
            error = payload.get("error")
            if not isinstance(message, str) and isinstance(error, dict):
                message = error.get("message")
                code = code or error.get("code")
            parts = [part for part in (code, message) if isinstance(part, str) and part.strip()]
            if parts:
                return ": ".join(parts)
            error_type = payload.get("type")
            if isinstance(error_type, str) and error_type.strip():
                return f"type={error_type}"
        index = text.find(marker, index + len(marker))
    return None


def codex_review_command(config: WorkerConfig, schema_path: str, output_path: str, prompt: str) -> list[str]:
    command = [
        config.codex_command,
        "exec",
        _CODEX_SKIP_GIT_REPO_CHECK_ARG,
        "--ignore-user-config",
        "--config",
        f'model_reasoning_effort="{config.codex_reasoning_effort}"',
        "--sandbox",
        "read-only",
        "--output-schema",
        schema_path,
        "--output-last-message",
        output_path,
    ]
    if config.codex_model:
        command.extend(["--model", config.codex_model])
    command.append(prompt)
    return command


def run_opencode_provider_review(config: WorkerConfig, job: dict, checkout_dir: Path) -> tuple[dict, dict, str, dict]:
    prompt = review_prompt(job)
    command = opencode_review_command(config, prompt)
    completed = subprocess.run(
        command,
        cwd=str(checkout_dir),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=config.codex_timeout_seconds,
    )
    raw_logs = completed.stderr or completed.stdout
    logs_summary = redact_secrets(raw_logs[-1000:], config)
    if completed.returncode != 0:
        raise RuntimeError(f"opencode run failed with exit code {completed.returncode}: {logs_summary[:300]}")
    audit_payload = parse_audit_swarm_payload(completed.stdout)
    return (
        audit_payload,
        summarize(audit_swarm_findings_from_payload(audit_payload) or []),
        logs_summary,
        opencode_ai_usage("\n".join([completed.stdout or "", completed.stderr or ""]), config),
    )


def opencode_review_command(config: WorkerConfig, prompt: str) -> list[str]:
    command = [config.opencode_command, "run"]
    if config.opencode_model:
        command.extend(["--model", config.opencode_model])
    if config.opencode_variant:
        command.extend(["--variant", config.opencode_variant])
    command.append(prompt)
    return command


def review_prompt(job: dict) -> str:
    convergence_context = job.get("convergence_context") if isinstance(job.get("convergence_context"), dict) else {}
    previous_head_sha = normalized_head_sha(convergence_context.get("previous_head_sha"))
    open_findings = convergence_context.get("open_findings") if isinstance(convergence_context.get("open_findings"), list) else []
    convergence_instruction = ""
    if previous_head_sha or open_findings:
        prior_refs = []
        for item in open_findings:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()[:120]
            issue_id = str(item.get("issue_id") or item.get("issueId") or "").strip()[:80]
            fingerprint = str(item.get("fingerprint") or "").strip()[:80]
            anchor = issue_id or fingerprint
            if anchor and title:
                prior_refs.append(f"{anchor} ({title})")
            elif anchor or title:
                prior_refs.append(anchor or title)
            if len(prior_refs) >= 8:
                break
        convergence_instruction = (
            " This is an incremental convergent review. First verify whether prior open findings still exist; "
            f"previous_head_sha: {previous_head_sha or 'unknown'}. "
            "Only report new issues that are directly introduced after that previous head. "
            "Do not report latent pre-existing issues, style preferences, or speculative risks. "
            "When a prior finding still exists, reuse its issue_id exactly. "
            "For new findings, choose deterministic issue_id values from the bug shape and primary path."
        )
        if prior_refs:
            convergence_instruction += f" Prior open findings to verify: {', '.join(prior_refs)}."
    return (
        "Run the Audit Swarm protocol for this repository. Return only JSON with top-level "
        "`audit_protocol`, `issue_cards`, and `verification_results`. Do not return Pullwise legacy "
        "`findings`. Each issue card is a hypothesis and must include a concrete title, severity "
        "(P0/P1/P2/P3/P4 or critical/high/medium/low/info), one or more repository-relative locations, "
        "a claim, evidence, reproduction_idea, suggested_test, and false_positive_checks. "
        "Each verification result must reference an issue_id and use verdict `confirmed`, `rejected`, "
        "or `inconclusive`; include commands_run only for commands a user can copy to verify the issue. "
        "Do not emit vague concerns. Do not include absolute worker checkout paths or server filesystem paths. "
        "If a candidate has no file/line, no evidence, and no verifiable hypothesis, omit it. "
        f"{convergence_instruction} "
        f"Repository: {job.get('repo')} branch: {job.get('branch')} commit: {job.get('commit')}."
    )


def parse_audit_swarm_payload(output: str) -> dict:
    decoder = json.JSONDecoder()
    text = output.strip()
    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    candidates.extend(line.strip() for line in text.splitlines() if line.strip().startswith(("{", "[")))
    matched: dict | None = None
    for candidate in candidates:
        try:
            parsed = decoder.decode(candidate)
        except json.JSONDecodeError:
            continue
        payload = audit_swarm_payload_from_document(parsed)
        if payload is not None:
            matched = payload
    if matched is not None:
        return matched
    raise RuntimeError("review provider did not return an Audit Swarm payload")


def audit_swarm_payload_from_document(parsed: object) -> dict | None:
    if not isinstance(parsed, dict) or "event" in parsed:
        return None
    cards = first_list(parsed, "issue_cards", "issueCards")
    if cards is None:
        return None
    results = first_list(parsed, "verification_results", "verificationResults") or []
    return {
        "audit_protocol": clean_protocol_text(
            parsed.get("audit_protocol") or parsed.get("auditProtocol") or AUDIT_SWARM_PROTOCOL_VERSION
        )
        or AUDIT_SWARM_PROTOCOL_VERSION,
        "issue_cards": [item for item in cards if isinstance(item, dict)],
        "verification_results": [item for item in results if isinstance(item, dict)],
    }


def empty_audit_swarm_payload() -> dict:
    return {
        "audit_protocol": AUDIT_SWARM_PROTOCOL_VERSION,
        "issue_cards": [],
        "verification_results": [],
    }


def merge_audit_swarm_payloads(*payloads: dict) -> dict:
    merged = empty_audit_swarm_payload()
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        protocol = clean_protocol_text(payload.get("audit_protocol") or payload.get("auditProtocol"))
        if protocol:
            merged["audit_protocol"] = protocol
        cards = first_list(payload, "issue_cards", "issueCards") or []
        results = first_list(payload, "verification_results", "verificationResults") or []
        merged["issue_cards"].extend(item for item in cards if isinstance(item, dict))
        merged["verification_results"].extend(item for item in results if isinstance(item, dict))
    return merged


def filter_audit_swarm_payload_by_findings(payload: dict, findings: list[dict]) -> dict:
    reported_ids = {clean_protocol_text(finding.get("id")) for finding in findings if isinstance(finding, dict)}
    raw_cards = first_list(payload, "issue_cards", "issueCards") or []
    filtered_cards = []
    filtered_placeholder_ids = []
    for index, card in enumerate(raw_cards):
        if not isinstance(card, dict):
            continue
        card_id = audit_swarm_resolved_issue_id(card, index)
        if card_id in reported_ids:
            next_card = dict(card)
            next_card["issue_id"] = card_id
            filtered_cards.append(next_card)
            if audit_swarm_placeholder_issue_id(audit_swarm_issue_key(card)):
                filtered_placeholder_ids.append(card_id)
    filtered_card_ids = {audit_swarm_issue_key(card) for card in filtered_cards}
    single_placeholder_id = filtered_placeholder_ids[0] if len(filtered_placeholder_ids) == 1 else ""
    filtered_results = []
    for result in first_list(payload, "verification_results", "verificationResults") or []:
        if not isinstance(result, dict):
            continue
        result_issue_id = audit_swarm_resolved_verification_issue_id(result, single_placeholder_id)
        if result_issue_id in filtered_card_ids:
            next_result = dict(result)
            next_result["issue_id"] = result_issue_id
            filtered_results.append(next_result)
    return {
        "audit_protocol": clean_protocol_text(payload.get("audit_protocol") or payload.get("auditProtocol"))
        or AUDIT_SWARM_PROTOCOL_VERSION,
        "issue_cards": filtered_cards,
        "verification_results": filtered_results,
    }


def audit_swarm_payload_from_findings(findings: list[dict], *, verifier_role: str) -> dict:
    payload = empty_audit_swarm_payload()
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            continue
        card = issue_card_from_finding(finding, index)
        payload["issue_cards"].append(card)
        payload["verification_results"].append(verification_result_from_finding(finding, card, verifier_role))
    return payload


def audit_swarm_location(file_path: str, start_line: int, end_line: int) -> dict:
    if start_line and end_line and end_line != start_line:
        lines = f"{start_line}-{end_line}"
    elif start_line:
        lines = str(start_line)
    elif end_line:
        lines = str(end_line)
    else:
        lines = ""
    return {"file": file_path, "lines": lines, "startLine": start_line, "endLine": end_line}


def issue_card_from_finding(finding: dict, index: int) -> dict:
    issue_id = clean_protocol_text(finding.get("id")) or audit_swarm_generated_id(finding, index)
    locations = []
    raw_locations = finding.get("affectedLocations") if isinstance(finding.get("affectedLocations"), list) else []
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        file_path = safe_repo_relative_file(item.get("file"))
        if file_path:
            start_line = positive_int(item.get("startLine") or item.get("line"))
            end_line = positive_int(item.get("endLine") or item.get("startLine") or item.get("line"))
            locations.append(audit_swarm_location(file_path, start_line, end_line or start_line))
    file_path = safe_repo_relative_file(finding.get("file"))
    line = positive_int(finding.get("line"))
    if file_path and not locations:
        locations.append(audit_swarm_location(file_path, line, line))
    return {
        "issue_id": issue_id,
        "shard_id": clean_protocol_text(finding.get("category")).lower() or "repository",
        "agent_role": clean_protocol_text(finding.get("agent_role") or finding.get("agentRole")) or "deterministic-reviewer",
        "title": clean_protocol_text(finding.get("title")) or f"Audit candidate {index + 1}",
        "category": clean_protocol_text(finding.get("category")) or "Quality",
        "severity": clean_protocol_text(finding.get("severity")) or "medium",
        "confidence": finding.get("confidence", 0.9),
        "locations": locations,
        "claim": protocol_multiline_text(finding.get("summary") or finding.get("claim") or finding.get("title")),
        "violated_invariants": protocol_text_list(finding.get("violated_invariants") or finding.get("violatedInvariants")),
        "evidence": [
            item if isinstance(item, dict) else protocol_multiline_text(item)
            for item in (finding.get("evidence") if isinstance(finding.get("evidence"), list) else [])
        ],
        "reproduction_idea": protocol_multiline_text(finding.get("reproductionPath")),
        "suggested_test": audit_swarm_suggested_test_from_finding(finding),
        "false_positive_checks": protocol_text_list(finding.get("whyNotFalsePositive")),
        "limitations": protocol_text_list(finding.get("limitations")),
        "impact": protocol_multiline_text(finding.get("impact")),
        "steps": protocol_text_list(finding.get("steps")),
        "references": finding.get("references") if isinstance(finding.get("references"), list) else [],
    }


def verification_result_from_finding(finding: dict, card: dict, verifier_role: str) -> dict:
    status = clean_protocol_text(finding.get("verificationStatus")).lower()
    commands = []
    reproduction = finding.get("reproduction") if isinstance(finding.get("reproduction"), dict) else {}
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    verdict = "confirmed" if status in {"verified", "static_proof"} else "inconclusive"
    proof_type = "failing_test" if status == "verified" else "static_proof"
    if status == "verified":
        commands.extend(protocol_text_list(reproduction.get("commands")))
        commands.extend(clean_protocol_text(item.get("command")) for item in raw_evidence if isinstance(item, dict) and item.get("command"))
    return {
        "issue_id": card["issue_id"],
        "verifier_role": verifier_role,
        "verdict": verdict,
        "confidence": finding.get("confidence", 0.9),
        "proof_type": proof_type,
        "proof_strength": 3 if verdict == "confirmed" else 1,
        "evidence": audit_swarm_verification_evidence_from_finding(finding),
        "commands_run": dedupe_text(commands)[:5],
        "result_summary": protocol_multiline_text(finding.get("verificationSummary") or finding.get("summary")),
        "notes_for_fix": protocol_text_list(finding.get("steps")),
    }


def audit_swarm_suggested_test_from_finding(finding: dict) -> str:
    reproduction = finding.get("reproduction") if isinstance(finding.get("reproduction"), dict) else {}
    commands = protocol_text_list(reproduction.get("commands"))
    if commands:
        return f"Run `{commands[0]}`."
    return ""


def audit_swarm_verification_evidence_from_finding(finding: dict) -> list[str]:
    evidence = []
    summary = protocol_multiline_text(finding.get("verificationSummary"))
    if summary:
        evidence.append(summary)
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        item_summary = protocol_multiline_text(item.get("summary"))
        if item_summary:
            evidence.append(item_summary)
    return dedupe_text(evidence)[:8]


def normalize_audit_swarm_files_for_checkout(payload: dict, checkout_dir: Path) -> dict:
    normalized = merge_audit_swarm_payloads(payload)
    for card in normalized["issue_cards"]:
        raw_locations = card.get("locations") if isinstance(card.get("locations"), list) else []
        for location in raw_locations:
            if isinstance(location, dict):
                location["file"] = normalize_finding_file_for_checkout(location.get("file"), checkout_dir)
        raw_evidence = card.get("evidence") if isinstance(card.get("evidence"), list) else []
        for item in raw_evidence:
            if isinstance(item, dict):
                normalize_audit_swarm_path_fields(item, checkout_dir, "file", "logPath", "log_path")
        normalize_audit_swarm_path_fields(card, checkout_dir, "file", "testFile", "test_file")
        reproduction = card.get("reproduction") if isinstance(card.get("reproduction"), dict) else {}
        normalize_audit_swarm_path_fields(reproduction, checkout_dir, "testFile", "test_file", "logPath", "log_path")
    for result in normalized["verification_results"]:
        if isinstance(result, dict):
            normalize_audit_swarm_path_fields(result, checkout_dir, "logPath", "log_path")
    return normalized


def normalize_audit_swarm_path_fields(item: dict, checkout_dir: Path, *keys: str) -> None:
    for key in keys:
        if key in item:
            item[key] = normalize_finding_file_for_checkout(item.get(key), checkout_dir)


def audit_swarm_findings_from_payload(parsed: object) -> list[dict] | None:
    if not isinstance(parsed, dict) or "event" in parsed:
        return None
    cards = first_list(parsed, "issue_cards", "issueCards")
    if cards is None:
        return None
    verification_results = first_list(parsed, "verification_results", "verificationResults") or []
    by_issue = audit_swarm_verifications_by_issue(verification_results, cards)
    findings = []
    for index, card in enumerate(cards):
        if isinstance(card, dict):
            issue_id = audit_swarm_resolved_issue_id(card, index)
            findings.append(audit_swarm_issue_card_to_finding(card, by_issue.get(issue_id, []), index))
    return findings


def first_list(source: dict, *keys: str) -> list | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, list):
            return value
    return None


def audit_swarm_issue_key(card: dict) -> str:
    return clean_protocol_text(
        card.get("issue_id")
        or card.get("issueId")
        or card.get("id")
        or card.get("candidate_id")
        or card.get("candidateId")
    )


def audit_swarm_placeholder_issue_id(issue_id: str) -> bool:
    return not issue_id or issue_id.lower() in {"null", "none"}


def audit_swarm_resolved_issue_id(card: dict, index: int) -> str:
    issue_id = audit_swarm_issue_key(card)
    return audit_swarm_generated_id(card, index) if audit_swarm_placeholder_issue_id(issue_id) else issue_id


def audit_swarm_single_placeholder_issue_id(cards: list) -> str:
    placeholder_ids = [
        audit_swarm_resolved_issue_id(card, index)
        for index, card in enumerate(cards)
        if isinstance(card, dict) and audit_swarm_placeholder_issue_id(audit_swarm_issue_key(card))
    ]
    return placeholder_ids[0] if len(placeholder_ids) == 1 else ""


def audit_swarm_resolved_verification_issue_id(result: dict, single_placeholder_id: str = "") -> str:
    issue_id = audit_swarm_issue_key(result)
    return single_placeholder_id if audit_swarm_placeholder_issue_id(issue_id) else issue_id


def audit_swarm_verifications_by_issue(results: list, cards: list | None = None) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    single_placeholder_id = audit_swarm_single_placeholder_issue_id(cards or [])
    for result in results:
        if not isinstance(result, dict):
            continue
        issue_id = audit_swarm_resolved_verification_issue_id(result, single_placeholder_id)
        if not issue_id:
            continue
        grouped.setdefault(issue_id, []).append(result)
    return grouped


def audit_swarm_issue_card_to_finding(card: dict, verifications: list[dict], index: int) -> dict:
    issue_id = audit_swarm_resolved_issue_id(card, index)
    locations = audit_swarm_locations(card)
    primary = locations[0] if locations else {}
    verdict = audit_swarm_verdict(verifications)
    severity = audit_swarm_severity(card.get("severity"))
    category = audit_swarm_category(card)
    evidence = audit_swarm_evidence(card, verifications, primary)
    reproduction = audit_swarm_reproduction(card, verifications)
    confidence = audit_swarm_confidence(card.get("confidence"), verdict)
    finding_id = issue_id
    claim = protocol_multiline_text(card.get("claim") or card.get("summary") or card.get("description"))
    title = clean_protocol_text(card.get("title")) or f"Audit candidate {index + 1}"
    verification_summary = audit_swarm_verification_summary(verifications, verdict)
    invariants = protocol_text_list(card.get("violated_invariants") or card.get("violatedInvariants"))
    false_positive_checks = protocol_text_list(card.get("false_positive_checks") or card.get("falsePositiveChecks"))
    limitations = [
        *(f"Violated invariant: {item}" for item in invariants),
        *(f"False-positive check: {item}" for item in false_positive_checks),
        *protocol_text_list(card.get("limitations")),
    ]
    why_not_false_positive = audit_swarm_positive_checks(verifications)
    return {
        "id": finding_id,
        "severity": severity,
        "category": category,
        "title": title,
        "summary": claim or title,
        "impact": protocol_multiline_text(card.get("impact")) or audit_swarm_impact_from_invariants(invariants),
        "detectionReasoning": audit_swarm_detection_reasoning(card, verifications),
        "reproductionPath": audit_swarm_reproduction_path(card, verifications),
        "verificationStatus": audit_swarm_verification_status(verdict, verifications),
        "verificationSummary": verification_summary,
        "affectedLocations": locations,
        "evidence": evidence,
        "reproduction": reproduction,
        "whyNotFalsePositive": why_not_false_positive,
        "limitations": limitations[:8],
        "file": str(primary.get("file") or ""),
        "line": int(primary.get("startLine") or 0),
        "confidence": confidence,
        "confidenceRationale": audit_swarm_confidence_rationale(card, verifications, verdict, confidence),
        "autoFix": False,
        "effort": clean_protocol_text(card.get("effort")) or "review required",
        "fixBenefits": protocol_multiline_text(card.get("fixBenefits") or card.get("fix_benefits")),
        "fixRisks": protocol_multiline_text(card.get("fixRisks") or card.get("fix_risks")),
        "tags": audit_swarm_tags(card, verifications),
        "steps": audit_swarm_steps(card),
        "badCode": [],
        "goodCode": [],
        "references": audit_swarm_references(card),
        "_auditSwarmVerdict": verdict,
        "_auditSwarmRole": clean_protocol_text(card.get("agent_role") or card.get("agentRole")),
        "_auditSwarmShard": clean_protocol_text(card.get("shard_id") or card.get("shardId")),
    }


def audit_swarm_generated_id(card: dict, index: int) -> str:
    seed = json.dumps(card, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha1(f"{index}:{seed}".encode("utf-8")).hexdigest()[:10]
    return f"audit_swarm_{digest}"


def audit_swarm_verdict(verifications: list[dict]) -> str:
    verdicts = [clean_protocol_text(item.get("verdict")).lower() for item in verifications if isinstance(item, dict)]
    if any(
        clean_protocol_text(item.get("verdict")).lower() == "confirmed"
        and audit_swarm_confirmed_verification_has_support(item)
        for item in verifications
        if isinstance(item, dict)
    ):
        return "confirmed"
    if verdicts and all(item == "rejected" for item in verdicts):
        return "rejected"
    if "inconclusive" in verdicts:
        return "inconclusive"
    return "candidate"


def audit_swarm_confirmed_verification_has_support(result: dict) -> bool:
    if protocol_text_list(result.get("commands_run") or result.get("commandsRun")):
        return True
    if protocol_text_list(result.get("evidence")):
        return True
    if protocol_multiline_text(result.get("result_summary") or result.get("resultSummary") or result.get("summary")):
        return True
    if protocol_multiline_text(result.get("output")):
        return True
    if clean_protocol_text(result.get("logPath") or result.get("log_path")):
        return True
    return False


def audit_swarm_verification_status(verdict: str, verifications: list[dict]) -> str:
    if verdict == "confirmed":
        proof_types = {
            clean_protocol_text(item.get("proof_type") or item.get("proofType")).lower()
            for item in verifications
            if isinstance(item, dict)
        }
        has_command = any(protocol_text_list(item.get("commands_run") or item.get("commandsRun")) for item in verifications)
        if proof_types & {"failing_test", "runtime_log", "test", "command"} or has_command:
            return "verified"
        return "static_proof"
    if verdict == "rejected":
        return "unverified"
    if verdict == "inconclusive":
        return "potential_risk"
    return "potential_risk"


def audit_swarm_severity(value: object) -> str:
    severity = clean_protocol_text(value).lower()
    mapping = {
        "p0": "critical",
        "p1": "high",
        "p2": "medium",
        "p3": "low",
        "p4": "info",
        "critical": "critical",
        "high": "high",
        "medium": "medium",
        "low": "low",
        "info": "info",
    }
    return mapping.get(severity, "medium")


def audit_swarm_category(card: dict) -> str:
    raw = " ".join(
        clean_protocol_text(value).lower()
        for value in (card.get("category"), card.get("agent_role"), card.get("agentRole"))
        if clean_protocol_text(value)
    )
    if "security" in raw or "auth" in raw or "permission" in raw:
        return "Security"
    if "performance" in raw:
        return "Performance"
    if "dependency" in raw or "cve" in raw:
        return "Dependencies"
    if "test" in raw or "coverage" in raw:
        return "Tests"
    if "doc" in raw:
        return "Docs"
    if "architecture" in raw or "contract" in raw or "api" in raw:
        return "Architecture"
    return "Quality"


def audit_swarm_confidence(value: object, verdict: str) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.7
    confidence = max(0.0, min(1.0, confidence))
    if verdict == "confirmed":
        return max(confidence, 0.85)
    if verdict == "rejected":
        return min(confidence, 0.2)
    if verdict == "inconclusive":
        return min(confidence, 0.79)
    return confidence


def audit_swarm_locations(card: dict) -> list[dict]:
    raw_locations = first_list(card, "locations", "affectedLocations", "affected_locations") or []
    locations = []
    seen = set()
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        file_path = safe_repo_relative_file(item.get("file") or item.get("path"))
        if not file_path:
            continue
        start_line, end_line = audit_swarm_line_range(item)
        key = (file_path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        locations.append({"file": file_path, "startLine": start_line, "endLine": end_line})
    file_path = safe_repo_relative_file(card.get("file"))
    if file_path:
        line = positive_int(card.get("line"))
        key = (file_path, line, line)
        if key not in seen:
            locations.append({"file": file_path, "startLine": line, "endLine": line})
    return locations[:10]


def audit_swarm_line_range(item: dict) -> tuple[int, int]:
    start = positive_int(item.get("startLine") or item.get("start_line") or item.get("line"))
    end = positive_int(item.get("endLine") or item.get("end_line"))
    lines = clean_protocol_text(item.get("lines") or item.get("lineRange") or item.get("line_range"))
    if lines and not start:
        match = re.search(r"(\d+)(?:\s*[-:]\s*(\d+))?", lines)
        if match:
            start = int(match.group(1))
            end = int(match.group(2) or match.group(1))
    if start and (not end or end < start):
        end = start
    return start, end


def audit_swarm_evidence(card: dict, verifications: list[dict], primary: dict) -> list[dict]:
    evidence = []
    role = clean_protocol_text(card.get("agent_role") or card.get("agentRole")) or "discovery agent"
    for index, item in enumerate(first_list(card, "evidence") or []):
        if isinstance(item, dict):
            summary = protocol_multiline_text(item.get("summary") or item.get("claim") or item.get("text"))
            file_path = safe_repo_relative_file(item.get("file") or item.get("path")) or str(primary.get("file") or "")
            start_line, end_line = audit_swarm_line_range(item)
            record = {
                "type": audit_swarm_evidence_type(item.get("type"), default="code" if file_path else "path"),
                "label": clean_protocol_text(item.get("label")) or f"{role} evidence",
                "summary": summary,
                "file": file_path,
                "startLine": start_line or int(primary.get("startLine") or 0),
                "endLine": end_line or int(primary.get("endLine") or primary.get("startLine") or 0),
                "command": clean_protocol_text(item.get("command")),
                "exitCode": positive_int(item.get("exitCode") or item.get("exit_code")),
                "logPath": clean_protocol_text(item.get("logPath") or item.get("log_path")),
                "output": protocol_multiline_text(item.get("output"))[:4000],
                "url": clean_protocol_text(item.get("url")),
            }
        else:
            summary = protocol_multiline_text(item)
            record = {
                "type": "code" if primary.get("file") else "path",
                "label": f"{role} evidence" if index == 0 else "Discovery evidence",
                "summary": summary,
                "file": str(primary.get("file") or ""),
                "startLine": int(primary.get("startLine") or 0),
                "endLine": int(primary.get("endLine") or primary.get("startLine") or 0),
                "command": "",
                "exitCode": 0,
                "logPath": "",
                "output": "",
                "url": "",
            }
        if any(record.get(key) for key in ("summary", "file", "command", "logPath", "output", "url")):
            evidence.append(record)
    for result in verifications:
        if not isinstance(result, dict):
            continue
        verifier_role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        proof_type = clean_protocol_text(result.get("proof_type") or result.get("proofType"))
        evidence_type = audit_swarm_evidence_type(proof_type, default="test" if proof_type else "tool")
        commands = protocol_text_list(result.get("commands_run") or result.get("commandsRun"))
        for index, summary in enumerate(protocol_text_list(result.get("evidence"))):
            evidence.append(
                {
                    "type": evidence_type,
                    "label": f"{verifier_role} verification" if index == 0 else "Verification evidence",
                    "summary": summary,
                    "file": str(primary.get("file") or ""),
                    "startLine": int(primary.get("startLine") or 0),
                    "endLine": int(primary.get("endLine") or primary.get("startLine") or 0),
                    "command": commands[0] if commands else "",
                    "exitCode": 0,
                    "logPath": clean_protocol_text(result.get("logPath") or result.get("log_path")),
                    "output": protocol_multiline_text(result.get("output"))[:4000],
                    "url": "",
                }
            )
    return evidence[:20]


def audit_swarm_evidence_type(value: object, *, default: str) -> str:
    raw = clean_protocol_text(value).lower()
    if raw in {"failing_test", "test"}:
        return "test"
    if raw in {"runtime", "runtime_log", "command"}:
        return "runtime_log"
    if raw in {"static", "static_proof", "code"}:
        return "code"
    if raw in {"path", "reachability", "data_flow", "data-flow"}:
        return "path"
    if raw in {"trigger", "input"}:
        return "trigger"
    if raw in {"documentation", "docs"}:
        return "documentation"
    if raw in {"fix", "fix_verification"}:
        return "fix_verification"
    if raw in {"tool", "environment"}:
        return raw
    return default


def audit_swarm_reproduction(card: dict, verifications: list[dict]) -> dict:
    commands = []
    for result in verifications:
        if isinstance(result, dict):
            commands.extend(protocol_text_list(result.get("commands_run") or result.get("commandsRun")))
    reproduction = card.get("reproduction") if isinstance(card.get("reproduction"), dict) else {}
    commands.extend(protocol_text_list(reproduction.get("commands")))
    commands = dedupe_text(commands)[:5]
    return {
        "commands": commands,
        "input": protocol_multiline_text(reproduction.get("input") or card.get("trigger") or card.get("input")),
        "expected": protocol_multiline_text(reproduction.get("expected") or card.get("expected")),
        "actual": audit_swarm_actual_result(verifications) or protocol_multiline_text(reproduction.get("actual") or card.get("actual")),
        "testFile": clean_protocol_text(reproduction.get("testFile") or reproduction.get("test_file") or card.get("test_file") or card.get("testFile")),
        "logPath": clean_protocol_text(reproduction.get("logPath") or reproduction.get("log_path")),
    }


def audit_swarm_actual_result(verifications: list[dict]) -> str:
    for result in verifications:
        if not isinstance(result, dict):
            continue
        summary = protocol_multiline_text(result.get("result_summary") or result.get("resultSummary"))
        if summary:
            return summary
    return ""


def audit_swarm_detection_reasoning(card: dict, verifications: list[dict]) -> str:
    parts = []
    role = clean_protocol_text(card.get("agent_role") or card.get("agentRole"))
    shard = clean_protocol_text(card.get("shard_id") or card.get("shardId"))
    if role or shard:
        parts.append(f"{role or 'reviewer'} reported this candidate" + (f" in shard `{shard}`." if shard else "."))
    claim = protocol_multiline_text(card.get("claim"))
    if claim:
        parts.append(f"Claim: {claim}")
    for invariant in protocol_text_list(card.get("violated_invariants") or card.get("violatedInvariants"))[:3]:
        parts.append(f"Violated invariant: {invariant}")
    for result in verifications[:3]:
        if not isinstance(result, dict):
            continue
        verifier = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        verdict = clean_protocol_text(result.get("verdict"))
        summary = protocol_multiline_text(result.get("result_summary") or result.get("resultSummary"))
        if verdict or summary:
            parts.append(f"{verifier} verdict: {verdict or 'reviewed'}" + (f" - {summary}" if summary else "."))
    return " ".join(parts)[:1200]


def audit_swarm_reproduction_path(card: dict, verifications: list[dict]) -> str:
    parts = []
    reproduction_idea = protocol_multiline_text(card.get("reproduction_idea") or card.get("reproductionIdea"))
    suggested_test = protocol_multiline_text(card.get("suggested_test") or card.get("suggestedTest"))
    if reproduction_idea:
        parts.append(reproduction_idea)
    if suggested_test:
        parts.append(f"Suggested test: {suggested_test}")
    for result in verifications:
        if not isinstance(result, dict):
            continue
        commands = protocol_text_list(result.get("commands_run") or result.get("commandsRun"))
        if commands:
            parts.append(f"Verifier command: {commands[0]}")
            break
    return " ".join(parts)[:1000]


def audit_swarm_verification_summary(verifications: list[dict], verdict: str) -> str:
    for result in verifications:
        if not isinstance(result, dict):
            continue
        summary = protocol_multiline_text(result.get("result_summary") or result.get("resultSummary"))
        if summary:
            role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole"))
            return f"{role}: {summary}" if role else summary
    if verdict == "confirmed":
        return "Audit verifier confirmed this candidate."
    if verdict == "rejected":
        return "Audit verifier rejected this candidate before reporting."
    if verdict == "inconclusive":
        return "Audit verifier could not conclusively prove or disprove this candidate."
    return "Discovery candidate has not been independently verified."


def audit_swarm_confidence_rationale(card: dict, verifications: list[dict], verdict: str, confidence: float) -> str:
    explicit = protocol_multiline_text(card.get("confidenceRationale") or card.get("confidence_rationale"))
    if explicit:
        return explicit
    if verifications:
        return f"Audit Swarm verdict is {verdict}; projected confidence is {confidence:.2f} after verifier evidence."
    return f"Discovery confidence is {confidence:.2f}; no separate verifier result was supplied in the payload."


def audit_swarm_impact_from_invariants(invariants: list[str]) -> str:
    if invariants:
        return f"The finding may violate this required behavior: {invariants[0]}"
    return ""


def audit_swarm_positive_checks(verifications: list[dict]) -> list[str]:
    checks = []
    for result in verifications:
        if not isinstance(result, dict):
            continue
        role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole")) or "verifier"
        for item in protocol_text_list(result.get("evidence"))[:3]:
            checks.append(f"{role}: {item}")
    return dedupe_text(checks)[:6]


def audit_swarm_tags(card: dict, verifications: list[dict]) -> list[str]:
    tags = ["audit-swarm"]
    tags.extend(protocol_text_list(card.get("risk_tags") or card.get("riskTags")))
    tags.extend(protocol_text_list(card.get("tags")))
    for value in (card.get("agent_role"), card.get("agentRole"), card.get("shard_id"), card.get("shardId")):
        text = clean_protocol_text(value)
        if text:
            tags.append(text)
    for result in verifications:
        if isinstance(result, dict):
            role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole"))
            if role:
                tags.append(role)
    return [slugify_tag(tag) for tag in dedupe_text(tags) if slugify_tag(tag)][:12]


def audit_swarm_steps(card: dict) -> list[str]:
    steps = protocol_text_list(card.get("steps"))
    suggested_test = protocol_multiline_text(card.get("suggested_test") or card.get("suggestedTest"))
    remediation = protocol_multiline_text(card.get("remediation") or card.get("fix"))
    if suggested_test:
        steps.append(f"Add or run the suggested test: {suggested_test}")
    if remediation:
        steps.append(remediation)
    return dedupe_text(steps)[:8]


def audit_swarm_references(card: dict) -> list[dict]:
    references = []
    for item in first_list(card, "references") or []:
        if isinstance(item, dict):
            label = clean_protocol_text(item.get("label")) or clean_protocol_text(item.get("url"))
            url = clean_protocol_text(item.get("url"))
        else:
            label = clean_protocol_text(item)
            url = clean_protocol_text(item)
        if label and url.startswith(("http://", "https://")):
            references.append({"label": label, "url": url})
    return references[:10]


def protocol_text_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (protocol_multiline_text(item) for item in value) if text]


def protocol_text_items(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items = []
    for item in value:
        if isinstance(item, dict):
            text = protocol_multiline_text(item.get("summary") or item.get("text") or item.get("claim") or item.get("label"))
        else:
            text = protocol_multiline_text(item)
        if text:
            items.append(text)
    return items


def clean_protocol_text(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).replace("\x00", "")
    lines = text.splitlines()
    text = lines[0] if lines else text
    text = text.strip()
    return text[:500]


def protocol_multiline_text(value: object) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    text = str(value).replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    return text[:4000]


def dedupe_text(items: list[str]) -> list[str]:
    deduped = []
    seen = set()
    for item in items:
        text = protocol_multiline_text(item)
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return deduped


def slugify_tag(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40]


def normalize_finding_files_for_checkout(findings: list[dict], checkout_dir: Path) -> list[dict]:
    normalized: list[dict] = []
    for finding in findings:
        item = dict(finding)
        item["file"] = normalize_finding_file_for_checkout(item.get("file"), checkout_dir)
        normalized.append(item)
    return normalized


def normalize_finding_file_for_checkout(value: object, checkout_dir: Path) -> str:
    relative_path = relative_file_inside_checkout(value, checkout_dir)
    return safe_repo_relative_file(relative_path if relative_path is not None else value)


def relative_file_inside_checkout(value: object, checkout_dir: Path) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(char in raw for char in "\r\n\x00"):
        return None
    normalized = raw.replace("\\", "/")
    if not (normalized.startswith("/") or _WINDOWS_DRIVE_RE.match(raw)):
        return None

    root = str(checkout_dir.resolve(strict=False)).replace("\\", "/").rstrip("/")
    root_prefix = f"{root}/"
    if normalized.casefold() == root.casefold():
        return ""
    if normalized.casefold().startswith(root_prefix.casefold()):
        return normalized[len(root_prefix) :]
    return None


def safe_repo_relative_file(value: object) -> str:
    if not isinstance(value, str):
        return ""
    raw = value.strip()
    normalized = raw.replace("\\", "/")
    if (
        not raw
        or any(char in raw for char in "\r\n\x00")
        or _WINDOWS_DRIVE_RE.match(raw)
        or normalized.startswith("/")
        or normalized.startswith("//")
        or raw.startswith("\\")
    ):
        return ""
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    if any(part.casefold() == ".git" for part in parts):
        return ""
    return "/".join(parts)


def audit_swarm_output_schema() -> dict:
    location = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file": {"type": "string"},
            "lines": {"type": "string"},
            "startLine": {"type": "integer"},
            "endLine": {"type": "integer"},
        },
        "required": ["file", "lines", "startLine", "endLine"],
    }
    evidence = {
        "anyOf": [
            {"type": "string"},
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string"},
                    "label": {"type": "string"},
                    "summary": {"type": "string"},
                    "file": {"type": "string"},
                    "startLine": {"type": "integer"},
                    "endLine": {"type": "integer"},
                    "command": {"type": "string"},
                    "exitCode": {"type": "integer"},
                    "logPath": {"type": "string"},
                    "output": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": [
                    "type",
                    "label",
                    "summary",
                    "file",
                    "startLine",
                    "endLine",
                    "command",
                    "exitCode",
                    "logPath",
                    "output",
                    "url",
                ],
            },
        ]
    }
    issue_card = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "issue_id": {"type": "string"},
            "shard_id": {"type": "string"},
            "agent_role": {"type": "string"},
            "title": {"type": "string"},
            "category": {"type": "string"},
            "severity": {"type": "string"},
            "confidence": {"type": "number"},
            "locations": {"type": "array", "items": location},
            "claim": {"type": "string"},
            "violated_invariants": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": evidence},
            "reproduction_idea": {"type": "string"},
            "suggested_test": {"type": "string"},
            "false_positive_checks": {"type": "array", "items": {"type": "string"}},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "issue_id",
            "shard_id",
            "agent_role",
            "title",
            "category",
            "severity",
            "confidence",
            "locations",
            "claim",
            "violated_invariants",
            "evidence",
            "reproduction_idea",
            "suggested_test",
            "false_positive_checks",
            "limitations",
        ],
    }
    verification_result = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "issue_id": {"type": "string"},
            "verifier_role": {"type": "string"},
            "verdict": {"type": "string", "enum": ["confirmed", "rejected", "inconclusive"]},
            "confidence": {"type": "number"},
            "proof_type": {"type": "string"},
            "proof_strength": {"type": "integer"},
            "evidence": {"type": "array", "items": {"type": "string"}},
            "commands_run": {"type": "array", "items": {"type": "string"}},
            "result_summary": {"type": "string"},
            "notes_for_fix": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "issue_id",
            "verifier_role",
            "verdict",
            "confidence",
            "proof_type",
            "proof_strength",
            "evidence",
            "commands_run",
            "result_summary",
            "notes_for_fix",
        ],
    }
    return {
        "type": "object",
        "required": ["audit_protocol", "issue_cards", "verification_results"],
        "additionalProperties": False,
        "properties": {
            "audit_protocol": {"type": "string"},
            "issue_cards": {"type": "array", "items": issue_card, "maxItems": 25},
            "verification_results": {"type": "array", "items": verification_result, "maxItems": 50},
        },
    }


def summarize(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        severity = str(finding.get("severity") or "low").lower()
        if severity not in summary:
            severity = "low"
        summary[severity] += 1
    return summary


def positive_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return number if number > 0 else 0


def finding_precise_location(finding: dict) -> bool:
    if safe_repo_relative_file(finding.get("file")) and positive_int(finding.get("line")):
        return True
    raw_locations = finding.get("affectedLocations") if isinstance(finding.get("affectedLocations"), list) else []
    for item in raw_locations:
        if not isinstance(item, dict):
            continue
        if safe_repo_relative_file(item.get("file")) and positive_int(item.get("startLine") or item.get("line")):
            return True
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        if safe_repo_relative_file(item.get("file")) and positive_int(item.get("startLine") or item.get("line")):
            return True
    return False


def finding_structured_evidence(finding: dict) -> bool:
    raw_evidence = finding.get("evidence") if isinstance(finding.get("evidence"), list) else []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        has_summary = bool(str(item.get("summary") or "").strip())
        has_command = reproduction_command_looks_executable(item.get("command"))
        has_log = evidence_log_path_is_structured(item.get("logPath") or item.get("log_path"))
        has_file_line = safe_repo_relative_file(item.get("file")) and positive_int(item.get("startLine") or item.get("line"))
        if has_summary and (has_command or has_log or has_file_line):
            return True
    return False


def finding_reproduction_evidence(finding: dict) -> bool:
    reproduction = finding.get("reproduction") if isinstance(finding.get("reproduction"), dict) else {}
    commands = reproduction.get("commands") if isinstance(reproduction.get("commands"), list) else []
    if any(reproduction_command_looks_executable(command) for command in commands):
        return True
    return reproduction_path_has_executable_command(finding.get("reproductionPath"))


def evidence_log_path_is_structured(value: object) -> bool:
    path = safe_repo_relative_file(value)
    if not path or re.search(r"\s", path):
        return False
    if "/" in path:
        return True
    return bool(re.search(r"\.(log|txt|json|out|err|trace)\Z", path, flags=re.IGNORECASE))


def reproduction_path_has_executable_command(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    verifier_match = re.search(r"Verifier command:\s*([^`.;\n\r]+)", text, flags=re.IGNORECASE)
    if verifier_match and reproduction_command_looks_executable(verifier_match.group(1)):
        return True
    return any(reproduction_command_looks_executable(match) for match in re.findall(r"`([^`]+)`", text))


def reproduction_command_looks_executable(command: object) -> bool:
    text = str(command or "").strip()
    if not text or "\n" in text or "\r" in text:
        return False
    first = text.split(maxsplit=1)[0].strip("\"'")
    if first.startswith(("./", ".\\", "scripts/", "bin/")):
        return True
    executable = first.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    executable = executable[:-4] if executable.endswith(".exe") else executable
    return executable in {
        "bun",
        "cargo",
        "deno",
        "docker",
        "dotnet",
        "go",
        "gradle",
        "java",
        "make",
        "mvn",
        "node",
        "npm",
        "npx",
        "pnpm",
        "pytest",
        "python",
        "python3",
        "ruby",
        "ruff",
        "tox",
        "uv",
        "yarn",
    }


def finding_has_false_positive_check(finding: dict) -> bool:
    if finding_has_verification_proof(finding):
        return True
    for key in ("whyNotFalsePositive", "false_positive_checks", "falsePositiveChecks"):
        values = finding.get(key) if isinstance(finding.get(key), list) else []
        if any(false_positive_check_is_substantive(item) for item in values):
            return True
    limitations = finding.get("limitations") if isinstance(finding.get("limitations"), list) else []
    return any(
        false_positive_check_is_substantive(
            re.sub(r"^.*?false-positive check:\s*", "", str(item or ""), flags=re.IGNORECASE)
        )
        for item in limitations
        if "false-positive check:" in str(item or "").strip().lower()
    )


def false_positive_check_is_substantive(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = normalized_fingerprint_text(text)
    return normalized not in {
        "n/a",
        "na",
        "none",
        "not applicable",
        "unknown",
        "unchecked",
        "not checked",
        "not verified",
        "no check",
        "no false positive check",
        "could not verify",
        "cannot verify",
        "todo",
        "tbd",
    }


def reportability_rejection_reason(finding: object) -> str:
    if not isinstance(finding, dict):
        return "invalid_candidate"
    if not str(finding.get("title") or "").strip():
        return "missing_title"
    if finding_has_verification_proof(finding) and (
        finding_precise_location(finding) or finding_structured_evidence(finding) or finding_reproduction_evidence(finding)
    ):
        return ""
    if finding_structured_evidence(finding) or finding_reproduction_evidence(finding):
        if not finding_has_false_positive_check(finding):
            return "missing_false_positive_check"
        return ""
    return "missing_evidence"


def rejected_candidate_sample(finding: object, reason: str) -> dict:
    sample = {"reason": reason}
    if not isinstance(finding, dict):
        return sample
    title = str(finding.get("title") or "").strip()
    if title:
        sample["title"] = title[:160]
    severity = str(finding.get("severity") or "").strip().lower()
    if severity in {"critical", "high", "medium", "low", "info"}:
        sample["severity"] = severity
    category = str(finding.get("category") or "").strip()
    if category:
        sample["category"] = category[:80]
    file_path = safe_repo_relative_file(finding.get("file"))
    if file_path:
        sample["file"] = file_path
    line = positive_int(finding.get("line"))
    if line:
        sample["line"] = line
    status = str(finding.get("verificationStatus") or "").strip().lower()
    if status in _VERIFICATION_STATUSES:
        sample["verificationStatus"] = status
    return sample


def filter_reportable_findings(findings: list[dict]) -> tuple[list[dict], dict[str, int], list[dict]]:
    reportable: list[dict] = []
    rejected_reasons: dict[str, int] = {}
    rejected_samples: list[dict] = []
    for finding in findings:
        reason = reportability_rejection_reason(finding)
        if not reason:
            reportable.append(finding)
            continue
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
        if len(rejected_samples) < 5:
            rejected_samples.append(rejected_candidate_sample(finding, reason))
    return reportable, rejected_reasons, rejected_samples


def normalized_fingerprint_text(value: object) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    return re.sub(r"[^a-z0-9_./:-]+", " ", text).strip()


def finding_primary_file(finding: dict) -> str:
    file_path = safe_repo_relative_file(finding.get("file"))
    if file_path:
        return file_path
    for key in ("affectedLocations", "evidence"):
        items = finding.get(key) if isinstance(finding.get(key), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            file_path = safe_repo_relative_file(item.get("file"))
            if file_path:
                return file_path
    return ""


def finding_source(finding: dict) -> str:
    source = str(finding.get("_auditSwarmRole") or finding.get("source") or finding.get("category") or "reviewer")
    return normalized_source_key(source) or "reviewer"


def normalized_source_key(value: object) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:80]


def finding_fingerprint(finding: dict) -> str:
    parts = [
        normalized_fingerprint_text(finding.get("category")),
        normalized_fingerprint_text(finding_primary_file(finding)),
        normalized_fingerprint_text(finding.get("title") or finding.get("summary")),
        normalized_fingerprint_text(finding.get("summary") or finding.get("impact")),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest


def finding_delta_files(finding: dict) -> set[str]:
    files = set()
    primary = finding_primary_file(finding)
    if primary:
        files.add(primary)
    for key in ("affectedLocations", "evidence"):
        items = finding.get(key) if isinstance(finding.get(key), list) else []
        for item in items:
            if isinstance(item, dict):
                file_path = safe_repo_relative_file(item.get("file"))
                if file_path:
                    files.add(file_path)
    return files


def finding_primary_line(finding: dict) -> int:
    line = positive_int(finding.get("line") or finding.get("startLine"))
    if line:
        return line
    for key in ("affectedLocations", "evidence"):
        items = finding.get(key) if isinstance(finding.get(key), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            line = positive_int(item.get("startLine") or item.get("line"))
            if line:
                return line
    return 0


def finding_location_exists_in_checkout(checkout_dir: Path, finding: dict, fallback: dict | None = None) -> bool:
    file_path = finding_primary_file(finding)
    if not file_path and isinstance(fallback, dict):
        file_path = finding_primary_file(fallback)
    if not file_path:
        return True
    path = checkout_dir / file_path
    if not path.is_file():
        return False
    line = finding_primary_line(finding)
    if not line and isinstance(fallback, dict):
        fallback_file = finding_primary_file(fallback)
        if not finding_primary_file(finding) or fallback_file == file_path:
            line = finding_primary_line(fallback)
    if not line:
        return True
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            line_count = sum(1 for _ in handle)
    except OSError:
        return False
    return line <= line_count


def normalized_head_sha(value: object) -> str:
    text = str(value or "").strip().lower()
    if text and text != "pending" and re.fullmatch(r"[0-9a-f]{7,64}", text):
        return text
    return ""


def git_diff_name_only(checkout_dir: Path, previous: str, current: str) -> set[str] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout_dir), "diff", "--name-only", f"{previous}..{current}"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return {safe_repo_relative_file(line.strip()) for line in completed.stdout.splitlines() if safe_repo_relative_file(line.strip())}


def parse_git_diff_changed_line_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    ranges: dict[str, list[tuple[int, int]]] = {}
    current_file = ""
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            file_path = line[4:].strip()
            if file_path == "/dev/null":
                current_file = ""
                continue
            if file_path.startswith("b/"):
                file_path = file_path[2:]
            current_file = safe_repo_relative_file(file_path)
            continue
        if not current_file or not line.startswith("@@"):
            continue
        match = re.search(r"\+(\d+)(?:,(\d+))?", line)
        if not match:
            continue
        start = positive_int(match.group(1))
        count = positive_int(match.group(2) or 1)
        if not start or not count:
            continue
        ranges.setdefault(current_file, []).append((start, start + count - 1))
    return ranges


def git_diff_changed_line_ranges(checkout_dir: Path, previous: str, current: str) -> dict[str, list[tuple[int, int]]] | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout_dir), "diff", "--unified=0", f"{previous}..{current}"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600),
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return parse_git_diff_changed_line_ranges(completed.stdout)


def fetch_git_head(checkout_dir: Path, head_sha: str, job: dict | None = None) -> bool:
    head = normalized_head_sha(head_sha)
    if not head:
        return False
    try:
        subprocess.run(
            ["git", "-C", str(checkout_dir), "fetch", "--depth", "1", "origin", head],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=env_int("PULLWISE_GIT_TIMEOUT_SECONDS", 600),
            env=git_auth_env(job.get("clone_token")) if isinstance(job, dict) else None,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False
    return True


def changed_files_between_heads(
    checkout_dir: Path,
    previous_head_sha: str,
    current_head_sha: str,
    *,
    job: dict | None = None,
) -> set[str] | None:
    previous = normalized_head_sha(previous_head_sha)
    current = normalized_head_sha(current_head_sha)
    if not previous or not current:
        return None
    if previous == current:
        return set()
    changed_files = git_diff_name_only(checkout_dir, previous, current)
    if changed_files is not None:
        return changed_files
    if fetch_git_head(checkout_dir, previous, job):
        return git_diff_name_only(checkout_dir, previous, current)
    return None


def changed_line_ranges_between_heads(
    checkout_dir: Path,
    previous_head_sha: str,
    current_head_sha: str,
    *,
    job: dict | None = None,
) -> dict[str, list[tuple[int, int]]] | None:
    previous = normalized_head_sha(previous_head_sha)
    current = normalized_head_sha(current_head_sha)
    if not previous or not current:
        return None
    if previous == current:
        return {}
    changed_ranges = git_diff_changed_line_ranges(checkout_dir, previous, current)
    if changed_ranges is not None:
        return changed_ranges
    if fetch_git_head(checkout_dir, previous, job):
        return git_diff_changed_line_ranges(checkout_dir, previous, current)
    return None


def finding_line_within_changed_ranges(finding: dict, changed_line_ranges: dict[str, list[tuple[int, int]]] | None) -> bool:
    if changed_line_ranges is None:
        return True
    file_path = finding_primary_file(finding)
    line = finding_primary_line(finding)
    if not file_path or not line:
        return True
    ranges = changed_line_ranges.get(file_path)
    if not ranges:
        return False
    return any(start <= line <= end for start, end in ranges)


def source_stat_count(stats: dict, key: str) -> int:
    return positive_int(stats.get(key)) if isinstance(stats, dict) else 0


def wilson_lower_bound(successes: int, total: int, *, z: float = 1.0) -> float:
    if total <= 0:
        return 1.0
    p = successes / total
    denominator = 1 + (z * z / total)
    centre = p + (z * z / (2 * total))
    margin = z * ((p * (1 - p) + (z * z / (4 * total))) / total) ** 0.5
    return max(0.0, min(1.0, (centre - margin) / denominator))


def statistically_calibrated_confidence(finding: dict, source_stats: dict) -> float:
    try:
        base_confidence = float(finding.get("confidence") or 0.0)
    except (OverflowError, TypeError, ValueError):
        base_confidence = 0.0
    base_confidence = max(0.0, min(1.0, base_confidence))
    confirmed = source_stat_count(source_stats, "confirmed")
    resolved = source_stat_count(source_stats, "resolved")
    rejected = source_stat_count(source_stats, "rejected")
    if finding_has_verification_proof(finding):
        return base_confidence
    if not confirmed and resolved and not rejected:
        reliability = wilson_lower_bound(1, resolved + 2)
        return base_confidence * reliability
    positive_feedback = confirmed
    if rejected <= positive_feedback:
        total = positive_feedback + rejected
        if total < 8 or rejected <= 1:
            return base_confidence
    reliability = wilson_lower_bound(positive_feedback + 1, positive_feedback + rejected + 2)
    return base_confidence * reliability


def convergence_min_confidence(finding: dict) -> float:
    if finding_has_verification_proof(finding):
        return CONVERGENCE_MIN_VERIFIED_CONFIDENCE
    return CONVERGENCE_MIN_UNVERIFIED_CONFIDENCE


def finding_has_verification_proof(finding: dict) -> bool:
    status = str(finding.get("verificationStatus") or "").strip().lower()
    return status in {"verified", "static_proof"}


def merge_source_stats(context: dict) -> dict[str, dict[str, int]]:
    raw_stats = context.get("source_stats") if isinstance(context.get("source_stats"), dict) else {}
    stats: dict[str, dict[str, int]] = {}
    for raw_source, raw_counts in raw_stats.items():
        source = normalized_source_key(raw_source)
        if not source or not isinstance(raw_counts, dict):
            continue
        stats[source] = {
            "reported": source_stat_count(raw_counts, "reported"),
            "confirmed": source_stat_count(raw_counts, "confirmed"),
            "resolved": source_stat_count(raw_counts, "resolved"),
            "rejected": source_stat_count(raw_counts, "rejected"),
        }
    return stats


def bump_source_stat(stats: dict[str, dict[str, int]], source: str, key: str) -> None:
    bucket = stats.setdefault(source or "reviewer", {"reported": 0, "confirmed": 0, "resolved": 0, "rejected": 0})
    bucket[key] = positive_int(bucket.get(key)) + 1


def convergence_record_for_finding(finding: dict, fingerprint: str) -> dict:
    record = {
        "fingerprint": fingerprint,
        "issue_id": str(finding.get("id") or "").strip()[:120],
        "title": str(finding.get("title") or "").strip()[:180],
        "file": finding_primary_file(finding),
        "line": positive_int(finding.get("line")),
        "confidence": max(0.0, min(1.0, float(finding.get("confidence") or 0.0))),
        "source": finding_source(finding),
        "status": "open",
    }
    return {key: value for key, value in record.items() if value not in ("", 0, [], {})}


def finding_issue_id(finding: dict) -> str:
    return str(finding.get("id") or finding.get("issue_id") or finding.get("issueId") or "").strip()


def convergence_scope_key(job: dict) -> str:
    repo = normalized_fingerprint_text(job.get("repo"))
    branch = normalized_fingerprint_text(job.get("branch") or "main")
    return f"repo:{repo}|branch:{branch}"


def convergence_context_for_job(job: dict) -> dict:
    context = job.get("convergence_context") if isinstance(job.get("convergence_context"), dict) else {}
    if not context:
        return {}
    scope_key = normalized_fingerprint_text(context.get("scope_key") or context.get("scopeKey"))
    expected_scope_key = normalized_fingerprint_text(convergence_scope_key(job))
    if scope_key and scope_key != expected_scope_key:
        return {}
    protocol = str(context.get("protocol") or "").strip()
    if protocol and protocol != CONVERGENCE_PROTOCOL_VERSION:
        return {}
    return context


def apply_convergence_gate(
    job: dict,
    checkout_dir: Path,
    findings: list[dict],
) -> tuple[list[dict], dict[str, int], list[dict], dict]:
    context = convergence_context_for_job(job)
    previous_open = [
        item
        for item in (context.get("open_findings") if isinstance(context.get("open_findings"), list) else [])
        if isinstance(item, dict) and str(item.get("fingerprint") or "").strip()
    ]
    previous_by_fingerprint = {str(item.get("fingerprint")).strip(): item for item in previous_open}
    previous_fingerprint_by_issue_id = {
        str(item.get("issue_id") or item.get("issueId")).strip(): str(item.get("fingerprint")).strip()
        for item in previous_open
        if str(item.get("issue_id") or item.get("issueId")).strip()
    }
    previous_head_sha = normalized_head_sha(context.get("previous_head_sha"))
    current_head_sha = normalized_head_sha(job.get("resolved_commit") or job.get("commit"))
    has_prior_run = bool(previous_head_sha or previous_open or context.get("source_stats"))
    source_stats = merge_source_stats(context)
    known_sources = set(source_stats)
    changed_files: set[str] | None = None
    changed_files_loaded = False
    changed_line_ranges: dict[str, list[tuple[int, int]]] | None = None
    changed_line_ranges_loaded = False
    reportable: list[dict] = []
    open_fingerprints = set()
    seen_current_fingerprints = set()
    rejected_reasons: dict[str, int] = {}
    rejected_samples: list[dict] = []

    def reject(finding: dict, reason: str) -> None:
        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
        bump_source_stat(source_stats, finding_source(finding), "rejected")
        if len(rejected_samples) < 5:
            rejected_samples.append(rejected_candidate_sample(finding, reason))

    for finding in findings:
        if not isinstance(finding, dict):
            continue
        fingerprint = finding_fingerprint(finding)
        matched_fingerprint = previous_fingerprint_by_issue_id.get(finding_issue_id(finding)) or fingerprint
        source = finding_source(finding)
        source_counts = source_stats.get(source, {})
        if matched_fingerprint in seen_current_fingerprints:
            reject(finding, "duplicate_finding")
            continue
        if matched_fingerprint in previous_by_fingerprint:
            if not finding_location_exists_in_checkout(checkout_dir, finding, previous_by_fingerprint[matched_fingerprint]):
                reject(finding, "stale_previous_location")
                continue
            reportable.append(finding)
            open_fingerprints.add(matched_fingerprint)
            seen_current_fingerprints.add(matched_fingerprint)
            bump_source_stat(source_stats, source, "reported")
            if str(finding.get("verificationStatus") or "").lower() in {"verified", "static_proof"}:
                bump_source_stat(source_stats, source, "confirmed")
            continue
        if has_prior_run and known_sources and source not in known_sources and not finding_has_verification_proof(finding):
            reject(finding, "unknown_source_after_prior_run")
            continue
        if statistically_calibrated_confidence(finding, source_counts) < convergence_min_confidence(finding):
            reject(finding, "low_statistical_confidence")
            continue
        if has_prior_run:
            if not changed_files_loaded:
                changed_files = changed_files_between_heads(checkout_dir, previous_head_sha, current_head_sha, job=job)
                changed_files_loaded = True
            finding_files = finding_delta_files(finding)
            if changed_files is None or not finding_files or not (finding_files & changed_files):
                reject(finding, "not_introduced_by_current_delta")
                continue
            if not changed_line_ranges_loaded:
                changed_line_ranges = changed_line_ranges_between_heads(checkout_dir, previous_head_sha, current_head_sha, job=job)
                changed_line_ranges_loaded = True
            if not finding_line_within_changed_ranges(finding, changed_line_ranges):
                reject(finding, "not_introduced_by_current_delta")
                continue
            if not finding_location_exists_in_checkout(checkout_dir, finding):
                reject(finding, "invalid_candidate_location")
                continue
        reportable.append(finding)
        open_fingerprints.add(matched_fingerprint)
        seen_current_fingerprints.add(matched_fingerprint)
        bump_source_stat(source_stats, source, "reported")
        if str(finding.get("verificationStatus") or "").lower() in {"verified", "static_proof"}:
            bump_source_stat(source_stats, source, "confirmed")

    resolved_fingerprints = []
    for fingerprint, record in previous_by_fingerprint.items():
        if fingerprint not in open_fingerprints:
            resolved_fingerprints.append(fingerprint)
            source = normalized_source_key(record.get("source")) or "reviewer"
            bump_source_stat(source_stats, source, "resolved")

    state = {
        "protocol": CONVERGENCE_PROTOCOL_VERSION,
        "scope_key": convergence_scope_key(job),
        "head_sha": current_head_sha,
        "open_findings": [
            convergence_record_for_finding(
                finding,
                previous_fingerprint_by_issue_id.get(finding_issue_id(finding)) or finding_fingerprint(finding),
            )
            for finding in reportable
        ],
        "resolved_fingerprints": sorted(resolved_fingerprints),
        "source_stats": source_stats,
    }
    return reportable, rejected_reasons, rejected_samples, state


def verification_audit_payload(
    *,
    candidate_count: int,
    reported_findings: list[dict],
    rejected_reasons: dict[str, int],
    rejected_samples: list[dict] | None = None,
) -> dict:
    rejected_count = sum(rejected_reasons.values())
    status_counts = {status: 0 for status in _VERIFICATION_STATUSES}
    for finding in reported_findings:
        status = str(finding.get("verificationStatus") or "").strip().lower()
        if status not in status_counts:
            status = "potential_risk"
        status_counts[status] += 1
    parts = [
        f"{candidate_count} candidates evaluated",
        f"{len(reported_findings)} reported",
    ]
    if rejected_count:
        parts.append(f"{rejected_count} rejected before reporting")
    return {
        "candidateCount": max(0, int(candidate_count)),
        "reportedCount": len(reported_findings),
        "rejectedCount": rejected_count,
        "downgradedCount": 0,
        "verifiedCount": status_counts["verified"],
        "staticProofCount": status_counts["static_proof"],
        "potentialRiskCount": status_counts["potential_risk"],
        "unverifiedCount": status_counts["unverified"],
        "rejectedReasons": [
            {"reason": reason, "count": count}
            for reason, count in sorted(rejected_reasons.items())
            if count > 0
        ],
        "rejectedSamples": [sample for sample in rejected_samples or [] if isinstance(sample, dict)][:5],
        "summary": "; ".join(parts) + ".",
    }


def audit_swarm_scan_artifacts(
    stage: str,
    *,
    config: WorkerConfig | None = None,
    audit_payload: dict | None = None,
    preflight: dict | None = None,
    verification_audit: dict | None = None,
    summary: str = "",
    logs_summary: str = "",
) -> dict:
    audit_payload = audit_payload if isinstance(audit_payload, dict) else {}
    preflight = preflight if isinstance(preflight, dict) else {}
    verification_audit = verification_audit if isinstance(verification_audit, dict) else {}
    cards = [item for item in (first_list(audit_payload, "issue_cards", "issueCards") or []) if isinstance(item, dict)]
    results = [
        item
        for item in (first_list(audit_payload, "verification_results", "verificationResults") or [])
        if isinstance(item, dict)
    ]
    provider_chain = list(getattr(config, "provider_chain", []) or [])
    roles = dedupe_text(
        [
            *[
                clean_protocol_text(card.get("agent_role") or card.get("agentRole"))
                for card in cards
            ],
            *[
                clean_protocol_text(result.get("verifier_role") or result.get("verifierRole"))
                for result in results
            ],
        ]
    )
    shards = dedupe_text(
        [
            clean_protocol_text(card.get("shard_id") or card.get("shardId"))
            for card in cards
        ]
    )
    verifier_runs = []
    verifier = preflight.get("verifier") if isinstance(preflight.get("verifier"), dict) else {}
    if isinstance(verifier.get("runs"), list):
        verifier_runs = [item for item in verifier["runs"] if isinstance(item, dict)]
    counts = {
        "issueCards": len(cards),
        "verificationResults": len(results),
        "candidateCount": protocol_count(verification_audit.get("candidateCount") or verification_audit.get("candidate_count")),
        "reportedCount": protocol_count(verification_audit.get("reportedCount") or verification_audit.get("reported_count")),
        "rejectedCount": protocol_count(verification_audit.get("rejectedCount") or verification_audit.get("rejected_count")),
        "verifiedCount": protocol_count(verification_audit.get("verifiedCount") or verification_audit.get("verified_count")),
        "staticProofCount": protocol_count(verification_audit.get("staticProofCount") or verification_audit.get("static_proof_count")),
        "potentialRiskCount": protocol_count(verification_audit.get("potentialRiskCount") or verification_audit.get("potential_risk_count")),
        "unverifiedCount": protocol_count(verification_audit.get("unverifiedCount") or verification_audit.get("unverified_count")),
        "manifestCount": len(preflight.get("manifests") or []) if isinstance(preflight.get("manifests"), list) else 0,
        "toolCount": len(preflight.get("toolVersions") or []) if isinstance(preflight.get("toolVersions"), list) else 0,
        "verifierRunCount": len(verifier_runs),
    }
    payload = {
        "protocol": clean_protocol_text(audit_payload.get("audit_protocol") or audit_payload.get("auditProtocol"))
        or AUDIT_SWARM_PROTOCOL_VERSION,
        "stage": clean_protocol_text(stage),
        "adapter": provider_chain[0] if provider_chain else clean_protocol_text(getattr(config, "provider", "")),
        "providerChain": provider_chain,
        "summary": protocol_multiline_text(summary) or protocol_multiline_text(verification_audit.get("summary")),
        "logsSummary": protocol_multiline_text(logs_summary)[:1000],
        "counts": {key: value for key, value in counts.items() if value},
        "roles": roles[:12],
        "shards": shards[:20],
        "issueCards": [audit_swarm_issue_card_summary(card, index) for index, card in enumerate(cards[:10])],
        "verificationResults": [
            audit_swarm_verification_result_summary(result)
            for result in results[:20]
        ],
        "evidenceBlocks": audit_swarm_evidence_blocks(
            stage,
            cards=cards,
            results=results,
            preflight=preflight,
            verification_audit=verification_audit,
            summary=summary,
        ),
    }
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def audit_swarm_evidence_blocks(
    stage: str,
    *,
    cards: list[dict],
    results: list[dict],
    preflight: dict,
    verification_audit: dict,
    summary: str = "",
) -> list[dict]:
    blocks: list[dict] = []
    stage_text = clean_protocol_text(stage)
    summary_text = protocol_multiline_text(summary) or protocol_multiline_text(verification_audit.get("summary"))
    if summary_text:
        blocks.append(
            audit_swarm_evidence_block(
                "summary",
                block_id=f"{stage_text or 'audit'}:summary",
                title="Audit summary",
                summary=summary_text,
                stage=stage_text,
            )
        )
    if verification_audit:
        rejected_count = protocol_count(verification_audit.get("rejectedCount") or verification_audit.get("rejected_count"))
        if rejected_count:
            blocks.append(
                audit_swarm_evidence_block(
                    "risk",
                    block_id=f"{stage_text or 'audit'}:rejected",
                    title="Rejected before reporting",
                    summary=f"{rejected_count} candidates were rejected before reporting because they lacked enough evidence.",
                    stage=stage_text,
                    status="rejected",
                )
            )
    if preflight:
        preflight_summary = protocol_multiline_text(preflight.get("summary"))
        if preflight_summary and not cards and not results:
            blocks.append(
                audit_swarm_evidence_block(
                    "summary",
                    block_id=f"{stage_text or 'audit'}:preflight",
                    title="Preflight evidence",
                    summary=preflight_summary,
                    stage=stage_text,
                )
            )
    results_by_issue = audit_swarm_verifications_by_issue(results)
    for index, card in enumerate(cards[:8]):
        blocks.extend(audit_swarm_issue_card_evidence_blocks(card, results_by_issue.get(audit_swarm_issue_key(card), []), index))
    for index, result in enumerate(results[:12]):
        blocks.extend(audit_swarm_verification_evidence_blocks(result, index))
    return audit_swarm_dedupe_blocks(blocks)[:40]


def audit_swarm_issue_card_evidence_blocks(card: dict, results: list[dict], index: int) -> list[dict]:
    issue_id = audit_swarm_issue_key(card) or audit_swarm_generated_id(card, index)
    title = clean_protocol_text(card.get("title")) or f"Audit candidate {index + 1}"
    severity = audit_swarm_severity(card.get("severity"))
    category = audit_swarm_category(card)
    role = clean_protocol_text(card.get("agent_role") or card.get("agentRole"))
    shard_id = clean_protocol_text(card.get("shard_id") or card.get("shardId"))
    confidence = audit_swarm_confidence(card.get("confidence"), audit_swarm_verdict(results))
    common = {
        "issueId": issue_id,
        "severity": severity,
        "category": category,
        "role": role,
        "shardId": shard_id,
        "confidence": confidence,
    }
    blocks = []
    claim = protocol_multiline_text(card.get("claim") or card.get("summary") or card.get("description"))
    if claim:
        blocks.append(
            audit_swarm_evidence_block(
                "claim",
                block_id=f"{issue_id}:claim",
                title=title,
                summary=claim,
                **common,
            )
        )
    for location_index, location in enumerate(audit_swarm_locations(card)[:2]):
        blocks.append(
            audit_swarm_evidence_block(
                "code_location",
                block_id=f"{issue_id}:location:{location_index}",
                title="Code location",
                summary=claim or title,
                file=clean_protocol_text(location.get("file")),
                startLine=protocol_count(location.get("startLine")),
                endLine=protocol_count(location.get("endLine")),
                **common,
            )
        )
    for evidence_index, evidence in enumerate(protocol_text_items(card.get("evidence"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "evidence",
                block_id=f"{issue_id}:evidence:{evidence_index}",
                title="Discovery evidence",
                summary=evidence,
                **common,
            )
        )
    for check_index, check in enumerate(protocol_text_list(card.get("false_positive_checks") or card.get("falsePositiveChecks"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "false_positive_check",
                block_id=f"{issue_id}:false-positive:{check_index}",
                title="False-positive check",
                summary=check,
                **common,
            )
        )
    for invariant_index, invariant in enumerate(protocol_text_list(card.get("violated_invariants") or card.get("violatedInvariants"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "invariant",
                block_id=f"{issue_id}:invariant:{invariant_index}",
                title="Violated invariant",
                summary=invariant,
                **common,
            )
        )
    suggested_test = protocol_multiline_text(card.get("suggested_test") or card.get("suggestedTest"))
    if suggested_test:
        blocks.append(
            audit_swarm_evidence_block(
                "command",
                block_id=f"{issue_id}:suggested-test",
                title="Suggested test",
                summary=suggested_test,
                status="suggested",
                **common,
            )
        )
    return blocks


def audit_swarm_verification_evidence_blocks(result: dict, index: int) -> list[dict]:
    issue_id = clean_protocol_text(result.get("issue_id") or result.get("issueId"))
    role = clean_protocol_text(result.get("verifier_role") or result.get("verifierRole"))
    verdict = clean_protocol_text(result.get("verdict")).lower()
    proof_type = clean_protocol_text(result.get("proof_type") or result.get("proofType"))
    confidence = audit_swarm_confidence(result.get("confidence"), verdict)
    summary = protocol_multiline_text(result.get("result_summary") or result.get("resultSummary") or result.get("summary"))
    common = {
        "issueId": issue_id,
        "role": role,
        "verdict": verdict if verdict in {"confirmed", "rejected", "inconclusive"} else "",
        "proofType": proof_type,
        "proofStrength": protocol_count(result.get("proof_strength") or result.get("proofStrength")),
        "confidence": confidence,
    }
    key = issue_id or f"verification-{index}"
    blocks = [
        audit_swarm_evidence_block(
            "verifier_verdict",
            block_id=f"{key}:verdict:{role or index}",
            title="Verifier verdict",
            summary=summary or f"{role or 'verifier'} returned {common['verdict'] or 'a verdict'}.",
            **common,
        )
    ]
    for command_index, command in enumerate(protocol_text_list(result.get("commands_run") or result.get("commandsRun"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "command",
                block_id=f"{key}:command:{command_index}",
                title="Verifier command",
                summary=summary,
                command=command,
                status="executed",
                **common,
            )
        )
    for evidence_index, evidence in enumerate(protocol_text_items(result.get("evidence"))[:3]):
        blocks.append(
            audit_swarm_evidence_block(
                "evidence",
                block_id=f"{key}:verification-evidence:{evidence_index}",
                title="Verifier evidence",
                summary=evidence,
                **common,
            )
        )
    return blocks


def audit_swarm_evidence_block(kind: str, *, block_id: str = "", title: str = "", summary: str = "", **fields: object) -> dict:
    normalized_kind = clean_protocol_text(kind).lower()
    if normalized_kind not in AUDIT_SWARM_EVIDENCE_BLOCK_KINDS:
        normalized_kind = "evidence"
    payload = {
        "id": clean_protocol_text(block_id),
        "kind": normalized_kind,
        "title": clean_protocol_text(title),
        "summary": protocol_multiline_text(summary),
    }
    for key in (
        "issueId",
        "severity",
        "category",
        "role",
        "shardId",
        "stage",
        "status",
        "verdict",
        "proofType",
        "command",
        "file",
    ):
        text = clean_protocol_text(fields.get(key))
        if text:
            payload[key] = text
    for key in ("startLine", "endLine", "proofStrength"):
        count = protocol_count(fields.get(key))
        if count:
            payload[key] = count
    if "confidence" in fields:
        try:
            confidence = float(fields["confidence"])
        except (OverflowError, TypeError, ValueError):
            confidence = 0.0
        if confidence:
            payload["confidence"] = max(0.0, min(1.0, confidence))
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def audit_swarm_dedupe_blocks(blocks: list[dict]) -> list[dict]:
    deduped = []
    seen = set()
    for block in blocks:
        if not isinstance(block, dict):
            continue
        key = (
            clean_protocol_text(block.get("kind")),
            clean_protocol_text(block.get("issueId")),
            clean_protocol_text(block.get("title")),
            protocol_multiline_text(block.get("summary")),
            clean_protocol_text(block.get("command")),
            clean_protocol_text(block.get("file")),
            protocol_count(block.get("startLine")),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(block)
    return deduped


def audit_swarm_issue_card_summary(card: dict, index: int) -> dict:
    locations = audit_swarm_locations(card)
    primary = locations[0] if locations else {}
    payload = {
        "issueId": audit_swarm_issue_key(card) or audit_swarm_generated_id(card, index),
        "title": clean_protocol_text(card.get("title")) or f"Audit candidate {index + 1}",
        "severity": audit_swarm_severity(card.get("severity")),
        "category": audit_swarm_category(card),
        "shardId": clean_protocol_text(card.get("shard_id") or card.get("shardId")),
        "agentRole": clean_protocol_text(card.get("agent_role") or card.get("agentRole")),
        "confidence": audit_swarm_confidence(card.get("confidence"), "candidate"),
        "file": clean_protocol_text(primary.get("file")),
        "line": protocol_count(primary.get("startLine")),
        "evidenceCount": len(card.get("evidence") or []) if isinstance(card.get("evidence"), list) else 0,
    }
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def audit_swarm_verification_result_summary(result: dict) -> dict:
    commands = protocol_text_list(result.get("commands_run") or result.get("commandsRun"))
    evidence = protocol_text_list(result.get("evidence"))
    payload = {
        "issueId": clean_protocol_text(result.get("issue_id") or result.get("issueId")),
        "verifierRole": clean_protocol_text(result.get("verifier_role") or result.get("verifierRole")),
        "verdict": clean_protocol_text(result.get("verdict")),
        "proofType": clean_protocol_text(result.get("proof_type") or result.get("proofType")),
        "proofStrength": protocol_count(result.get("proof_strength") or result.get("proofStrength")),
        "confidence": audit_swarm_confidence(result.get("confidence"), clean_protocol_text(result.get("verdict"))),
        "commandCount": len(commands),
        "evidenceCount": len(evidence),
        "summary": protocol_multiline_text(result.get("result_summary") or result.get("resultSummary")),
    }
    if commands:
        payload["command"] = commands[0]
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def protocol_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, count)


def result_checksum(payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def redact_secrets(text: str, config: WorkerConfig | None = None) -> str:
    redacted = str(text or "")
    if config and config.worker_token:
        redacted = redacted.replace(config.worker_token, "[redacted]")
    redacted = re.sub(r"x-access-token:[^@\s]+@", "x-access-token:[redacted]@", redacted)
    return redacted


def write_scan_summary(config: WorkerConfig, job_id: str, status: str, duration_ms: int, error: str) -> None:
    config.log_dir.mkdir(parents=True, exist_ok=True)
    path = config.log_dir / "scan-summary.log"
    line = json.dumps(
        {
            "time": int(time.time()),
            "job_id": job_id,
            "status": status,
            "duration_ms": duration_ms,
            "error": redact_secrets(error, config),
        },
        sort_keys=True,
    )
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(line + "\n")
    trim_file_to_last_bytes(path, config.scan_summary_log_max_bytes)


def worker_readiness_checks(config: WorkerConfig) -> tuple[list[tuple[str, bool, str]], bool]:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("server_url", bool(config.server_url.startswith(("http://", "https://"))), config.server_url))
    checks.append(("worker_token", bool(config.worker_token), "configured" if config.worker_token else "missing"))
    checks.append(("max_concurrent_jobs", config.max_concurrent_jobs > 0, str(config.max_concurrent_jobs)))

    git_ok, git_detail = command_ok(["git", "--version"])
    checks.append(("git", git_ok, git_detail))
    ready_providers: list[str] = []
    if "codex" in config.provider_chain:
        node_ok, node_detail = node_version_check()
        checks.append(("node", node_ok, node_detail))
        codex_cli_ok, codex_cli_detail = command_ok([config.codex_command, "--version"])
        checks.append(("codex", codex_cli_ok, codex_cli_detail))
        codex_login_ok, codex_login_detail = codex_ready_check(config) if codex_cli_ok else (False, "skipped until codex CLI passes --version")
        checks.append(("codex_ready", codex_login_ok, codex_login_detail))
        if node_ok and codex_cli_ok and codex_login_ok:
            ready_providers.append("codex")
    if "opencode" in config.provider_chain:
        opencode_ok, opencode_detail = command_ok([config.opencode_command, "--version"])
        checks.append(("opencode", opencode_ok, opencode_detail))
        if opencode_ok:
            ready_providers.append("opencode")
    provider_ready = bool(ready_providers)
    checks.append(("provider_ready", provider_ready, ", ".join(ready_providers) if provider_ready else "no configured provider is ready"))

    for label, path in (("checkout_root", config.work_dir), ("log_dir", config.log_dir)):
        ok, detail = writable_path_check(path)
        checks.append((label, ok, detail))
    checks.append(("disk_space", *disk_space_check(config.work_dir)))
    return checks, provider_ready


def first_failed_check(checks: list[tuple[str, bool, str]]) -> tuple[str, bool, str] | None:
    provider_ready = readiness_check_ok(checks, "provider_ready")
    optional_provider_checks = {"node", "codex", "codex_ready", "opencode"}
    for check in checks:
        name, ok, _detail = check
        if ok:
            continue
        if provider_ready and name in optional_provider_checks:
            continue
        return check
    return None


def readiness_check_ok(checks: list[tuple[str, bool, str]], name: str) -> bool:
    return any(check_name == name and ok for check_name, ok, _detail in checks)


def readiness_error_message(check: tuple[str, bool, str], config: WorkerConfig) -> str:
    name, _ok, detail = check
    return f"worker not ready: {name}: {redact_secrets(detail, config)}"[:500]


def writable_path_check(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / f".pullwise-write-test-{os.getpid()}"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return True, str(path)
    except Exception as exc:
        return False, str(exc)


def disk_space_check(path: Path) -> tuple[bool, str]:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    try:
        usage = shutil.disk_usage(target)
    except Exception as exc:
        return False, str(exc)
    return usage.free > _MIN_READY_DISK_BYTES, f"{usage.free // (1024 * 1024)} MB free"


def run_doctor(config: WorkerConfig) -> bool:
    checks, provider_ready = worker_readiness_checks(config)
    codex_ready = readiness_check_ok(checks, "codex_ready")
    systemd_ok, systemd_detail = command_ok(["systemctl", "is-active", "pullwise-worker"])
    checks.append(("systemd", systemd_ok, systemd_detail))
    heartbeat_ok = True
    heartbeat_detail = "ok"
    doctor_required_ok = first_failed_check(checks) is None
    try:
        PullwiseClient(config).heartbeat(
            last_error=None,
            doctor_status="ok" if doctor_required_ok else "degraded",
            codex_ready=codex_ready,
            systemd_active=systemd_ok,
            doctor_checked_at=int(time.time()),
        )
    except Exception as exc:
        heartbeat_ok = False
        heartbeat_detail = redact_secrets(str(exc), config)
    checks.append(("heartbeat", heartbeat_ok, heartbeat_detail))
    for name, ok, detail in checks:
        print(f"{'ok' if ok else 'fail'} {name}: {detail}")
    codex_login_check = next((check for check in checks if check[0] == "codex_ready"), None)
    if not provider_ready and "codex" in config.provider_chain and codex_login_check and not codex_login_check[1]:
        print(f"Codex may require device authorization. Run: {CODEX_LOGIN_COMMAND}")
    return first_failed_check(checks) is None


def command_ok(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        detail = (completed.stdout or completed.stderr).strip().splitlines()
        return completed.returncode == 0, detail[0] if detail else f"exit {completed.returncode}"
    except FileNotFoundError:
        return False, "not found"
    except Exception as exc:
        return False, str(exc)


def node_version_check() -> tuple[bool, str]:
    ok, detail = command_ok(["node", "--version"])
    if not ok:
        return False, detail
    match = re.search(r"v?(\d+)", detail.strip())
    if not match:
        return False, f"unable to parse Node.js version: {detail}"
    if int(match.group(1)) < _MIN_NODE_MAJOR:
        return False, f"Node.js {_MIN_NODE_MAJOR}+ required, found {detail}"
    return True, detail


def codex_node_runtime_error(output: str) -> bool:
    lowered = output.lower()
    return "@openai/codex" in lowered and "syntaxerror: unexpected reserved word" in lowered


def codex_ready_check(config: WorkerConfig) -> tuple[bool, str]:
    command = [
        config.codex_command,
        "exec",
        _CODEX_SKIP_GIT_REPO_CHECK_ARG,
        "--ignore-user-config",
        "--json",
        "--config",
        f'model_reasoning_effort="{config.codex_reasoning_effort}"',
        "--sandbox",
        "read-only",
    ]
    if config.codex_model:
        command.extend(["--model", config.codex_model])
    command.append('Return only JSON: {"ok": true}')
    auth_failure = codex_auth_failure_error(config)
    if auth_failure:
        return False, auth_failure
    if not _CODEX_EXEC_LOCK.acquire(blocking=False):
        return True, "ready check deferred while codex is running"
    try:
        auth_failure = codex_auth_failure_error(config)
        if auth_failure:
            return False, auth_failure
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.codex_doctor_timeout_seconds,
        )
    except FileNotFoundError:
        return False, "codex not found"
    except subprocess.TimeoutExpired:
        return False, "codex ready check timed out"
    except Exception as exc:
        return False, str(exc)
    finally:
        _CODEX_EXEC_LOCK.release()
    output = redact_secrets((completed.stderr or completed.stdout).strip(), config)
    detail = output.splitlines()[0] if output else f"exit {completed.returncode}"
    if completed.returncode == 0:
        return True, "ready"
    if looks_like_codex_auth_failure(output):
        mark_codex_auth_failure(config, output)
    lowered = output.lower()
    if "login" in lowered or "auth" in lowered or "api key" in lowered or "not authenticated" in lowered:
        return False, "not logged in"
    if codex_node_runtime_error(output):
        node_ok, node_detail = node_version_check()
        if not node_ok:
            return False, node_detail
        return False, "Codex CLI failed to start; reinstall Codex CLI or verify Node.js 20+"
    return False, detail


def service_action(action: str, *, dry_run: bool = False, no_block: bool = False) -> int:
    command = ["systemctl"]
    if no_block:
        command.append("--no-block")
    command.extend([action, "pullwise-worker"])
    if dry_run:
        print(" ".join(command))
        return 0
    return subprocess.run(command).returncode


def execute_lifecycle_command(action: str) -> int:
    if action in {"stop", "uninstall"}:
        # Admin-queued lifecycle commands run inside the unprivileged service
        # process. Exit cleanly and let Restart=on-failure keep it stopped.
        # Full local uninstall still requires root via `pullwise-worker uninstall`.
        return 0
    return 2


def default_worker_package() -> str:
    package = os.environ.get("PULLWISE_WORKER_PACKAGE")
    if package:
        return package
    return f"{DEFAULT_WORKER_PACKAGE_BASE_URL}/v{__version__}/pullwise_worker-{__version__}-py3-none-any.whl"


def service_user_doctor_command(bin_path: Path) -> list[str]:
    service_user = os.environ.get("PULLWISE_SERVICE_USER", "").strip() or "pullwise-worker"
    service_home = os.environ.get("PULLWISE_SERVICE_HOME", "").strip() or "/var/lib/pullwise-worker"
    service_path = (
        os.environ.get("PULLWISE_SERVICE_PATH", "").strip()
        or "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    )
    service_bin = str(bin_path).replace("\\", "/")
    return [
        "runuser",
        "-u",
        service_user,
        "--",
        "env",
        f"HOME={service_home}",
        f"PATH={service_path}",
        service_bin,
        "doctor",
    ]


def worker_wrapper_script(env_path: Path) -> str:
    env_file = shlex.quote(str(env_path))
    return f"""#!/usr/bin/env bash
set -euo pipefail
load_worker_env() {{
  local env_file="$1"
  local key value
  [ -f "$env_file" ] || return 0
  while IFS="=" read -r key value || [ -n "$key" ]; do
    [[ -z "$key" || "$key" == \\#* ]] && continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$key=$value"
  done < "$env_file"
}}
load_worker_env {env_file}
export PATH="${{PULLWISE_SERVICE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}}"
PYTHON_BIN="${{PULLWISE_PYTHON_BIN:-python3}}"
exec "$PYTHON_BIN" -m pullwise_worker.main "$@"
"""


def write_worker_wrapper(bin_path: Path, env_path: Path) -> None:
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_text(worker_wrapper_script(env_path), encoding="utf-8")
    bin_path.chmod(0o755)


def update_worker(config: WorkerConfig, *, dry_run: bool = False) -> int:
    package = default_worker_package()
    python_bin = os.environ.get("PULLWISE_PYTHON_BIN", "").strip() or "python3"
    env_path = Path(os.environ.get("PULLWISE_WORKER_ENV_FILE") or "/etc/pullwise-worker/worker.env")
    backup_path = Path(os.environ.get("PULLWISE_WORKER_ENV_BACKUP_FILE") or "/etc/pullwise-worker/worker.env.bak")
    bin_path = Path(os.environ.get("PULLWISE_WORKER_BIN_PATH") or "/usr/local/bin/pullwise-worker")
    install_command = [python_bin, "-m", "pip", "install", "--upgrade", package]
    commands = [
        ["systemctl", "stop", "pullwise-worker"],
        install_command,
        ["systemctl", "restart", "pullwise-worker"],
        service_user_doctor_command(bin_path),
    ]
    if dry_run:
        print(f"backup {env_path} to {backup_path}")
    else:
        try:
            if env_path.exists():
                shutil.copy2(env_path, backup_path)
        except OSError as exc:
            print(f"failed to back up env file: {exc}", file=sys.stderr)
            return 1
    for command in commands:
        if dry_run:
            print(" ".join(command))
            if command is install_command:
                print(f"write env-loading wrapper {bin_path}")
            continue
        completed = subprocess.run(command)
        if completed.returncode != 0:
            if backup_path.exists():
                shutil.copy2(backup_path, env_path)
            subprocess.run(["systemctl", "restart", "pullwise-worker"])
            return completed.returncode
        if command is install_command:
            try:
                write_worker_wrapper(bin_path, env_path)
            except OSError as exc:
                print(f"failed to write worker wrapper: {exc}", file=sys.stderr)
                if backup_path.exists():
                    shutil.copy2(backup_path, env_path)
                subprocess.run(["systemctl", "restart", "pullwise-worker"])
                return 1
    return 0


def uninstall_worker(
    *,
    remove_config: bool = False,
    remove_logs: bool = False,
    dry_run: bool = False,
) -> int:
    commands = [
        ["systemctl", "stop", "pullwise-worker"],
        ["systemctl", "disable", "pullwise-worker"],
    ]
    for command in commands:
        if dry_run:
            print(" ".join(command))
            continue
        completed = subprocess.run(command)
        if completed.returncode != 0:
            return completed.returncode
    if dry_run:
        print("remove /etc/systemd/system/pullwise-worker.service")
        if remove_config:
            print("remove /etc/pullwise-worker")
        if remove_logs:
            print("remove /var/log/pullwise-worker")
        print("systemctl daemon-reload")
    else:
        safe_unlink(Path("/etc/systemd/system/pullwise-worker.service"))
        if remove_config:
            safe_rmtree(Path("/etc/pullwise-worker"), Path("/etc/pullwise-worker"))
        if remove_logs:
            safe_rmtree(Path("/var/log/pullwise-worker"), Path("/var/log/pullwise-worker"))
        completed = subprocess.run(["systemctl", "daemon-reload"])
        if completed.returncode != 0:
            return completed.returncode
    print("Worker disabled locally. Disable or delete it from Pullwise admin separately.")
    return 0


def cleanup_worker_resources(config: WorkerConfig, *, active_job_ids: set[str] | None = None) -> None:
    cleanup_checkouts(config, active_job_ids=active_job_ids)
    cleanup_logs(config, active_job_ids=active_job_ids)


def cleanup_checkouts(config: WorkerConfig, *, active_job_ids: set[str] | None = None) -> None:
    now_ts = int(time.time())
    active = set(active_job_ids or set())
    protected = active | _CHECKOUT_RUNTIME_DIR_NAMES
    config.work_dir.mkdir(parents=True, exist_ok=True)
    if not checkout_root_is_owned(config.work_dir):
        return
    for marker in config.work_dir.glob(f"*{_FAILED_CHECKOUT_MARKER_SUFFIX}"):
        checkout = checkout_dir_from_failed_marker(marker)
        if checkout.name in protected:
            continue
        try:
            expires_at = int(marker.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            expires_at = 0
        if expires_at <= now_ts:
            shutil.rmtree(checkout, ignore_errors=True)
            marker.unlink(missing_ok=True)
    entries = sorted(
        [path for path in config.work_dir.iterdir() if path.is_dir() and path.name not in protected],
        key=lambda path: path.stat().st_mtime,
    )
    while directory_size(config.work_dir) > config.max_checkout_bytes and entries:
        checkout = entries.pop(0)
        shutil.rmtree(checkout, ignore_errors=True)
        failed_checkout_marker(checkout).unlink(missing_ok=True)


def cleanup_logs(config: WorkerConfig, *, active_job_ids: set[str] | None = None) -> None:
    active = set(active_job_ids or set())
    config.log_dir.mkdir(parents=True, exist_ok=True)
    now_ts = int(time.time())
    files: list[tuple[float, Path]] = []
    for path in config.log_dir.rglob("*"):
        try:
            if not path.is_file() or log_path_has_active_job_id(path, config.log_dir, active):
                continue
            stat = path.stat()
        except OSError:
            continue
        if config.log_retention_seconds and stat.st_mtime < now_ts - config.log_retention_seconds:
            path.unlink(missing_ok=True)
            continue
        files.append((stat.st_mtime, path))
    files.sort(key=lambda item: item[0])
    while directory_size(config.log_dir) > config.max_log_bytes and files:
        _mtime, path = files.pop(0)
        try:
            if not log_path_has_active_job_id(path, config.log_dir, active):
                path.unlink(missing_ok=True)
        except OSError:
            continue
    prune_empty_directories(config.log_dir)


def log_path_has_active_job_id(path: Path, log_dir: Path, active_job_ids: set[str]) -> bool:
    if not active_job_ids:
        return False
    try:
        parts = path.resolve(strict=False).relative_to(log_dir.resolve(strict=False)).parts
    except ValueError:
        return True
    return any(part in active_job_ids for part in parts)


def prune_empty_directories(root: Path) -> None:
    directories = sorted(
        [item for item in root.rglob("*") if item.is_dir()],
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for path in directories:
        try:
            path.rmdir()
        except OSError:
            continue


def trim_file_to_last_bytes(path: Path, max_bytes: int) -> None:
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    keep = max(1, max_bytes)
    with path.open("rb") as handle:
        handle.seek(-keep, os.SEEK_END)
        data = handle.read()
    newline = data.find(b"\n")
    if newline >= 0 and newline + 1 < len(data):
        data = data[newline + 1 :]
    with path.open("wb") as handle:
        handle.write(data)


def safe_unlink(path: Path) -> None:
    if str(path) != "/etc/systemd/system/pullwise-worker.service":
        raise ValueError(f"refusing to remove unexpected file: {path}")
    path.unlink(missing_ok=True)


def safe_rmtree(path: Path, allowed_root: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to remove symlinked directory: {path}")
    resolved = path.resolve(strict=False)
    allowed = allowed_root.resolve(strict=False)
    if resolved != allowed:
        raise ValueError(f"refusing to remove unexpected directory: {path}")
    shutil.rmtree(path, ignore_errors=True)


def directory_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


if __name__ == "__main__":
    main()
