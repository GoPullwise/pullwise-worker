from __future__ import annotations

import unittest
from pathlib import Path

from scripts.agent_first_decision_register import (
    canonical_resolution_sha256,
    load_register,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = (
    REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
)


class AgentFirstDecisionRegisterD35Test(unittest.TestCase):
    def setUp(self) -> None:
        self.register = load_register(REGISTER_PATH)
        self.decision = next(
            item for item in self.register["decisions"] if item["id"] == "D35"
        )

    def test_d35_is_an_append_only_resolved_supersession_of_d34(self) -> None:
        self.assertEqual(["D35", "D36"], self.register["question_order"][-2:])
        self.assertEqual(
            ["D35", "D36"],
            [item["id"] for item in self.register["decisions"][-2:]],
        )
        self.assertEqual("current-candidate-replacement-generate", self.decision["key"])
        self.assertEqual("resolved", self.decision["status"])
        self.assertEqual(["D34"], self.decision["supersedes"])
        self.assertEqual("S7", self.decision["required_by_slice"])
        self.assertEqual(
            ["mvp-executable-gates", "post-closure"],
            self.decision["affected_units"],
        )

        resolution = self.decision["resolution"]
        self.assertEqual("option", resolution["kind"])
        self.assertEqual(
            "replacement_generate_candidate_only_no_activation",
            resolution["selected_option_id"],
        )
        self.assertIsNone(resolution["custom_text"])
        self.assertEqual("user", resolution["authority"])
        self.assertEqual("2026-07-29", resolution["decided_at"])
        self.assertEqual(
            [
                "conversation:user-approval:2026-07-29:"
                "replacement_generate_candidate_only_no_activation"
            ],
            resolution["evidence_refs"],
        )
        self.assertEqual(
            canonical_resolution_sha256(
                "D35", resolution, self.decision["supersedes"]
            ),
            resolution["resolution_sha256"],
        )

    def test_d35_authorizes_one_replacement_without_activation(self) -> None:
        selected = next(
            option
            for option in self.decision["options"]
            if option["id"] == self.decision["resolution"]["selected_option_id"]
        )
        text = " ".join(
            [
                selected["summary"],
                selected["rationale"],
                *selected["consequences"],
                self.decision["resolution"]["decision_text"],
            ]
        )

        for invariant in (
            "exactly one replacement Generate",
            "Server/Worker/Web exact pins",
            "production HTTP/auth",
            "production Worker loop",
            "D24",
            "deployment",
            "canary",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, text)


if __name__ == "__main__":
    unittest.main()
