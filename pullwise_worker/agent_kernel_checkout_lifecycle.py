"""Process-local acquisition bounds for checkout coordination."""

from __future__ import annotations

import math
import threading
import time
from typing import Callable

from .agent_kernel_source_state import SourceStateError


_MAX_POLL_SECONDS = 0.05


def _finite_number(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SourceStateError(code)
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise SourceStateError(code) from exc
    if not math.isfinite(normalized):
        raise SourceStateError(code)
    return normalized


class CheckoutAcquisitionBounds:
    """One non-renewable monotonic deadline and local cancellation source.

    The caller derives the process-local deadline. This type neither interprets
    wall time nor persists authority. The cancellation callback is trusted to
    be local and non-blocking and must return an exact ``bool``.
    """

    __slots__ = (
        "_cancellation_requested",
        "_deadline_monotonic",
        "_last_monotonic",
        "_observation_lock",
    )

    def __init__(
        self,
        *,
        deadline_monotonic: int | float,
        cancellation_requested: Callable[[], bool],
    ) -> None:
        deadline = _finite_number(
            deadline_monotonic,
            "CHECKOUT_ACQUISITION_DEADLINE_INVALID",
        )
        if not callable(cancellation_requested):
            raise SourceStateError("CHECKOUT_CANCELLATION_SOURCE_INVALID")
        self._deadline_monotonic = deadline
        self._cancellation_requested = cancellation_requested
        self._last_monotonic: float | None = None
        self._observation_lock = threading.Lock()

    @property
    def deadline_monotonic(self) -> float:
        return self._deadline_monotonic

    def _observe(self) -> float:
        """Check cancellation first, then sample a non-decreasing clock."""
        with self._observation_lock:
            try:
                cancelled = self._cancellation_requested()
            except BaseException as exc:
                raise SourceStateError(
                    "CHECKOUT_CANCELLATION_CHECK_FAILED"
                ) from exc
            if type(cancelled) is not bool:
                raise SourceStateError("CHECKOUT_CANCELLATION_RESULT_INVALID")
            if cancelled:
                raise SourceStateError("CHECKOUT_ACQUISITION_CANCELLED")
            try:
                now = float(time.monotonic())
            except (OverflowError, TypeError, ValueError) as exc:
                raise SourceStateError(
                    "CHECKOUT_MONOTONIC_CLOCK_INVALID"
                ) from exc
            if not math.isfinite(now):
                raise SourceStateError("CHECKOUT_MONOTONIC_CLOCK_INVALID")
            if self._last_monotonic is not None and now < self._last_monotonic:
                raise SourceStateError("CHECKOUT_MONOTONIC_CLOCK_REVERSED")
            self._last_monotonic = now
            return now

    def _remaining_until(
        self, effective_deadline: float | None = None
    ) -> float:
        now = self._observe()
        if now >= self._deadline_monotonic:
            raise SourceStateError(
                "CHECKOUT_ACQUISITION_DEADLINE_EXCEEDED"
            )
        if effective_deadline is None:
            return self._deadline_monotonic - now
        deadline = _finite_number(
            effective_deadline,
            "CHECKOUT_ACQUISITION_DEADLINE_INVALID",
        )
        if deadline > self._deadline_monotonic:
            raise SourceStateError("CHECKOUT_ACQUISITION_DEADLINE_INVALID")
        if now >= deadline:
            raise SourceStateError("CHECKOUT_LOCK_TIMEOUT")
        return deadline - now

    def checkpoint(self) -> float:
        """Fail closed if cancelled/expired and return global time remaining."""
        return self._remaining_until()

    def _effective_deadline(self, local_timeout_seconds: int | float) -> float:
        timeout = _finite_number(
            local_timeout_seconds,
            "CHECKOUT_LOCK_TIMEOUT_INVALID",
        )
        if timeout <= 0:
            raise SourceStateError("CHECKOUT_LOCK_TIMEOUT_INVALID")
        remaining = self.checkpoint()
        now = self._deadline_monotonic - remaining
        local_deadline = now + timeout
        if not math.isfinite(local_deadline):
            raise SourceStateError("CHECKOUT_LOCK_TIMEOUT_INVALID")
        return min(self._deadline_monotonic, local_deadline)

    def _checkpoint(self, effective_deadline: float) -> float:
        """Return time remaining under the global/local acquisition clamp."""
        return self._remaining_until(effective_deadline)

    def _wait_seconds(self, effective_deadline: float) -> float:
        """Return a cancellation polling wait no longer than 50 milliseconds."""
        return min(
            _MAX_POLL_SECONDS,
            self._remaining_until(effective_deadline),
        )


__all__ = ["CheckoutAcquisitionBounds"]
