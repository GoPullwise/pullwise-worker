from __future__ import annotations

import inspect
import math
import unittest

from pullwise_worker.agent_kernel_checkout_lifecycle import (
    CheckoutInvocationBounds,
)
from pullwise_worker.agent_kernel_checkout_window import (
    CheckoutCaptureCoordinator,
)
from pullwise_worker.agent_kernel_source_state import SourceStateError


class CheckoutInvocationBoundsContractTest(unittest.TestCase):
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
                    CheckoutInvocationBounds(
                        deadline_monotonic=value,  # type: ignore[arg-type]
                        cancellation_requested=lambda: False,
                    )

    def test_finite_deadline_and_callable_cancellation_are_accepted(self) -> None:
        bounds = CheckoutInvocationBounds(
            deadline_monotonic=123.5,
            cancellation_requested=lambda: False,
        )

        self.assertIsInstance(bounds, CheckoutInvocationBounds)

    def test_cancellation_source_must_be_callable(self) -> None:
        with self.assertRaises(SourceStateError):
            CheckoutInvocationBounds(
                deadline_monotonic=123.5,
                cancellation_requested=False,  # type: ignore[arg-type]
            )

    def test_capture_coordinator_requires_keyword_invocation_bounds(self) -> None:
        parameter = inspect.signature(CheckoutCaptureCoordinator).parameters[
            "invocation_bounds"
        ]

        self.assertEqual(inspect.Parameter.KEYWORD_ONLY, parameter.kind)
        self.assertIs(inspect.Parameter.empty, parameter.default)


if __name__ == "__main__":
    unittest.main()
