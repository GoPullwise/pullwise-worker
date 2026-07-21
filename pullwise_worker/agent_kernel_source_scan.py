"""Descriptor-rooted SourceState scanner with a safe Windows fallback."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
from typing import Callable

from .agent_kernel_source_state import (
    SourceEntry,
    SourceSelectionPolicy,
    SourceStateError,
    SourceTreeSnapshot,
    _canonical_path,
    _is_excluded,
    _path_key,
)


StageHook = Callable[[str, Path], None]
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_HAS_DIRFD = (
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.readlink in os.supports_dir_fd
    and os.scandir in os.supports_fd
)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
    )
    return all(getattr(left, field) == getattr(right, field) for field in fields)


def _is_reparse(metadata: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(metadata, "st_file_attributes", 0) & marker)


def _assert_directory(metadata: os.stat_result, path: str) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse(metadata)
    ):
        raise SourceStateError("SOURCE_DIRECTORY_IDENTITY_INVALID", path)


def _record_case_identity(relative: str, seen: dict[str, str]) -> None:
    parts = relative.split("/")
    for length in range(1, len(parts) + 1):
        path = "/".join(parts[:length])
        prior = seen.setdefault(path.casefold(), path)
        if prior != path:
            raise SourceStateError(
                "SOURCE_PATH_CASE_COLLISION", f"{prior}, {path}"
            )


def _normalize_link_target(target: str) -> str:
    if os.name != "nt":
        return target
    separator = chr(92)
    extended = separator * 2 + "?" + separator
    unc = extended + "UNC" + separator
    if target.startswith(unc):
        return separator * 2 + target[len(unc) :]
    if target.startswith(extended):
        return target[len(extended) :]
    return target


def _names(directory: int | Path) -> tuple[str, ...]:
    try:
        with os.scandir(directory) as entries:
            return tuple(entry.name for entry in entries)
    except OSError as exc:
        raise SourceStateError("SOURCE_DIRECTORY_UNREADABLE") from exc


def snapshot_source_tree(
    root: Path,
    *,
    policy: SourceSelectionPolicy,
    base_revision: str,
    gitlink_catalog: object | None = None,
    stage_hook: StageHook | None = None,
) -> SourceTreeSnapshot:
    if gitlink_catalog is not None:
        raise SourceStateError("SOURCE_GITLINK_CATALOG_UNVERIFIED")
    root = Path(root)
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise SourceStateError("SOURCE_ROOT_UNREADABLE") from exc
    _assert_directory(root_metadata, ".")
    if _HAS_DIRFD:
        entries = _scan_with_dirfd(root, policy, stage_hook)
    else:
        from .agent_kernel_source_scan_windows import scan_with_paths

        entries = scan_with_paths(root, policy, stage_hook)
    return SourceTreeSnapshot(
        base_revision=base_revision,
        selection_policy_digest=policy.digest,
        entries=tuple(sorted(entries, key=lambda entry: _path_key(entry.path))),
    )


def _read_at(
    parent_fd: int,
    name: str,
    relative: str,
    before: os.stat_result,
    display_path: Path,
    hook: StageHook | None,
) -> SourceEntry:
    try:
        if hook is not None:
            hook("before_file_open", display_path)
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
                raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", relative)
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after_read = os.fstat(handle.fileno())
        if hook is not None:
            hook("after_file_read", display_path)
        after_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except SourceStateError:
        raise
    except OSError as exc:
        raise SourceStateError("SOURCE_FILE_UNREADABLE", relative) from exc
    if not (
        _same_identity(before, opened)
        and _same_identity(opened, after_read)
        and _same_identity(after_read, after_path)
    ):
        raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", relative)
    return SourceEntry.file(
        relative,
        size_bytes=opened.st_size,
        sha256=digest.hexdigest(),
        executable=bool(opened.st_mode & 0o111),
    )


def _scan_fd(
    directory_fd: int,
    *,
    root: Path,
    prefix: str,
    policy: SourceSelectionPolicy,
    seen: dict[str, str],
    hook: StageHook | None,
) -> list[SourceEntry]:
    before = os.fstat(directory_fd)
    _assert_directory(before, prefix or ".")
    names_before = _names(directory_fd)
    for name in names_before:
        _canonical_path(f"{prefix}/{name}" if prefix else name)
    collected: list[SourceEntry] = []
    for name in sorted(names_before, key=_path_key):
        relative = f"{prefix}/{name}" if prefix else name
        _record_case_identity(relative, seen)
        if _is_excluded(relative, policy.excluded_control_roots):
            continue
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise SourceStateError("SOURCE_ENTRY_UNREADABLE", relative) from exc
        display_path = root / Path(relative)
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = _normalize_link_target(
                    os.readlink(name, dir_fd=directory_fd)
                )
                after = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
            except OSError as exc:
                raise SourceStateError(
                    "SOURCE_SYMLINK_UNREADABLE", relative
                ) from exc
            if not _same_identity(metadata, after):
                raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", relative)
            collected.append(SourceEntry.symlink(relative, target=target))
        elif _is_reparse(metadata):
            raise SourceStateError("SOURCE_REPARSE_POINT", relative)
        elif stat.S_ISDIR(metadata.st_mode):
            try:
                if hook is not None:
                    hook("before_directory_open", display_path)
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            except OSError as exc:
                raise SourceStateError(
                    "SOURCE_CHANGED_DURING_SCAN", relative
                ) from exc
            try:
                opened = os.fstat(child_fd)
                if not _same_identity(metadata, opened):
                    raise SourceStateError(
                        "SOURCE_CHANGED_DURING_SCAN", relative
                    )
                collected.extend(
                    _scan_fd(
                        child_fd,
                        root=root,
                        prefix=relative,
                        policy=policy,
                        seen=seen,
                        hook=hook,
                    )
                )
                after = os.stat(
                    name, dir_fd=directory_fd, follow_symlinks=False
                )
                if not _same_identity(opened, after):
                    raise SourceStateError(
                        "SOURCE_CHANGED_DURING_SCAN", relative
                    )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            collected.append(
                _read_at(
                    directory_fd,
                    name,
                    relative,
                    metadata,
                    display_path,
                    hook,
                )
            )
        else:
            raise SourceStateError("SOURCE_SPECIAL_FILE", relative)
    after = os.fstat(directory_fd)
    names_after = _names(directory_fd)
    if not _same_identity(before, after) or set(names_before) != set(names_after):
        raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", prefix or ".")
    return collected


def _scan_with_dirfd(
    root: Path, policy: SourceSelectionPolicy, hook: StageHook | None
) -> list[SourceEntry]:
    try:
        before = root.lstat()
        root_fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise SourceStateError("SOURCE_ROOT_UNREADABLE") from exc
    try:
        opened = os.fstat(root_fd)
        if not _same_identity(before, opened):
            raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", ".")
        if hook is not None:
            hook("after_root_open", root)
        entries = _scan_fd(
            root_fd,
            root=root,
            prefix="",
            policy=policy,
            seen={},
            hook=hook,
        )
        try:
            after = root.lstat()
        except OSError as exc:
            raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", ".") from exc
        if not _same_identity(opened, after):
            raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", ".")
        return entries
    finally:
        os.close(root_fd)


__all__ = ["snapshot_source_tree"]
