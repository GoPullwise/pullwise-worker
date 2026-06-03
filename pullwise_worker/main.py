from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

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
_MIN_READY_DISK_BYTES = 1024 * 1024 * 1024
_MIN_NODE_MAJOR = 20
DEFAULT_WORKER_PACKAGE_BASE_URL = "https://github.com/GoPullwise/pullwise-worker/releases/download"


class WorkerConfig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.server_url = (getattr(args, "server_url", None) or os.environ.get("PULLWISE_SERVER_URL") or "http://localhost:8080").rstrip("/")
        self.worker_token = getattr(args, "worker_token", None) or os.environ.get("PULLWISE_WORKER_TOKEN") or ""
        self.worker_id = getattr(args, "worker_id", None) or os.environ.get("PULLWISE_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
        self.provider = getattr(args, "provider", None) or os.environ.get("PULLWISE_PROVIDER") or "codex"
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
        self.codex_timeout_seconds = max(60, int(getattr(args, "codex_timeout_seconds", None) or os.environ.get("PULLWISE_CODEX_TIMEOUT_SECONDS") or 1800))
        self.codex_doctor_timeout_seconds = max(10, int(os.environ.get("PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS") or 60))
        self.readiness_check_seconds = max(10, int(os.environ.get("PULLWISE_READINESS_CHECK_SECONDS") or 60))
        self.result_upload_attempts = max(1, int(os.environ.get("PULLWISE_RESULT_UPLOAD_ATTEMPTS") or 5))
        self.failed_checkout_retention_seconds = max(0, int(os.environ.get("PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS") or 0))
        self.max_checkout_bytes = max(1, int(os.environ.get("PULLWISE_MAX_CHECKOUT_BYTES") or 20 * 1024 * 1024 * 1024))
        if not self.worker_token:
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
        parsed = json.loads(self.body.decode("utf-8"))
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

    def progress(self, job_id: str, phase: str, progress: int, message: str = "", logs_summary: str = "") -> None:
        self.post(
            f"/worker/jobs/{job_id}/progress",
            {
                "phase": phase,
                "progress": progress,
                "message": message,
                "started_at": int(time.time()),
                "logs_summary": logs_summary,
            },
        )

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

    try:
        config = WorkerConfig(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    if args.command == "doctor":
        raise SystemExit(0 if run_doctor(config) else 1)
    if args.command in {"start", "stop", "status", "restart"}:
        raise SystemExit(service_action(args.command, dry_run=args.dry_run))
    if args.command == "update":
        raise SystemExit(update_worker(config, dry_run=args.dry_run))
    if args.command == "uninstall":
        raise SystemExit(uninstall_worker(remove_config=args.remove_config, remove_logs=args.remove_logs, dry_run=args.dry_run))
    if args.command == "cleanup":
        cleanup_checkouts(config)
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

    def run(self, *, once: bool = False) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
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
                            jobs = self.client.claim_many(free_slots)
                        except PullwiseRequestError as exc:
                            self.last_error = f"job claim failed: {redact_secrets(str(exc), self.config)}"[:500]
                            loop_error = True
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
            return self._doctor_status == "ok" and self._codex_ready
        checks, codex_ready = worker_readiness_checks(self.config)
        failed_check = first_failed_check(checks)
        self._codex_ready = codex_ready
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
        try:
            self.client.progress(job_id, "clone", PHASE_PROGRESS["clone"], "Cloning repository")
            clone_repository(job, checkout_dir)
            self.client.progress(job_id, "index", PHASE_PROGRESS["index"], "Repository ready")
            self.client.progress(job_id, "ai", PHASE_PROGRESS["ai"], "Running Codex review")
            findings, summary, logs_summary = run_codex_review(self.config, job, checkout_dir)
            duration_ms = int((time.monotonic() - started) * 1000)
            payload = {
                "status": "done",
                "findings": findings,
                "summary": summary,
                "duration_ms": duration_ms,
                "attempt_id": attempt_id,
            }
            payload["result_checksum"] = result_checksum(payload)
            self.client.progress(job_id, "report", 100, "Uploading result", logs_summary)
            try:
                self.upload_result_with_retry(job_id, payload)
            except Exception as exc:
                self.last_error = f"result upload failed for {job_id}: {redact_secrets(str(exc), self.config)}"[:500]
                write_scan_summary(self.config, job_id, "upload_failed", duration_ms, self.last_error)
                return
            write_scan_summary(self.config, job_id, "done", duration_ms, "")
            self.last_error = None
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            error = redact_secrets(str(exc)[:500], self.config)
            error_payload = {
                "status": "failed",
                "findings": [],
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "duration_ms": duration_ms,
                "error": error,
                "attempt_id": attempt_id,
            }
            error_payload["result_checksum"] = result_checksum(error_payload)
            try:
                self.upload_result_with_retry(job_id, error_payload)
            except Exception as upload_exc:
                self.last_error = f"failed result upload failed for {job_id}: {redact_secrets(str(upload_exc), self.config)}"[:500]
                write_scan_summary(self.config, job_id, "upload_failed", duration_ms, self.last_error)
                return
            write_scan_summary(self.config, job_id, "failed", duration_ms, error)
            self.last_error = error
        finally:
            if self.last_error and self.config.failed_checkout_retention_seconds > 0:
                marker = checkout_dir.with_suffix(".failed-retain")
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


def clone_repository(job: dict, checkout_dir: Path) -> None:
    shutil.rmtree(checkout_dir, ignore_errors=True)
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_url = str(job.get("clone_url") or "")
    if not clone_url:
        repo = str(job.get("repo") or "")
        clone_url = f"https://github.com/{repo}.git"
    git_env = git_auth_env(job.get("clone_token"))
    run_git_command(
        ["git", "clone", "--depth", "1", "--branch", str(job.get("branch") or "main"), clone_url, str(checkout_dir)],
        phase="clone",
        env=git_env,
    )
    commit = str(job.get("commit") or "pending")
    if commit and commit != "pending":
        run_git_command(
            ["git", "-C", str(checkout_dir), "checkout", commit],
            phase="checkout",
        )


def run_git_command(command: list[str], *, phase: str, env: dict[str, str] | None = None) -> None:
    try:
        subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
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


def run_codex_review(config: WorkerConfig, job: dict, checkout_dir: Path) -> tuple[list[dict], dict, str]:
    prompt = review_prompt(job)
    with tempfile.TemporaryDirectory(prefix="pullwise-codex-") as tmpdir:
        schema_path = Path(tmpdir) / "findings.schema.json"
        output_path = Path(tmpdir) / "findings.json"
        schema_path.write_text(json.dumps(findings_schema()), encoding="utf-8")
        command = [
            config.codex_command,
            "exec",
            "--ignore-user-config",
            "--config",
            'model_reasoning_effort="xhigh"',
            "--sandbox",
            "read-only",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            prompt,
        ]
        completed = subprocess.run(
            command,
            cwd=str(checkout_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=config.codex_timeout_seconds,
        )
        logs_summary = redact_secrets((completed.stderr or completed.stdout)[-1000:], config)
        if completed.returncode != 0:
            raise RuntimeError(f"codex exec failed with exit code {completed.returncode}: {logs_summary[:300]}")
        output = output_path.read_text(encoding="utf-8") if output_path.exists() else completed.stdout
    findings = parse_findings(output)
    return findings, summarize(findings), logs_summary


def review_prompt(job: dict) -> str:
    required_fields = ", ".join(
        [
            "id",
            "severity",
            "category",
            "title",
            "summary",
            "impact",
            "detectionReasoning",
            "reproductionPath",
            "file",
            "line",
            "confidence",
            "confidenceRationale",
            "autoFix",
            "effort",
            "fixBenefits",
            "fixRisks",
            "tags",
            "steps",
            "badCode",
            "goodCode",
            "references",
        ]
    )
    return (
        "Review this repository for production-impacting bugs, security issues, dependency risks, "
        "and reliability problems. Return only JSON with a top-level findings array. Each finding must "
        f"include these schema-required fields: {required_fields}. Use empty arrays for badCode, "
        "goodCode, and references when not applicable. "
        f"Repository: {job.get('repo')} branch: {job.get('branch')} commit: {job.get('commit')}."
    )


def parse_findings(output: str) -> list[dict]:
    decoder = json.JSONDecoder()
    text = output.strip()
    candidates = [text]
    first = text.find("{")
    last = text.rfind("}")
    if first >= 0 and last > first:
        candidates.append(text[first : last + 1])
    candidates.extend(line.strip() for line in text.splitlines() if line.strip().startswith(("{", "[")))
    matched: list[dict] | None = None
    for candidate in candidates:
        try:
            parsed = decoder.decode(candidate)
        except json.JSONDecodeError:
            continue
        findings = findings_from_payload(parsed)
        if findings is not None:
            matched = findings
    if matched is not None:
        return matched
    raise RuntimeError("codex exec did not return a JSON findings payload")


def findings_from_payload(parsed: object) -> list[dict] | None:
    if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list) and "event" not in parsed:
        return [item for item in parsed["findings"] if isinstance(item, dict)]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return None


def findings_schema() -> dict:
    code_line = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "ln": {"type": "integer"},
            "code": {"type": "string"},
            "t": {"type": ["string", "null"], "enum": ["del", "add", None]},
        },
        "required": ["ln", "code", "t"],
    }
    reference = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "label": {"type": "string"},
            "url": {"type": "string"},
        },
        "required": ["label", "url"],
    }
    finding_properties = {
        "id": {"type": "string"},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", "info"]},
        "category": {"type": "string"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "impact": {"type": "string"},
        "detectionReasoning": {"type": "string"},
        "reproductionPath": {"type": "string"},
        "file": {"type": "string"},
        "line": {"type": "integer"},
        "confidence": {"type": "number"},
        "confidenceRationale": {"type": "string"},
        "autoFix": {"type": "boolean"},
        "effort": {"type": "string"},
        "fixBenefits": {"type": "string"},
        "fixRisks": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "steps": {"type": "array", "items": {"type": "string"}},
        "badCode": {"type": "array", "items": code_line},
        "goodCode": {"type": "array", "items": code_line},
        "references": {"type": "array", "items": reference},
    }
    finding = {
        "type": "object",
        "additionalProperties": False,
        "properties": finding_properties,
        "required": list(finding_properties),
    }
    return {
        "type": "object",
        "required": ["findings"],
        "additionalProperties": False,
        "properties": {"findings": {"type": "array", "items": finding, "maxItems": 25}},
    }


def summarize(findings: list[dict]) -> dict:
    summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for finding in findings:
        severity = str(finding.get("severity") or "low").lower()
        if severity not in summary:
            severity = "low"
        summary[severity] += 1
    return summary


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


def worker_readiness_checks(config: WorkerConfig) -> tuple[list[tuple[str, bool, str]], bool]:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("server_url", bool(config.server_url.startswith(("http://", "https://"))), config.server_url))
    checks.append(("worker_token", bool(config.worker_token), "configured" if config.worker_token else "missing"))
    checks.append(("max_concurrent_jobs", config.max_concurrent_jobs > 0, str(config.max_concurrent_jobs)))

    git_ok, git_detail = command_ok(["git", "--version"])
    checks.append(("git", git_ok, git_detail))
    node_ok, node_detail = node_version_check()
    checks.append(("node", node_ok, node_detail))
    codex_cli_ok, codex_cli_detail = command_ok([config.codex_command, "--version"])
    checks.append(("codex", codex_cli_ok, codex_cli_detail))
    codex_login_ok, codex_login_detail = codex_ready_check(config) if codex_cli_ok else (False, "skipped until codex CLI passes --version")
    checks.append(("codex_ready", codex_login_ok, codex_login_detail))

    for label, path in (("checkout_root", config.work_dir), ("log_dir", config.log_dir)):
        ok, detail = writable_path_check(path)
        checks.append((label, ok, detail))
    checks.append(("disk_space", *disk_space_check(config.work_dir)))
    return checks, bool(node_ok and codex_cli_ok and codex_login_ok)


def first_failed_check(checks: list[tuple[str, bool, str]]) -> tuple[str, bool, str] | None:
    return next((check for check in checks if not check[1]), None)


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
    checks, codex_ready = worker_readiness_checks(config)
    systemd_ok, systemd_detail = command_ok(["systemctl", "is-active", "pullwise-worker"])
    checks.append(("systemd", systemd_ok, systemd_detail))
    heartbeat_ok = True
    heartbeat_detail = "ok"
    doctor_required_ok = all(ok for name, ok, _detail in checks if name != "heartbeat")
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
    if codex_login_check and not codex_login_check[1] and codex_login_check[2] == "not logged in":
        print("Codex may require interactive login: sudo -u pullwise-worker codex login")
    return all(ok for _name, ok, _detail in checks)


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
        "--json",
        'Return only JSON: {"ok": true}',
    ]
    try:
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
    output = redact_secrets((completed.stderr or completed.stdout).strip(), config)
    detail = output.splitlines()[0] if output else f"exit {completed.returncode}"
    if completed.returncode == 0:
        return True, "ready"
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


def update_worker(config: WorkerConfig, *, dry_run: bool = False) -> int:
    package = default_worker_package()
    env_path = Path(os.environ.get("PULLWISE_WORKER_ENV_FILE") or "/etc/pullwise-worker/worker.env")
    backup_path = Path(os.environ.get("PULLWISE_WORKER_ENV_BACKUP_FILE") or "/etc/pullwise-worker/worker.env.bak")
    commands = [
        ["systemctl", "stop", "pullwise-worker"],
        ["python3", "-m", "pip", "install", "--upgrade", package],
        ["pullwise-worker", "doctor"],
        ["systemctl", "restart", "pullwise-worker"],
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
            continue
        completed = subprocess.run(command)
        if completed.returncode != 0:
            if backup_path.exists():
                shutil.copy2(backup_path, env_path)
            subprocess.run(["systemctl", "restart", "pullwise-worker"])
            return completed.returncode
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


def cleanup_checkouts(config: WorkerConfig) -> None:
    now_ts = int(time.time())
    config.work_dir.mkdir(parents=True, exist_ok=True)
    for marker in config.work_dir.glob("*.failed-retain"):
        try:
            expires_at = int(marker.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            expires_at = 0
        checkout = marker.with_suffix("")
        if expires_at <= now_ts:
            shutil.rmtree(checkout, ignore_errors=True)
            marker.unlink(missing_ok=True)
    entries = sorted(
        [path for path in config.work_dir.iterdir() if path.is_dir()],
        key=lambda path: path.stat().st_mtime,
    )
    while directory_size(config.work_dir) > config.max_checkout_bytes and entries:
        shutil.rmtree(entries.pop(0), ignore_errors=True)


def safe_unlink(path: Path) -> None:
    if str(path) != "/etc/systemd/system/pullwise-worker.service":
        raise ValueError(f"refusing to remove unexpected file: {path}")
    path.unlink(missing_ok=True)


def safe_rmtree(path: Path, allowed_root: Path) -> None:
    resolved = path.resolve(strict=False)
    allowed = allowed_root.resolve(strict=False)
    if resolved != allowed:
        raise ValueError(f"refusing to remove unexpected directory: {path}")
    shutil.rmtree(resolved, ignore_errors=True)


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
