from __future__ import annotations

import unittest

from scripts.agent_first_decision_register import load_register, verify_register
from tests.test_agent_first_decision_register_current_state import (
    REGISTER_PATH,
    REPO_ROOT,
)


class AgentFirstDecisionRegisterD37Test(unittest.TestCase):
    def setUp(self) -> None:
        self.register = load_register(REGISTER_PATH)
        self.decision = next(
            item for item in self.register["decisions"] if item["id"] == "D37"
        )

    def test_d37_is_the_active_append_only_s4_contract_gap(self) -> None:
        self.assertEqual("D37", self.register["question_order"][-1])
        self.assertEqual("D37", self.register["decisions"][-1]["id"])
        self.assertEqual("D37", self.register["active_decision_id"])
        self.assertEqual("pending", self.decision["status"])
        self.assertIsNone(self.decision["resolution"])
        self.assertEqual([], self.decision["supersedes"])
        self.assertEqual("S4", self.decision["required_by_slice"])
        self.assertEqual(
            ["D6", "D7", "D8", "D23", "D28", "D29", "D30", "D31", "D32", "D36"],
            self.decision["depends_on"],
        )
        self.assertEqual(
            [
                "mvp-contract-pack",
                "mvp-state-semantics",
                "mvp-executable-gates",
                "post-closure",
            ],
            self.decision["affected_units"],
        )
        self.assertEqual(
            "bounded_s4_contract_closure_one_generate_no_activation",
            self.decision["recommended_option_id"],
        )

    def test_recommendation_names_every_observed_gap_and_preserves_boundaries(
        self,
    ) -> None:
        selected = next(
            option
            for option in self.decision["options"]
            if option["id"] == self.decision["recommended_option_id"]
        )
        text = " ".join(
            (
                self.decision["question"],
                selected["summary"],
                selected["rationale"],
                *selected["consequences"],
            )
        )
        for invariant in (
            "agent-task-accept-request/v1",
            "agent-task-runtime-bootstrap/v1",
            "TaskRequest",
            "EffectiveExecutionPolicy",
            "RequirementLedger",
            "outer_job_id",
            "run_id",
            "server-authority-envelope/v1",
            "machine checkpoint",
            "semantic checkpoint",
            "committed-checkpoint-manifest/v1",
            "exactly one Generate",
            "Server/Worker/Web exact pins",
            "D24",
            "deployment",
            "production traffic",
            "canary",
            "legacy",
            "second runner",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, text)

    def test_d37_blocks_s4_and_later_but_not_the_completed_s3_scope(self) -> None:
        s3 = verify_register(
            self.register,
            REPO_ROOT,
            require_slice="S3",
            check_document=False,
            check_history=False,
        )
        self.assertEqual("valid_pending", s3["status"])
        self.assertTrue(s3["valid"])
        self.assertFalse(s3["ready"])
        self.assertEqual([], s3["failures"])

        for slice_id in ("S4", "S5", "S6", "S7", "S8"):
            with self.subTest(slice_id=slice_id):
                report = verify_register(
                    self.register,
                    REPO_ROOT,
                    require_slice=slice_id,
                    check_document=False,
                    check_history=False,
                )
                self.assertEqual("blocked", report["status"])
                self.assertTrue(report["valid"])
                self.assertFalse(report["ready"])
                self.assertEqual("D37", report["active_decision_id"])
                self.assertEqual(
                    [
                        {
                            "code": "slice_blocked_by_pending_decisions",
                            "slice": slice_id,
                            "decision_ids": ["D37"],
                        }
                    ],
                    report["failures"],
                )


if __name__ == "__main__":
    unittest.main()
