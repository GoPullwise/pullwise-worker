from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import requests

from . import __version__


PHASE_PROGRESS = {
    "clone": 10,
    "index": 25,
    "secrets": 40,
    "deps": 55,
    "ai": 80,
    "report": 95,
}


class WorkerConfig:
    def __init__(self, args: argparse.Namespace) -> None:
        self.server_url = (getattr(args, "server_url", None) or os.environ.get("PULLWISE_SERVER_URL") or "http://localhost:8080").rstrip("/")
        self.worker_token = getattr(args, "worker_token", None) or os.environ.get("PULLWISE_WORKER_TOKEN") or ""
        self.worker_id = getattr(args, "worker_id", None) or os.environ.get("PULLWISE_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
        self.provider = getattr(args, "provider", None) or os.environ.get("PULLWISE_PROVIDER") or "codex"
        self.max_concurrent_jobs = max(1, int(getattr(args, "max_concurrent_jobs", None) or os.environ.get("PULLWISE_MAX_CONCURRENT_JOBS") or 1))
        self.poll_seconds = max(1, int(getattr(args, "poll_seconds", None) or os.environ.get("PULLWISE_WORKER_POLL_SECONDS") or 5))
        checkout_root = getattr(args, "checkout_root", None) or os.environ.get("PULLWISE_CHECKOUT_ROOT")
        work_dir = getattr(args, "work_dir", None) or os.environ.get("PULLWISE_WORKER_WORK_DIR")
        self.work_dir = Path(checkout_root) if checkout_root else Path(work_dir or tempfile.gettempdir()) / "pullwise-worker"
        log_dir = getattr(args, "log_dir", None) or os.environ.get("PULLWISE_LOG_DIR")
        self.log_dir = Path(log_dir) if log_dir else Path(tempfile.gettempdir()) / "pullwise-worker-logs"
        self.codex_command = getattr(args, "codex_command", None) or os.environ.get("PULLWISE_CODEX_COMMAND") or "codex"
        self.codex_timeout_seconds = max(60, int(getattr(args, "codex_timeout_seconds", None) or os.environ.get("PULLWISE_CODEX_TIMEOUT_SECONDS") or 1800))
        self.failed_checkout_retention_seconds = max(0, int(os.environ.get("PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS") or 0))
        self.max_checkout_bytes = max(1, int(os.environ.get("PULLWISE_MAX_CHECKOUT_BYTES") or 20 * 1024 * 1024 * 1024))
        if not self.worker_token:
            raise ValueError("PULLWISE_WORKER_TOKEN or --worker-token is required")


class PullwiseClient:
    def __init__(self, config: WorkerConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {config.worker_token}"})

    def post(self, path: str, payload: dict) -> requests.Response:
        response = self.session.post(f"{self.config.server_url}{path}", json=payload, timeout=30)
        response.raise_for_status()
        return response

    def heartbeat(
        self,
        *,
        running_jobs: int = 0,
        last_error: str | None = None,
        doctor_status: str | None = None,
        codex_ready: bool | None = None,
        systemd_active: bool | None = None,
        doctor_checked_at: int | None = None,
    ) -> None:
        self.post(
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
    parser.add_argument("command", nargs="?", default="run", choices=["run", "doctor", "status", "restart", "update", "uninstall", "cleanup"])
    parser.add_argument("--server-url")
    parser.add_argument("--worker-token")
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
    if args.command == "status":
        raise SystemExit(service_action("status", dry_run=args.dry_run))
    if args.command == "restart":
        raise SystemExit(service_action("restart", dry_run=args.dry_run))
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
                self.client.heartbeat(running_jobs=len(running), last_error=self.last_error)
                if free_slots:
                    jobs = self.client.claim_many(free_slots)
                    for job in jobs:
                        future = executor.submit(self.run_job, job)
                        running[future] = job
                    if once:
                        concurrent.futures.wait(running)
                        return
                elif once:
                    concurrent.futures.wait(running)
                    return
                time.sleep(self.config.poll_seconds)

    def run_job(self, job: dict) -> None:
        job_id = str(job["job_id"])
        attempt_id = f"{self.config.worker_id}-{job.get('attempt') or 1}"
        checkout_dir = self.config.work_dir / job_id
        started = time.monotonic()
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
            self.client.result(job_id, payload)
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
            self.client.result(job_id, error_payload)
            write_scan_summary(self.config, job_id, "failed", duration_ms, error)
            self.last_error = error
        finally:
            if self.last_error and self.config.failed_checkout_retention_seconds > 0:
                marker = checkout_dir.with_suffix(".failed-retain")
                marker.write_text(str(int(time.time()) + self.config.failed_checkout_retention_seconds), encoding="utf-8")
            else:
                shutil.rmtree(checkout_dir, ignore_errors=True)


def clone_repository(job: dict, checkout_dir: Path) -> None:
    shutil.rmtree(checkout_dir, ignore_errors=True)
    checkout_dir.parent.mkdir(parents=True, exist_ok=True)
    clone_url = authenticated_clone_url(str(job.get("clone_url") or ""), job.get("clone_token"))
    if not clone_url:
        repo = str(job.get("repo") or "")
        clone_url = authenticated_clone_url(f"https://github.com/{repo}.git", job.get("clone_token"))
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", str(job.get("branch") or "main"), clone_url, str(checkout_dir)],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    commit = str(job.get("commit") or "pending")
    if commit and commit != "pending":
        subprocess.run(
            ["git", "-C", str(checkout_dir), "checkout", commit],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def authenticated_clone_url(clone_url: str, clone_token: object) -> str:
    token = clone_token.get("token") if isinstance(clone_token, dict) else None
    if not token:
        return clone_url
    parsed = urlparse(clone_url)
    netloc = f"x-access-token:{token}@{parsed.netloc}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def run_codex_review(config: WorkerConfig, job: dict, checkout_dir: Path) -> tuple[list[dict], dict, str]:
    prompt = review_prompt(job)
    command = [config.codex_command, "exec", "--json", prompt]
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
    findings = parse_findings(completed.stdout)
    return findings, summarize(findings), logs_summary


def review_prompt(job: dict) -> str:
    return (
        "Review this repository for production-impacting bugs, security issues, dependency risks, "
        "and reliability problems. Return only JSON with a top-level findings array. Each finding "
        "must include title, severity, category, summary, impact, file, line, confidence, and recommendation. "
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
    for candidate in candidates:
        try:
            parsed = decoder.decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("findings"), list):
            return [item for item in parsed["findings"] if isinstance(item, dict)]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    raise RuntimeError("codex exec did not return a JSON findings payload")


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


def run_doctor(config: WorkerConfig) -> bool:
    checks: list[tuple[str, bool, str]] = []
    checks.append(("server_url", bool(config.server_url.startswith(("http://", "https://"))), config.server_url))
    checks.append(("worker_token", bool(config.worker_token), "configured" if config.worker_token else "missing"))
    checks.append(("max_concurrent_jobs", config.max_concurrent_jobs > 0, str(config.max_concurrent_jobs)))
    codex_cli_ok = False
    for label, command in (("git", ["git", "--version"]), ("codex", [config.codex_command, "--version"])):
        ok, detail = command_ok(command)
        checks.append((label, ok, detail))
        if label == "codex":
            codex_cli_ok = ok
    codex_login_ok, codex_login_detail = command_ok([config.codex_command, "exec", "--help"])
    checks.append(("codex_login_hint", codex_login_ok, codex_login_detail or "run codex login as the service user if scans fail"))
    systemd_ok, systemd_detail = command_ok(["systemctl", "is-active", "pullwise-worker"])
    checks.append(("systemd", systemd_ok, systemd_detail))
    for label, path in (("checkout_root", config.work_dir), ("log_dir", config.log_dir)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            test_file = path / ".pullwise-write-test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink(missing_ok=True)
            checks.append((label, True, str(path)))
        except Exception as exc:
            checks.append((label, False, str(exc)))
    usage = shutil.disk_usage(config.work_dir if config.work_dir.exists() else config.work_dir.parent)
    checks.append(("disk_space", usage.free > 1024 * 1024 * 1024, f"{usage.free // (1024 * 1024)} MB free"))
    heartbeat_ok = True
    heartbeat_detail = "ok"
    doctor_required_ok = all(ok for name, ok, _detail in checks if name not in {"codex_login_hint", "heartbeat"})
    codex_ready = bool(codex_cli_ok and codex_login_ok)
    try:
        PullwiseClient(config).heartbeat(
            last_error=None,
            doctor_status="ok" if doctor_required_ok and codex_ready else "degraded",
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
    if not codex_login_ok:
        print("Codex may require interactive login: sudo -u pullwise-worker codex login")
    return all(ok for name, ok, _detail in checks if name != "codex_login_hint")


def command_ok(command: list[str]) -> tuple[bool, str]:
    try:
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        detail = (completed.stdout or completed.stderr).strip().splitlines()
        return completed.returncode == 0, detail[0] if detail else f"exit {completed.returncode}"
    except FileNotFoundError:
        return False, "not found"
    except Exception as exc:
        return False, str(exc)


def service_action(action: str, *, dry_run: bool = False) -> int:
    command = ["systemctl", action, "pullwise-worker"]
    if dry_run:
        print(" ".join(command))
        return 0
    return subprocess.run(command).returncode


def update_worker(config: WorkerConfig, *, dry_run: bool = False) -> int:
    package = os.environ.get("PULLWISE_WORKER_PACKAGE") or "pullwise-worker"
    env_path = Path("/etc/pullwise-worker/worker.env")
    backup_path = Path("/etc/pullwise-worker/worker.env.bak")
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


def uninstall_worker(*, remove_config: bool = False, remove_logs: bool = False, dry_run: bool = False) -> int:
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
