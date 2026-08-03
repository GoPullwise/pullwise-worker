from __future__ import annotations

import unittest

from tests.test_agent_first_decision_register_current_state import (
    D22_CUSTOM_TEXT,
    REGISTER_PATH,
)


class AgentFirstDecisionRegisterCurrentDecisionTest(unittest.TestCase):
    def test_d22_records_the_exact_user_confirmed_custom_resolution(self) -> None:
        decision_text = f"确认选择 absolute_plus_baseline：{D22_CUSTOM_TEXT}"
        expected_resolution = {
            "kind": "custom",
            "selected_option_id": "absolute_plus_baseline",
            "custom_text": D22_CUSTOM_TEXT,
            "decision_text": decision_text,
            "authority": "user",
            "decided_at": "2026-07-21",
            "evidence_refs": [
                "conversation:user-confirmation:2026-07-21:D22:absolute_plus_baseline"
            ],
            "resolution_sha256": (
                "94ec57c0b72801dc37d8a7de08b16cc78b8ffc8bdb69b39f0eb0b56cf80d6e96"
            ),
        }
        register = load_register(REGISTER_PATH)
        decision = next(item for item in register["decisions"] if item["id"] == "D22")

        self.assertEqual(4725, len(D22_CUSTOM_TEXT))
        self.assertEqual(6931, len(D22_CUSTOM_TEXT.encode("utf-8")))
        self.assertEqual(4753, len(decision_text))
        self.assertEqual(6969, len(decision_text.encode("utf-8")))
        self.assertEqual("resolved", decision["status"])
        self.assertEqual(expected_resolution, decision["resolution"])
        self.assertEqual([], decision["supersedes"])
if __name__ == "__main__":
    unittest.main()