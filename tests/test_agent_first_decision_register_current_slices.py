from __future__ import annotations

import unittest

from scripts.agent_first_decision_gate import verify_register
from scripts.agent_first_decision_register import load_register
from tests.test_agent_first_decision_register_current_state import (
    REGISTER_PATH,
    REPO_ROOT,
)


class AgentFirstDecisionRegisterCurrentSlicesTest(unittest.TestCase):
    def test_d36_does_not_block_completed_slice_two(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(
            register, REPO_ROOT, require_slice="S2", check_document=False
        )

        self.assertEqual("valid_pending", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        self.assertEqual([], report["failures"])
        self.assertEqual("D36", report["active_decision_id"])
        self.assertEqual(["D2"], report["inactive_decision_ids"])

    def test_pending_d36_blocks_slice_three_and_later(self) -> None:
        register = load_register(REGISTER_PATH)
        for slice_id in ("S3", "S4", "S5", "S6", "S7", "S8"):
            with self.subTest(slice_id=slice_id):
                report = verify_register(
                    register,
                    REPO_ROOT,
                    require_slice=slice_id,
                    check_document=False,
                    check_history=False,
                )
                self.assertEqual("blocked", report["status"])
                self.assertEqual(
                    [
                        {
                            "code": "slice_blocked_by_pending_decisions",
                            "slice": slice_id,
                            "decision_ids": ["D36"],
                        }
                    ],
                    report["failures"],
                )
                self.assertTrue(report["valid"])
                self.assertFalse(report["ready"])
                self.assertEqual("D36", report["active_decision_id"])


if __name__ == "__main__":
    unittest.main()
