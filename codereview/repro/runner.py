from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path

from ..codex_runner import run_codex_exec
from ..config import ReviewConfig
from ..utils.process import run_process
from .filesystem_guard import guard_worker_result
from .worker_dir import create_worker_dir


def run_repro_workers_parallel(checkout: Path, run: Path, candidates: list[dict], config: ReviewConfig) -> list[dict]:
    if not config.repro.enabled:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.repro.max_workers) as executor:
        futures = [executor.submit(run_repro_worker, checkout, run, candidate, config) for candidate in candidates]
        return [future.result() for future in futures]


def run_repro_worker(checkout: Path, run: Path, candidate: dict, config: ReviewConfig) -> dict:
    issue_id = str(candidate.get("issue_id") or "candidate")
    worker = run / "workers" / issue_id
    checkout_status_before = git_status_porcelain(checkout)
    create_worker_dir(checkout, worker, candidate)
    prompt = (checkout / ".codereview" / "prompts" / "repro_worker.md").read_text(encoding="utf-8")
    output = worker / "result.json"
    process = run_codex_exec(
        cd=worker,
        prompt=prompt,
        output_schema=checkout / ".codereview" / "schemas" / "repro_result.schema.json",
        output_file=output,
        sandbox="workspace-write",
        timeout_seconds=config.repro.timeout_seconds,
        config=config.codex,
        env=worker_env(worker),
    )
    parsed = {}
    if output.is_file():
        try:
            parsed = json.loads(output.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parsed = {}
    violations = guard_worker_result(worker, parsed)
    checkout_status_after = git_status_porcelain(checkout)
    if checkout_status_after != checkout_status_before:
        violations.append("original checkout changed during repro worker execution")
    return {
        "candidate_id": issue_id,
        "worker": str(worker),
        "process": process.to_dict(),
        "result": parsed,
        "filesystem_violations": violations,
        "checkout_status_before": checkout_status_before,
        "checkout_status_after": checkout_status_after,
    }


def worker_env(worker: Path) -> dict[str, str]:
    import os

    env = os.environ.copy()
    env["HOME"] = str(worker)
    env["USERPROFILE"] = str(worker)
    env["CODEX_HOME"] = str(worker / ".codex")
    return env


def git_status_porcelain(path: Path) -> list[str]:
    if not (path / ".git").exists():
        return []
    result = run_process(["git", "status", "--porcelain"], cwd=path, timeout=60)
    if result.returncode != 0:
        return [f"git status failed: {(result.stderr or result.stdout)[-200:]}"]
    return result.stdout.splitlines()
