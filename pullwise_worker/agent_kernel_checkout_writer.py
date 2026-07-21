"""Cooperating writer ownership for materialized checkout mutation."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .agent_kernel_checkout_lifecycle import CheckoutAcquisitionBounds
from .agent_kernel_checkout_lock import _HeldPosixLock, _PosixCheckoutLock
from .agent_kernel_source_state import SourceStateError


class CheckoutWriterCoordinator:
    """Acquire the same control-root lock used by source capture."""

    def __init__(
        self,
        *,
        control_root: Path,
        acquisition_bounds: CheckoutAcquisitionBounds,
        lock_timeout_seconds: int = 30,
    ) -> None:
        if not isinstance(acquisition_bounds, CheckoutAcquisitionBounds):
            raise SourceStateError("CHECKOUT_ACQUISITION_BOUNDS_INVALID")
        self._acquisition_bounds = acquisition_bounds
        self._lock = _PosixCheckoutLock(
            control_root,
            lock_timeout_seconds=lock_timeout_seconds,
        )

    @property
    def control_root(self) -> Path:
        return self._lock.control_root

    @property
    def acquisition_bounds(self) -> CheckoutAcquisitionBounds:
        return self._acquisition_bounds

    def _acquire(self) -> _HeldPosixLock:
        """Acquire a lease for capture or writer use under identical bounds."""
        return self._lock.acquire(self._acquisition_bounds)

    @contextmanager
    def writer(self) -> Iterator[CheckoutAcquisitionBounds]:
        held_lock = self._acquire()
        primary_error: BaseException | None = None
        try:
            held_lock.assert_current()
            yield self._acquisition_bounds
        except BaseException as exc:
            primary_error = exc
        finally:
            if primary_error is None:
                try:
                    self._acquisition_bounds.checkpoint()
                except BaseException as exc:
                    primary_error = exc
            try:
                held_lock.assert_current()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
            try:
                held_lock.release()
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
        if primary_error is not None:
            raise primary_error.with_traceback(primary_error.__traceback__)


__all__ = ["CheckoutWriterCoordinator"]
