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
    SourceEntry,
    SourceStateError,
    _canonical_path,
    _is_excluded,
    _ordered_paths,
)


_CATALOG_TOKEN = object()
_OBJECT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_VERSION_PATTERN = re.compile(
    rb"git version (0|[1-9][0-9]{0,2})\."
    rb"(0|[1-9][0-9]{0,2})\."
    rb"(0|[1-9][0-9]{0,8})"
    rb"(?:\.[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?\r?\n"
)
_MINIMUM_GIT_VERSION = (2, 45)


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


def _git_executable_identity(executable: Path) -> tuple[int, ...]:
    try:
        metadata = executable.lstat()
    except OSError as exc:
        raise SourceStateError("SOURCE_GIT_EXECUTABLE_INVALID") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
        or not os.access(executable, os.X_OK)
    ):
        raise SourceStateError("SOURCE_GIT_EXECUTABLE_INVALID")
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _assert_executable_identity(
    executable: Path, expected: tuple[int, ...]
) -> None:
    try:
        observed = _git_executable_identity(executable)
    except SourceStateError as exc:
        raise SourceStateError("SOURCE_GIT_EXECUTABLE_CHANGED") from exc
    if observed != expected:
        raise SourceStateError("SOURCE_GIT_EXECUTABLE_CHANGED")


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

    @property
    def root_identity(self) -> tuple[int, int]:
        return self._root_device, self._root_inode

    def assert_matches(self, root: Path, base_revision: str) -> None:
        identity = _root_identity(Path(root))
        if (
            base_revision != self._base_revision
            or identity != self.root_identity
        ):
            raise SourceStateError("SOURCE_GITLINK_CATALOG_MISMATCH")
        _assert_checkout_topology(Path(root), self._entries)


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
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    return allowed


def _parse_tree(payload: bytes) -> tuple[SourceEntry, ...]:
    entries: list[SourceEntry] = []
    node_types: dict[str, str] = {}
    for index, record in enumerate(payload.split(b"\x00")):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, kind, object_id = header.split(b" ", 2)
            path = raw_path.decode("utf-8", errors="strict")
            object_text = object_id.decode("ascii", errors="strict")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SourceStateError(
                "SOURCE_GITLINK_TREE_INVALID", str(index)
            ) from exc
        _canonical_path(path)
        if not _OBJECT_PATTERN.fullmatch(object_text) or path in node_types:
            raise SourceStateError("SOURCE_GITLINK_TREE_INVALID", path)
        if mode == b"160000" and kind == b"commit":
            if _is_excluded(path, PULLWISE_EXCLUDED_CONTROL_ROOTS):
                raise SourceStateError("SOURCE_GITLINK_TREE_INVALID", path)
            node_types[path] = "gitlink"
            entries.append(SourceEntry.gitlink(path, commit_sha=object_text))
        elif mode == b"040000" and kind == b"tree":
            node_types[path] = "tree"
        elif mode in {b"100644", b"100755", b"120000"} and kind == b"blob":
            node_types[path] = "leaf"
        else:
            raise SourceStateError("SOURCE_GITLINK_TREE_INVALID", path)
    for path in node_types:
        parts = path.split("/")
        for length in range(1, len(parts)):
            ancestor = "/".join(parts[:length])
            if ancestor in node_types and node_types[ancestor] != "tree":
                raise SourceStateError("SOURCE_GITLINK_TOPOLOGY_INVALID", path)
    ordered_paths = _ordered_paths(tuple(entry.path for entry in entries))
    by_path = {entry.path: entry for entry in entries}
    return tuple(by_path[path] for path in ordered_paths)


def _assert_checkout_topology(
    root: Path, entries: tuple[SourceEntry, ...]
) -> None:
    checked: set[str] = set()
    for entry in entries:
        parts = entry.path.split("/")
        for length in range(1, len(parts) + 1):
            relative = "/".join(parts[:length])
            if relative in checked:
                continue
            checked.add(relative)
            try:
                metadata = root.joinpath(*parts[:length]).lstat()
            except OSError as exc:
                raise SourceStateError(
                    "SOURCE_GITLINK_TOPOLOGY_INVALID", relative
                ) from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
            ):
                raise SourceStateError(
                    "SOURCE_GITLINK_TOPOLOGY_INVALID", relative
                )


def _run_git(
    command: list[str],
    *,
    executable: Path,
    executable_identity: tuple[int, ...],
    environment: dict[str, str],
    timeout_seconds: int,
    unavailable_code: str,
) -> bytes:
    _assert_executable_identity(executable, executable_identity)
    try:
        result = subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _assert_executable_identity(executable, executable_identity)
        raise SourceStateError(unavailable_code) from exc
    _assert_executable_identity(executable, executable_identity)
    if result.returncode != 0 or result.stderr:
        raise SourceStateError(unavailable_code)
    return result.stdout


def _assert_supported_git_version(payload: bytes) -> None:
    match = _VERSION_PATTERN.fullmatch(payload)
    if match is None:
        raise SourceStateError("SOURCE_GIT_VERSION_INVALID")
    version = int(match.group(1)), int(match.group(2))
    if version < _MINIMUM_GIT_VERSION:
        raise SourceStateError("SOURCE_GIT_VERSION_UNSUPPORTED")


def _assert_repository_root(payload: bytes, root: Path) -> None:
    if payload.endswith(b"\r\n"):
        raw_path = payload[:-2]
    elif payload.endswith(b"\n"):
        raw_path = payload[:-1]
    else:
        raise SourceStateError("SOURCE_GIT_REPOSITORY_MISMATCH")
    if not raw_path or b"\x00" in raw_path or b"\r" in raw_path or b"\n" in raw_path:
        raise SourceStateError("SOURCE_GIT_REPOSITORY_MISMATCH")
    try:
        reported = Path(raw_path.decode("utf-8", errors="strict"))
        expected = root.resolve(strict=True)
        actual = reported.resolve(strict=True)
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceStateError("SOURCE_GIT_REPOSITORY_MISMATCH") from exc
    if not reported.is_absolute() or actual != expected:
        raise SourceStateError("SOURCE_GIT_REPOSITORY_MISMATCH")


def inspect_gitlinks(
    root: Path,
    *,
    base_revision: str,
    git_executable: Path,
    timeout_seconds: int = 30,
) -> VerifiedGitlinkCatalog:
    root = Path(root)
    identity = _root_identity(root)
    if not _OBJECT_PATTERN.fullmatch(base_revision):
        raise SourceStateError("SOURCE_BASE_REVISION_INVALID")
    executable = Path(git_executable)
    if not executable.is_absolute():
        raise SourceStateError("SOURCE_GIT_EXECUTABLE_INVALID")
    executable_identity = _git_executable_identity(executable)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
        or timeout_seconds > 300
    ):
        raise SourceStateError("SOURCE_GIT_TIMEOUT_INVALID")
    environment = _git_environment()
    version_payload = _run_git(
        [str(executable), "--version"],
        executable=executable,
        executable_identity=executable_identity,
        environment=environment,
        timeout_seconds=timeout_seconds,
        unavailable_code="SOURCE_GIT_VERSION_UNAVAILABLE",
    )
    _assert_supported_git_version(version_payload)
    common = [
        str(executable),
        "--no-replace-objects",
        "--no-lazy-fetch",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        "-C",
        str(root),
    ]
    top_level = _run_git(
        [*common, "rev-parse", "--path-format=absolute", "--show-toplevel"],
        executable=executable,
        executable_identity=executable_identity,
        environment=environment,
        timeout_seconds=timeout_seconds,
        unavailable_code="SOURCE_GITLINK_CATALOG_UNAVAILABLE",
    )
    _assert_repository_root(top_level, root)
    command = [
        *common,
        "ls-tree",
        "-rzt",
        "--full-tree",
        base_revision,
    ]
    tree_payload = _run_git(
        command,
        executable=executable,
        executable_identity=executable_identity,
        environment=environment,
        timeout_seconds=timeout_seconds,
        unavailable_code="SOURCE_GITLINK_CATALOG_UNAVAILABLE",
    )
    if _root_identity(root) != identity:
        raise SourceStateError("SOURCE_GITLINK_CATALOG_MISMATCH")
    entries = _parse_tree(tree_payload)
    _assert_checkout_topology(root, entries)
    return VerifiedGitlinkCatalog(
        token=_CATALOG_TOKEN,
        root_identity=identity,
        base_revision=base_revision,
        entries=entries,
        tree_digest=hashlib.sha256(tree_payload).hexdigest(),
    )


__all__ = ["VerifiedGitlinkCatalog", "inspect_gitlinks"]
