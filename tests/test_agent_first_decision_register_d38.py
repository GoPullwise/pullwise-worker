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


D38_OPTION_ID = (
    "bounded_s5_terminal_control_and_selector_closure_one_generate_no_activation"
)
D38_DECISION_TEXT = (
    "批准 D38："
    "bounded_s5_terminal_control_and_selector_closure_one_generate_no_activation。"
    "授权先以 RED tests 定义并完成 bounded S5 contract closure：把 passed Success "
    "Gate 版本化桥接到唯一 canonical mechanical terminal selector，success 分支不得"
    "要求或伪造 terminalization fact；selector 仍只从 profile、gate_mode、"
    "cancel_state、effect_state、cause_family、delivery_state 六轴机械派生 "
    "lifecycle/outcome/reason/digest，caller 不得选择；以 immutable Server "
    "grant/authority 和本地 checkpoint/control-event Task version chain 证明单一权威，"
    "不新增第二 authority/store/runner；实现真实 FINALIZING -> TERMINAL TaskResult "
    "CAS，在同一闭包绑定 published_from_version=N、terminal_task_version=N+1、"
    "transport/result/version/fence。仅在 source、golden/negative/idempotency/fence/"
    "crash fixtures、semantic closure、DAG、registry、digest、Python/Node parity "
    "全部预生成门绿色后执行且仅执行一次 Generate；随后同步 Server/Worker/Web exact "
    "pins 并继续 D36 的本地 S5-S7 candidate。D38 仅 supersede D37 的已消费 Generate "
    "边界以覆盖上述 S5 closure；继续禁止 D24 实现或启用、deployment、修改已部署 "
    "Worker、production traffic、canary、legacy 删除、S8 release/cutover/rollback、"
    "fallback、dual path、compatibility/downgrade shim、second runner/store/"
    "production authority。"
)


class AgentFirstDecisionRegisterD38Test(unittest.TestCase):
    def setUp(self) -> None:
        self.register = load_register(REGISTER_PATH)

    def _decision(self) -> dict[str, object]:
        decisions = {
            item["id"]: item for item in self.register["decisions"]
        }
        self.assertIn("D38", decisions)
        return decisions["D38"]

    def test_d38_records_the_exact_user_approved_bounded_s5_closure(self) -> None:
        decision = self._decision()
        self.assertEqual("D38", self.register["question_order"][-1])
        self.assertEqual("D38", self.register["decisions"][-1]["id"])
        self.assertIsNone(self.register["active_decision_id"])
        self.assertEqual("resolved", decision["status"])
        self.assertEqual(["D37"], decision["supersedes"])
        self.assertEqual("S5", decision["required_by_slice"])
        self.assertEqual(
            [
                "D5",
                "D8",
                "D9",
                "D10",
                "D13",
                "D20",
                "D23",
                "D25",
                "D28",
                "D29",
                "D30",
                "D31",
                "D33",
                "D36",
                "D37",
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
        self.assertEqual(D38_OPTION_ID, decision["recommended_option_id"])
        resolution = decision["resolution"]
        self.assertEqual("option", resolution["kind"])
        self.assertEqual(D38_OPTION_ID, resolution["selected_option_id"])
        self.assertIsNone(resolution["custom_text"])
        self.assertEqual(D38_DECISION_TEXT, resolution["decision_text"])
        self.assertEqual("user", resolution["authority"])
        self.assertEqual("2026-07-31", resolution["decided_at"])
        self.assertEqual(
            [
                "conversation:user-approval:2026-07-31:"
                f"{D38_OPTION_ID}"
            ],
            resolution["evidence_refs"],
        )
        self.assertEqual(
            canonical_resolution_sha256(
                "D38", resolution, decision["supersedes"]
            ),
            resolution["resolution_sha256"],
        )

    def test_d38_boundary_closes_every_observed_gap_without_activation(self) -> None:
        decision = self._decision()
        selected = next(
            option
            for option in decision["options"]
            if option["id"] == D38_OPTION_ID
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
            "passed Success Gate",
            "canonical mechanical terminal selector",
            "terminalization fact",
            "profile",
            "gate_mode",
            "cancel_state",
            "effect_state",
            "cause_family",
            "delivery_state",
            "immutable Server grant/authority",
            "checkpoint/control-event Task version chain",
            "single authority",
            "FINALIZING -> TERMINAL",
            "published_from_version=N",
            "terminal_task_version=N+1",
            "transport/result/version/fence",
            "exactly one Generate",
            "Server/Worker/Web exact pins",
            "D24",
            "deployment",
            "production traffic",
            "canary",
            "legacy",
            "S8",
            "fallback",
            "dual path",
            "second runner/store/production authority",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, text)

    def test_resolved_d38_keeps_s5_through_s8_decision_gates_ready(self) -> None:
        for slice_id in ("S5", "S6", "S7", "S8"):
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
