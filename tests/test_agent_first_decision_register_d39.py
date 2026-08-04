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


D39_OPTION_ID = (
    "bounded_s7_transport_attempt_binding_one_generate_no_activation"
)
D39_DECISION_TEXT = (
    "批准 D39："
    "bounded_s7_transport_attempt_binding_one_generate_no_activation。"
    "授权先以 RED tests 定义并完成 bounded S7 transport-attempt authority closure："
    "将 exact Server-owned outer transport_attempt_id 加入 authenticated "
    "agent-task-runtime-bootstrap/v1.transport_binding，并仅在 proven single-authority "
    "chain 所需时同步 claim/accept authority source；完整绑定 valid/invalid/golden/"
    "idempotency/fence/crash fixtures、semantic closure、registry/DAG/digest 以及 "
    "Python/Node parity；严格区分 outer transport_attempt_id 与 Worker "
    "native_attempt_id，禁止 fabrication、derivation/substitution 或第二 authority/"
    "store/runner。仅在全部预生成门绿色后执行且仅执行一次 Generate；立即同步 "
    "Server/Worker/Web exact pins 与 producer provenance，然后恢复 D36 的本地 S7 "
    "TDD，完成 WorkerDebugFragment candidate 并停止在 S8 前。D39 仅 supersede "
    "D38 的已消费 Generate 边界以覆盖上述 S7 closure；继续禁止 D24 实现或启用、"
    "activation、deployment、修改已部署 Worker、production traffic、canary、legacy "
    "删除、S8、fallback、dual path、compatibility/downgrade、second authority/store/"
    "runner 和 codegraph。"
)


class AgentFirstDecisionRegisterD39Test(unittest.TestCase):
    def setUp(self) -> None:
        self.register = load_register(REGISTER_PATH)

    def _decision(self) -> dict[str, object]:
        decisions = {item["id"]: item for item in self.register["decisions"]}
        self.assertIn("D39", decisions)
        return decisions["D39"]

    def test_d39_records_the_exact_user_approved_s7_binding(self) -> None:
        decision = self._decision()
        self.assertEqual(
            ["D39", "D40", "D41"], self.register["question_order"][-3:]
        )
        self.assertEqual(
            ["D39", "D40", "D41"],
            [item["id"] for item in self.register["decisions"][-3:]],
        )
        self.assertIsNone(self.register["active_decision_id"])
        self.assertEqual("resolved", decision["status"])
        self.assertEqual(["D38"], decision["supersedes"])
        self.assertEqual("S7", decision["required_by_slice"])
        self.assertEqual(
            [
                "D8",
                "D9",
                "D23",
                "D25",
                "D28",
                "D29",
                "D30",
                "D31",
                "D34",
                "D36",
                "D37",
                "D38",
            ],
            decision["depends_on"],
        )
        self.assertEqual(
            [
                "mvp-contract-pack",
                "mvp-state-semantics",
                "mvp-executable-gates",
                "post-closure",
            ],
            decision["affected_units"],
        )
        self.assertEqual(D39_OPTION_ID, decision["recommended_option_id"])
        resolution = decision["resolution"]
        self.assertEqual("option", resolution["kind"])
        self.assertEqual(D39_OPTION_ID, resolution["selected_option_id"])
        self.assertIsNone(resolution["custom_text"])
        self.assertEqual(D39_DECISION_TEXT, resolution["decision_text"])
        self.assertEqual("user", resolution["authority"])
        self.assertEqual("2026-08-03", resolution["decided_at"])
        self.assertEqual(
            [f"conversation:user-approval:2026-08-03:{D39_OPTION_ID}"],
            resolution["evidence_refs"],
        )
        self.assertEqual(
            canonical_resolution_sha256(
                "D39", resolution, decision["supersedes"]
            ),
            resolution["resolution_sha256"],
        )

    def test_d39_closes_only_the_bounded_s7_gap(self) -> None:
        decision = self._decision()
        selected = next(
            option
            for option in decision["options"]
            if option["id"] == D39_OPTION_ID
        )
        text = " ".join(
            (
                decision["question"],
                selected["summary"],
                selected["rationale"],
                *selected["consequences"],
                decision["resolution"]["decision_text"],
            )
        )
        for invariant in (
            "Server-owned outer transport_attempt_id",
            "agent-task-runtime-bootstrap/v1.transport_binding",
            "single-authority chain",
            "valid/invalid/golden/idempotency/fence/crash fixtures",
            "semantic closure",
            "registry/DAG/digest",
            "Python/Node parity",
            "native_attempt_id",
            "exactly one Generate",
            "Server/Worker/Web exact pins",
            "producer provenance",
            "local S7",
            "stop before S8",
            "no activation",
            "D24",
            "deployment",
            "production traffic",
            "canary",
            "legacy",
            "fallback",
            "dual path",
            "compatibility/downgrade",
            "second authority/store/runner",
            "codegraph",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, text)

    def test_resolved_d39_keeps_s7_ready_after_d41_resolution(self) -> None:
        report = verify_register(
            self.register,
            REPO_ROOT,
            require_slice="S7",
            check_history=False,
        )
        self.assertEqual("ready", report["status"])
        self.assertTrue(report["valid"])
        self.assertTrue(report["ready"])
        self.assertIsNone(report["active_decision_id"])
        self.assertEqual([], report["failures"])


if __name__ == "__main__":
    unittest.main()
