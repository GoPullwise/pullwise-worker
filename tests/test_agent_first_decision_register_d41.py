from __future__ import annotations

import unittest

from scripts.agent_first_decision_register import load_register, verify_register
from tests.test_agent_first_decision_register_current_state import (
    REGISTER_PATH,
    REPO_ROOT,
)


D41_OPTION_ID = "bounded_s8_raw_evidence_contract_one_generate_no_activation"


class AgentFirstDecisionRegisterD41Test(unittest.TestCase):
    def setUp(self) -> None:
        self.register = load_register(REGISTER_PATH)

    def test_d41_records_the_proven_s8_raw_evidence_spec_gap(self) -> None:
        decisions = {item["id"]: item for item in self.register["decisions"]}
        self.assertIn("D41", decisions)
        decision = decisions["D41"]

        self.assertEqual("D41", self.register["question_order"][-1])
        self.assertEqual("D41", self.register["decisions"][-1]["id"])
        self.assertEqual("D41", self.register["active_decision_id"])
        self.assertEqual("pending", decision["status"])
        self.assertIsNone(decision["resolution"])
        self.assertEqual([], decision["supersedes"])
        self.assertEqual("S8", decision["required_by_slice"])
        self.assertEqual(["D22", "D23", "D28", "D29", "D40"], decision["depends_on"])
        self.assertEqual(
            [
                "mvp-contract-pack",
                "mvp-state-semantics",
                "mvp-executable-gates",
                "post-closure",
            ],
            decision["affected_units"],
        )
        self.assertEqual(D41_OPTION_ID, decision["recommended_option_id"])

        selected = next(
            option for option in decision["options"] if option["id"] == D41_OPTION_ID
        )
        text = " ".join(
            (
                decision["question"],
                selected["summary"],
                selected["rationale"],
                *selected["consequences"],
            )
        )
        for invariant in (
            "release-gate-report/v1",
            "raw samples",
            "excluded samples",
            "Wilson",
            "ContentRef",
            "exactly one Generate",
            "Server/Worker/Web exact pins",
            "local/offline report-builder TDD",
            "D24",
            "deployment",
            "production traffic",
            "real signing",
            "canary",
            "fallback",
            "second authority/store/runner",
            "codegraph",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, text)

    def test_pending_d41_blocks_s8_without_granting_contract_authority(self) -> None:
        report = verify_register(
            self.register,
            REPO_ROOT,
            require_slice="S8",
            check_document=False,
            check_history=False,
        )
        self.assertEqual("blocked", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        self.assertEqual("D41", report["active_decision_id"])
        self.assertEqual(1, report["pending_decision_count"])
        self.assertEqual(
            [
                {
                    "code": "slice_blocked_by_pending_decisions",
                    "slice": "S8",
                    "decision_ids": ["D41"],
                }
            ],
            report["failures"],
        )


if __name__ == "__main__":
    unittest.main()
