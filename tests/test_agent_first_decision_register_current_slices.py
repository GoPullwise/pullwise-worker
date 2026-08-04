from __future__ import annotations

import unittest

from scripts.agent_first_decision_gate import verify_register
from scripts.agent_first_decision_register import load_register
from tests.test_agent_first_decision_register_current_state import (
    REGISTER_PATH,
    REPO_ROOT,
)


class AgentFirstDecisionRegisterCurrentSlicesTest(unittest.TestCase):
    def test_pending_d41_blocks_only_its_required_s8_slice(self) -> None:
        register = load_register(REGISTER_PATH)
        for slice_id in ("S2", "S3", "S4", "S5", "S6", "S7"):
            with self.subTest(slice_id=slice_id):
                report = verify_register(
                    register,
                    REPO_ROOT,
                    require_slice=slice_id,
                    check_document=False,
                    check_history=False,
                )
                self.assertEqual("valid_pending", report["status"])
                self.assertTrue(report["valid"])
                self.assertFalse(report["ready"])
                self.assertEqual([], report["failures"])
                self.assertEqual("D41", report["active_decision_id"])
                self.assertEqual(["D2"], report["inactive_decision_ids"])

        report = verify_register(
            register,
            REPO_ROOT,
            require_slice="S8",
            check_document=False,
            check_history=False,
        )
        self.assertEqual("blocked", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        self.assertEqual("D41", report["active_decision_id"])
        self.assertEqual(["D41"], report["failures"][0]["decision_ids"])


if __name__ == "__main__":
    unittest.main()
