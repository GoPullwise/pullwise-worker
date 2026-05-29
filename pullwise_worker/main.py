from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
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
        self.server_url = (args.server_url or os.environ.get("PULLWISE_SERVER_URL") or "http://localhost:8080").rstrip("/")
        self.worker_token = args.worker_token or os.environ.get("PULLWISE_WORKER_TOKEN") or ""
        self.worker_id = args.worker_id or os.environ.get("PULLWISE_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
        self.max_concurrent_jobs = max(1, int(args.max_concurrent_jobs or os.environ.get("PULLWISE_MAX_CONCURRENT_JOBS") or 1))
        self.poll_seconds = max(1, int(args.poll_seconds or os.environ.get("PULLWISE_WORKER_POLL_SECONDS") or 5))
        self.work_dir = Path(args.work_dir or os.environ.get("PULLWISE_WORKER_WORK_DIR") or tempfile.gettempdir()) / "pullwise-worker"
        self.codex_command = args.codex_command or os.environ.get("PULLWISE_CODEX_COMMAND") or "codex"
        self.codex_timeout_seconds = max(60, int(args.codex_timeout_seconds or os.environ.get("PULLWISE_CODEX_TIMEOUT_SECONDS") or 1800))
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

    def heartbeat(self, *, running_jobs: int = 0, last_error: str | None = None) -> None:
        self.post(
            "/worker/heartbeat",
            {
                "worker_id": self.config.worker_id,
                "version": __version__,
                "provider": "codex",
                "max_concurrent_jobs": self.config.max_concurrent_jobs,
                "running_jobs": running_jobs,
                "free_slots": max(0, self.config.max_concurrent_jobs - running_jobs),
                "hostname": socket.gethostname(),
                "last_error": last_error,
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
    parser.add_argument("--server-url")
    parser.add_argument("--worker-token")
    parser.add_argument("--worker-id")
    parser.add_argument("--max-concurrent-jobs", type=int)
    parser.add_argument("--poll-seconds", type=int)
    parser.add_argument("--work-dir")
    parser.add_argument("--codex-command")
    parser.add_argument("--codex-timeout-seconds", type=int)
    parser.add_argument("--once", action="store_true", help="Process at most one job and exit.")
    args = parser.parse_args()

    try:
        config = WorkerConfig(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
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
            self.last_error = None
        except Exception as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            error_payload = {
                "status": "failed",
                "findings": [],
                "summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "duration_ms": duration_ms,
                "error": str(exc)[:500],
                "attempt_id": attempt_id,
            }
            error_payload["result_checksum"] = result_checksum(error_payload)
            self.client.result(job_id, error_payload)
            self.last_error = str(exc)[:500]
        finally:
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
    logs_summary = (completed.stderr or completed.stdout)[-1000:]
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


if __name__ == "__main__":
    main()
