from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ..utils.paths import safe_relative_path
from .file_hashes import line_count, looks_binary, sha256_file


EXCLUDED_DIRS = {
    ".git",
    ".codereview",
    ".codereview/runs",
    ".codegraph",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "target",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
}
GENERATED_SUFFIXES = {
    ".min.js",
    ".bundle.js",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
}
GIT_CAPTURE_MAX_BYTES = 16 * 1024 * 1024


def build_git_inventory(checkout: Path, *, include_untracked: bool = True) -> dict:
    checkout = checkout.resolve(strict=False)
    files = _git_files(checkout, include_untracked=include_untracked)
    source = "git" if files else "filesystem"
    if not files:
        files = _walk_files(checkout)
    statuses = _git_status(checkout)
    entries = [_file_entry(checkout, rel, statuses.get(rel, "")) for rel in files]
    return {
        "source": source,
        "files": entries,
        "summary": {
            "files": len(entries),
            "analyzable_files": sum(1 for item in entries if item.get("scope") == "analyze"),
            "excluded_files": sum(1 for item in entries if item.get("scope") == "excluded"),
            "inventory_mode": "full-repository-snapshot",
            "include_untracked": include_untracked,
        },
    }


def analyzable_files(inventory: dict) -> list[dict]:
    return [item for item in inventory.get("files", []) if isinstance(item, dict) and item.get("scope") == "analyze"]


def _git_files(checkout: Path, *, include_untracked: bool) -> list[str]:
    if not (checkout / ".git").exists():
        return []
    command = ["git", "ls-files", "-z"]
    if include_untracked:
        command = ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"]
    output = _run_git_capture(command, cwd=checkout, timeout=120)
    if output is None:
        return []
    return sorted(_safe_paths(output.split("\x00")))


def _walk_files(checkout: Path) -> list[str]:
    paths: list[str] = []
    for root, dirs, names in os.walk(checkout):
        root_path = Path(root)
        rel_root = root_path.relative_to(checkout).as_posix()
        dirs[:] = [
            name
            for name in dirs
            if not (root_path / name).is_symlink() and not _is_excluded_path(f"{rel_root}/{name}".strip("./"))
        ]
        for name in names:
            rel = (root_path / name).relative_to(checkout).as_posix()
            safe = safe_relative_path(rel)
            if safe:
                paths.append(safe)
    return sorted(paths)


def _git_status(checkout: Path) -> dict[str, str]:
    if not (checkout / ".git").exists():
        return {}
    output = _run_git_capture(["git", "status", "--porcelain=v1"], cwd=checkout, timeout=60)
    if output is None:
        return {}
    statuses: dict[str, str] = {}
    for line in output.splitlines():
        if len(line) < 4:
            continue
        status = line[:2].strip()
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1].strip()
        safe = safe_relative_path(path)
        if safe:
            statuses[safe] = status
    return statuses


def _run_git_capture(command: list[str], *, cwd: Path, timeout: int) -> str | None:
    try:
        with tempfile.TemporaryFile("w+b") as stdout, tempfile.TemporaryFile("w+b") as stderr:
            completed = subprocess.run(
                command,
                cwd=str(cwd),
                check=False,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout,
            )
            if completed.returncode != 0:
                return None
            output = _bounded_command_output(stdout, max_bytes=GIT_CAPTURE_MAX_BYTES)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if output is None:
        return None
    return output


def _bounded_command_output(handle, *, max_bytes: int) -> str | None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() > max_bytes:
        return None
    handle.seek(0)
    return handle.read(max_bytes + 1).decode("utf-8", errors="replace")


def _file_entry(checkout: Path, rel: str, status: str) -> dict:
    path = checkout / rel
    suffix = "".join(path.suffixes[-2:]) if path.suffixes[-2:] else path.suffix
    ext = path.suffix.lower()
    reason = ""
    scope = "analyze"
    is_symlink = path.is_symlink()
    is_regular_file = path.is_file() and not is_symlink
    binary = looks_binary(path) if is_regular_file else False
    if _is_excluded_path(rel):
        scope = "excluded"
        reason = "excluded-path"
    elif is_symlink:
        scope = "excluded"
        reason = "symlink"
    elif not is_regular_file:
        scope = "excluded"
        reason = "missing-or-non-file"
    elif binary:
        scope = "excluded"
        reason = "binary-file"
    elif suffix.lower() in GENERATED_SUFFIXES or ext.lower() in GENERATED_SUFFIXES:
        scope = "excluded"
        reason = "generated-or-binary-extension"
    return {
        "path": rel,
        "size_bytes": path.stat().st_size if is_regular_file else 0,
        "line_count": line_count(path) if is_regular_file and not binary else 0,
        "content_hash": sha256_file(path) if is_regular_file else "",
        "extension": ext,
        "git_status": status,
        "scope": scope,
        "reason": reason,
        "binary": binary,
    }


def _safe_paths(values: list[str]) -> list[str]:
    paths = []
    for value in values:
        rel = safe_relative_path(value)
        if rel:
            paths.append(rel)
    return paths


def _is_excluded_path(rel: str) -> bool:
    cleaned = rel.strip("/").replace("\\", "/")
    return cleaned in EXCLUDED_DIRS or any(cleaned.startswith(f"{prefix}/") for prefix in EXCLUDED_DIRS)
