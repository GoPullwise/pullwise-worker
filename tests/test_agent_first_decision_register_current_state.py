from __future__ import annotations

import unittest
from pathlib import Path

from scripts.agent_first_decision_catalog import QUESTION_ORDER, REQUIRED_CATALOG
from scripts.agent_first_decision_gate import verify_register
from scripts.agent_first_decision_register import load_register


REPO_ROOT = Path(__file__).resolve().parents[1]
REGISTER_PATH = (
    REPO_ROOT / "contracts" / "agent-first" / "spec-decision-register.json"
)
D24_CUSTOM_TEXT = (
    "该 option 采用 D27-compatible 单值特化。`new_tasks_only` 仅表示以 Server "
    "受审计的协调切换屏障为线性化边界：只有 Task acceptance/TaskRecord creation "
    "事务在该屏障生效后、按唯一 current TaskRecord schema 和 current Agent-First "
    "contract 成功提交的任务，才可创建并执行；不采纳原 option 中“所有旧任务走完 v1”以及 "
    "v1 drain/保留期限的语义。屏障生效前必须暂停 intake；所有 pre-cutover Task "
    "必须在屏障生效前完成权威终态或 tombstone/delete 处置，或者撤销执行授权并被 stop、fence "
    "或 reject 后隔离为不可执行状态。stop、fence 或 reject 只撤销 authorization/Attempt "
    "ownership，不得冒充 Task terminalization 或 TaskResult；屏障生效后，任何 pre-cutover "
    "Task 均不得再被 claim、grant、resume、replay、drain、写入、发布或执行，任何迟到的旧 "
    "lease、event、result 或 replay 必须 fail closed。pre-cutover "
    "submission_idempotency_key 的重放不得被重新创建或归类为新任务。不得为 "
    "pre-Agent-First/旧 TaskRecord 到 current contract 实施 lazy migration、batch "
    "backfill、dual read/write、compatibility reader 或运行时 schema/protocol "
    "negotiation；不保留 legacy Adapter/shim、production shadow、legacy fallback、"
    "protocol downgrade、compatibility rollback 或 old/new schema/contract 双轨。D24 "
    "本身不授予旧数据留存例外；只有另行获得明确的审计或合规留存授权时，旧数据才可隔离为与 current "
    "control plane、operational tables/readers 和 DTO projection 分离的 immutable、"
    "read-only、non-executable 审计归档，并且不得成为 current TaskRecord "
    "的输入、授权、恢复或执行来源。任何任务、TaskRecord 或 claim/grant 的 schema/contract "
    "identity/version 缺失、未知、旧版或与唯一 current schema/contract 不匹配时，create、"
    "claim、grant、resume、replay、写入、发布和执行均必须 fail closed。安全回滚仅可回到 "
    "exact-pin 同一 current package identity/version/digest、实现同一 current "
    "TaskRecord schema、storage semantics 和 current Agent-First contract 的先前 "
    "build，不得重新开放旧任务、旧数据形状、旧协议、旧入口或第二生产权威。本决议不禁止同一 current "
    "contract 的 clean initialization/rebuild、current-version upgrade、分批部署，或未来经独立决议"
    "协调切换的 current-contract 演进；这些路径不得引入 pre-Agent-First 兼容层、运行时协商或并行生产轨道。"
)


class AgentFirstDecisionRegisterCurrentStateTest(unittest.TestCase):
    def test_current_register_has_the_user_resolved_decision_prefix(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(register, REPO_ROOT)

        self.assertEqual("valid_pending", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        self.assertEqual([], report["failures"])
        self.assertEqual("D26", report["active_decision_id"])
        self.assertEqual(2, report["pending_decision_count"])
        self.assertEqual(24, report["resolved_decision_count"])
        self.assertEqual(1, report["inactive_decision_count"])
        self.assertEqual(["D2"], report["inactive_decision_ids"])
        self.assertTrue(report["document_matches"])
        expected_resolutions = {
            "D1": ("pullwise_full_scan", "ab117e7c86472b7ce57bf2433978df0efe1299353ad747b7eabbff723fec469a"),
            "D3": ("mvp_r0_r1_reject_r2", "0126d5ee3329c0f954e88e08979e8f0883086b3846315e2904cd7d323b97b07a"),
            "D4": ("field_by_field_ownership", "b009c68af93c965837e562d57cd20328e037b5fca0da30cc694125e0fee79654"),
            "D5": ("per_control_transaction", "859647945022b9d62bca4c6cf16b290c48e4e9bdb2f10700a40553194748b74a"),
            "D6": ("single_claim_owner_transaction", "e1ad16c135ae5f0880123becdd640bf685c0f201b44dd941830590b0b39174d8"),
            "D7": ("persist_elapsed_consumption", "5d7916e9389c0203185fb7e2e64be49df0ea52557d875f661f5d0180e093f5ea"),
            "D8": ("task_active_attempt_fenced", "e895f73c3a0962937cbab61b4c8037f9ccba9daa6e6de89d5004005dd830b98a"),
            "D9": ("internal_result_cas_authoritative", "3e8a5cf9d69cccd50667009c80e9a3176501d3c0150d5bec931ee71fb1cc46ce"),
            "D10": ("global_safety_first_matrix", "1daae4c66d41bd95a3eef8e24756590c8e6f75a05899548dc3126bfd39172e31"),
            "D11": ("partial_delivery_manifest", "dc65778d9f60563e39a9c3262200f8e26efd8c48c29aa0141087793186032a7e"),
            "D12": ("new_generation_supersession", "b459cd0e371c34702e654761aa89caa21238f5c5314020e9d9c7484d60902764"),
            "D13": ("prepublish_cancel_postpublish_reconcile", "4a90df4dce3840e2f726d952fa0b49ef9294e73e851208f968df30642720e5a7"),
            "D14": ("separate_bundle_integrity_manifest", "1798cd24165aa5be17f5e5b256e3ecfd61a2f02e63d064e9f5da60edcf30a889"),
            "D15": ("separate_predicate_registry", "47cf85a523a63a4c26775fe6929bdd132fb37ac82f5cdda41128ee248827cb1b"),
            "D16": ("remove_q0_success_path", "0acca8727c0044d5bc7ef7542e2bc51384c1ca56865889cec8e389b559403130"),
            "D17": ("versioned_concern_table", "8f125e98166a1fa6edacc6ef2e29a1749eb13d5ab5d187d1aab63c38d5cac3a8"),
            "D18": ("coordinator_is_owner", "16fb38386dfedc25cbd4f7d3cc25aeeeb9512b3d0e3733fdb8591441eca3c8de"),
            "D19": ("owner_remains_live", "0fb4d7e749fb873ccb7691ff2a87c30f2792969534311903ce439a5ac86c2796"),
            "D20": ("new_gate_immediate_authority", "3701e29aac3b42c5f88743cc21ea49cafe685d0d2c4b8ab0ec8ff5619dad023a"),
            "D21": ("server_claim_bound_mode", "ddfd221626d5677def6472f59e6fa002c56fd1f6ca6602188ebb7c23735a0282"),
            "D23": ("server_owned_package", "cecd60a0f27d18240d3222eb6aa117dc588b06ba3f9581c83af3d292dd4254e2"),
            "D24": ("new_tasks_only", "8e9b8ee728dabd8e8f07e3b6ce8057a6e3e11707d07bbaf4e5d1e67f7dfc3806"),
            "D25": ("immutable_receipt_mutable_binding", "03564c29030767d552a5759828970f30ed10c11bbd46c42c51f16a08c3e2f2d0"),
            "D27": ("clean_break_no_legacy", "f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e"),
        }
        decisions = {item["id"]: item for item in register["decisions"]}
        for decision_id, expected in expected_resolutions.items():
            with self.subTest(decision_id=decision_id):
                resolution = decisions[decision_id]["resolution"]
                self.assertIsNotNone(resolution)
                self.assertEqual(expected[0], resolution["selected_option_id"])
                self.assertEqual("user", resolution["authority"])
                self.assertEqual(expected[1], resolution["resolution_sha256"])
        custom_resolutions = {
            "D20": ("协调切换后，新 Gate 立即成为唯一生产权威；旧 QA 不作为 hard floor，不保留 production shadow、fallback、downgrade 或双轨共存。", "conversation:user-confirmation:2026-07-21:D20:new_gate_immediate_authority"),
            "D21": (
                "该 option 采用单值特化。协调切换后，生产执行只有唯一 current Agent-First contract；"
                "`legacy-only`、`shadow`、`kernel-authoritative` 不再是可签发、持久化或选择的 mode，"
                "Agent Kernel 权威是该 contract 的固有语义。Server claim/grant 只不可变绑定固定 "
                "contract identity、exact version、job/run scope 与授权，不进行 mode/protocol 协商；"
                "Worker 仅验证并执行该绑定，缺失、未知或不匹配时 fail closed。Worker config、deployment "
                "或单个 job 均不得换轨；不保留 production shadow、legacy fallback、protocol downgrade、"
                "compatibility rollback 或不同协议/权威的双轨部署。安全回滚仅可回到实现同一 current "
                "contract 的先前 build；授权失效时只能 stop、fence 或 reject，不能换轨。",
                "conversation:user-confirmation:2026-07-21:D21:server_claim_bound_mode",
            ),
        }
        for decision_id, (custom_text, evidence_ref) in custom_resolutions.items():
            with self.subTest(custom_decision_id=decision_id):
                resolution = decisions[decision_id]["resolution"]
                self.assertEqual("custom", resolution["kind"])
                self.assertEqual(custom_text, resolution["custom_text"])
                self.assertEqual(f"确认选择 {resolution['selected_option_id']}：{custom_text}", resolution["decision_text"])
                self.assertEqual("2026-07-21", resolution["decided_at"])
                self.assertEqual([evidence_ref], resolution["evidence_refs"])
        expected_order = list(QUESTION_ORDER)
        expected_order.insert(8, "D27")
        self.assertEqual(expected_order, register["question_order"])
        self.assertEqual(
            [*[item["id"] for item in REQUIRED_CATALOG], "D27"],
            [item["id"] for item in register["decisions"]],
        )
        self.assertEqual(["D4"], decisions["D27"]["supersedes"])

    def test_d24_records_the_exact_user_confirmed_custom_resolution(self) -> None:
        register = load_register(REGISTER_PATH)
        decision = next(
            item for item in register["decisions"] if item["id"] == "D24"
        )
        decision_text = f"确认选择 new_tasks_only：{D24_CUSTOM_TEXT}"
        expected_resolution = {
            "kind": "custom",
            "selected_option_id": "new_tasks_only",
            "custom_text": D24_CUSTOM_TEXT,
            "decision_text": decision_text,
            "authority": "user",
            "decided_at": "2026-07-21",
            "evidence_refs": [
                "conversation:user-confirmation:2026-07-21:D24:new_tasks_only"
            ],
            "resolution_sha256": (
                "8e9b8ee728dabd8e8f07e3b6ce8057a6e3e11707d07bbaf4e5d1e67f7dfc3806"
            ),
        }

        self.assertEqual(1630, len(D24_CUSTOM_TEXT))
        self.assertEqual(2546, len(D24_CUSTOM_TEXT.encode("utf-8")))
        self.assertEqual("resolved", decision["status"])
        self.assertEqual([], decision["supersedes"])
        resolution = decision["resolution"]
        self.assertIsNotNone(resolution)
        self.assertEqual("custom", resolution["kind"])
        self.assertEqual("new_tasks_only", resolution["selected_option_id"])
        self.assertEqual(D24_CUSTOM_TEXT, resolution["custom_text"])
        self.assertEqual(decision_text, resolution["decision_text"])
        self.assertEqual("user", resolution["authority"])
        self.assertEqual("2026-07-21", resolution["decided_at"])
        self.assertEqual(expected_resolution["evidence_refs"], resolution["evidence_refs"])
        self.assertEqual(
            expected_resolution["resolution_sha256"],
            resolution["resolution_sha256"],
        )
        self.assertEqual(expected_resolution, resolution)

    def test_d25_records_the_exact_authorized_option_resolution(self) -> None:
        decision_text = (
            "Select immutable_receipt_mutable_binding: 拆分 immutable "
            "upload/transport receipt、mutable Server binding/index，并分离 "
            "TaskResultCore 与 transport envelope digest。 形成无环内容 DAG，同时保留"
            "一次性 Server 绑定。 Constraints: 需要两个 digest、绑定 CAS 和 crash fixtures"
        )
        expected_resolution = {
            "kind": "option",
            "selected_option_id": "immutable_receipt_mutable_binding",
            "custom_text": None,
            "decision_text": decision_text,
            "authority": "user",
            "decided_at": "2026-07-21",
            "evidence_refs": [
                "conversation:user-directive:2026-07-21:all-subsequent-recommended-options"
            ],
            "resolution_sha256": (
                "03564c29030767d552a5759828970f30ed10c11bbd46c42c51f16a08c3e2f2d0"
            ),
        }
        register = load_register(REGISTER_PATH)
        decision = next(
            item for item in register["decisions"] if item["id"] == "D25"
        )

        self.assertEqual(235, len(decision_text))
        self.assertEqual(303, len(decision_text.encode("utf-8")))
        self.assertEqual("resolved", decision["status"])
        self.assertEqual(expected_resolution, decision["resolution"])
        self.assertEqual([], decision["supersedes"])

    def test_pullwise_scope_resolution_unblocks_slice_two(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(
            register, REPO_ROOT, require_slice="S2", check_document=False
        )

        self.assertEqual("valid_pending", report["status"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])
        self.assertEqual([], report["failures"])
        self.assertEqual("D26", report["active_decision_id"])
        self.assertEqual(["D2"], report["inactive_decision_ids"])

    def test_slice_gate_reports_every_due_active_pending_decision(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(
            register, REPO_ROOT, require_slice="S5",
            check_document=False, check_history=False,
        )
        self.assertEqual([], report["failures"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])

        report = verify_register(
            register, REPO_ROOT, require_slice="S6",
            check_document=False, check_history=False,
        )
        blocker = next(item for item in report["failures"]
                       if item["code"] == "slice_blocked_by_pending_decisions")
        self.assertEqual(["D22"], blocker["decision_ids"])
        self.assertTrue(report["valid"])
        self.assertFalse(report["ready"])

        report = verify_register(
            register, REPO_ROOT, require_slice="S7",
            check_document=False, check_history=False,
        )
        blocker = next(item for item in report["failures"]
                       if item["code"] == "slice_blocked_by_pending_decisions")
        self.assertEqual(["D26", "D22"], blocker["decision_ids"])

        report = verify_register(
            register, REPO_ROOT, require_slice="S8",
            check_document=False, check_history=False,
        )
        blocker = next(item for item in report["failures"]
                       if item["code"] == "slice_blocked_by_pending_decisions")
        self.assertEqual(["D26", "D22"], blocker["decision_ids"])


if __name__ == "__main__":
    unittest.main()
