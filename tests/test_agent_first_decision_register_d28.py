from __future__ import annotations

import unittest
from pathlib import Path

from scripts.agent_first_decision_register import load_register


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
D28_RESOLUTION = {
    "kind": "option",
    "selected_option_id": "logical_bundle_generated_wrappers",
    "custom_text": None,
    "decision_text": "我确认 D28 采用 logical_bundle_generated_wrappers。",
    "authority": "user",
    "decided_at": "2026-07-22",
    "evidence_refs": [
        "conversation:user-confirmation:2026-07-22:D28:logical_bundle_generated_wrappers"
    ],
    "resolution_sha256": (
        "0a9c7e47ab03c92e5d48003ee3d7dc1b5df1cd68031fdd97dda7f85520297204"
    ),
}


class AgentFirstDecisionRegisterD28Test(unittest.TestCase):
    def test_d28_records_the_exact_user_confirmed_option_resolution(self) -> None:
        register = load_register(REGISTER_PATH)
        decision = next(item for item in register["decisions"] if item["id"] == "D28")

        self.assertEqual("resolved", decision["status"])
        self.assertEqual(D28_RESOLUTION, decision["resolution"])
        self.assertEqual([], decision["supersedes"])


if __name__ == "__main__":
    unittest.main()
