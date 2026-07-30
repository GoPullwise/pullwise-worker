from __future__ import annotations

import unittest
from pathlib import Path

from scripts.agent_first_decision_register import load_register, verify_register


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = (
    REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
)


class AgentFirstDecisionRegisterD36Test(unittest.TestCase):
    def setUp(self) -> None:
        self.register = load_register(REGISTER_PATH)
        self.decision = next(
            item for item in self.register["decisions"] if item["id"] == "D36"
        )

    def test_d36_is_the_pending_append_only_implementation_boundary(self) -> None:
        self.assertEqual("D36", self.register["question_order"][-1])
        self.assertEqual("D36", self.register["decisions"][-1]["id"])
        self.assertEqual("D36", self.register["active_decision_id"])
        self.assertEqual(
            "mvp-s3-s7-implementation-authorization", self.decision["key"]
        )
        self.assertEqual("pending", self.decision["status"])
        self.assertIsNone(self.decision["resolution"])
        self.assertEqual([], self.decision["supersedes"])
        self.assertEqual("S3", self.decision["required_by_slice"])
        self.assertEqual(["D20", "D21", "D23", "D27", "D30", "D35"], self.decision["depends_on"])
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
            "mvp_s3_s7_implementation_only_no_external_activation",
            self.decision["recommended_option_id"],
        )

    def test_d36_blocks_s3_through_s8_until_explicit_resolution(self) -> None:
        for required_slice in ("S3", "S4", "S5", "S6", "S7", "S8"):
            with self.subTest(required_slice=required_slice):
                report = verify_register(
                    self.register,
                    REPO_ROOT,
                    require_slice=required_slice,
                )
                self.assertEqual("blocked", report["status"])
                self.assertTrue(report["valid"])
                self.assertFalse(report["ready"])
                self.assertEqual("D36", report["active_decision_id"])
                self.assertEqual(
                    [
                        {
                            "code": "slice_blocked_by_pending_decisions",
                            "slice": required_slice,
                            "decision_ids": ["D36"],
                        }
                    ],
                    report["failures"],
                )

    def test_recommended_option_separates_implementation_from_activation(self) -> None:
        selected = next(
            option
            for option in self.decision["options"]
            if option["id"] == self.decision["recommended_option_id"]
        )
        text = " ".join(
            [
                self.decision["question"],
                selected["summary"],
                selected["rationale"],
                *selected["consequences"],
            ]
        )

        for invariant in (
            "supersedes D35",
            "Server current-task/operator HTTP/Auth",
            "Worker main loop",
            "S3–S7 once",
            "S3–S4 current-runtime tracer bullet",
            "Gate/TaskResult",
            "current transport",
            "WorkerDebugFragment",
            "D24 implementation and enablement",
            "deployment",
            "production traffic",
            "canary",
            "legacy deletion",
            "S8 release/cutover/rollback",
            "Contract source changes and Generate count are both zero",
            "Server/Worker/Web ownership",
            "stop-condition reporting",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, text)


if __name__ == "__main__":
    unittest.main()
