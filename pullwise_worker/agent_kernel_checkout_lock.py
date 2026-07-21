"""POSIX no-follow process and file lock for checkout coordination."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import threading
import time

from .agent_kernel_checkout_lifecycle import CheckoutAcquisitionBounds
from .agent_kernel_source_state import SourceStateError


if os.name == "posix":
    import fcntl as _fcntl
else:  # pragma: no cover - PosixCheckoutLock rejects non-POSIX hosts.
    _fcntl = None


_LOCK_FILE_NAME = "agent-kernel-checkout.lock"
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_LOCK_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_LOCK_POLL_SECONDS = 0.05


def _require_absolute(value: Path, code: str) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise SourceStateError(code) from exc
    raw_path = os.fspath(path)
    if not path.is_absolute() or "\x00" in raw_path:
        raise SourceStateError(code)
    return Path(os.path.abspath(raw_path))


def _canonical_existing_directory(value: Path, code: str) -> Path:
    path = _require_absolute(value, code)
    try:
        canonical = path.resolve(strict=True)
        metadata = canonical.lstat()
    except OSError as exc:
        raise SourceStateError(code) from exc
    _assert_directory(metadata, code)
    return canonical


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _assert_directory(metadata: os.stat_result, code: str) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise SourceStateError(code)


def _open_directory(path: Path, code: str) -> tuple[int, tuple[int, int]]:
    try:
        before = path.lstat()
        _assert_directory(before, code)
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except SourceStateError:
        raise
    except OSError as exc:
        raise SourceStateError(code) from exc
    try:
        opened = os.fstat(descriptor)
        after = path.lstat()
        _assert_directory(opened, code)
        if _identity(before) != _identity(opened) or _identity(opened) != _identity(
            after
        ):
            raise SourceStateError(code)
        return descriptor, _identity(opened)
    except BaseException:
        os.close(descriptor)
        raise


def _assert_directory_identity(
    path: Path,
    descriptor: int,
    expected: tuple[int, int],
    code: str,
) -> None:
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
    except OSError as exc:
        raise SourceStateError(code) from exc
    _assert_directory(opened, code)
    _assert_directory(current, code)
    if _identity(opened) != expected or _identity(current) != expected:
        raise SourceStateError(code)


class _ProcessMutex:
    """Non-reentrant local exclusion that a capture session may release."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._owner: int | None = None

    def acquire(
        self,
        deadline: float,
        bounds: CheckoutAcquisitionBounds,
    ) -> None:
        identity = threading.get_ident()
        with self._condition:
            if self._owner == identity:
                raise SourceStateError("CHECKOUT_WINDOW_REENTRANT")
        while True:
            wait_seconds = bounds._wait_seconds(deadline)
            with self._condition:
                if self._owner == identity:
                    raise SourceStateError("CHECKOUT_WINDOW_REENTRANT")
                if self._owner is None:
                    self._owner = identity
                    return
                self._condition.wait(timeout=wait_seconds)

    def release(self) -> None:
        with self._condition:
            if self._owner is None:
                return
            self._owner = None
            self._condition.notify()


_PROCESS_MUTEXES_GUARD = threading.Lock()
_PROCESS_MUTEXES: dict[str, _ProcessMutex] = {}


def _process_mutex(control_root: Path) -> _ProcessMutex:
    key = os.fspath(control_root)
    with _PROCESS_MUTEXES_GUARD:
        return _PROCESS_MUTEXES.setdefault(key, _ProcessMutex())


class _HeldPosixLock:
    def __init__(
        self,
        *,
        control_root: Path,
        control_descriptor: int,
        control_identity: tuple[int, int],
        lock_descriptor: int,
        lock_identity: tuple[int, int],
        process_mutex: _ProcessMutex,
    ) -> None:
        self._control_root = control_root
        self._control_descriptor = control_descriptor
        self._control_identity = control_identity
        self._lock_descriptor = lock_descriptor
        self._lock_identity = lock_identity
        self._process_mutex = process_mutex
        self._release_guard = threading.Lock()
        self._released = False

    def assert_current(self) -> None:
        _assert_directory_identity(
            self._control_root,
            self._control_descriptor,
            self._control_identity,
            "CHECKOUT_CONTROL_ROOT_CHANGED",
        )
        try:
            control_opened = os.fstat(self._control_descriptor)
            lock_path = os.stat(
                _LOCK_FILE_NAME,
                dir_fd=self._control_descriptor,
                follow_symlinks=False,
            )
            lock_opened = os.fstat(self._lock_descriptor)
        except OSError as exc:
            raise SourceStateError("CHECKOUT_LOCK_INVALID") from exc
        if (
            control_opened.st_uid != os.geteuid()
            or stat.S_IMODE(control_opened.st_mode) & 0o022
        ):
            raise SourceStateError("CHECKOUT_CONTROL_ROOT_UNTRUSTED")
        if (
            not stat.S_ISREG(lock_path.st_mode)
            or not stat.S_ISREG(lock_opened.st_mode)
            or _identity(lock_path) != self._lock_identity
            or _identity(lock_opened) != self._lock_identity
            or lock_path.st_uid != os.geteuid()
            or lock_opened.st_uid != os.geteuid()
            or lock_path.st_nlink != 1
            or lock_opened.st_nlink != 1
            or stat.S_IMODE(lock_path.st_mode) != 0o600
            or stat.S_IMODE(lock_opened.st_mode) != 0o600
        ):
            raise SourceStateError("CHECKOUT_LOCK_INVALID")

    def release(self) -> None:
        with self._release_guard:
            if self._released:
                return
            self._released = True
            try:
                assert _fcntl is not None
                _fcntl.flock(self._lock_descriptor, _fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._lock_descriptor)
                finally:
                    try:
                        os.close(self._control_descriptor)
                    finally:
                        self._process_mutex.release()


class _PosixCheckoutLock:
    def __init__(
        self, control_root: Path, *, lock_timeout_seconds: int = 30
    ) -> None:
        if (
            os.name != "posix"
            or _fcntl is None
            or not getattr(os, "O_NOFOLLOW", 0)
            or os.open not in os.supports_dir_fd
        ):
            raise SourceStateError("CHECKOUT_CAPTURE_POSIX_REQUIRED")
        if (
            isinstance(lock_timeout_seconds, bool)
            or not isinstance(lock_timeout_seconds, int)
            or not 1 <= lock_timeout_seconds <= 300
        ):
            raise SourceStateError("CHECKOUT_LOCK_TIMEOUT_INVALID")
        self._control_root = _canonical_existing_directory(
            control_root, "CHECKOUT_CONTROL_ROOT_INVALID"
        )
        self._lock_timeout_seconds = lock_timeout_seconds
        self._mutex = _process_mutex(self._control_root)

    @property
    def control_root(self) -> Path:
        return self._control_root

    def acquire(
        self, bounds: CheckoutAcquisitionBounds
    ) -> _HeldPosixLock:
        if not isinstance(bounds, CheckoutAcquisitionBounds):
            raise SourceStateError("CHECKOUT_ACQUISITION_BOUNDS_INVALID")
        deadline = bounds._effective_deadline(self._lock_timeout_seconds)
        self._mutex.acquire(deadline, bounds)
        control_descriptor: int | None = None
        lock_descriptor: int | None = None
        locked = False
        try:
            bounds._checkpoint(deadline)
            control_descriptor, control_identity = _open_directory(
                self._control_root, "CHECKOUT_CONTROL_ROOT_INVALID"
            )
            control_metadata = os.fstat(control_descriptor)
            if (
                control_metadata.st_uid != os.geteuid()
                or stat.S_IMODE(control_metadata.st_mode) & 0o022
            ):
                raise SourceStateError("CHECKOUT_CONTROL_ROOT_UNTRUSTED")
            try:
                lock_descriptor = os.open(
                    _LOCK_FILE_NAME,
                    _LOCK_FLAGS,
                    0o600,
                    dir_fd=control_descriptor,
                )
            except OSError as exc:
                raise SourceStateError("CHECKOUT_LOCK_INVALID") from exc
            lock_metadata = os.fstat(lock_descriptor)
            if (
                not stat.S_ISREG(lock_metadata.st_mode)
                or lock_metadata.st_uid != os.geteuid()
                or lock_metadata.st_nlink != 1
            ):
                raise SourceStateError("CHECKOUT_LOCK_INVALID")
            os.fchmod(lock_descriptor, 0o600)
            lock_metadata = os.fstat(lock_descriptor)
            if stat.S_IMODE(lock_metadata.st_mode) != 0o600:
                raise SourceStateError("CHECKOUT_LOCK_INVALID")
            while True:
                bounds._checkpoint(deadline)
                try:
                    assert _fcntl is not None
                    _fcntl.flock(
                        lock_descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB
                    )
                    locked = True
                    break
                except OSError as exc:
                    if not isinstance(exc, BlockingIOError) and exc.errno not in {
                        errno.EACCES,
                        errno.EAGAIN,
                    }:
                        raise SourceStateError(
                            "CHECKOUT_LOCK_UNAVAILABLE"
                        ) from exc
                    time.sleep(bounds._wait_seconds(deadline))
            held = _HeldPosixLock(
                control_root=self._control_root,
                control_descriptor=control_descriptor,
                control_identity=control_identity,
                lock_descriptor=lock_descriptor,
                lock_identity=_identity(lock_metadata),
                process_mutex=self._mutex,
            )
            held.assert_current()
            bounds._checkpoint(deadline)
            return held
        except BaseException:
            if lock_descriptor is not None:
                if locked:
                    try:
                        assert _fcntl is not None
                        _fcntl.flock(lock_descriptor, _fcntl.LOCK_UN)
                    except BaseException:
                        pass
                try:
                    os.close(lock_descriptor)
                except BaseException:
                    pass
            if control_descriptor is not None:
                try:
                    os.close(control_descriptor)
                except BaseException:
                    pass
            try:
                self._mutex.release()
            except BaseException:
                pass
            raise


__all__: list[str] = []
