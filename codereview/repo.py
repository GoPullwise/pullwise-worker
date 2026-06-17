from __future__ import annotations

import time
from pathlib import Path

from .utils.process import run_process


def inspect_repo(checkout: Path, base_ref: str, head_ref: str) -> dict:
    head = run_process(["git", "rev-parse", head_ref], cwd=checkout, timeout=60)
    base = run_process(["git", "rev-parse", base_ref], cwd=checkout, timeout=60)
    status = run_process(["git", "status", "--porcelain"], cwd=checkout, timeout=60)
    failures = [result for result in (head, base, status) if result.returncode != 0]
    if failures:
        failure = failures[0]
        raise RuntimeError(f"git repository inspection failed: {(failure.stderr or failure.stdout)[-500:]}")
    return {
        "checkout": str(checkout),
        "base_ref": base_ref,
        "head_ref": head_ref,
        "base_commit": base.stdout.strip(),
        "head_commit": head.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
        "status": status.stdout.splitlines(),
        "started_at": int(time.time()),
    }


def git_diff_name_only(checkout: Path, base_ref: str, head_ref: str) -> list[str]:
    result = run_process(["git", "diff", "--name-only", f"{base_ref}...{head_ref}"], cwd=checkout, timeout=120)
    return [line.strip().replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]
