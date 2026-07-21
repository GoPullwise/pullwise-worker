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
D22_CUSTOM_TEXT = (
    "该 option 采用 D27-compatible 单值特化。`absolute_plus_baseline` 只以唯一 current Agent-First contract 的候选版本和已接受 stable build 为对象；legacy-v1 compatibility baseline、旧 QA、pre-cutover 任务或任何并行旧权威均不得成为 release baseline、hard floor、fallback 或 rollback target。"
    "benchmark owner 必须在揭示候选结果前签发 canonical `benchmark-bundle/v1`，release operator 必须在同一时点前签发 canonical `release-gate-policy/v1`；CI/eval owner 在评测完成后生成 canonical `release-gate-report/v1`，release operator 核验后再签发 `release-gate-attestation/v1`。"
    "policy、report 与 attestation 必须 exact-bind current contract package identity/version/digest、candidate 与 stable build identity、ControlPlaneDigest、EvaluationRuntimeDigest、CandidateDigest、benchmark_version、task inventory digest、hidden-oracle/rubric digest、environment/image digest、预声明抽样 seed、每 task 重复次数、统计实现版本、absolute/relative threshold 表、canary plan、policy/report digest、signer_id/key_id、issued_at/expires_at。"
    "签名固定为 organization trust registry 中未撤销 release key 对 RFC 8785 JCS bytes 的 Ed25519 signature；benchmark owner、CI evidence producer 与最终 release operator 必须是不同 principal。"
    "policy 最长有效 30 天，attestation 最长有效 7 天；key 撤销立即失效。"
    "missing、过期、撤销、签名无效、scope/digest 不匹配或证据不新鲜均为 indeterminate，并阻断发布。"
    "离线 benchmark 至少包含 120 个 known-gold tasks、3 个互不相同的 sealed unknown-stack families 且每 family 至少 15 tasks；每个适用核心评测簇对 real-fix、bad/incomplete patch、fake-success/zero-test、environment/capability failure、adversarial input 五类各至少 3 tasks，并合计包含至少 50 个 oracle-positive in-scope findings。"
    "每 task 使用预声明的 3 个不同 seed 独立运行 3 次；每个有效 run 等权进入预声明分母，不允许看到结果后改变权重。"
    "只有 policy 预列的 infrastructure reason code 可排除 invalid run，并必须报告原始样本、排除样本和逐项 reason；零分母、样本不足、oracle/rubric 冲突、超时、证据缺失或 evaluator failure 均为 indeterminate，禁止通过追加运行、重采样或更换 baseline 追到通过。"
    "比例使用冻结实现计算，false-verified 的 95% 上界固定使用 Wilson score interval。"
    "绝对门必须全部满足：安全越权、stale publish、duplicate effect/result 和 critical/adversarial false verified 均为 0；所有 `COMPLETED` 的 active mandatory Requirement Ledger 与 final SourceState proof 覆盖率均为 100%；总体 false_verified_rate 点估计小于 1% 且 95% Wilson 上界小于 2%；known/unknown task_success_rate 分别不低于 70%/50%；known/unknown unaided completion 分别不低于 60%/40%；false_discovery_rate 不高于 20%；environment/capability classification_accuracy 不低于 95%。"
    "相对已接受 stable baseline：总体 false verified 不得恶化；known/unknown task success、unaided completion 与 classification accuracy 的下降分别不得超过 2 percentage points；false discovery 增加不得超过 2 percentage points；verified-success p95 wall time 与 p95 cost 增加均不得超过 20%。"
    "每个 task profile 还必须在 policy 中签发正数、有限、无 wildcard 的 wall/token/cost 绝对上限，任一超限即失败。"
    "zero-tolerance 和 absolute safety gate 不可 waiver；其他阈值放宽、benchmark 难度重定版、统计算法或分母变更必须在候选结果揭示前取得新的独立决议或 ADR，不能用当次 operator exception 绕过。"
    "baseline 只能从通过全部 offline 门和后续 canary 的 candidate 晋升；release operator 签发 immutable baseline record 后，它才可成为下一版本比较对象，不得自动刷新、事后选择最有利 baseline 或把失败 candidate 写成 baseline。"
    "relative comparison 必须使用 exact stable package/ControlPlaneDigest，并与 candidate 使用相同 benchmark_version、task/oracle inventory、统计算法及 EvaluationRuntimeDigest；若 runtime/model 发生变化或供应方不提供 immutable model snapshot，必须在同一 72 小时窗口内以 candidate runtime 交错重跑 exact stable build，并把独立 comparison report digest 绑定进 attestation。"
    "任何不可比状态均为 indeterminate。"
    "首次 current-contract 发布没有 stable baseline 时，只允许 policy 显式标记 bootstrap：全部绝对门仍须通过，相对门记为 not_applicable，candidate 只有在 canary 完成后才能晋升为首个 baseline。"
    "旧 baseline 保留审计但撤销后不得用于新发布。"
    "CI evaluator 只允许三态：exit 0 表示全部适用门通过，exit 1 表示确定失败，exit 2 表示 indeterminate；只有 exit 0 的 exact report 可以签发 attestation。"
    "benchmark owner 负责冻结数据集与 oracle，CI/eval owner 负责产生可复算报告但无 promote 权，release operator 负责冻结 policy/baseline、核验报告与签发 promote，deployment operator 只能执行已签发的 canary/rollback plan且不得改写门值。"
    "D24 Task acceptance/TaskRecord creation barrier 只能在同一 exact package/CandidateDigest 的 offline attestation 为 exit 0，并完成 exact current-contract rollback 演练；bootstrap 没有 stable build 时必须改为 stop-intake/fence/reject 演练，绝不能回 legacy。"
    "barrier 生效后所有 accepted Task 都必须使用唯一 current contract；canary 只能限制 current-contract intake/capacity，剩余 intake 必须暂停或留在 Server current control plane，不得发往旧 contract。"
    "canary 先运行 5% target capacity（至少一台 Worker）且同时满足至少 24 小时和 200 个 accepted current Tasks，再运行 25% capacity 且同时满足至少 72 小时和 1000 个 accepted current Tasks，之后才可扩至 full capacity。"
    "canary platform_failure_rate 的分母是窗口内已终态或 deadline 已到的 accepted current Tasks，分子是以 `RUNTIME_FAILURE`、`STORAGE_FAILURE`、`PROTOCOL_FAILURE` 终止或 deadline 后仍无合法终态的 Task；该率必须低于 2%，且在已有 stable baseline 时增加不得超过 2 percentage points。"
    "任一 zero-tolerance 事件、platform failure 门失败或 p95 wall time/cost 增加超过 20% 都必须自动停止扩容并回到 exact-pin、实现同一 current contract package/schema/storage semantics 的已签发 stable build；样本或窗口不足不得晋级。"
    "若没有这样的 stable build，只能停止 intake、fence 或 reject，不能回到 legacy。"
    "D24 barrier 后不得重新开放旧任务、旧 schema、旧协议、旧 QA、legacy baseline 或第二生产轨道。"
    "未来 roadmap 每版必须另写完整 implementation design，并取得独立 release-gate 决议。"
)


class AgentFirstDecisionRegisterCurrentStateTest(unittest.TestCase):
    def test_current_register_has_the_user_resolved_decision_prefix(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(register, REPO_ROOT)

        self.assertEqual("ready", report["status"])
        self.assertTrue(report["valid"])
        self.assertTrue(report["ready"])
        self.assertEqual([], report["failures"])
        self.assertIsNone(report["active_decision_id"])
        self.assertEqual(0, report["pending_decision_count"])
        self.assertEqual(26, report["resolved_decision_count"])
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
            "D22": ("absolute_plus_baseline", "94ec57c0b72801dc37d8a7de08b16cc78b8ffc8bdb69b39f0eb0b56cf80d6e96"),
            "D23": ("server_owned_package", "cecd60a0f27d18240d3222eb6aa117dc588b06ba3f9581c83af3d292dd4254e2"),
            "D24": ("new_tasks_only", "8e9b8ee728dabd8e8f07e3b6ce8057a6e3e11707d07bbaf4e5d1e67f7dfc3806"),
            "D25": ("immutable_receipt_mutable_binding", "03564c29030767d552a5759828970f30ed10c11bbd46c42c51f16a08c3e2f2d0"),
            "D26": ("roadmap_separate_designs", "ce8a907836b3b8209f12f7c48f66878e9534d7cac667532c2899f3d74c86602f"),
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

    def test_d26_records_the_exact_authorized_option_resolution(self) -> None:
        decision_text = (
            "Select roadmap_separate_designs: 把未闭合远期版本明确标为 roadmap；"
            "每版开工前另写完整 implementation design。 诚实区分路线图与可执行规格，"
            "不让远期债务阻塞已闭合 MVP。 Constraints: 不得再宣称当前 Post 文档完整实现所有版本"
        )
        expected_resolution = {
            "kind": "option",
            "selected_option_id": "roadmap_separate_designs",
            "custom_text": None,
            "decision_text": decision_text,
            "authority": "user",
            "decided_at": "2026-07-21",
            "evidence_refs": [
                "conversation:user-directive:2026-07-21:all-subsequent-recommended-options"
            ],
            "resolution_sha256": (
                "ce8a907836b3b8209f12f7c48f66878e9534d7cac667532c2899f3d74c86602f"
            ),
        }
        register = load_register(REGISTER_PATH)
        decision = next(
            item for item in register["decisions"] if item["id"] == "D26"
        )

        self.assertEqual(154, len(decision_text))
        self.assertEqual(286, len(decision_text.encode("utf-8")))
        self.assertEqual("resolved", decision["status"])
        self.assertEqual(expected_resolution, decision["resolution"])
        self.assertEqual([], decision["supersedes"])

    def test_d22_records_the_exact_user_confirmed_custom_resolution(self) -> None:
        decision_text = f"确认选择 absolute_plus_baseline：{D22_CUSTOM_TEXT}"
        expected_resolution = {
            "kind": "custom",
            "selected_option_id": "absolute_plus_baseline",
            "custom_text": D22_CUSTOM_TEXT,
            "decision_text": decision_text,
            "authority": "user",
            "decided_at": "2026-07-21",
            "evidence_refs": [
                "conversation:user-confirmation:2026-07-21:D22:absolute_plus_baseline"
            ],
            "resolution_sha256": (
                "94ec57c0b72801dc37d8a7de08b16cc78b8ffc8bdb69b39f0eb0b56cf80d6e96"
            ),
        }
        register = load_register(REGISTER_PATH)
        decision = next(
            item for item in register["decisions"] if item["id"] == "D22"
        )

        self.assertEqual(4725, len(D22_CUSTOM_TEXT))
        self.assertEqual(6931, len(D22_CUSTOM_TEXT.encode("utf-8")))
        self.assertEqual(4753, len(decision_text))
        self.assertEqual(6969, len(decision_text.encode("utf-8")))
        self.assertEqual("resolved", decision["status"])
        self.assertEqual(expected_resolution, decision["resolution"])
        self.assertEqual([], decision["supersedes"])

    def test_pullwise_scope_resolution_unblocks_slice_two(self) -> None:
        register = load_register(REGISTER_PATH)
        report = verify_register(
            register, REPO_ROOT, require_slice="S2", check_document=False
        )

        self.assertEqual("ready", report["status"])
        self.assertTrue(report["valid"])
        self.assertTrue(report["ready"])
        self.assertEqual([], report["failures"])
        self.assertIsNone(report["active_decision_id"])
        self.assertEqual(["D2"], report["inactive_decision_ids"])

    def test_slice_gates_have_no_applicable_pending_decisions(self) -> None:
        register = load_register(REGISTER_PATH)
        for slice_id in ("S2", "S3", "S4", "S5", "S6", "S7", "S8"):
            with self.subTest(slice_id=slice_id):
                report = verify_register(
                    register, REPO_ROOT, require_slice=slice_id,
                    check_document=False, check_history=False,
                )
                self.assertEqual([], report["failures"])
                self.assertTrue(report["valid"])
                self.assertTrue(report["ready"])


if __name__ == "__main__":
    unittest.main()
