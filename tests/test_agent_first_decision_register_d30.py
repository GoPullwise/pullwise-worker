from __future__ import annotations

import unittest
from pathlib import Path

from scripts.agent_first_decision_register import load_register


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
D30_RESOLUTION = {
    "kind": "option",
    "selected_option_id": "worker_journal_server_authority",
    "custom_text": None,
    "decision_text": "推荐 worker_journal_server_authority   按照推荐来",
    "authority": "user",
    "decided_at": "2026-07-22",
    "evidence_refs": [
        "conversation:user-confirmation:2026-07-22:D30:worker_journal_server_authority"
    ],
    "resolution_sha256": (
        "4ab2e27ff93ea323673ccd36b0d4da41d3bd0e616160248660c3a274a59d44bf"
    ),
}


class AgentFirstDecisionRegisterD30Test(unittest.TestCase):
    def test_d30_records_the_exact_user_confirmed_option_resolution(self) -> None:
        register = load_register(REGISTER_PATH)
        decision = next(item for item in register["decisions"] if item["id"] == "D30")

        self.assertEqual("resolved", decision["status"])
        self.assertEqual(D30_RESOLUTION, decision["resolution"])
        self.assertEqual([], decision["supersedes"])


if __name__ == "__main__":
    unittest.main()
