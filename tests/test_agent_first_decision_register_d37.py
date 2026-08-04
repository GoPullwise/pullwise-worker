from __future__ import annotations

import unittest

from scripts.agent_first_decision_register import (
    canonical_resolution_sha256,
    load_register,
    verify_register,
)
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

    def test_d37_records_the_user_approved_bounded_contract_closure(self) -> None:
        self.assertEqual(
            ["D37", "D38", "D39", "D40", "D41"],
            self.register["question_order"][-5:],
        )
        self.assertEqual(
            ["D37", "D38", "D39", "D40", "D41"],
            [item["id"] for item in self.register["decisions"][-5:]],
        )
        self.assertIsNone(self.register["active_decision_id"])
        self.assertEqual("resolved", self.decision["status"])
        self.assertEqual(["D36"], self.decision["supersedes"])
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
        resolution = self.decision["resolution"]
        self.assertEqual("option", resolution["kind"])
        self.assertEqual(
            "bounded_s4_contract_closure_one_generate_no_activation",
            resolution["selected_option_id"],
        )
        self.assertIsNone(resolution["custom_text"])
        self.assertEqual("user", resolution["authority"])
        self.assertEqual("2026-07-30", resolution["decided_at"])
        self.assertEqual(
            [
                "conversation:user-approval:2026-07-30:"
                "bounded_s4_contract_closure_one_generate_no_activation"
            ],
            resolution["evidence_refs"],
        )
        self.assertEqual(
            canonical_resolution_sha256(
                "D37", resolution, self.decision["supersedes"]
            ),
            resolution["resolution_sha256"],
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

    def test_resolved_d37_remains_valid_with_resolved_d41(self) -> None:
        for slice_id in ("S3", "S4", "S5", "S6", "S7", "S8"):
            with self.subTest(slice_id=slice_id):
                report = verify_register(
                    self.register,
                    REPO_ROOT,
                    require_slice=slice_id,
                    check_history=False,
                )
                self.assertEqual("ready", report["status"])
                self.assertTrue(report["valid"])
                self.assertTrue(report["ready"])
                self.assertIsNone(report["active_decision_id"])
                self.assertEqual([], report["failures"])


if __name__ == "__main__":
    unittest.main()
