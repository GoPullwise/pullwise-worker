"""Atomic POSIX source capture around cooperating checkout writers."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import threading
from typing import Callable

from .agent_kernel_checkout_lock import (
    _HeldPosixLock,
    _canonical_existing_directory,
    _require_absolute,
)
from .agent_kernel_checkout_lifecycle import CheckoutAcquisitionBounds
from .agent_kernel_checkout_writer import CheckoutWriterCoordinator
from .agent_kernel_gitlinks import inspect_gitlinks
from .agent_kernel_source_scan import snapshot_source_tree
from .agent_kernel_source_state import (
    SourceSelectionPolicy,
    SourceStateError,
    SourceTreeSnapshot,
)


_EXACT_REVISION = re.compile(r"^[0-9a-f]{40}$")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _contains(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _assert_checkout_directory(metadata: os.stat_result) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SourceStateError("CHECKOUT_ROOT_INVALID")


def _cleanup_steps(
    primary_error: BaseException | None,
    *steps: Callable[[], object],
) -> BaseException | None:
    error = primary_error
    for step in steps:
        try:
            step()
        except BaseException as exc:
            if error is None:
                error = exc
    return error


def _raise_preserved(error: BaseException) -> None:
    raise error.with_traceback(error.__traceback__)


class _CheckoutBinding:
    def __init__(
        self, root: Path, descriptor: int, identity: tuple[int, int]
    ) -> None:
        self._root = root
        self._descriptor = descriptor
        self._identity = identity
        self._closed = False

    @classmethod
    def open(cls, root: Path) -> "_CheckoutBinding":
        try:
            before = root.lstat()
            _assert_checkout_directory(before)
            descriptor = os.open(root, _DIRECTORY_FLAGS)
        except SourceStateError:
            raise
        except OSError as exc:
            raise SourceStateError("CHECKOUT_ROOT_INVALID") from exc
        try:
            opened = os.fstat(descriptor)
            after = root.lstat()
            _assert_checkout_directory(opened)
            if _identity(before) != _identity(opened) or _identity(
                opened
            ) != _identity(after):
                raise SourceStateError("CHECKOUT_ROOT_INVALID")
            return cls(root, descriptor, _identity(opened))
        except BaseException:
            os.close(descriptor)
            raise

    def assert_current(self) -> None:
        try:
            opened = os.fstat(self._descriptor)
            current = self._root.lstat()
        except OSError as exc:
            raise SourceStateError("CHECKOUT_IDENTITY_CHANGED") from exc
        _assert_checkout_directory(opened)
        _assert_checkout_directory(current)
        if _identity(opened) != self._identity or _identity(
            current
        ) != self._identity:
            raise SourceStateError("CHECKOUT_IDENTITY_CHANGED")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._descriptor)


class _CaptureWindow:
    def __init__(
        self, lock: _HeldPosixLock, checkout: _CheckoutBinding
    ) -> None:
        self._lock = lock
        self._checkout = checkout
        self._release_guard = threading.Lock()
        self._released = False

    def assert_current(self) -> None:
        self._lock.assert_current()
        self._checkout.assert_current()

    def release(self) -> None:
        with self._release_guard:
            if self._released:
                return
            self._released = True
            error = _cleanup_steps(
                None,
                self._checkout.close,
                self._lock.release,
            )
        if error is not None:
            _raise_preserved(error)


class CheckoutCaptureSession:
    """An exclusive before/after capture lease; no catalog escapes it."""

    __slots__ = (
        "_base_revision",
        "_closed",
        "_coordinator",
        "_operation_lock",
        "_source_before",
        "_window",
    )

    def __init__(
        self,
        coordinator: "CheckoutCaptureCoordinator",
        window: _CaptureWindow,
        base_revision: str,
        source_before: SourceTreeSnapshot,
    ) -> None:
        self._coordinator = coordinator
        self._window = window
        self._base_revision = base_revision
        self._source_before = source_before
        self._operation_lock = threading.Lock()
        self._closed = False

    @property
    def source_before(self) -> SourceTreeSnapshot:
        return self._source_before

    @property
    def closed(self) -> bool:
        with self._operation_lock:
            return self._closed

    def capture_after(self) -> SourceTreeSnapshot:
        with self._operation_lock:
            if self._closed:
                raise SourceStateError("CHECKOUT_CAPTURE_SESSION_CLOSED")
            captured: SourceTreeSnapshot | None = None
            primary_error: BaseException | None = None
            try:
                captured = self._coordinator._capture(
                    self._window, self._base_revision
                )
            except BaseException as exc:
                primary_error = exc
            finally:
                self._closed = True
                primary_error = _cleanup_steps(
                    primary_error,
                    self._window.release,
                )
            if primary_error is not None:
                _raise_preserved(primary_error)
            assert captured is not None
            return captured

    def close(self) -> None:
        with self._operation_lock:
            if self._closed:
                return
            self._closed = True
            self._window.release()

    def __enter__(self) -> "CheckoutCaptureSession":
        return self

    def __exit__(
        self,
        _exc_type: object,
        body_error: object,
        _traceback: object,
    ) -> None:
        try:
            self.close()
        except BaseException:
            if body_error is None:
                raise


class CheckoutCaptureCoordinator:
    """Serializes checkout writers with a complete before/after scan window."""

    def __init__(
        self,
        *,
        checkout_root: Path,
        control_root: Path,
        policy: SourceSelectionPolicy,
        git_executable: Path,
        acquisition_bounds: CheckoutAcquisitionBounds,
        git_timeout_seconds: int = 30,
        lock_timeout_seconds: int = 30,
    ) -> None:
        self._writer = CheckoutWriterCoordinator(
            control_root=control_root,
            acquisition_bounds=acquisition_bounds,
            lock_timeout_seconds=lock_timeout_seconds,
        )
        if not isinstance(policy, SourceSelectionPolicy):
            raise SourceStateError("SOURCE_POLICY_INVALID")
        if (
            isinstance(git_timeout_seconds, bool)
            or not isinstance(git_timeout_seconds, int)
            or not 1 <= git_timeout_seconds <= 300
        ):
            raise SourceStateError("SOURCE_GIT_TIMEOUT_INVALID")
        self._checkout_root = _canonical_existing_directory(
            checkout_root, "CHECKOUT_ROOT_INVALID"
        )
        if _contains(self._checkout_root, self._writer.control_root) or _contains(
            self._writer.control_root, self._checkout_root
        ):
            raise SourceStateError("CHECKOUT_ROOTS_NOT_DISJOINT")
        self._policy = policy
        self._git_executable = _require_absolute(
            git_executable, "SOURCE_GIT_EXECUTABLE_INVALID"
        )
        self._git_timeout_seconds = git_timeout_seconds

    @property
    def checkout_root(self) -> Path:
        """Return the validated absolute root used by every capture."""
        return self._checkout_root

    def _capture(
        self, window: _CaptureWindow, base_revision: str
    ) -> SourceTreeSnapshot:
        window.assert_current()
        catalog = inspect_gitlinks(
            self._checkout_root,
            base_revision=base_revision,
            git_executable=self._git_executable,
            timeout_seconds=self._git_timeout_seconds,
        )
        window.assert_current()
        snapshot = snapshot_source_tree(
            self._checkout_root,
            policy=self._policy,
            base_revision=base_revision,
            gitlink_catalog=catalog,
        )
        window.assert_current()
        return snapshot

    def begin_capture(self, *, base_revision: str) -> CheckoutCaptureSession:
        if not isinstance(base_revision, str) or not _EXACT_REVISION.fullmatch(
            base_revision
        ):
            raise SourceStateError("SOURCE_BASE_REVISION_INVALID")
        held_lock = self._writer._acquire()
        checkout: _CheckoutBinding | None = None
        try:
            checkout = _CheckoutBinding.open(self._checkout_root)
            window = _CaptureWindow(held_lock, checkout)
            source_before = self._capture(window, base_revision)
            return CheckoutCaptureSession(
                self, window, base_revision, source_before
            )
        except BaseException as exc:
            steps = (
                (checkout.close,) if checkout is not None else ()
            ) + (held_lock.release,)
            error = _cleanup_steps(exc, *steps)
            assert error is not None
            _raise_preserved(error)


__all__ = ["CheckoutCaptureCoordinator", "CheckoutCaptureSession"]
