from __future__ import annotations

import time
from pathlib import Path

from .utils.process import run_process


def inspect_repo(checkout: Path, head_ref: str) -> dict:
    head = run_process(["git", "rev-parse", head_ref], cwd=checkout, timeout=60)
    status = run_process(["git", "status", "--porcelain"], cwd=checkout, timeout=60)
    failures = [result for result in (head, status) if result.returncode != 0]
    if failures:
        failure = failures[0]
        raise RuntimeError(f"git repository inspection failed: {(failure.stderr or failure.stdout)[-500:]}")
    return {
        "checkout": str(checkout),
        "scope": "repository",
        "head_ref": head_ref,
        "head_commit": head.stdout.strip(),
        "dirty": bool(status.stdout.strip()),
        "status": status.stdout.splitlines(),
        "started_at": int(time.time()),
    }
