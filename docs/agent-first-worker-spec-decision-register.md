# Agent-First Worker Specification Decision Register

Status: generated Agent-First decision packet. Pending recommendations are non-normative and grant no implementation authority.

Machine source: contracts/agent-first/spec-decision-register.json.

<!-- BEGIN GENERATED AGENT-FIRST DECISION REGISTER -->
> Generated from `agent-first-spec-remediation-2026-07-17`. Recommendations are non-normative and are never resolutions. Do not edit this block by hand.

Active question: `D8`. Questions are asked one at a time. User silence, existing prose, current code, and Agent inference cannot resolve a decision.

| ID | Scope | Decision | Stored status | Applicability | Required before | Depends on | Non-normative recommendation |
|---|---|---|---|---|---|---|---|
| `D1` | `P0.1` | MVP/Post-MVP 产品范围 | `resolved` | `active` | `S2` | — | `pullwise_full_scan` |
| `D2` | `P0.1` | 通用工程任务控制面归属 | `pending` | `inactive` | `S2` | D1 | `independent_generic_ingress` |
| `D3` | `P0.5` | MVP R2 能力边界 | `resolved` | `active` | `S3` | D1 | `mvp_r0_r1_reject_r2` |
| `D4` | `P0.4` | legacy claim 缺失 policy 字段来源 | `resolved` | `active` | `S3` | D1, D3 | `field_by_field_ownership` |
| `D5` | `P0.6` | task_version 递增单位 | `resolved` | `active` | `S4` | D4 | `per_control_transaction` |
| `D6` | `P0.6` | Attempt claim 与 Owner 创建事务 | `resolved` | `active` | `S4` | D5 | `single_claim_owner_transaction` |
| `D7` | `P0.6` | monotonic 时间持久化形式 | `resolved` | `active` | `S4` | — | `persist_elapsed_consumption` |
| `D8` | `P0.6/P0.7` | lease loss 与 same-run resume 状态边界 | `pending` | `active` | `S4` | D5 | `task_active_attempt_fenced` |
| `D9` | `P0.7` | 内部结果与 legacy 发布的终态权威 | `pending` | `active` | `S4` | D8 | `internal_result_cas_authoritative` |
| `D10` | `P0.7` | 并发终态事实优先级模型 | `pending` | `active` | `S4` | D9 | `global_safety_first_matrix` |
| `D11` | `P0.7` | PARTIAL 安全交付证据表示 | `pending` | `active` | `S3` | D10 | `partial_delivery_manifest` |
| `D12` | `P0.7` | Server 拒绝前的结果修复与代次 | `pending` | `active` | `S4` | D9 | `new_generation_supersession` |
| `D13` | `P0.7` | authoritative cancel 与已准备结果协调 | `pending` | `active` | `S4` | D9, D10, D12 | `prepublish_cancel_postpublish_reconcile` |
| `D14` | `P0.8` | SourceState 与 bundle 完整性归属 | `pending` | `active` | `S4` | D1 | `separate_bundle_integrity_manifest` |
| `D15` | `P0.3` | GATE_* predicate 与 stable error taxonomy | `pending` | `active` | `S3` | — | `separate_predicate_registry` |
| `D16` | `P0.9` | MVP Q0 Owner self-attestation 路径 | `pending` | `active` | `S3` | D1, D4 | `remove_q0_success_path` |
| `D17` | `P0.9` | Q2 concern/slot 规划算法 | `pending` | `active` | `S3` | D16 | `versioned_concern_table` |
| `D18` | `P0.10` | 现有 root coordinator 与 Task Owner 关系 | `pending` | `active` | `S5` | D1, D6 | `coordinator_is_owner` |
| `D19` | `P0.10` | reviewer fanout 期间 Owner liveness | `pending` | `active` | `S5` | D4, D18 | `owner_remains_live` |
| `D20` | `P0.10` | 旧 QA 与新 Gate 权威切换 | `pending` | `active` | `S5` | D10, D17, D18 | `shadow_floor_then_gate_cutover` |
| `D21` | `P0.11` | outer job 执行模式配置权威 | `pending` | `active` | `S6` | D9, D20 | `server_claim_bound_mode` |
| `D23` | `P1.2` | C0 contract package 真源归属 | `pending` | `active` | `S7` | D1, D2 | `server_owned_package` |
| `D24` | `P1.2` | Server TaskRecord v2 bootstrap 策略 | `pending` | `active` | `S7` | D8, D23 | `lazy_eligible_claim_migration` |
| `D25` | `P1.5` | TaskResult/receipt digest DAG | `pending` | `active` | `S7` | D9, D23 | `immutable_receipt_mutable_binding` |
| `D26` | `P1.6` | 远期版本规范深度与完成口径 | `pending` | `active` | `S7` | D1 | `roadmap_separate_designs` |
| `D22` | `P0.11` | Release/Operations 数值门与签发 owner | `pending` | `active` | `S6` | D1, D20, D21 | `absolute_plus_baseline` |

### D1 — MVP/Post-MVP 产品范围

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S2`.

**Question:** 本次规范以通用工程 Agent Worker 为完成范围，还是仅以 Pullwise repo_review.full_scan 为完成范围？

**Options:**

- `pullwise_full_scan` — selected by resolution: 本次 MVP/Post-MVP 规范仅以 Pullwise repo_review.full_scan 为产品范围；通用工程 Worker 仅保留为长期方向。 当前 claim、lease、result 和产品入口只形成 full_scan 的可执行闭环。 Consequences: 在独立版本化通用契约完成前不得广告或选择通用工程任务
- `generic_agent_worker`: 把通用工程 Agent Worker 纳入本次完成范围。 与目标设计的长期愿景一致，但必须先补齐通用 submission、workspace、interaction、approval 和写型 task 契约。 Consequences: 扩大 S2、S3 和 S7 的跨端设计与验收范围

**Resolution:** `pullwise_full_scan` (`option`). 确认选择 pullwise_full_scan：本次 MVP/Post-MVP 规范仅以 Pullwise repo_review.full_scan 为产品范围；通用工程 Worker 仅保留为长期方向。

**Authority/evidence:** `user` on `2026-07-18`; `conversation:user-selection:2026-07-18:pullwise_full_scan`; digest `ab117e7c86472b7ce57bf2433978df0efe1299353ad747b7eabbff723fec469a`.

**Supersedes:** none

**Effects:** `authority`, `external_behavior`, `compatibility`

**Sources:** `handoff:P0.1`, `docs/agent-first-worker-design.md:5`, `docs/agent-first-worker-mvp-implementation-design.md:34`, `pullwise_worker/review_worker_v1.py:7118`

### D2 — 通用工程任务控制面归属

**Stored status:** `pending`; **applicability:** `inactive`; **required before:** `S2`.

**Question:** 若 D1 选择通用 Worker，通用 submission/workspace/interaction/write-task 应扩展 Pullwise Server，还是使用独立 generic ingress/control plane？

**Options:**

- `independent_generic_ingress` — non-normative recommendation, not selected: 建立独立、版本化的 generic ingress/control plane。 避免把尚未闭合的写型与交互权限混入现有 full-scan 控制面。 Consequences: 需要独立 identity、submission、workspace 和 approval 边界
- `extend_pullwise_server`: 直接扩展 Pullwise Server 承载通用工程任务。 可以复用现有租约和租户边界，但扩大 Server 产品职责。 Consequences: Server/Web 必须新增并版本化通用任务 surface

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `external_behavior`, `data_model`, `permission`

**Sources:** `handoff:P0.1`, `handoff:P1.2`

### D3 — MVP R2 能力边界

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S3`.

**Question:** MVP 是否仅支持 R0/R1 并对全部 R2 请求无副作用拒绝？

**Options:**

- `mvp_r0_r1_reject_r2` — selected by resolution: MVP 仅实现 R0/R1；R2 在所有 profile 显式无副作用拒绝。 local/eval 尚无完整 interaction、approval、cancel、resume 和 effect transport。 Consequences: R2 正向路径移至明确 Post-MVP 版本
- `mvp_full_r2`: MVP 补齐并支持 R2 正向路径。 可更早提供外部 mutation，但必须同时闭合完整授权与恢复协议。 Consequences: 显著扩大 MVP schema、security、fixture 和 DoD

**Resolution:** `mvp_r0_r1_reject_r2` (`option`). 确认选择 mvp_r0_r1_reject_r2：MVP 仅实现 R0/R1；R2 在所有 profile 显式无副作用拒绝，R2 正向路径移至明确 Post-MVP 版本。

**Authority/evidence:** `user` on `2026-07-19`; `conversation:user-selection:2026-07-19:mvp_r0_r1_reject_r2`; digest `0126d5ee3329c0f954e88e08979e8f0883086b3846315e2904cd7d323b97b07a`.

**Supersedes:** none

**Effects:** `permission`, `external_behavior`

**Sources:** `handoff:P0.5`

### D4 — legacy claim 缺失 policy 字段来源

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S3`.

**Question:** legacy claim 未提供的 policy 字段应由 Adapter 常量/公式提供、全部 fail closed，还是逐字段冻结 ownership？

**Options:**

- `field_by_field_ownership` — selected by resolution: 逐字段分类：现行 wire 可确定的值用版本化 Adapter 常量/公式，真正授权字段缺失则 fail closed。 同时避免任意默认值和不必要的 Server wire 扩展。 Consequences: mapping manifest 必须逐字段声明来源和稳定错误
- `adapter_constants`: 全部缺失字段由 Worker Adapter 的版本化常量或公式提供。 无需修改 Server 即可运行，但可能把授权选择错误地下放给 Worker。 Consequences: 每个常量都必须有 owner、版本和兼容性依据
- `fail_closed_expand_server`: 任一缺失字段都拒绝，并先扩展 Server claim。 权限来源最明确，但与 MVP 不改 Server 的当前边界冲突。 Consequences: MVP 需要新的跨仓 wire 版本

**Resolution:** `field_by_field_ownership` (`option`). 确认选择 field_by_field_ownership：legacy_v1 policy 映射逐字段冻结；可从现行 wire 唯一确定的值仅按版本化 Adapter 常量/公式产生，真正授权字段缺失则以稳定错误 fail closed，Worker 不得补授权默认值。

**Authority/evidence:** `user` on `2026-07-19`; `conversation:user-selection:2026-07-19:field_by_field_ownership`; digest `b009c68af93c965837e562d57cd20328e037b5fca0da30cc694125e0fee79654`.

**Supersedes:** none

**Effects:** `authority`, `permission`, `compatibility`

**Sources:** `handoff:P0.4`

### D5 — task_version 递增单位

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** task_version 应按每个成功控制事件事务 +1，还是按每个字段 mutation +1？

**Options:**

- `per_control_transaction` — selected by resolution: 每个成功控制事件事务整体 +1。 为 guard、event、write set 和 replay 提供单一线性化单位。 Consequences: 同事务多个字段变化共享一个新版本
- `per_field_mutation`: 每个字段 mutation 分别 +1。 字段变化可逐项计数，但一个业务事件会产生多次版本跳跃。 Consequences: CAS、事件重放和 fixture 更复杂

**Resolution:** `per_control_transaction` (`option`). 确认选择 per_control_transaction：task_version 按每个成功控制事件事务整体 +1；同一事务内所有字段变化、guard、事件、write set 与 replay 共享一个新版本，不按字段 mutation 分别递增。

**Authority/evidence:** `user` on `2026-07-19`; `conversation:user-selection:2026-07-19:per_control_transaction`; digest `859647945022b9d62bca4c6cf16b290c48e4e9bdb2f10700a40553194748b74a`.

**Supersedes:** none

**Effects:** `data_model`, `state_semantics`

**Sources:** `handoff:P0.6`

### D6 — Attempt claim 与 Owner 创建事务

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** attempt.claimed 是否在同一事务创建 Attempt 与 STARTING Owner incarnation？

**Options:**

- `single_claim_owner_transaction` — selected by resolution: 同一事务创建 Attempt 和 STARTING Owner，task_version 总计 +1。 避免存在无 Owner 的已 claim Attempt 中间态。 Consequences: 一个 idempotency key 覆盖完整 claim write set
- `split_claim_owner_transactions`: 拆成 attempt.claimed 与 owner.started 两个事务。 边界更细，但必须定义中间态、两套 guard 和 crash recovery。 Consequences: 总版本变化和 orphan 处理增加

**Resolution:** `single_claim_owner_transaction` (`option`). 确认选择 single_claim_owner_transaction：attempt.claimed 在同一控制事件事务内创建 Attempt 和 STARTING Owner incarnation；完整 claim write set 共用一个 idempotency key，task_version 总计只增加一次，不允许持久化无 Owner 的已 claim Attempt 中间态。

**Authority/evidence:** `user` on `2026-07-20`; `conversation:user-selection:2026-07-20:single_claim_owner_transaction`; digest `e1ad16c135ae5f0880123becdd640bf685c0f201b44dd941830590b0b39174d8`.

**Supersedes:** none

**Effects:** `data_model`, `state_semantics`

**Sources:** `handoff:P0.6`

### D7 — monotonic 时间持久化形式

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** 恢复所需 monotonic 时间应只持久化 elapsed consumption，还是保存值并绑定 boot/clock epoch？

**Options:**

- `persist_elapsed_consumption` — selected by resolution: 只持久化已消耗时长，重启时从权威 wall deadline 重建 monotonic origin。 避免比较不同进程或 boot 的裸 monotonic 值。 Consequences: 恢复公式必须同时绑定 absolute deadline
- `persist_clock_epoch`: 持久化 monotonic 值并绑定明确 clock/boot epoch。 可保留原始读数，但需要可靠的 epoch identity 和跨重启规则。 Consequences: schema 与平台时钟契约更复杂

**Resolution:** `persist_elapsed_consumption` (`option`). 确认选择 persist_elapsed_consumption：恢复只持久化已消耗时长；重启时从权威 wall deadline 重建本进程的 monotonic origin，不比较或恢复不同进程或 boot 的裸 monotonic 值；恢复公式必须同时绑定 immutable absolute_deadline_at。

**Authority/evidence:** `user` on `2026-07-20`; `conversation:user-selection:2026-07-20:persist_elapsed_consumption`; digest `5d7916e9389c0203185fb7e2e64be49df0ea52557d875f661f5d0180e093f5ea`.

**Supersedes:** none

**Effects:** `data_model`, `state_semantics`

**Sources:** `handoff:P0.6`

### D8 — lease loss 与 same-run resume 状态边界

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S4`.

**Question:** 外层 lease 丢失时 Task、transport Attempt 与未来 successor resume 应如何分层终态化？

**Options:**

- `task_active_attempt_fenced` — non-normative recommendation, not selected: Task 保持 ACTIVE/FINALIZING；仅旧 transport/native Attempt fenced，并由 successor 接管。 与 Post-MVP same-run resume 目标一致且保持 Task/Attempt 分层。 Consequences: 必须新增 transport abandonment 与恢复资格谓词
- `task_recovery_pending`: 新增显式 RECOVERY_PENDING Task 状态等待 successor。 可见性更强，但扩大 state machine 与 public projection。 Consequences: 所有状态矩阵和旧客户端行为需版本化
- `task_terminal_no_resume`: lease loss 直接令 Task 终态，并放弃同 Task resume。 MVP 最简单，但不能与 V1.3 恢复同一 Task 同时成立。 Consequences: Post-MVP 必须改为新 Task 或取消 same-run resume

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `state_semantics`, `compatibility`

**Sources:** `handoff:P0.6/P0.7`, `handoff:P0.6`, `handoff:P0.7`, `handoff:P1.4`

### D9 — 内部结果与 legacy 发布的终态权威

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S4`.

**Question:** 内部 TaskResult CAS 还是 Server ACK 应成为唯一语义终态线性化点？

**Options:**

- `internal_result_cas_authoritative` — non-normative recommendation, not selected: 内部 TaskResult CAS 是语义终态；legacy outbox/Server ACK 只是可恢复 transport projection。 保持一套内部真相并复用现有 terminal outbox 作为投影 WAL。 Consequences: ACK 前 slot 仍占用，但不得改写已提交 outcome
- `server_ack_authoritative`: 只有 Server 接受 ACK 才形成语义终态。 控制面与外部投影一致，但本地结果在网络故障期间没有最终身份。 Consequences: TaskResult publication 与 Server transaction 强耦合

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `state_semantics`, `compatibility`

**Sources:** `handoff:P0.7`

### D10 — 并发终态事实优先级模型

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S4`.

**Question:** cancel、deadline、effect、quality、protocol 等并发事实采用何种确定性优先规则？

**Options:**

- `global_safety_first_matrix` — non-normative recommendation, not selected: 采用一张全局 safety-first 穷举优先矩阵。 同一权威事实组合可跨 profile 机械复算唯一 outcome。 Consequences: 必须冻结完整 fact×state×availability×effect 表
- `first_cas_wins`: 第一个成功 CAS 的候选终态获胜，后到事实只审计。 事务简单，但可能让低优先结果隐藏未知 effect 或删除 fence。 Consequences: 需要证明所有先到候选都安全且诚实
- `profile_specific_precedence`: 每个 profile 使用独立终态优先表。 可按领域定制，但通用内核不再有单一总函数。 Consequences: 跨 profile 兼容与验证矩阵扩大

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `state_semantics`, `external_behavior`

**Sources:** `handoff:P0.7`

### D11 — PARTIAL 安全交付证据表示

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S3`.

**Question:** PARTIAL 是否新增 Worker-owned partial-delivery-manifest，还是只依赖严格 AvailabilityRef？

**Options:**

- `partial_delivery_manifest` — non-normative recommendation, not selected: 新增 Worker-owned partial-delivery-manifest，显式列出安全可交付对象。 Terminalization 不必依赖尚不可构造的 proposal/ObservationManifest。 Consequences: 新增 schema、digest、owner 和 fixtures
- `availability_refs_only`: 只用严格 AvailabilityRef 表达 PARTIAL 可交付性。 对象更少，但必须为每个缺失组合冻结 safe-deliverable 谓词。 Consequences: availability union 和终态矩阵更复杂

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `data_model`, `state_semantics`

**Sources:** `handoff:P0.7`

### D12 — Server 拒绝前的结果修复与代次

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S4`.

**Question:** terminal payload 在 Server 400 拒绝后是否允许生成新 generation 并保留 supersession journal？

**Options:**

- `new_generation_supersession` — non-normative recommendation, not selected: ACK 前允许以新 generation 修复，并写不可变 supersession journal。 闭合 immutable payload 与可修复 contract error 的冲突。 Consequences: 旧代次永不覆盖且所有 replay 必须识别 supersession
- `no_repair_terminal_failure`: contract-invalid 后不修 payload，直接诚实非成功终态。 不可变性最简单，但瞬时格式问题会永久终止任务。 Consequences: operator 或新 Task 才能重试

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `data_model`, `compatibility`, `state_semantics`

**Sources:** `handoff:P0.7`

### D13 — authoritative cancel 与已准备结果协调

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S4`.

**Question:** authoritative cancellation 在内部结果发布前后分别如何与 prepared non-cancel result 协调？

**Options:**

- `prepublish_cancel_postpublish_reconcile` — non-normative recommendation, not selected: 内部发布前改为 CANCELLED；发布后只允许显式 reconciled variant/addendum。 避免 Worker completed 与 Server cancelled 静默分叉。 Consequences: 必须冻结 publication boundary 和 reconciled schema
- `transport_projection_only`: cancel 永远只改变 legacy transport projection，不改变内部 outcome。 内部记录稳定，但 public 状态可能不同。 Consequences: 必须公开且可审计地表示内部/外部差异
- `reject_supersession`: 拒绝 cancellation supersession 并阻塞 operator。 不产生双版本，但无法自动满足 Server authority。 Consequences: slot 可能长期占用且需人工协调

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `state_semantics`, `compatibility`

**Sources:** `handoff:P0.7`

### D14 — SourceState 与 bundle 完整性归属

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S4`.

**Question:** run/bundles 源码副本应属于 SourceState、独立 BundleIntegrityManifest，还是普通 runtime artifact？

**Options:**

- `separate_bundle_integrity_manifest` — non-normative recommendation, not selected: SourceState 仅描述 materialized source；独立 BundleIntegrityManifest 绑定派生 bundle。 消除 source ownership 与 worker runtime tree 的循环。 Consequences: bundle plan、entry digest、render digest 和 reviewer assignment 必须绑定
- `bundles_inside_source_state`: 把 rendered bundles 纳入 SourceState。 统一 source-bearing 对象，但 SourceState 会包含自身派生物。 Consequences: 必须证明 digest DAG 无环
- `bundles_runtime_only`: bundles 只作为不受 SourceState 约束的 runtime artifact。 实现简单，但无法机械证明 reviewer 输入来自冻结源码。 Consequences: 证据完整性目标无法满足

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `data_model`, `authority`

**Sources:** `handoff:P0.8`

### D15 — GATE_* predicate 与 stable error taxonomy

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S3`.

**Question:** GATE_* 应是独立 predicate ID，还是直接作为 stable error code？

**Options:**

- `separate_predicate_registry` — non-normative recommendation, not selected: GATE_* 属独立 predicate registry，失败结果引用 stable error registry。 谓词身份与失败原因是不同维度，可保持多对多映射。 Consequences: 两个 registry 必须有双向消费校验
- `gate_ids_are_errors`: 全部 GATE_* 直接纳入 stable error registry。 registry 更少，但一个谓词的不同失败原因难以区分。 Consequences: schema 和 prose 必须统一只使用 error code 语义

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `data_model`, `compatibility`

**Sources:** `handoff:P0.3`

### D16 — MVP Q0 Owner self-attestation 路径

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S3`.

**Question:** MVP 是否删除 Q0 Owner self-attestation 成功路径，还是补齐其签名与 key-owner 契约？

**Options:**

- `remove_q0_success_path` — non-normative recommendation, not selected: 删除 MVP Q0 self-attestation 成功路径；当前 full-scan 至少使用 Q1。 避免在没有签名/key ownership 的情况下制造伪验证。 Consequences: Q0 请求拒绝或提升到 Q1
- `implement_q0_signing`: 保留 Q0 并补齐 self-attestation schema、签名、key owner 和验证。 支持低风险任务快速闭环，但增加密钥与信任面。 Consequences: 必须加入签发、轮换、撤销和 negative fixtures

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `permission`, `data_model`

**Sources:** `handoff:P0.9`

### D17 — Q2 concern/slot 规划算法

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S3`.

**Question:** Q2 slots 应由固定版本化 concern/coverage 表，还是由版本化纯函数 classifier 生成？

**Options:**

- `versioned_concern_table` — non-normative recommendation, not selected: 使用固定版本化 concern/coverage 表生成 slot ID。 最易审计、复算和冻结 golden vectors。 Consequences: 新增 concern 必须升 registry version
- `pure_classifier_slots`: 由版本化纯函数 QualityRisk classifier 生成 slots。 表达力更高，但输入维度和完整决策表必须全部冻结。 Consequences: classifier 版本与所有 slot fixtures 成为 contract

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `state_semantics`, `authority`

**Sources:** `handoff:P0.9`

### D18 — 现有 root coordinator 与 Task Owner 关系

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S5`.

**Question:** strangler 接管中现有 root coordinator 是否直接成为逻辑 Task Owner？

**Options:**

- `coordinator_is_owner` — non-normative recommendation, not selected: 现有 root coordinator 直接成为逻辑 Task Owner，旧 phase 逐步成为其 activities。 符合 strangler 约束并避免并行第二控制器。 Consequences: 必须把 coordinator identity 与 owner incarnation 明确分层
- `new_owner_wraps_coordinator`: 新增 Owner，把旧 coordinator 作为 domain activity 调用。 新内核边界更纯，但过渡期存在两层协调控制。 Consequences: 必须证明只有新 Owner 拥有状态与终态权威

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `state_semantics`

**Sources:** `handoff:P0.10`

### D19 — reviewer fanout 期间 Owner liveness

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S5`.

**Question:** fanout 期间 Owner 应保持 live、suspend/archive，还是按阶段使用混合规则？

**Options:**

- `owner_remains_live` — non-normative recommendation, not selected: Owner incarnation 在 fanout 全程保持 live，并与 reviewer/verifier 一起计入 agent/session budget。 保持一个连续控制 owner，不需要恢复新 incarnation。 Consequences: max_agents 必须为 Owner 预留固定 slot
- `owner_suspended`: fanout 前 suspend/archive Owner，完成后创建新 incarnation。 释放并发 slot，但增加 checkpoint 与恢复边界。 Consequences: 每次 fanout 都需要 owner epoch 与恢复事务
- `phase_specific_liveness`: 按 domain reviewer 与 Quality Verifier 阶段冻结不同 liveness 规则。 可优化资源，但控制模型更复杂。 Consequences: 每个阶段必须有确定 transition 和 budget 公式

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `state_semantics`, `permission`

**Sources:** `handoff:P0.10`

### D20 — 旧 QA 与新 Gate 权威切换

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S5`.

**Question:** 旧 QA 与新 Gate 分歧时，shadow、cutover 和终态化采用何种 precedence？

**Options:**

- `shadow_floor_then_gate_cutover` — non-normative recommendation, not selected: shadow 期旧 QA 是 hard floor；显式 cutover 后新 Gate 成为唯一权威。 兼容期不放宽当前安全门，同时给出终止双权威的明确时点。 Consequences: cutover 前置条件、divergence 阈值和 rollback 必须版本化
- `legacy_qa_permanent_floor`: 旧 QA 永久作为新 Gate 的 hard floor。 最保守，但内核永远无法独立成为权威。 Consequences: 旧 pipeline 债务成为永久依赖
- `new_gate_immediate_authority`: 新 Gate 从首次启用即为唯一权威。 迁移快速，但没有 shadow 证据保护。 Consequences: 首次 divergence 即可能改变用户结果

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `compatibility`, `state_semantics`

**Sources:** `handoff:P0.10`

### D21 — outer job 执行模式配置权威

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S6`.

**Question:** legacy-only、shadow、kernel-authoritative 模式应由 Worker config、Server claim/grant，还是独立 deployment 绑定？

**Options:**

- `server_claim_bound_mode` — non-normative recommendation, not selected: Server 在 claim/grant 中签发并不可变绑定每个 outer job 的 mode。 控制面是 job authority，重启与 rollback 不会由本地配置换轨。 Consequences: 需要 additive/versioned claim 字段或冻结 legacy 常量
- `worker_config_bound_mode`: Worker config 在 claim 时快照 mode 并持久化。 无需 Server 字段，但配置 owner 与租约事实分离。 Consequences: 必须证明多 Worker 与重启使用同一 mode
- `deployment_bound_mode`: 使用独立 binary/deployment 固定 mode。 运行时无切换，但发布拓扑与回滚更重。 Consequences: shadow 与 canary 需要多套部署身份

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `compatibility`, `release_ownership`

**Sources:** `handoff:P0.11`

### D23 — C0 contract package 真源归属

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S7`.

**Question:** 跨端 contract package 的唯一真源应在 Server、Worker，还是独立 shared package？

**Options:**

- `server_owned_package` — non-normative recommendation, not selected: 由 Server 仓库发布，Worker/Web pin 消费。 Server 拥有 wire ingest、storage 和 public projection，且当前 Post 文档沿用此方向。 Consequences: Worker/Web 更新需显式 pin 与 compatibility matrix
- `worker_owned_package`: 由 Worker 仓库发布，Server/Web 消费。 producer 靠近内部 schema，但 Server wire authority 被倒置。 Consequences: Server 必须信任 Worker 发布周期
- `independent_shared_package`: 建立独立 shared contract package 仓库/包。 三端中立，但新增发布、ownership 和供应链边界。 Consequences: 必须定义 generator、签名、pin 和回滚流程

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `compatibility`, `data_model`

**Sources:** `handoff:P1.2`

### D24 — Server TaskRecord v2 bootstrap 策略

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S7`.

**Question:** C0 应只为新任务建立 v2、在 eligible claim 时惰性迁移，还是批量 backfill？

**Options:**

- `lazy_eligible_claim_migration` — non-normative recommendation, not selected: 仅在 QUEUED、无 active lease/result 且将选择新协议的 claim 事务中惰性迁移。 避免迁移活跃 legacy run，同时不需要全量 backfill。 Consequences: claim 事务需 old/new CAS 和 schema_migrated event
- `new_tasks_only`: 只有新 agent_task_v1 任务创建 v2；所有旧任务走完 v1。 迁移最简单，但旧 queued 任务无法采用新能力。 Consequences: 需明确 v1 drain 与保留期限
- `batch_backfill`: 部署时批量 backfill eligible v1 TaskRecord。 切换集中，但放大 migration 与 rollback 风险。 Consequences: 需要全量锁、resume、tombstone 和 rollback 方案

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `data_model`, `compatibility`

**Sources:** `handoff:P1.2`

### D25 — TaskResult/receipt digest DAG

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S7`.

**Question:** 如何消除 immutable receipt 与 full TaskResult digest 的循环？

**Options:**

- `immutable_receipt_mutable_binding` — non-normative recommendation, not selected: 拆分 immutable upload/transport receipt、mutable Server binding/index，并分离 TaskResultCore 与 transport envelope digest。 形成无环内容 DAG，同时保留一次性 Server 绑定。 Consequences: 需要两个 digest、绑定 CAS 和 crash fixtures
- `receipt_after_full_digest`: 等 full result digest 已知后才创建 receipt。 避免回填 receipt，但 receipt 无法被 result 本体引用。 Consequences: transport envelope 必须把 receipt 放在被哈希 core 之外
- `mutable_receipt_record`: 明确 receipt 是可变 DB record，不再作为 immutable ContentRef。 实现直接，但失去内容寻址不可变语义。 Consequences: 所有引用和审计必须改为 versioned row identity

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `data_model`, `compatibility`

**Sources:** `handoff:P1.5`

### D26 — 远期版本规范深度与完成口径

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S7`.

**Question:** V1.1/V2.x/V3.0 现在补成可实施规格，还是明确标为 roadmap 并逐版另写 implementation design？

**Options:**

- `roadmap_separate_designs` — non-normative recommendation, not selected: 把未闭合远期版本明确标为 roadmap；每版开工前另写完整 implementation design。 诚实区分路线图与可执行规格，不让远期债务阻塞已闭合 MVP。 Consequences: 不得再宣称当前 Post 文档完整实现所有版本
- `complete_all_versions_now`: 现在补齐 V1.1/V2.x/V3.0 的 schema/state/storage/wire/fixtures/rollout/DoD。 可维持完整实现设计标题，但本次规范范围显著扩大。 Consequences: S7 必须完成所有远期版本的实施级闭环

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `release_ownership`

**Sources:** `handoff:P1.6`

### D22 — Release/Operations 数值门与签发 owner

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S6`.

**Question:** Release DoD 使用绝对数值门叠加 baseline，还是只使用重新冻结的 baseline-relative thresholds？

**Options:**

- `absolute_plus_baseline` — non-normative recommendation, not selected: 采用安全绝对门，并叠加版本化 baseline-relative regression 门。 绝对底线防止坏 baseline 被继承，相对门捕获回归。 Consequences: operator 必须签发数据集、分母、窗口和阈值
- `baseline_relative_only`: 仅使用重新冻结的 baseline-relative thresholds。 更适应环境差异，但没有独立安全下限。 Consequences: baseline 质量与刷新审批成为唯一保护

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `release_ownership`, `external_behavior`

**Sources:** `handoff:P0.11`
<!-- END GENERATED AGENT-FIRST DECISION REGISTER -->
