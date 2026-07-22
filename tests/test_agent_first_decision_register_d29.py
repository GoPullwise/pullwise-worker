from __future__ import annotations

import unittest
from pathlib import Path

from scripts.agent_first_decision_register import load_register


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
D29_RESOLUTION = {
    "kind": "option",
    "selected_option_id": "layered_atomic_root",
    "custom_text": None,
    "decision_text": "确认 D29 采用 layered_atomic_root",
    "authority": "user",
    "decided_at": "2026-07-22",
    "evidence_refs": [
        "conversation:user-confirmation:2026-07-22:D29:layered_atomic_root"
    ],
    "resolution_sha256": (
        "dfe6c2e4b62226d5e7b155e2b7a51d04c94fd13905834b908e5d1b24f30eb5da"
    ),
}


class AgentFirstDecisionRegisterD29Test(unittest.TestCase):
    def test_d29_records_the_exact_user_confirmed_option_resolution(self) -> None:
        register = load_register(REGISTER_PATH)
        decision = next(item for item in register["decisions"] if item["id"] == "D29")

        self.assertEqual("resolved", decision["status"])
        self.assertEqual(D29_RESOLUTION, decision["resolution"])
        self.assertEqual([], decision["supersedes"])


if __name__ == "__main__":
    unittest.main()
