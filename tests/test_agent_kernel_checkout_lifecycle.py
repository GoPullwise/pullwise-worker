from __future__ import annotations

import inspect
import math
import time
import unittest

from pullwise_worker.agent_kernel_checkout_lifecycle import (
    CheckoutAcquisitionBounds,
)
from pullwise_worker.agent_kernel_checkout_lock import _ProcessMutex
from pullwise_worker.agent_kernel_checkout_window import (
    CheckoutCaptureCoordinator,
)
from pullwise_worker.agent_kernel_source_state import SourceStateError


class CheckoutAcquisitionBoundsContractTest(unittest.TestCase):
    def test_deadline_must_be_a_finite_number_but_not_bool(self) -> None:
        invalid_values = (
            True,
            False,
            None,
            "100.0",
            math.nan,
            math.inf,
            -math.inf,
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(SourceStateError):
                    CheckoutAcquisitionBounds(
                        deadline_monotonic=value,  # type: ignore[arg-type]
                        cancellation_requested=lambda: False,
                    )

    def test_finite_deadline_and_callable_cancellation_are_accepted(self) -> None:
        bounds = CheckoutAcquisitionBounds(
            deadline_monotonic=123.5,
            cancellation_requested=lambda: False,
        )

        self.assertIsInstance(bounds, CheckoutAcquisitionBounds)
        with self.assertRaises(AttributeError):
            bounds.deadline_monotonic = 456.0  # type: ignore[misc]

    def test_cancellation_source_must_be_callable(self) -> None:
        with self.assertRaises(SourceStateError):
            CheckoutAcquisitionBounds(
                deadline_monotonic=123.5,
                cancellation_requested=False,  # type: ignore[arg-type]
            )

    def test_cancellation_result_must_be_exact_bool(self) -> None:
        bounds = CheckoutAcquisitionBounds(
            deadline_monotonic=time.monotonic() + 5,
            cancellation_requested=lambda: 1,  # type: ignore[return-value]
        )

        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_CANCELLATION_RESULT_INVALID"
        ):
            bounds.checkpoint()

    def test_cancellation_failure_is_not_treated_as_normal_cancel(self) -> None:
        def fail() -> bool:
            raise RuntimeError("probe failed")

        bounds = CheckoutAcquisitionBounds(
            deadline_monotonic=time.monotonic() + 5,
            cancellation_requested=fail,
        )

        with self.assertRaisesRegex(
            SourceStateError, "CHECKOUT_CANCELLATION_CHECK_FAILED"
        ) as raised:
            bounds.checkpoint()
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)

    def test_capture_coordinator_requires_keyword_acquisition_bounds(self) -> None:
        parameter = inspect.signature(CheckoutCaptureCoordinator).parameters[
            "acquisition_bounds"
        ]

        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
        self.assertIs(inspect.Parameter.empty, parameter.default)

    def test_process_mutex_never_polls_callback_under_condition_lock(
        self,
    ) -> None:
        class TrackingCondition:
            def __init__(self) -> None:
                self.owned = False

            def __enter__(self) -> "TrackingCondition":
                self.owned = True
                return self

            def __exit__(self, *exc_info: object) -> None:
                self.owned = False

            def wait(self, *, timeout: float) -> None:
                self.fail_if_called(timeout)

            def notify(self) -> None:
                pass

            @staticmethod
            def fail_if_called(_timeout: float) -> None:
                raise AssertionError("free mutex must not wait")

        condition = TrackingCondition()
        callback_lock_states: list[bool] = []
        bounds = CheckoutAcquisitionBounds(
            deadline_monotonic=time.monotonic() + 5,
            cancellation_requested=lambda: bool(
                callback_lock_states.append(condition.owned)
            ),
        )
        mutex = _ProcessMutex()
        mutex._condition = condition  # type: ignore[assignment]

        mutex.acquire(time.monotonic() + 1, bounds)
        mutex.release()

        self.assertGreaterEqual(len(callback_lock_states), 1)
        self.assertFalse(any(callback_lock_states))


if __name__ == "__main__":
    unittest.main()
