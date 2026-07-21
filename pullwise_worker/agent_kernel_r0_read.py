"""Descriptor-held R0 source reads for the package-independent Gateway."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import threading

from .agent_kernel_gateway import (
    CheckedInvocation,
    GatewayError,
    PreparedDispatch,
    ToolDescriptor,
)
from .agent_kernel_source_scan import (
    StageHook,
    _DIRECTORY_FLAGS,
    _FILE_FLAGS,
    _HAS_DIRFD,
    _assert_directory,
    _is_reparse,
    _same_identity,
)
from .agent_kernel_source_state import (
    SourceEntry,
    SourceSelectionPolicy,
    SourceStateError,
    SourceTreeSnapshot,
    _canonical_path,
    _is_excluded,
    snapshot_source_tree,
)


READ_TOOL_KEY = "internal.read_source"


class R0ReadError(GatewayError):
    pass


@dataclass(frozen=True)
class ReadSourceFileInput:
    relative_path: str

    def __post_init__(self) -> None:
        try:
            _canonical_path(self.relative_path)
        except SourceStateError as exc:
            raise R0ReadError("READ_PATH_INVALID", exc.code) from exc


class PreparedR0ReadHandle:
    __slots__ = ("_descriptor", "_expected_sha256", "_expected_size", "_lock")

    def __init__(self, descriptor: int, entry: SourceEntry) -> None:
        self._descriptor: int | None = descriptor
        self._expected_sha256 = entry.sha256
        self._expected_size = entry.size_bytes
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._descriptor is None

    def take(self) -> tuple[int, str, int]:
        with self._lock:
            if self._descriptor is None:
                raise R0ReadError("PREPARED_READ_CLOSED")
            descriptor = self._descriptor
            self._descriptor = None
        assert self._expected_sha256 is not None
        assert self._expected_size is not None
        return descriptor, self._expected_sha256, self._expected_size

    def discard(self) -> None:
        with self._lock:
            descriptor = self._descriptor
            self._descriptor = None
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True)
class R0ReadReceipt:
    payload: bytes
    sha256: str
    size_bytes: int


def _read_descriptor(
    descriptor: int,
    *,
    byte_limit: int,
    overflow_code: str,
) -> tuple[bytes, os.stat_result]:
    before = os.fstat(descriptor)
    if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise R0ReadError("READ_LEAF_NOT_REGULAR")
    if before.st_size > byte_limit:
        raise R0ReadError(overflow_code)
    chunks: list[bytes] = []
    remaining = byte_limit + 1
    try:
        with os.fdopen(os.dup(descriptor), "rb", closefd=True) as handle:
            while remaining:
                chunk = handle.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
    finally:
        os.lseek(descriptor, 0, os.SEEK_SET)
    payload = b"".join(chunks)
    if len(payload) > byte_limit:
        raise R0ReadError(overflow_code)
    after = os.fstat(descriptor)
    if not _same_identity(before, after):
        raise R0ReadError("READ_SOURCE_ENTRY_CHANGED")
    return payload, before


def _assert_entry_matches(
    descriptor: int, entry: SourceEntry, max_bytes: int
) -> None:
    payload, metadata = _read_descriptor(
        descriptor,
        byte_limit=max_bytes,
        overflow_code="READ_SIZE_LIMIT",
    )
    executable = os.name != "nt" and bool(metadata.st_mode & 0o111)
    if (
        entry.type != "file"
        or entry.size_bytes != len(payload)
        or entry.sha256 != hashlib.sha256(payload).hexdigest()
        or entry.executable != executable
    ):
        raise R0ReadError("READ_SOURCE_ENTRY_CHANGED")


def _open_with_dirfd(root: Path, relative: str) -> int:
    directory_fds: list[int] = []
    descriptor: int | None = None
    try:
        root_before = root.lstat()
        _assert_directory(root_before, ".")
        root_fd = os.open(root, _DIRECTORY_FLAGS)
        directory_fds.append(root_fd)
        root_opened = os.fstat(root_fd)
        if not _same_identity(root_before, root_opened):
            raise R0ReadError("READ_PATH_UNSAFE")
        current_fd = root_fd
        parts = relative.split("/")
        for part in parts[:-1]:
            metadata = os.stat(
                part, dir_fd=current_fd, follow_symlinks=False
            )
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
            ):
                raise R0ReadError("READ_PATH_UNSAFE")
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            directory_fds.append(child_fd)
            if not _same_identity(metadata, os.fstat(child_fd)):
                raise R0ReadError("READ_PATH_UNSAFE")
            current_fd = child_fd
        leaf = parts[-1]
        before = os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or _is_reparse(before):
            raise R0ReadError("READ_PATH_UNSAFE")
        if not stat.S_ISREG(before.st_mode):
            raise R0ReadError("READ_LEAF_NOT_REGULAR")
        descriptor = os.open(leaf, _FILE_FLAGS, dir_fd=current_fd)
        opened = os.fstat(descriptor)
        after = os.stat(leaf, dir_fd=current_fd, follow_symlinks=False)
        if not (
            _same_identity(before, opened)
            and _same_identity(opened, after)
            and _same_identity(root_opened, root.lstat())
        ):
            raise R0ReadError("READ_PATH_UNSAFE")
        result = descriptor
        descriptor = None
        return result
    except R0ReadError:
        raise
    except (OSError, SourceStateError) as exc:
        raise R0ReadError("READ_PATH_UNSAFE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def _open_with_paths(root: Path, relative: str) -> int:
    parents: list[tuple[Path, os.stat_result]] = []
    descriptor: int | None = None
    try:
        current = root
        root_metadata = root.lstat()
        _assert_directory(root_metadata, ".")
        parents.append((root, root_metadata))
        parts = relative.split("/")
        for part in parts[:-1]:
            current = current / part
            metadata = current.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or _is_reparse(metadata)
            ):
                raise R0ReadError("READ_PATH_UNSAFE")
            parents.append((current, metadata))
        leaf = current / parts[-1]
        before = leaf.lstat()
        if stat.S_ISLNK(before.st_mode) or _is_reparse(before):
            raise R0ReadError("READ_PATH_UNSAFE")
        if not stat.S_ISREG(before.st_mode):
            raise R0ReadError("READ_LEAF_NOT_REGULAR")
        descriptor = os.open(leaf, _FILE_FLAGS)
        opened = os.fstat(descriptor)
        after = leaf.lstat()
        parents_stable = all(
            _same_identity(metadata, path.lstat())
            for path, metadata in parents
        )
        if not (
            _same_identity(before, opened)
            and _same_identity(opened, after)
            and parents_stable
        ):
            raise R0ReadError("READ_PATH_UNSAFE")
        result = descriptor
        descriptor = None
        return result
    except R0ReadError:
        raise
    except (OSError, SourceStateError) as exc:
        raise R0ReadError("READ_PATH_UNSAFE") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_verified(root: Path, relative: str) -> int:
    return (
        _open_with_dirfd(root, relative)
        if _HAS_DIRFD
        else _open_with_paths(root, relative)
    )


class R0ReadPreparer:
    def __init__(
        self,
        *,
        root: Path,
        policy: SourceSelectionPolicy,
        base_revision: str,
        max_bytes: int,
        gitlink_catalog: object | None = None,
        stage_hook: StageHook | None = None,
    ) -> None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
        ):
            raise R0ReadError("READ_SIZE_LIMIT_INVALID")
        self.root = Path(root)
        self.policy = policy
        self.base_revision = base_revision
        self.max_bytes = max_bytes
        self.gitlink_catalog = gitlink_catalog
        self.stage_hook = stage_hook

    def prepare(
        self,
        ticket: object,
        call: CheckedInvocation,
        descriptor: ToolDescriptor,
    ) -> PreparedDispatch:
        del ticket
        if descriptor.tool_key != READ_TOOL_KEY or call.tool_key != READ_TOOL_KEY:
            raise R0ReadError("READ_TOOL_IDENTITY_INVALID")
        if not isinstance(call.tool_input, ReadSourceFileInput):
            raise R0ReadError("READ_INPUT_INVALID")
        relative = call.tool_input.relative_path
        if _is_excluded(relative, self.policy.excluded_control_roots):
            raise R0ReadError("READ_PATH_EXCLUDED")
        source_before = snapshot_source_tree(
            self.root,
            policy=self.policy,
            base_revision=self.base_revision,
            gitlink_catalog=self.gitlink_catalog,
        )
        if self.stage_hook is not None:
            self.stage_hook("after_source_before", self.root)
        entries = {entry.path: entry for entry in source_before.entries}
        entry = entries.get(relative)
        if entry is None:
            parts = relative.split("/")
            if any(
                entries.get("/".join(parts[:index])) is not None
                for index in range(1, len(parts))
            ):
                raise R0ReadError("READ_PATH_UNSAFE")
            if any(path.startswith(relative + "/") for path in entries):
                raise R0ReadError("READ_LEAF_NOT_REGULAR")
            raise R0ReadError("READ_SOURCE_ENTRY_CHANGED")
        if entry.type != "file":
            raise R0ReadError("READ_PATH_UNSAFE")
        descriptor_fd: int | None = None
        try:
            descriptor_fd = _open_verified(self.root, relative)
            _assert_entry_matches(descriptor_fd, entry, self.max_bytes)
            handle = PreparedR0ReadHandle(descriptor_fd, entry)
            prepared = PreparedDispatch(
                tool_key=descriptor.tool_key,
                tool_version=descriptor.tool_version,
                source_before=source_before,
                dispatch_handle=handle,
            )
            if self.stage_hook is not None:
                self.stage_hook("after_file_prepared", self.root)
            descriptor_fd = None
            return prepared
        finally:
            if descriptor_fd is not None:
                os.close(descriptor_fd)

    def capture_after(
        self, prepared: PreparedDispatch
    ) -> SourceTreeSnapshot:
        self._handle(prepared)
        return snapshot_source_tree(
            self.root,
            policy=self.policy,
            base_revision=self.base_revision,
            gitlink_catalog=self.gitlink_catalog,
        )

    def discard(self, prepared: PreparedDispatch) -> None:
        self._handle(prepared).discard()

    @staticmethod
    def _handle(prepared: PreparedDispatch) -> PreparedR0ReadHandle:
        handle = prepared.dispatch_handle
        if (
            prepared.tool_key != READ_TOOL_KEY
            or not isinstance(handle, PreparedR0ReadHandle)
        ):
            raise R0ReadError("PREPARED_READ_INVALID")
        return handle


class R0ReadDispatcher:
    def dispatch(
        self, dispatch_capability: object, prepared: PreparedDispatch
    ) -> R0ReadReceipt:
        if dispatch_capability is None:
            raise R0ReadError("DISPATCH_CAPABILITY_INVALID")
        handle = R0ReadPreparer._handle(prepared)
        descriptor, expected_digest, expected_size = handle.take()
        try:
            payload, _ = _read_descriptor(
                descriptor,
                byte_limit=expected_size,
                overflow_code="READ_SOURCE_ENTRY_CHANGED",
            )
            digest = hashlib.sha256(payload).hexdigest()
            if len(payload) != expected_size or digest != expected_digest:
                raise R0ReadError("READ_SOURCE_ENTRY_CHANGED")
            return R0ReadReceipt(
                payload=payload,
                sha256=digest,
                size_bytes=len(payload),
            )
        finally:
            os.close(descriptor)


__all__ = [
    "R0ReadDispatcher",
    "R0ReadError",
    "R0ReadPreparer",
    "R0ReadReceipt",
    "ReadSourceFileInput",
]
