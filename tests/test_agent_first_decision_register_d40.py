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


D40_OPTION_ID = "bounded_s8_offline_candidate_no_generate_no_activation"
D40_DECISION_TEXT = (
    "批准 D40：bounded_s8_offline_candidate_no_generate_no_activation。"
    "D40 显式 supersede D39，仅解除其 stop-before-S8 边界：授权在本地 "
    "Worker/Server/Web 仓库和 CI 内盘点现有 exact-pinned current package 的 S8 "
    "public interfaces，并在用户确认行为计划后按 one-test-at-a-time RED -> GREEN "
    "完成 bounded offline candidate。只允许 deterministic synthetic fixtures、纯离线"
    "验证和 evaluator composition；这些产物不得冒充真实签名 benchmark-bundle/v1、"
    "release-gate-policy/v1、release-gate-report/v1、release-gate-attestation/v1 或"
    "发布证据。Contract source、schema、protocol、package changes 和 Generate count "
    "均保持为零；若现有 exact current package 无法表达必需行为，必须记录新的 "
    "SPEC_GAP 并停止。继续禁止 D24 实现或激活、deployment、修改已部署 Worker、"
    "production traffic、真实 benchmark/evidence signing、canary、cutover、legacy "
    "删除、fallback、dual path、compatibility/downgrade、second authority/store/"
    "runner 和 codegraph；本决议不授权真实 release 或 MVP DoD closure。"
)


class AgentFirstDecisionRegisterD40Test(unittest.TestCase):
    def setUp(self) -> None:
        self.register = load_register(REGISTER_PATH)

    def test_d40_records_the_exact_user_approved_offline_s8_boundary(self) -> None:
        decisions = {item["id"]: item for item in self.register["decisions"]}
        self.assertIn("D40", decisions)
        decision = decisions["D40"]

        self.assertEqual(["D40", "D41"], self.register["question_order"][-2:])
        self.assertEqual(
            ["D40", "D41"],
            [item["id"] for item in self.register["decisions"][-2:]],
        )
        self.assertIsNone(self.register["active_decision_id"])
        self.assertEqual("resolved", decision["status"])
        self.assertEqual(["D39"], decision["supersedes"])
        self.assertEqual("S8", decision["required_by_slice"])
        self.assertEqual(
            [
                "mvp-contract-pack",
                "mvp-state-semantics",
                "mvp-executable-gates",
                "post-closure",
            ],
            decision["affected_units"],
        )
        self.assertEqual(D40_OPTION_ID, decision["recommended_option_id"])

        resolution = decision["resolution"]
        self.assertEqual("option", resolution["kind"])
        self.assertEqual(D40_OPTION_ID, resolution["selected_option_id"])
        self.assertIsNone(resolution["custom_text"])
        self.assertEqual(D40_DECISION_TEXT, resolution["decision_text"])
        self.assertEqual("user", resolution["authority"])
        self.assertEqual("2026-08-04", resolution["decided_at"])
        self.assertEqual(
            [f"conversation:user-approval:2026-08-04:{D40_OPTION_ID}"],
            resolution["evidence_refs"],
        )
        self.assertEqual(
            canonical_resolution_sha256(
                "D40", resolution, decision["supersedes"]
            ),
            resolution["resolution_sha256"],
        )

        report = verify_register(
            self.register,
            REPO_ROOT,
            require_slice="S8",
            check_document=False,
            check_history=False,
        )
        self.assertEqual("ready", report["status"])
        self.assertTrue(report["valid"])
        self.assertTrue(report["ready"])
        self.assertIsNone(report["active_decision_id"])
        self.assertEqual([], report["failures"])


if __name__ == "__main__":
    unittest.main()
