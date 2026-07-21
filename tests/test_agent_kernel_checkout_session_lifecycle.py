from __future__ import annotations

import threading
import unittest

from pullwise_worker.agent_kernel_checkout_window import CheckoutCaptureSession
from pullwise_worker.agent_kernel_source_state import SourceStateError


class _WindowStub:
    def __init__(self, release_error: BaseException | None = None) -> None:
        self._release_error = release_error
        self.release_calls = 0

    def release(self) -> None:
        self.release_calls += 1
        if self._release_error is not None:
            raise self._release_error


def _session(window: _WindowStub) -> CheckoutCaptureSession:
    session = object.__new__(CheckoutCaptureSession)
    session._operation_lock = threading.Lock()
    session._closed = False
    session._window = window  # type: ignore[assignment]
    return session


class CheckoutCaptureSessionLifecycleTest(unittest.TestCase):
    def test_body_error_wins_over_context_release_error(self) -> None:
        window = _WindowStub(
            SourceStateError("CHECKOUT_LOCK_INVALID")
        )
        session = _session(window)

        with self.assertRaisesRegex(RuntimeError, "body failed"):
            with session:
                raise RuntimeError("body failed")

        self.assertTrue(session.closed)
        self.assertEqual(1, window.release_calls)

    def test_release_error_surfaces_after_successful_context_body(self) -> None:
        window = _WindowStub(
            SourceStateError("CHECKOUT_LOCK_INVALID")
        )
        session = _session(window)

        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_LOCK_INVALID"
        ):
            with session:
                pass

        self.assertTrue(session.closed)
        self.assertEqual(1, window.release_calls)


if __name__ == "__main__":
    unittest.main()
