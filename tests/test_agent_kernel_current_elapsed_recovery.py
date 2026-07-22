from __future__ import annotations

import unittest

from pullwise_worker.agent_kernel_current_budget import CurrentBudgetError

from pullwise_worker.agent_kernel_current_elapsed_recovery import (
    rebuild_execution_window,
)


class CurrentElapsedRecoveryTest(unittest.TestCase):
    def test_rebuild_rejects_boolean_hard_wall(self) -> None:
        with self.assertRaises(CurrentBudgetError) as raised:
            rebuild_execution_window(
                absolute_deadline_ms=80_000,
                trusted_wall_ms=20_000,
                durable_control_wall_ms=19_000,
                hard_wall_ms=True,
                durable_consumed_ms=17_000,
                active_reserved_ms=8_000,
                local_monotonic_now_ms=250,
            )

        self.assertEqual("CONTRACT_INVALID", raised.exception.code)

    def test_rebuild_rejects_boolean_absolute_deadline(self) -> None:
        with self.assertRaises(CurrentBudgetError) as raised:
            rebuild_execution_window(
                absolute_deadline_ms=True,
                trusted_wall_ms=20_000,
                durable_control_wall_ms=19_000,
                hard_wall_ms=55_000,
                durable_consumed_ms=17_000,
                active_reserved_ms=8_000,
                local_monotonic_now_ms=250,
            )

        self.assertEqual("CONTRACT_INVALID", raised.exception.code)

    def test_rebuild_rejects_boolean_durable_control_wall_clock(self) -> None:
        with self.assertRaises(CurrentBudgetError) as raised:
            rebuild_execution_window(
                absolute_deadline_ms=80_000,
                trusted_wall_ms=20_000,
                durable_control_wall_ms=True,
                hard_wall_ms=55_000,
                durable_consumed_ms=17_000,
                active_reserved_ms=8_000,
                local_monotonic_now_ms=250,
            )

        self.assertEqual("CONTRACT_INVALID", raised.exception.code)

    def test_rebuild_rejects_boolean_trusted_wall_clock(self) -> None:
        with self.assertRaises(CurrentBudgetError) as raised:
            rebuild_execution_window(
                absolute_deadline_ms=80_000,
                trusted_wall_ms=True,
                durable_control_wall_ms=0,
                hard_wall_ms=55_000,
                durable_consumed_ms=17_000,
                active_reserved_ms=8_000,
                local_monotonic_now_ms=250,
            )

        self.assertEqual("CONTRACT_INVALID", raised.exception.code)

    def test_rebuild_rejects_control_wall_clock_after_trusted_wall_clock(self) -> None:
        with self.assertRaises(CurrentBudgetError) as raised:
            rebuild_execution_window(
                absolute_deadline_ms=80_000,
                trusted_wall_ms=19_999,
                durable_control_wall_ms=20_000,
                hard_wall_ms=55_000,
                durable_consumed_ms=17_000,
                active_reserved_ms=8_000,
                local_monotonic_now_ms=250,
            )

        self.assertEqual(raised.exception.code, "CONTRACT_INVALID")

    def test_rebuilds_window_from_durable_elapsed_and_local_monotonic_now(self) -> None:
        inputs = {
            "durable_control_wall_ms": 19_000,
            "absolute_deadline_ms": 80_000,
            "trusted_wall_ms": 20_000,
            "hard_wall_ms": 55_000,
            "durable_consumed_ms": 17_000,
            "active_reserved_ms": 8_000,
        }

        early_origin = rebuild_execution_window(
            **inputs,
            local_monotonic_now_ms=250,
        )
        late_origin = rebuild_execution_window(
            **inputs,
            local_monotonic_now_ms=9_000_000_000,
        )

        self.assertEqual(30_000, early_origin.execution_window_ms)
        self.assertEqual(30_250, early_origin.local_monotonic_deadline_ms)
        self.assertEqual(30_000, late_origin.execution_window_ms)
        self.assertEqual(
            9_000_030_000,
            late_origin.local_monotonic_deadline_ms,
        )


if __name__ == "__main__":
    unittest.main()
