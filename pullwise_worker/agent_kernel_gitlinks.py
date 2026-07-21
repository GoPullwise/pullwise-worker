"""Exact-revision Gitlink inspection for SourceState scans."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

from .agent_kernel_source_state import (
    PULLWISE_EXCLUDED_CONTROL_ROOTS,
    REVISION_PATTERN,
    SourceEntry,
    SourceStateError,
    _canonical_path,
    _is_excluded,
    _ordered_paths,
)


_CATALOG_TOKEN = object()
_OBJECT_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _is_reparse(metadata: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(metadata, "st_file_attributes", 0) & marker)


def _root_identity(root: Path) -> tuple[int, int]:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise SourceStateError("SOURCE_GITLINK_ROOT_INVALID") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise SourceStateError("SOURCE_GITLINK_ROOT_INVALID")
    return metadata.st_dev, metadata.st_ino


class VerifiedGitlinkCatalog:
    """Opaque result of inspecting one root at one exact Git revision."""

    __slots__ = (
        "_base_revision",
        "_entries",
        "_root_device",
        "_root_inode",
        "_tree_digest",
    )

    def __init__(
        self,
        *,
        token: object,
        root_identity: tuple[int, int],
        base_revision: str,
        entries: tuple[SourceEntry, ...],
        tree_digest: str,
    ) -> None:
        if token is not _CATALOG_TOKEN:
            raise SourceStateError("SOURCE_GITLINK_CATALOG_UNVERIFIED")
        self._root_device, self._root_inode = root_identity
        self._base_revision = base_revision
        self._entries = entries
        self._tree_digest = tree_digest

    @property
    def entries(self) -> tuple[SourceEntry, ...]:
        return self._entries

    @property
    def tree_digest(self) -> str:
        return self._tree_digest

    def assert_matches(self, root: Path, base_revision: str) -> None:
        identity = _root_identity(Path(root))
        if (
            base_revision != self._base_revision
            or identity != (self._root_device, self._root_inode)
        ):
            raise SourceStateError("SOURCE_GITLINK_CATALOG_MISMATCH")


def _git_environment() -> dict[str, str]:
    allowed = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "TEMP", "TMP", "WINDIR"}
    }
    allowed.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    return allowed


def _parse_tree(payload: bytes) -> tuple[SourceEntry, ...]:
    entries: list[SourceEntry] = []
    for index, record in enumerate(payload.split(b"\x00")):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, kind, object_id = header.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceStateError(
                "SOURCE_GITLINK_TREE_INVALID", str(index)
            ) from exc
        _canonical_path(path)
        if mode == b"160000":
            commit_sha = object_id.decode("ascii", errors="strict")
            if kind != b"commit" or not _OBJECT_PATTERN.fullmatch(commit_sha):
                raise SourceStateError("SOURCE_GITLINK_TREE_INVALID", path)
            if _is_excluded(path, PULLWISE_EXCLUDED_CONTROL_ROOTS):
                raise SourceStateError("SOURCE_GITLINK_TREE_INVALID", path)
            entries.append(SourceEntry.gitlink(path, commit_sha=commit_sha))
        elif kind not in {b"blob", b"tree"}:
            raise SourceStateError("SOURCE_GITLINK_TREE_INVALID", path)
    ordered_paths = _ordered_paths(tuple(entry.path for entry in entries))
    by_path = {entry.path: entry for entry in entries}
    return tuple(by_path[path] for path in ordered_paths)


def inspect_gitlinks(
    root: Path,
    *,
    base_revision: str,
    git_executable: Path,
    timeout_seconds: int = 30,
) -> VerifiedGitlinkCatalog:
    root = Path(root)
    identity = _root_identity(root)
    if not REVISION_PATTERN.fullmatch(base_revision):
        raise SourceStateError("SOURCE_BASE_REVISION_INVALID")
    executable = Path(git_executable).resolve(strict=True)
    metadata = executable.lstat()
    if not stat.S_ISREG(metadata.st_mode) or _is_reparse(metadata):
        raise SourceStateError("SOURCE_GIT_EXECUTABLE_INVALID")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
        or timeout_seconds > 300
    ):
        raise SourceStateError("SOURCE_GIT_TIMEOUT_INVALID")
    command = [
        str(executable),
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(root),
        "ls-tree",
        "-rz",
        "--full-tree",
        base_revision,
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            env=_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceStateError("SOURCE_GITLINK_CATALOG_UNAVAILABLE") from exc
    if result.returncode != 0 or result.stderr:
        raise SourceStateError("SOURCE_GITLINK_CATALOG_UNAVAILABLE")
    entries = _parse_tree(result.stdout)
    return VerifiedGitlinkCatalog(
        token=_CATALOG_TOKEN,
        root_identity=identity,
        base_revision=base_revision,
        entries=entries,
        tree_digest=hashlib.sha256(result.stdout).hexdigest(),
    )


__all__ = ["VerifiedGitlinkCatalog", "inspect_gitlinks"]
