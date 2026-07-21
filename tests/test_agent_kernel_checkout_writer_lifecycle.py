from __future__ import annotations

import threading
import time
import unittest

from pullwise_worker.agent_kernel_checkout_lifecycle import (
    CheckoutAcquisitionBounds,
)
from pullwise_worker.agent_kernel_checkout_writer import (
    CheckoutWriterCoordinator,
)
from pullwise_worker.agent_kernel_source_state import SourceStateError


class _HeldLockStub:
    def __init__(
        self,
        *,
        drift_after_body: bool = False,
        release_error: BaseException | None = None,
    ) -> None:
        self._drift_after_body = drift_after_body
        self._release_error = release_error
        self.assertions = 0
        self.released = False

    def assert_current(self) -> None:
        self.assertions += 1
        if self._drift_after_body and self.assertions == 2:
            raise SourceStateError("CHECKOUT_CONTROL_ROOT_CHANGED")

    def release(self) -> None:
        self.released = True
        if self._release_error is not None:
            raise self._release_error


def _coordinator(
    bounds: CheckoutAcquisitionBounds,
    held_lock: _HeldLockStub,
) -> CheckoutWriterCoordinator:
    coordinator = object.__new__(CheckoutWriterCoordinator)
    coordinator._acquisition_bounds = bounds
    coordinator._acquire = lambda: held_lock  # type: ignore[method-assign]
    return coordinator


class CheckoutWriterLifecycleTest(unittest.TestCase):
    @staticmethod
    def bounds(callback=lambda: False) -> CheckoutAcquisitionBounds:
        return CheckoutAcquisitionBounds(
            deadline_monotonic=time.monotonic() + 5,
            cancellation_requested=callback,
        )

    def test_lock_drift_wins_over_cancel_after_successful_body(self) -> None:
        cancelled = threading.Event()
        held_lock = _HeldLockStub(drift_after_body=True)
        coordinator = _coordinator(self.bounds(cancelled.is_set), held_lock)

        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_CONTROL_ROOT_CHANGED"
        ):
            with coordinator.writer():
                cancelled.set()

        self.assertTrue(held_lock.released)

    def test_cancellation_probe_failure_still_releases_lock(self) -> None:
        fail_probe = threading.Event()

        def cancellation_requested() -> bool:
            if fail_probe.is_set():
                raise RuntimeError("probe failed")
            return False

        held_lock = _HeldLockStub()
        coordinator = _coordinator(
            self.bounds(cancellation_requested), held_lock
        )

        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_CANCELLATION_CHECK_FAILED"
        ) as raised:
            with coordinator.writer():
                fail_probe.set()

        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertTrue(held_lock.released)

    def test_body_error_wins_over_release_error(self) -> None:
        held_lock = _HeldLockStub(
            release_error=SourceStateError("CHECKOUT_LOCK_INVALID")
        )
        coordinator = _coordinator(self.bounds(), held_lock)

        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with coordinator.writer():
                raise RuntimeError("body failed")

        self.assertTrue(held_lock.released)


if __name__ == "__main__":
    unittest.main()
