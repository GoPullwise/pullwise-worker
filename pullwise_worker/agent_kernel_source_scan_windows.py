"""Race-checked SourceState scanning where Python lacks dirfd traversal."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from .agent_kernel_source_scan import (
    StageHook,
    _FILE_FLAGS,
    _assert_directory,
    _is_reparse,
    _names,
    _normalize_link_target,
    _record_case_identity,
    _same_identity,
)
from .agent_kernel_source_state import (
    SourceEntry,
    SourceSelectionPolicy,
    SourceStateError,
    _canonical_path,
    _is_excluded,
    _path_key,
)


def _read_path(
    path: Path,
    relative: str,
    before: os.stat_result,
    hook: StageHook | None,
) -> SourceEntry:
    try:
        if hook is not None:
            hook("before_file_open", path)
        descriptor = os.open(path, _FILE_FLAGS)
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode) or _is_reparse(opened):
                raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", relative)
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after_read = os.fstat(handle.fileno())
        if hook is not None:
            hook("after_file_read", path)
        after_path = path.lstat()
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
        executable=os.name != "nt" and bool(opened.st_mode & 0o111),
    )


def _scan_path(
    directory: Path,
    *,
    prefix: str,
    policy: SourceSelectionPolicy,
    seen: dict[str, str],
    hook: StageHook | None,
    gitlinks: dict[str, SourceEntry],
) -> list[SourceEntry]:
    try:
        before = directory.lstat()
    except OSError as exc:
        raise SourceStateError("SOURCE_DIRECTORY_UNREADABLE", prefix or ".") from exc
    _assert_directory(before, prefix or ".")
    names_before = _names(directory)
    for name in names_before:
        _canonical_path(f"{prefix}/{name}" if prefix else name)
    collected: list[SourceEntry] = []
    for name in sorted(names_before, key=_path_key):
        relative = f"{prefix}/{name}" if prefix else name
        _record_case_identity(relative, seen)
        if _is_excluded(relative, policy.excluded_control_roots):
            continue
        path = directory / name
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise SourceStateError("SOURCE_ENTRY_UNREADABLE", relative) from exc
        if relative in gitlinks:
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
            ):
                raise SourceStateError(
                    "SOURCE_GITLINK_IDENTITY_INVALID", relative
                )
            continue
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = _normalize_link_target(os.readlink(path))
                after = path.lstat()
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
            if hook is not None:
                hook("before_directory_open", path)
            try:
                ready = path.lstat()
            except OSError as exc:
                raise SourceStateError(
                    "SOURCE_CHANGED_DURING_SCAN", relative
                ) from exc
            if not _same_identity(metadata, ready):
                raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", relative)
            collected.extend(
                _scan_path(
                    path,
                    prefix=relative,
                    policy=policy,
                    seen=seen,
                    hook=hook,
                    gitlinks=gitlinks,
                )
            )
            try:
                after = path.lstat()
            except OSError as exc:
                raise SourceStateError(
                    "SOURCE_CHANGED_DURING_SCAN", relative
                ) from exc
            if not _same_identity(metadata, after):
                raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", relative)
        elif stat.S_ISREG(metadata.st_mode):
            collected.append(_read_path(path, relative, metadata, hook))
        else:
            raise SourceStateError("SOURCE_SPECIAL_FILE", relative)
    try:
        after = directory.lstat()
    except OSError as exc:
        raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", prefix or ".") from exc
    names_after = _names(directory)
    if not _same_identity(before, after) or set(names_before) != set(names_after):
        raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", prefix or ".")
    return collected


def scan_with_paths(
    root: Path,
    policy: SourceSelectionPolicy,
    hook: StageHook | None,
    gitlinks: dict[str, SourceEntry],
) -> list[SourceEntry]:
    before = root.lstat()
    _assert_directory(before, ".")
    if hook is not None:
        hook("after_root_open", root)
    entries = _scan_path(
        root,
        prefix="",
        policy=policy,
        seen={},
        hook=hook,
        gitlinks=gitlinks,
    )
    try:
        after = root.lstat()
    except OSError as exc:
        raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", ".") from exc
    if not _same_identity(before, after):
        raise SourceStateError("SOURCE_CHANGED_DURING_SCAN", ".")
    return entries


__all__ = ["scan_with_paths"]
