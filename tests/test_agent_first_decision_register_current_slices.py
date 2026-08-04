from __future__ import annotations

import unittest

from scripts.agent_first_decision_gate import verify_register
from scripts.agent_first_decision_register import load_register
from tests.test_agent_first_decision_register_current_state import (
    REGISTER_PATH,
    REPO_ROOT,
)


class AgentFirstDecisionRegisterCurrentSlicesTest(unittest.TestCase):
    def test_resolved_d41_leaves_all_current_slices_ready(self) -> None:
        register = load_register(REGISTER_PATH)
        for slice_id in ("S2", "S3", "S4", "S5", "S6", "S7", "S8"):
            with self.subTest(slice_id=slice_id):
                report = verify_register(
                    register,
                    REPO_ROOT,
                    require_slice=slice_id,
                    check_document=False,
                    check_history=False,
                )
                self.assertEqual("ready", report["status"])
                self.assertTrue(report["valid"])
                self.assertTrue(report["ready"])
                self.assertEqual([], report["failures"])
                self.assertIsNone(report["active_decision_id"])
                self.assertEqual(["D2"], report["inactive_decision_ids"])


if __name__ == "__main__":
    unittest.main()
