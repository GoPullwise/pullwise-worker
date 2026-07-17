"""Read-only Git and contract-input observations for baseline verification."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

try:
    from scripts.agent_first_contract_files import surface_path, text_sha256
    from scripts.agent_first_contract_manifest import GIT_SHA_PATTERN
except ModuleNotFoundError:
    from agent_first_contract_files import surface_path, text_sha256  # type: ignore[no-redef]
    from agent_first_contract_manifest import GIT_SHA_PATTERN  # type: ignore[no-redef]


def git_output(repo_root: Path, arguments: list[str], timeout: int = 15) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def git_head(repo_root: Path) -> str | None:
    output = git_output(repo_root, ["rev-parse", "HEAD"], timeout=5)
    if output is None:
        return None
    head = output.decode("ascii", errors="ignore").strip().lower()
    return head if GIT_SHA_PATTERN.fullmatch(head) else None


def git_worktree_digest(repo_root: Path) -> str | None:
    status = git_output(
        repo_root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    )
    diff = git_output(
        repo_root,
        ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
    )
    if status is None or diff is None:
        return None
    return hashlib.sha256(status + b"\0" + diff).hexdigest()


def input_snapshot(
    manifest: dict[str, Any], roots: dict[str, Path]
) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    for surface in manifest["surfaces"]:
        path = surface_path(roots[surface["repo"]], surface["path"])
        snapshot[f"surface:{surface['id']}"] = text_sha256(path) if path else None
    appendix = manifest["appendix"]
    appendix_path = surface_path(roots[appendix["repo"]], appendix["path"])
    snapshot["appendix"] = text_sha256(appendix_path) if appendix_path else None
    for repo_id in sorted(roots):
        snapshot[f"head:{repo_id}"] = git_head(roots[repo_id])
        snapshot[f"worktree:{repo_id}"] = git_worktree_digest(roots[repo_id])
    return snapshot


def input_snapshot_sha256(snapshot: dict[str, str | None]) -> str:
    payload = json.dumps(
        snapshot,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
