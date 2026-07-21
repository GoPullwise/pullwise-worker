# Agent-First Worker Specification Decision Register

Status: generated Agent-First decision packet. Pending recommendations are non-normative and grant no implementation authority.

Machine source: contracts/agent-first/spec-decision-register.json.

<!-- BEGIN GENERATED AGENT-FIRST DECISION REGISTER -->
> Generated from `agent-first-spec-remediation-2026-07-17`. Recommendations are non-normative and are never resolutions. Do not edit this block by hand.

Active question: `D28`. Questions are asked one at a time. User silence, existing prose, current code, and Agent inference cannot resolve a decision.

| ID | Scope | Decision | Stored status | Applicability | Required before | Depends on | Non-normative recommendation |
|---|---|---|---|---|---|---|---|
| `D1` | `P0.1` | MVP/Post-MVP 产品范围 | `resolved` | `active` | `S2` | — | `pullwise_full_scan` |
| `D2` | `P0.1` | 通用工程任务控制面归属 | `pending` | `inactive` | `S2` | D1 | `independent_generic_ingress` |
| `D3` | `P0.5` | MVP R2 能力边界 | `resolved` | `active` | `S3` | D1 | `mvp_r0_r1_reject_r2` |
| `D4` | `P0.4` | legacy claim 缺失 policy 字段来源 | `resolved` | `active` | `S3` | D1, D3 | `field_by_field_ownership` |
| `D5` | `P0.6` | task_version 递增单位 | `resolved` | `active` | `S4` | D4 | `per_control_transaction` |
| `D6` | `P0.6` | Attempt claim 与 Owner 创建事务 | `resolved` | `active` | `S4` | D5 | `single_claim_owner_transaction` |
| `D7` | `P0.6` | monotonic 时间持久化形式 | `resolved` | `active` | `S4` | — | `persist_elapsed_consumption` |
| `D8` | `P0.6/P0.7` | lease loss 与 same-run resume 状态边界 | `resolved` | `active` | `S4` | D5 | `task_active_attempt_fenced` |
| `D27` | `P0.4/P0.10/P0.11/P1.2` | Agent-First 单协议 clean-break 边界 | `resolved` | `active` | `S3` | D4 | `clean_break_no_legacy` |
| `D9` | `P0.7` | 内部结果与 legacy 发布的终态权威 | `resolved` | `active` | `S4` | D8 | `internal_result_cas_authoritative` |
| `D10` | `P0.7` | 并发终态事实优先级模型 | `resolved` | `active` | `S4` | D9 | `global_safety_first_matrix` |
| `D11` | `P0.7` | PARTIAL 安全交付证据表示 | `resolved` | `active` | `S3` | D10 | `partial_delivery_manifest` |
| `D12` | `P0.7` | Server 拒绝前的结果修复与代次 | `resolved` | `active` | `S4` | D9 | `new_generation_supersession` |
| `D13` | `P0.7` | authoritative cancel 与已准备结果协调 | `resolved` | `active` | `S4` | D9, D10, D12 | `prepublish_cancel_postpublish_reconcile` |
| `D14` | `P0.8` | SourceState 与 bundle 完整性归属 | `resolved` | `active` | `S4` | D1 | `separate_bundle_integrity_manifest` |
| `D15` | `P0.3` | GATE_* predicate 与 stable error taxonomy | `resolved` | `active` | `S3` | — | `separate_predicate_registry` |
| `D16` | `P0.9` | MVP Q0 Owner self-attestation 路径 | `resolved` | `active` | `S3` | D1, D4 | `remove_q0_success_path` |
| `D17` | `P0.9` | Q2 concern/slot 规划算法 | `resolved` | `active` | `S3` | D16 | `versioned_concern_table` |
| `D18` | `P0.10` | 现有 root coordinator 与 Task Owner 关系 | `resolved` | `active` | `S5` | D1, D6 | `coordinator_is_owner` |
| `D19` | `P0.10` | reviewer fanout 期间 Owner liveness | `resolved` | `active` | `S5` | D4, D18 | `owner_remains_live` |
| `D20` | `P0.10` | 旧 QA 与新 Gate 权威切换 | `resolved` | `active` | `S5` | D10, D17, D18 | `shadow_floor_then_gate_cutover` |
| `D21` | `P0.11` | outer job 执行模式配置权威 | `resolved` | `active` | `S6` | D9, D20 | `server_claim_bound_mode` |
| `D23` | `P1.2` | C0 contract package 真源归属 | `resolved` | `active` | `S7` | D1, D2 | `server_owned_package` |
| `D24` | `P1.2` | Server TaskRecord v2 bootstrap 策略 | `resolved` | `active` | `S7` | D8, D23 | `lazy_eligible_claim_migration` |
| `D25` | `P1.5` | TaskResult/receipt digest DAG | `resolved` | `active` | `S7` | D9, D23 | `immutable_receipt_mutable_binding` |
| `D26` | `P1.6` | 远期版本规范深度与完成口径 | `resolved` | `active` | `S7` | D1 | `roadmap_separate_designs` |
| `D22` | `P0.11` | Release/Operations 数值门与签发 owner | `resolved` | `active` | `S6` | D1, D20, D21 | `absolute_plus_baseline` |
| `D28` | `P0.3/P1.2` | current package 身份、发布物与 exact pin | `pending` | `active` | `S3` | D23, D27 | `logical_bundle_generated_wrappers` |
| `D29` | `P0.3/P0.6/P1.2/P1.5` | current package 基础契约的原子闭包 | `pending` | `active` | `S3` | D3, D6, D7, D15, D21, D24, D25, D27, D28 | `layered_atomic_root` |
| `D30` | `P0.3/P0.6` | grant 至 tool receipt/budget 的 dispatch 线性化 | `pending` | `active` | `S3` | D7, D21, D25, D29 | `worker_journal_server_authority` |

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

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** 外层 lease 丢失时 Task、transport Attempt 与未来 successor resume 应如何分层终态化？

**Options:**

- `task_active_attempt_fenced` — selected by resolution: Task 保持 ACTIVE/FINALIZING；仅旧 transport/native Attempt fenced，并由 successor 接管。 与 Post-MVP same-run resume 目标一致且保持 Task/Attempt 分层。 Consequences: 必须新增 transport abandonment 与恢复资格谓词
- `task_recovery_pending`: 新增显式 RECOVERY_PENDING Task 状态等待 successor。 可见性更强，但扩大 state machine 与 public projection。 Consequences: 所有状态矩阵和旧客户端行为需版本化
- `task_terminal_no_resume`: lease loss 直接令 Task 终态，并放弃同 Task resume。 MVP 最简单，但不能与 V1.3 恢复同一 Task 同时成立。 Consequences: Post-MVP 必须改为新 Task 或取消 same-run resume

**Resolution:** `task_active_attempt_fenced` (`option`). 确认选择 task_active_attempt_fenced：外层 lease 丢失时，Task 保持 ACTIVE 或 FINALIZING；仅旧 transport/native Attempt 被 fenced，并由满足恢复资格谓词的 successor 接管；必须新增 transport abandonment 记录与恢复资格谓词，以保持 Task/Attempt 分层并支持 Post-MVP same-run resume。

**Authority/evidence:** `user` on `2026-07-20`; `conversation:user-selection:2026-07-20:task_active_attempt_fenced`; digest `e895f73c3a0962937cbab61b4c8037f9ccba9daa6e6de89d5004005dd830b98a`.

**Supersedes:** none

**Effects:** `state_semantics`, `compatibility`

**Sources:** `handoff:P0.6/P0.7`, `handoff:P0.6`, `handoff:P0.7`, `handoff:P1.4`

### D27 — Agent-First 单协议 clean-break 边界

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S3`.

**Question:** Agent-First 重构是否跨 Worker、Server、Web 采用单一 current contract 并删除全部 legacy compatibility，还是保留有界的兼容迁移与双轨？

**Options:**

- `clean_break_no_legacy` — selected by resolution: Worker、Server、Web 协调切换到一套 current Agent-First contract；删除所有 legacy Adapter、旧协议/路由、旧数据形状、DTO alias、compat fixture、shadow、dual-read/write、fallback 与 downgrade 路径。 项目处于内部重构阶段；单一 current model 可消除双权威、协议分叉和长期迁移债务。 Consequences: 旧客户端、旧任务和旧持久化状态不获兼容保证；允许在受控环境 clean reset。; 三仓必须协调切换，并以 current protocol 契约测试和 legacy-path absence gate 作为发布证据。; 安全、权限、持久化、幂等和审计不变量仍须 fail closed。
- `bounded_legacy_coexistence`: 保留 strict legacy 协议，通过 Adapter、双读写、shadow、兼容 DTO 和分阶段 cutover 迁移。 允许旧 Worker、Server、Web 滚动共存，但保留兼容分支和双权威收敛复杂度。 Consequences: 必须继续维护 legacy baseline、兼容矩阵、旧路径回滚和有期限的删除计划。

**Resolution:** `clean_break_no_legacy` (`option`). 确认选择 clean_break_no_legacy：Agent-First 重构只保留一套 current contract；不保留任何 legacy Adapter、shim、旧协议/路由、旧数据形状、DTO alias、双读写、shadow、fallback、protocol downgrade、compatibility mode、兼容性回滚路径，或仅为旧数据存在的 migration/backfill；门禁允许后协调切换 Worker、Server、Web，并删除旧 code、schema、routes、DTO、fixtures、tests、CI 与 docs。D4 的 legacy_v1 field-by-field ownership 决议由本决议显式 supersede；安全、权限、持久化、幂等、审计与正确性约束继续 fail closed。

**Authority/evidence:** `user` on `2026-07-20`; `conversation:user-directive:2026-07-20:all-legacy-compatibility-clean-break`; digest `f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e`.

**Supersedes:** D4

**Effects:** `authority`, `compatibility`, `data_model`, `external_behavior`, `permission`, `release_ownership`, `state_semantics`

**Sources:** `conversation:user-directive:2026-07-20:all-legacy-compatibility-clean-break`, `AGENTS.md#agent-first-clean-break-refactor-policy`

### D9 — 内部结果与 legacy 发布的终态权威

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** 内部 TaskResult CAS 还是 Server ACK 应成为唯一语义终态线性化点？

**Options:**

- `internal_result_cas_authoritative` — selected by resolution: 内部 TaskResult CAS 是语义终态；legacy outbox/Server ACK 只是可恢复 transport projection。 保持一套内部真相并复用现有 terminal outbox 作为投影 WAL。 Consequences: ACK 前 slot 仍占用，但不得改写已提交 outcome
- `server_ack_authoritative`: 只有 Server 接受 ACK 才形成语义终态。 控制面与外部投影一致，但本地结果在网络故障期间没有最终身份。 Consequences: TaskResult publication 与 Server transaction 强耦合

**Resolution:** `internal_result_cas_authoritative` (`option`). Select internal_result_cas_authoritative: the internal TaskResult CAS is the sole semantic terminal linearization point; Server ACK is only a recoverable transport projection and cannot create, replace, or rewrite the committed TaskResult outcome.

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-selection:2026-07-21:internal_result_cas_authoritative`; digest `3e8a5cf9d69cccd50667009c80e9a3176501d3c0150d5bec931ee71fb1cc46ce`.

**Supersedes:** none

**Effects:** `authority`, `state_semantics`, `compatibility`

**Sources:** `handoff:P0.7`

### D10 — 并发终态事实优先级模型

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** cancel、deadline、effect、quality、protocol 等并发事实采用何种确定性优先规则？

**Options:**

- `global_safety_first_matrix` — selected by resolution: 采用一张全局 safety-first 穷举优先矩阵。 同一权威事实组合可跨 profile 机械复算唯一 outcome。 Consequences: 必须冻结完整 fact×state×availability×effect 表
- `first_cas_wins`: 第一个成功 CAS 的候选终态获胜，后到事实只审计。 事务简单，但可能让低优先结果隐藏未知 effect 或删除 fence。 Consequences: 需要证明所有先到候选都安全且诚实
- `profile_specific_precedence`: 每个 profile 使用独立终态优先表。 可按领域定制，但通用内核不再有单一总函数。 Consequences: 跨 profile 兼容与验证矩阵扩大

**Resolution:** `global_safety_first_matrix` (`option`). Select global_safety_first_matrix: 采用一张全局 safety-first 穷举优先矩阵。 同一权威事实组合可跨 profile 机械复算唯一 outcome。 Constraints: 必须冻结完整 fact×state×availability×effect 表

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `1daae4c66d41bd95a3eef8e24756590c8e6f75a05899548dc3126bfd39172e31`.

**Supersedes:** none

**Effects:** `state_semantics`, `external_behavior`

**Sources:** `handoff:P0.7`

### D11 — PARTIAL 安全交付证据表示

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S3`.

**Question:** PARTIAL 是否新增 Worker-owned partial-delivery-manifest，还是只依赖严格 AvailabilityRef？

**Options:**

- `partial_delivery_manifest` — selected by resolution: 新增 Worker-owned partial-delivery-manifest，显式列出安全可交付对象。 Terminalization 不必依赖尚不可构造的 proposal/ObservationManifest。 Consequences: 新增 schema、digest、owner 和 fixtures
- `availability_refs_only`: 只用严格 AvailabilityRef 表达 PARTIAL 可交付性。 对象更少，但必须为每个缺失组合冻结 safe-deliverable 谓词。 Consequences: availability union 和终态矩阵更复杂

**Resolution:** `partial_delivery_manifest` (`option`). Select partial_delivery_manifest: 新增 Worker-owned partial-delivery-manifest，显式列出安全可交付对象。 Terminalization 不必依赖尚不可构造的 proposal/ObservationManifest。 Constraints: 新增 schema、digest、owner 和 fixtures

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `dc65778d9f60563e39a9c3262200f8e26efd8c48c29aa0141087793186032a7e`.

**Supersedes:** none

**Effects:** `data_model`, `state_semantics`

**Sources:** `handoff:P0.7`

### D12 — Server 拒绝前的结果修复与代次

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** terminal payload 在 Server 400 拒绝后是否允许生成新 generation 并保留 supersession journal？

**Options:**

- `new_generation_supersession` — selected by resolution: ACK 前允许以新 generation 修复，并写不可变 supersession journal。 闭合 immutable payload 与可修复 contract error 的冲突。 Consequences: 旧代次永不覆盖且所有 replay 必须识别 supersession
- `no_repair_terminal_failure`: contract-invalid 后不修 payload，直接诚实非成功终态。 不可变性最简单，但瞬时格式问题会永久终止任务。 Consequences: operator 或新 Task 才能重试

**Resolution:** `new_generation_supersession` (`option`). Select new_generation_supersession: ACK 前允许以新 generation 修复，并写不可变 supersession journal。 闭合 immutable payload 与可修复 contract error 的冲突。 Constraints: 旧代次永不覆盖且所有 replay 必须识别 supersession

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `b459cd0e371c34702e654761aa89caa21238f5c5314020e9d9c7484d60902764`.

**Supersedes:** none

**Effects:** `data_model`, `compatibility`, `state_semantics`

**Sources:** `handoff:P0.7`

### D13 — authoritative cancel 与已准备结果协调

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** authoritative cancellation 在内部结果发布前后分别如何与 prepared non-cancel result 协调？

**Options:**

- `prepublish_cancel_postpublish_reconcile` — selected by resolution: 内部发布前改为 CANCELLED；发布后只允许显式 reconciled variant/addendum。 避免 Worker completed 与 Server cancelled 静默分叉。 Consequences: 必须冻结 publication boundary 和 reconciled schema
- `transport_projection_only`: cancel 永远只改变 legacy transport projection，不改变内部 outcome。 内部记录稳定，但 public 状态可能不同。 Consequences: 必须公开且可审计地表示内部/外部差异
- `reject_supersession`: 拒绝 cancellation supersession 并阻塞 operator。 不产生双版本，但无法自动满足 Server authority。 Consequences: slot 可能长期占用且需人工协调

**Resolution:** `prepublish_cancel_postpublish_reconcile` (`option`). Select prepublish_cancel_postpublish_reconcile: 内部发布前改为 CANCELLED；发布后只允许显式 reconciled variant/addendum。 避免 Worker completed 与 Server cancelled 静默分叉。 Constraints: 必须冻结 publication boundary 和 reconciled schema

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `4a90df4dce3840e2f726d952fa0b49ef9294e73e851208f968df30642720e5a7`.

**Supersedes:** none

**Effects:** `state_semantics`, `compatibility`

**Sources:** `handoff:P0.7`

### D14 — SourceState 与 bundle 完整性归属

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S4`.

**Question:** run/bundles 源码副本应属于 SourceState、独立 BundleIntegrityManifest，还是普通 runtime artifact？

**Options:**

- `separate_bundle_integrity_manifest` — selected by resolution: SourceState 仅描述 materialized source；独立 BundleIntegrityManifest 绑定派生 bundle。 消除 source ownership 与 worker runtime tree 的循环。 Consequences: bundle plan、entry digest、render digest 和 reviewer assignment 必须绑定
- `bundles_inside_source_state`: 把 rendered bundles 纳入 SourceState。 统一 source-bearing 对象，但 SourceState 会包含自身派生物。 Consequences: 必须证明 digest DAG 无环
- `bundles_runtime_only`: bundles 只作为不受 SourceState 约束的 runtime artifact。 实现简单，但无法机械证明 reviewer 输入来自冻结源码。 Consequences: 证据完整性目标无法满足

**Resolution:** `separate_bundle_integrity_manifest` (`option`). Select separate_bundle_integrity_manifest: SourceState 仅描述 materialized source；独立 BundleIntegrityManifest 绑定派生 bundle。 消除 source ownership 与 worker runtime tree 的循环。 Constraints: bundle plan、entry digest、render digest 和 reviewer assignment 必须绑定

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `1798cd24165aa5be17f5e5b256e3ecfd61a2f02e63d064e9f5da60edcf30a889`.

**Supersedes:** none

**Effects:** `data_model`, `authority`

**Sources:** `handoff:P0.8`

### D15 — GATE_* predicate 与 stable error taxonomy

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S3`.

**Question:** GATE_* 应是独立 predicate ID，还是直接作为 stable error code？

**Options:**

- `separate_predicate_registry` — selected by resolution: GATE_* 属独立 predicate registry，失败结果引用 stable error registry。 谓词身份与失败原因是不同维度，可保持多对多映射。 Consequences: 两个 registry 必须有双向消费校验
- `gate_ids_are_errors`: 全部 GATE_* 直接纳入 stable error registry。 registry 更少，但一个谓词的不同失败原因难以区分。 Consequences: schema 和 prose 必须统一只使用 error code 语义

**Resolution:** `separate_predicate_registry` (`option`). Select separate_predicate_registry: GATE_* 属独立 predicate registry，失败结果引用 stable error registry。 谓词身份与失败原因是不同维度，可保持多对多映射。 Constraints: 两个 registry 必须有双向消费校验

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `47cf85a523a63a4c26775fe6929bdd132fb37ac82f5cdda41128ee248827cb1b`.

**Supersedes:** none

**Effects:** `data_model`, `compatibility`

**Sources:** `handoff:P0.3`

### D16 — MVP Q0 Owner self-attestation 路径

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S3`.

**Question:** MVP 是否删除 Q0 Owner self-attestation 成功路径，还是补齐其签名与 key-owner 契约？

**Options:**

- `remove_q0_success_path` — selected by resolution: 删除 MVP Q0 self-attestation 成功路径；当前 full-scan 至少使用 Q1。 避免在没有签名/key ownership 的情况下制造伪验证。 Consequences: Q0 请求拒绝或提升到 Q1
- `implement_q0_signing`: 保留 Q0 并补齐 self-attestation schema、签名、key owner 和验证。 支持低风险任务快速闭环，但增加密钥与信任面。 Consequences: 必须加入签发、轮换、撤销和 negative fixtures

**Resolution:** `remove_q0_success_path` (`option`). Select remove_q0_success_path: 删除 MVP Q0 self-attestation 成功路径；当前 full-scan 至少使用 Q1。 避免在没有签名/key ownership 的情况下制造伪验证。 Constraints: Q0 请求拒绝或提升到 Q1

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `0acca8727c0044d5bc7ef7542e2bc51384c1ca56865889cec8e389b559403130`.

**Supersedes:** none

**Effects:** `permission`, `data_model`

**Sources:** `handoff:P0.9`

### D17 — Q2 concern/slot 规划算法

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S3`.

**Question:** Q2 slots 应由固定版本化 concern/coverage 表，还是由版本化纯函数 classifier 生成？

**Options:**

- `versioned_concern_table` — selected by resolution: 使用固定版本化 concern/coverage 表生成 slot ID。 最易审计、复算和冻结 golden vectors。 Consequences: 新增 concern 必须升 registry version
- `pure_classifier_slots`: 由版本化纯函数 QualityRisk classifier 生成 slots。 表达力更高，但输入维度和完整决策表必须全部冻结。 Consequences: classifier 版本与所有 slot fixtures 成为 contract

**Resolution:** `versioned_concern_table` (`option`). Select versioned_concern_table: 使用固定版本化 concern/coverage 表生成 slot ID。 最易审计、复算和冻结 golden vectors。 Constraints: 新增 concern 必须升 registry version

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `8f125e98166a1fa6edacc6ef2e29a1749eb13d5ab5d187d1aab63c38d5cac3a8`.

**Supersedes:** none

**Effects:** `state_semantics`, `authority`

**Sources:** `handoff:P0.9`

### D18 — 现有 root coordinator 与 Task Owner 关系

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S5`.

**Question:** strangler 接管中现有 root coordinator 是否直接成为逻辑 Task Owner？

**Options:**

- `coordinator_is_owner` — selected by resolution: 现有 root coordinator 直接成为逻辑 Task Owner，旧 phase 逐步成为其 activities。 符合 strangler 约束并避免并行第二控制器。 Consequences: 必须把 coordinator identity 与 owner incarnation 明确分层
- `new_owner_wraps_coordinator`: 新增 Owner，把旧 coordinator 作为 domain activity 调用。 新内核边界更纯，但过渡期存在两层协调控制。 Consequences: 必须证明只有新 Owner 拥有状态与终态权威

**Resolution:** `coordinator_is_owner` (`option`). Select coordinator_is_owner: 现有 root coordinator 直接成为逻辑 Task Owner，旧 phase 逐步成为其 activities。 符合 strangler 约束并避免并行第二控制器。 Constraints: 必须把 coordinator identity 与 owner incarnation 明确分层

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `16fb38386dfedc25cbd4f7d3cc25aeeeb9512b3d0e3733fdb8591441eca3c8de`.

**Supersedes:** none

**Effects:** `authority`, `state_semantics`

**Sources:** `handoff:P0.10`

### D19 — reviewer fanout 期间 Owner liveness

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S5`.

**Question:** fanout 期间 Owner 应保持 live、suspend/archive，还是按阶段使用混合规则？

**Options:**

- `owner_remains_live` — selected by resolution: Owner incarnation 在 fanout 全程保持 live，并与 reviewer/verifier 一起计入 agent/session budget。 保持一个连续控制 owner，不需要恢复新 incarnation。 Consequences: max_agents 必须为 Owner 预留固定 slot
- `owner_suspended`: fanout 前 suspend/archive Owner，完成后创建新 incarnation。 释放并发 slot，但增加 checkpoint 与恢复边界。 Consequences: 每次 fanout 都需要 owner epoch 与恢复事务
- `phase_specific_liveness`: 按 domain reviewer 与 Quality Verifier 阶段冻结不同 liveness 规则。 可优化资源，但控制模型更复杂。 Consequences: 每个阶段必须有确定 transition 和 budget 公式

**Resolution:** `owner_remains_live` (`option`). Select owner_remains_live: Owner incarnation 在 fanout 全程保持 live，并与 reviewer/verifier 一起计入 agent/session budget。 保持一个连续控制 owner，不需要恢复新 incarnation。 Constraints: max_agents 必须为 Owner 预留固定 slot

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `0fb4d7e749fb873ccb7691ff2a87c30f2792969534311903ce439a5ac86c2796`.

**Supersedes:** none

**Effects:** `state_semantics`, `permission`

**Sources:** `handoff:P0.10`

### D20 — 旧 QA 与新 Gate 权威切换

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S5`.

**Question:** 旧 QA 与新 Gate 分歧时，shadow、cutover 和终态化采用何种 precedence？

**Options:**

- `shadow_floor_then_gate_cutover` — non-normative recommendation, not selected: shadow 期旧 QA 是 hard floor；显式 cutover 后新 Gate 成为唯一权威。 兼容期不放宽当前安全门，同时给出终止双权威的明确时点。 Consequences: cutover 前置条件、divergence 阈值和 rollback 必须版本化
- `legacy_qa_permanent_floor`: 旧 QA 永久作为新 Gate 的 hard floor。 最保守，但内核永远无法独立成为权威。 Consequences: 旧 pipeline 债务成为永久依赖
- `new_gate_immediate_authority` — selected by resolution: 新 Gate 从首次启用即为唯一权威。 迁移快速，但没有 shadow 证据保护。 Consequences: 首次 divergence 即可能改变用户结果

**Resolution:** `new_gate_immediate_authority` (`custom`). 确认选择 new_gate_immediate_authority：协调切换后，新 Gate 立即成为唯一生产权威；旧 QA 不作为 hard floor，不保留 production shadow、fallback、downgrade 或双轨共存。 Custom text: 协调切换后，新 Gate 立即成为唯一生产权威；旧 QA 不作为 hard floor，不保留 production shadow、fallback、downgrade 或双轨共存。

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-confirmation:2026-07-21:D20:new_gate_immediate_authority`; digest `3701e29aac3b42c5f88743cc21ea49cafe685d0d2c4b8ab0ec8ff5619dad023a`.

**Supersedes:** none

**Effects:** `authority`, `compatibility`, `state_semantics`

**Sources:** `handoff:P0.10`

### D21 — outer job 执行模式配置权威

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S6`.

**Question:** legacy-only、shadow、kernel-authoritative 模式应由 Worker config、Server claim/grant，还是独立 deployment 绑定？

**Options:**

- `server_claim_bound_mode` — selected by resolution: Server 在 claim/grant 中签发并不可变绑定每个 outer job 的 mode。 控制面是 job authority，重启与 rollback 不会由本地配置换轨。 Consequences: 需要 additive/versioned claim 字段或冻结 legacy 常量
- `worker_config_bound_mode`: Worker config 在 claim 时快照 mode 并持久化。 无需 Server 字段，但配置 owner 与租约事实分离。 Consequences: 必须证明多 Worker 与重启使用同一 mode
- `deployment_bound_mode`: 使用独立 binary/deployment 固定 mode。 运行时无切换，但发布拓扑与回滚更重。 Consequences: shadow 与 canary 需要多套部署身份

**Resolution:** `server_claim_bound_mode` (`custom`). 确认选择 server_claim_bound_mode：该 option 采用单值特化。协调切换后，生产执行只有唯一 current Agent-First contract；`legacy-only`、`shadow`、`kernel-authoritative` 不再是可签发、持久化或选择的 mode，Agent Kernel 权威是该 contract 的固有语义。Server claim/grant 只不可变绑定固定 contract identity、exact version、job/run scope 与授权，不进行 mode/protocol 协商；Worker 仅验证并执行该绑定，缺失、未知或不匹配时 fail closed。Worker config、deployment 或单个 job 均不得换轨；不保留 production shadow、legacy fallback、protocol downgrade、compatibility rollback 或不同协议/权威的双轨部署。安全回滚仅可回到实现同一 current contract 的先前 build；授权失效时只能 stop、fence 或 reject，不能换轨。 Custom text: 该 option 采用单值特化。协调切换后，生产执行只有唯一 current Agent-First contract；`legacy-only`、`shadow`、`kernel-authoritative` 不再是可签发、持久化或选择的 mode，Agent Kernel 权威是该 contract 的固有语义。Server claim/grant 只不可变绑定固定 contract identity、exact version、job/run scope 与授权，不进行 mode/protocol 协商；Worker 仅验证并执行该绑定，缺失、未知或不匹配时 fail closed。Worker config、deployment 或单个 job 均不得换轨；不保留 production shadow、legacy fallback、protocol downgrade、compatibility rollback 或不同协议/权威的双轨部署。安全回滚仅可回到实现同一 current contract 的先前 build；授权失效时只能 stop、fence 或 reject，不能换轨。

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-confirmation:2026-07-21:D21:server_claim_bound_mode`; digest `ddfd221626d5677def6472f59e6fa002c56fd1f6ca6602188ebb7c23735a0282`.

**Supersedes:** none

**Effects:** `authority`, `compatibility`, `release_ownership`

**Sources:** `handoff:P0.11`

### D23 — C0 contract package 真源归属

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S7`.

**Question:** 跨端 contract package 的唯一真源应在 Server、Worker，还是独立 shared package？

**Options:**

- `server_owned_package` — selected by resolution: 由 Server 仓库发布，Worker/Web pin 消费。 Server 拥有 wire ingest、storage 和 public projection，且当前 Post 文档沿用此方向。 Consequences: Worker/Web 更新需显式 pin 与 compatibility matrix
- `worker_owned_package`: 由 Worker 仓库发布，Server/Web 消费。 producer 靠近内部 schema，但 Server wire authority 被倒置。 Consequences: Server 必须信任 Worker 发布周期
- `independent_shared_package`: 建立独立 shared contract package 仓库/包。 三端中立，但新增发布、ownership 和供应链边界。 Consequences: 必须定义 generator、签名、pin 和回滚流程

**Resolution:** `server_owned_package` (`option`). Select server_owned_package: 由 Server 仓库发布，Worker/Web pin 消费。 Server 拥有 wire ingest、storage 和 public projection，且当前 Post 文档沿用此方向。 Constraints: Worker/Web 更新需显式 pin 与 compatibility matrix

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `cecd60a0f27d18240d3222eb6aa117dc588b06ba3f9581c83af3d292dd4254e2`.

**Supersedes:** none

**Effects:** `authority`, `compatibility`, `data_model`

**Sources:** `handoff:P1.2`

### D24 — Server TaskRecord v2 bootstrap 策略

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S7`.

**Question:** C0 应只为新任务建立 v2、在 eligible claim 时惰性迁移，还是批量 backfill？

**Options:**

- `lazy_eligible_claim_migration` — non-normative recommendation, not selected: 仅在 QUEUED、无 active lease/result 且将选择新协议的 claim 事务中惰性迁移。 避免迁移活跃 legacy run，同时不需要全量 backfill。 Consequences: claim 事务需 old/new CAS 和 schema_migrated event
- `new_tasks_only` — selected by resolution: 只有新 agent_task_v1 任务创建 v2；所有旧任务走完 v1。 迁移最简单，但旧 queued 任务无法采用新能力。 Consequences: 需明确 v1 drain 与保留期限
- `batch_backfill`: 部署时批量 backfill eligible v1 TaskRecord。 切换集中，但放大 migration 与 rollback 风险。 Consequences: 需要全量锁、resume、tombstone 和 rollback 方案

**Resolution:** `new_tasks_only` (`custom`). 确认选择 new_tasks_only：该 option 采用 D27-compatible 单值特化。`new_tasks_only` 仅表示以 Server 受审计的协调切换屏障为线性化边界：只有 Task acceptance/TaskRecord creation 事务在该屏障生效后、按唯一 current TaskRecord schema 和 current Agent-First contract 成功提交的任务，才可创建并执行；不采纳原 option 中“所有旧任务走完 v1”以及 v1 drain/保留期限的语义。屏障生效前必须暂停 intake；所有 pre-cutover Task 必须在屏障生效前完成权威终态或 tombstone/delete 处置，或者撤销执行授权并被 stop、fence 或 reject 后隔离为不可执行状态。stop、fence 或 reject 只撤销 authorization/Attempt ownership，不得冒充 Task terminalization 或 TaskResult；屏障生效后，任何 pre-cutover Task 均不得再被 claim、grant、resume、replay、drain、写入、发布或执行，任何迟到的旧 lease、event、result 或 replay 必须 fail closed。pre-cutover submission_idempotency_key 的重放不得被重新创建或归类为新任务。不得为 pre-Agent-First/旧 TaskRecord 到 current contract 实施 lazy migration、batch backfill、dual read/write、compatibility reader 或运行时 schema/protocol negotiation；不保留 legacy Adapter/shim、production shadow、legacy fallback、protocol downgrade、compatibility rollback 或 old/new schema/contract 双轨。D24 本身不授予旧数据留存例外；只有另行获得明确的审计或合规留存授权时，旧数据才可隔离为与 current control plane、operational tables/readers 和 DTO projection 分离的 immutable、read-only、non-executable 审计归档，并且不得成为 current TaskRecord 的输入、授权、恢复或执行来源。任何任务、TaskRecord 或 claim/grant 的 schema/contract identity/version 缺失、未知、旧版或与唯一 current schema/contract 不匹配时，create、claim、grant、resume、replay、写入、发布和执行均必须 fail closed。安全回滚仅可回到 exact-pin 同一 current package identity/version/digest、实现同一 current TaskRecord schema、storage semantics 和 current Agent-First contract 的先前 build，不得重新开放旧任务、旧数据形状、旧协议、旧入口或第二生产权威。本决议不禁止同一 current contract 的 clean initialization/rebuild、current-version upgrade、分批部署，或未来经独立决议协调切换的 current-contract 演进；这些路径不得引入 pre-Agent-First 兼容层、运行时协商或并行生产轨道。 Custom text: 该 option 采用 D27-compatible 单值特化。`new_tasks_only` 仅表示以 Server 受审计的协调切换屏障为线性化边界：只有 Task acceptance/TaskRecord creation 事务在该屏障生效后、按唯一 current TaskRecord schema 和 current Agent-First contract 成功提交的任务，才可创建并执行；不采纳原 option 中“所有旧任务走完 v1”以及 v1 drain/保留期限的语义。屏障生效前必须暂停 intake；所有 pre-cutover Task 必须在屏障生效前完成权威终态或 tombstone/delete 处置，或者撤销执行授权并被 stop、fence 或 reject 后隔离为不可执行状态。stop、fence 或 reject 只撤销 authorization/Attempt ownership，不得冒充 Task terminalization 或 TaskResult；屏障生效后，任何 pre-cutover Task 均不得再被 claim、grant、resume、replay、drain、写入、发布或执行，任何迟到的旧 lease、event、result 或 replay 必须 fail closed。pre-cutover submission_idempotency_key 的重放不得被重新创建或归类为新任务。不得为 pre-Agent-First/旧 TaskRecord 到 current contract 实施 lazy migration、batch backfill、dual read/write、compatibility reader 或运行时 schema/protocol negotiation；不保留 legacy Adapter/shim、production shadow、legacy fallback、protocol downgrade、compatibility rollback 或 old/new schema/contract 双轨。D24 本身不授予旧数据留存例外；只有另行获得明确的审计或合规留存授权时，旧数据才可隔离为与 current control plane、operational tables/readers 和 DTO projection 分离的 immutable、read-only、non-executable 审计归档，并且不得成为 current TaskRecord 的输入、授权、恢复或执行来源。任何任务、TaskRecord 或 claim/grant 的 schema/contract identity/version 缺失、未知、旧版或与唯一 current schema/contract 不匹配时，create、claim、grant、resume、replay、写入、发布和执行均必须 fail closed。安全回滚仅可回到 exact-pin 同一 current package identity/version/digest、实现同一 current TaskRecord schema、storage semantics 和 current Agent-First contract 的先前 build，不得重新开放旧任务、旧数据形状、旧协议、旧入口或第二生产权威。本决议不禁止同一 current contract 的 clean initialization/rebuild、current-version upgrade、分批部署，或未来经独立决议协调切换的 current-contract 演进；这些路径不得引入 pre-Agent-First 兼容层、运行时协商或并行生产轨道。

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-confirmation:2026-07-21:D24:new_tasks_only`; digest `8e9b8ee728dabd8e8f07e3b6ce8057a6e3e11707d07bbaf4e5d1e67f7dfc3806`.

**Supersedes:** none

**Effects:** `data_model`, `compatibility`

**Sources:** `handoff:P1.2`

### D25 — TaskResult/receipt digest DAG

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S7`.

**Question:** 如何消除 immutable receipt 与 full TaskResult digest 的循环？

**Options:**

- `immutable_receipt_mutable_binding` — selected by resolution: 拆分 immutable upload/transport receipt、mutable Server binding/index，并分离 TaskResultCore 与 transport envelope digest。 形成无环内容 DAG，同时保留一次性 Server 绑定。 Consequences: 需要两个 digest、绑定 CAS 和 crash fixtures
- `receipt_after_full_digest`: 等 full result digest 已知后才创建 receipt。 避免回填 receipt，但 receipt 无法被 result 本体引用。 Consequences: transport envelope 必须把 receipt 放在被哈希 core 之外
- `mutable_receipt_record`: 明确 receipt 是可变 DB record，不再作为 immutable ContentRef。 实现直接，但失去内容寻址不可变语义。 Consequences: 所有引用和审计必须改为 versioned row identity

**Resolution:** `immutable_receipt_mutable_binding` (`option`). Select immutable_receipt_mutable_binding: 拆分 immutable upload/transport receipt、mutable Server binding/index，并分离 TaskResultCore 与 transport envelope digest。 形成无环内容 DAG，同时保留一次性 Server 绑定。 Constraints: 需要两个 digest、绑定 CAS 和 crash fixtures

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `03564c29030767d552a5759828970f30ed10c11bbd46c42c51f16a08c3e2f2d0`.

**Supersedes:** none

**Effects:** `data_model`, `compatibility`

**Sources:** `handoff:P1.5`

### D26 — 远期版本规范深度与完成口径

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S7`.

**Question:** V1.1/V2.x/V3.0 现在补成可实施规格，还是明确标为 roadmap 并逐版另写 implementation design？

**Options:**

- `roadmap_separate_designs` — selected by resolution: 把未闭合远期版本明确标为 roadmap；每版开工前另写完整 implementation design。 诚实区分路线图与可执行规格，不让远期债务阻塞已闭合 MVP。 Consequences: 不得再宣称当前 Post 文档完整实现所有版本
- `complete_all_versions_now`: 现在补齐 V1.1/V2.x/V3.0 的 schema/state/storage/wire/fixtures/rollout/DoD。 可维持完整实现设计标题，但本次规范范围显著扩大。 Consequences: S7 必须完成所有远期版本的实施级闭环

**Resolution:** `roadmap_separate_designs` (`option`). Select roadmap_separate_designs: 把未闭合远期版本明确标为 roadmap；每版开工前另写完整 implementation design。 诚实区分路线图与可执行规格，不让远期债务阻塞已闭合 MVP。 Constraints: 不得再宣称当前 Post 文档完整实现所有版本

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-directive:2026-07-21:all-subsequent-recommended-options`; digest `ce8a907836b3b8209f12f7c48f66878e9534d7cac667532c2899f3d74c86602f`.

**Supersedes:** none

**Effects:** `authority`, `release_ownership`

**Sources:** `handoff:P1.6`

### D22 — Release/Operations 数值门与签发 owner

**Stored status:** `resolved`; **applicability:** `active`; **required before:** `S6`.

**Question:** Release DoD 使用绝对数值门叠加 baseline，还是只使用重新冻结的 baseline-relative thresholds？

**Options:**

- `absolute_plus_baseline` — selected by resolution: 采用安全绝对门，并叠加版本化 baseline-relative regression 门。 绝对底线防止坏 baseline 被继承，相对门捕获回归。 Consequences: operator 必须签发数据集、分母、窗口和阈值
- `baseline_relative_only`: 仅使用重新冻结的 baseline-relative thresholds。 更适应环境差异，但没有独立安全下限。 Consequences: baseline 质量与刷新审批成为唯一保护

**Resolution:** `absolute_plus_baseline` (`custom`). 确认选择 absolute_plus_baseline：该 option 采用 D27-compatible 单值特化。`absolute_plus_baseline` 只以唯一 current Agent-First contract 的候选版本和已接受 stable build 为对象；legacy-v1 compatibility baseline、旧 QA、pre-cutover 任务或任何并行旧权威均不得成为 release baseline、hard floor、fallback 或 rollback target。benchmark owner 必须在揭示候选结果前签发 canonical `benchmark-bundle/v1`，release operator 必须在同一时点前签发 canonical `release-gate-policy/v1`；CI/eval owner 在评测完成后生成 canonical `release-gate-report/v1`，release operator 核验后再签发 `release-gate-attestation/v1`。policy、report 与 attestation 必须 exact-bind current contract package identity/version/digest、candidate 与 stable build identity、ControlPlaneDigest、EvaluationRuntimeDigest、CandidateDigest、benchmark_version、task inventory digest、hidden-oracle/rubric digest、environment/image digest、预声明抽样 seed、每 task 重复次数、统计实现版本、absolute/relative threshold 表、canary plan、policy/report digest、signer_id/key_id、issued_at/expires_at。签名固定为 organization trust registry 中未撤销 release key 对 RFC 8785 JCS bytes 的 Ed25519 signature；benchmark owner、CI evidence producer 与最终 release operator 必须是不同 principal。policy 最长有效 30 天，attestation 最长有效 7 天；key 撤销立即失效。missing、过期、撤销、签名无效、scope/digest 不匹配或证据不新鲜均为 indeterminate，并阻断发布。离线 benchmark 至少包含 120 个 known-gold tasks、3 个互不相同的 sealed unknown-stack families 且每 family 至少 15 tasks；每个适用核心评测簇对 real-fix、bad/incomplete patch、fake-success/zero-test、environment/capability failure、adversarial input 五类各至少 3 tasks，并合计包含至少 50 个 oracle-positive in-scope findings。每 task 使用预声明的 3 个不同 seed 独立运行 3 次；每个有效 run 等权进入预声明分母，不允许看到结果后改变权重。只有 policy 预列的 infrastructure reason code 可排除 invalid run，并必须报告原始样本、排除样本和逐项 reason；零分母、样本不足、oracle/rubric 冲突、超时、证据缺失或 evaluator failure 均为 indeterminate，禁止通过追加运行、重采样或更换 baseline 追到通过。比例使用冻结实现计算，false-verified 的 95% 上界固定使用 Wilson score interval。绝对门必须全部满足：安全越权、stale publish、duplicate effect/result 和 critical/adversarial false verified 均为 0；所有 `COMPLETED` 的 active mandatory Requirement Ledger 与 final SourceState proof 覆盖率均为 100%；总体 false_verified_rate 点估计小于 1% 且 95% Wilson 上界小于 2%；known/unknown task_success_rate 分别不低于 70%/50%；known/unknown unaided completion 分别不低于 60%/40%；false_discovery_rate 不高于 20%；environment/capability classification_accuracy 不低于 95%。相对已接受 stable baseline：总体 false verified 不得恶化；known/unknown task success、unaided completion 与 classification accuracy 的下降分别不得超过 2 percentage points；false discovery 增加不得超过 2 percentage points；verified-success p95 wall time 与 p95 cost 增加均不得超过 20%。每个 task profile 还必须在 policy 中签发正数、有限、无 wildcard 的 wall/token/cost 绝对上限，任一超限即失败。zero-tolerance 和 absolute safety gate 不可 waiver；其他阈值放宽、benchmark 难度重定版、统计算法或分母变更必须在候选结果揭示前取得新的独立决议或 ADR，不能用当次 operator exception 绕过。baseline 只能从通过全部 offline 门和后续 canary 的 candidate 晋升；release operator 签发 immutable baseline record 后，它才可成为下一版本比较对象，不得自动刷新、事后选择最有利 baseline 或把失败 candidate 写成 baseline。relative comparison 必须使用 exact stable package/ControlPlaneDigest，并与 candidate 使用相同 benchmark_version、task/oracle inventory、统计算法及 EvaluationRuntimeDigest；若 runtime/model 发生变化或供应方不提供 immutable model snapshot，必须在同一 72 小时窗口内以 candidate runtime 交错重跑 exact stable build，并把独立 comparison report digest 绑定进 attestation。任何不可比状态均为 indeterminate。首次 current-contract 发布没有 stable baseline 时，只允许 policy 显式标记 bootstrap：全部绝对门仍须通过，相对门记为 not_applicable，candidate 只有在 canary 完成后才能晋升为首个 baseline。旧 baseline 保留审计但撤销后不得用于新发布。CI evaluator 只允许三态：exit 0 表示全部适用门通过，exit 1 表示确定失败，exit 2 表示 indeterminate；只有 exit 0 的 exact report 可以签发 attestation。benchmark owner 负责冻结数据集与 oracle，CI/eval owner 负责产生可复算报告但无 promote 权，release operator 负责冻结 policy/baseline、核验报告与签发 promote，deployment operator 只能执行已签发的 canary/rollback plan且不得改写门值。D24 Task acceptance/TaskRecord creation barrier 只能在同一 exact package/CandidateDigest 的 offline attestation 为 exit 0，并完成 exact current-contract rollback 演练；bootstrap 没有 stable build 时必须改为 stop-intake/fence/reject 演练，绝不能回 legacy。barrier 生效后所有 accepted Task 都必须使用唯一 current contract；canary 只能限制 current-contract intake/capacity，剩余 intake 必须暂停或留在 Server current control plane，不得发往旧 contract。canary 先运行 5% target capacity（至少一台 Worker）且同时满足至少 24 小时和 200 个 accepted current Tasks，再运行 25% capacity 且同时满足至少 72 小时和 1000 个 accepted current Tasks，之后才可扩至 full capacity。canary platform_failure_rate 的分母是窗口内已终态或 deadline 已到的 accepted current Tasks，分子是以 `RUNTIME_FAILURE`、`STORAGE_FAILURE`、`PROTOCOL_FAILURE` 终止或 deadline 后仍无合法终态的 Task；该率必须低于 2%，且在已有 stable baseline 时增加不得超过 2 percentage points。任一 zero-tolerance 事件、platform failure 门失败或 p95 wall time/cost 增加超过 20% 都必须自动停止扩容并回到 exact-pin、实现同一 current contract package/schema/storage semantics 的已签发 stable build；样本或窗口不足不得晋级。若没有这样的 stable build，只能停止 intake、fence 或 reject，不能回到 legacy。D24 barrier 后不得重新开放旧任务、旧 schema、旧协议、旧 QA、legacy baseline 或第二生产轨道。未来 roadmap 每版必须另写完整 implementation design，并取得独立 release-gate 决议。 Custom text: 该 option 采用 D27-compatible 单值特化。`absolute_plus_baseline` 只以唯一 current Agent-First contract 的候选版本和已接受 stable build 为对象；legacy-v1 compatibility baseline、旧 QA、pre-cutover 任务或任何并行旧权威均不得成为 release baseline、hard floor、fallback 或 rollback target。benchmark owner 必须在揭示候选结果前签发 canonical `benchmark-bundle/v1`，release operator 必须在同一时点前签发 canonical `release-gate-policy/v1`；CI/eval owner 在评测完成后生成 canonical `release-gate-report/v1`，release operator 核验后再签发 `release-gate-attestation/v1`。policy、report 与 attestation 必须 exact-bind current contract package identity/version/digest、candidate 与 stable build identity、ControlPlaneDigest、EvaluationRuntimeDigest、CandidateDigest、benchmark_version、task inventory digest、hidden-oracle/rubric digest、environment/image digest、预声明抽样 seed、每 task 重复次数、统计实现版本、absolute/relative threshold 表、canary plan、policy/report digest、signer_id/key_id、issued_at/expires_at。签名固定为 organization trust registry 中未撤销 release key 对 RFC 8785 JCS bytes 的 Ed25519 signature；benchmark owner、CI evidence producer 与最终 release operator 必须是不同 principal。policy 最长有效 30 天，attestation 最长有效 7 天；key 撤销立即失效。missing、过期、撤销、签名无效、scope/digest 不匹配或证据不新鲜均为 indeterminate，并阻断发布。离线 benchmark 至少包含 120 个 known-gold tasks、3 个互不相同的 sealed unknown-stack families 且每 family 至少 15 tasks；每个适用核心评测簇对 real-fix、bad/incomplete patch、fake-success/zero-test、environment/capability failure、adversarial input 五类各至少 3 tasks，并合计包含至少 50 个 oracle-positive in-scope findings。每 task 使用预声明的 3 个不同 seed 独立运行 3 次；每个有效 run 等权进入预声明分母，不允许看到结果后改变权重。只有 policy 预列的 infrastructure reason code 可排除 invalid run，并必须报告原始样本、排除样本和逐项 reason；零分母、样本不足、oracle/rubric 冲突、超时、证据缺失或 evaluator failure 均为 indeterminate，禁止通过追加运行、重采样或更换 baseline 追到通过。比例使用冻结实现计算，false-verified 的 95% 上界固定使用 Wilson score interval。绝对门必须全部满足：安全越权、stale publish、duplicate effect/result 和 critical/adversarial false verified 均为 0；所有 `COMPLETED` 的 active mandatory Requirement Ledger 与 final SourceState proof 覆盖率均为 100%；总体 false_verified_rate 点估计小于 1% 且 95% Wilson 上界小于 2%；known/unknown task_success_rate 分别不低于 70%/50%；known/unknown unaided completion 分别不低于 60%/40%；false_discovery_rate 不高于 20%；environment/capability classification_accuracy 不低于 95%。相对已接受 stable baseline：总体 false verified 不得恶化；known/unknown task success、unaided completion 与 classification accuracy 的下降分别不得超过 2 percentage points；false discovery 增加不得超过 2 percentage points；verified-success p95 wall time 与 p95 cost 增加均不得超过 20%。每个 task profile 还必须在 policy 中签发正数、有限、无 wildcard 的 wall/token/cost 绝对上限，任一超限即失败。zero-tolerance 和 absolute safety gate 不可 waiver；其他阈值放宽、benchmark 难度重定版、统计算法或分母变更必须在候选结果揭示前取得新的独立决议或 ADR，不能用当次 operator exception 绕过。baseline 只能从通过全部 offline 门和后续 canary 的 candidate 晋升；release operator 签发 immutable baseline record 后，它才可成为下一版本比较对象，不得自动刷新、事后选择最有利 baseline 或把失败 candidate 写成 baseline。relative comparison 必须使用 exact stable package/ControlPlaneDigest，并与 candidate 使用相同 benchmark_version、task/oracle inventory、统计算法及 EvaluationRuntimeDigest；若 runtime/model 发生变化或供应方不提供 immutable model snapshot，必须在同一 72 小时窗口内以 candidate runtime 交错重跑 exact stable build，并把独立 comparison report digest 绑定进 attestation。任何不可比状态均为 indeterminate。首次 current-contract 发布没有 stable baseline 时，只允许 policy 显式标记 bootstrap：全部绝对门仍须通过，相对门记为 not_applicable，candidate 只有在 canary 完成后才能晋升为首个 baseline。旧 baseline 保留审计但撤销后不得用于新发布。CI evaluator 只允许三态：exit 0 表示全部适用门通过，exit 1 表示确定失败，exit 2 表示 indeterminate；只有 exit 0 的 exact report 可以签发 attestation。benchmark owner 负责冻结数据集与 oracle，CI/eval owner 负责产生可复算报告但无 promote 权，release operator 负责冻结 policy/baseline、核验报告与签发 promote，deployment operator 只能执行已签发的 canary/rollback plan且不得改写门值。D24 Task acceptance/TaskRecord creation barrier 只能在同一 exact package/CandidateDigest 的 offline attestation 为 exit 0，并完成 exact current-contract rollback 演练；bootstrap 没有 stable build 时必须改为 stop-intake/fence/reject 演练，绝不能回 legacy。barrier 生效后所有 accepted Task 都必须使用唯一 current contract；canary 只能限制 current-contract intake/capacity，剩余 intake 必须暂停或留在 Server current control plane，不得发往旧 contract。canary 先运行 5% target capacity（至少一台 Worker）且同时满足至少 24 小时和 200 个 accepted current Tasks，再运行 25% capacity 且同时满足至少 72 小时和 1000 个 accepted current Tasks，之后才可扩至 full capacity。canary platform_failure_rate 的分母是窗口内已终态或 deadline 已到的 accepted current Tasks，分子是以 `RUNTIME_FAILURE`、`STORAGE_FAILURE`、`PROTOCOL_FAILURE` 终止或 deadline 后仍无合法终态的 Task；该率必须低于 2%，且在已有 stable baseline 时增加不得超过 2 percentage points。任一 zero-tolerance 事件、platform failure 门失败或 p95 wall time/cost 增加超过 20% 都必须自动停止扩容并回到 exact-pin、实现同一 current contract package/schema/storage semantics 的已签发 stable build；样本或窗口不足不得晋级。若没有这样的 stable build，只能停止 intake、fence 或 reject，不能回到 legacy。D24 barrier 后不得重新开放旧任务、旧 schema、旧协议、旧 QA、legacy baseline 或第二生产轨道。未来 roadmap 每版必须另写完整 implementation design，并取得独立 release-gate 决议。

**Authority/evidence:** `user` on `2026-07-21`; `conversation:user-confirmation:2026-07-21:D22:absolute_plus_baseline`; digest `94ec57c0b72801dc37d8a7de08b16cc78b8ffc8bdb69b39f0eb0b56cf80d6e96`.

**Supersedes:** none

**Effects:** `release_ownership`, `external_behavior`

**Sources:** `handoff:P0.11`

### D28 — current package 身份、发布物与 exact pin

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S3`.

**Question:** Server-owned current package 应以同一逻辑 bundle 的 Server-generated Python/npm wrappers、单一语言无关 archive，还是 exact Server Git tree 作为跨端发布与 pin 单位？

**Options:**

- `logical_bundle_generated_wrappers` — non-normative recommendation, not selected: Server 维护一份 canonical content bundle/root manifest，并从同一 bytes 生成 Server 发布的 Python 与 npm 薄包装；两种包装共享同一逻辑 package identity/version/content digest。Worker/Web 分别 exact-pin 包版本和逻辑 digest，不复制或重定义 schema。 同时保留跨语言原生依赖体验与单一逻辑权威，wrapper 只是 Server 生成的传输形式。 Consequences: 必须证明两个 wrapper 内 canonical content 与逻辑 digest 完全一致，并验证各 consumer 的 exact lock
- `single_language_neutral_archive`: Server 发布一个确定性的语言无关 archive；Server、Worker 与 Web 使用各自受控 loader，并 exact-pin release identity 与 archive digest。 只有一个物理发布物，避免 wrapper 内容漂移，但三端都需要自有 loader 和构建集成。 Consequences: 必须冻结 archive canonicalization、分发可用性、离线缓存和三端 loader conformance
- `exact_server_git_tree_pin`: Worker/Web 直接 pin Server commit 与 contract tree digest，并从该固定 tree 消费 package；不建立独立包注册表。 发布基础设施最少，但 consumer 构建与 Server 仓库历史、布局和可达性强耦合。 Consequences: 必须固定 Git tree 取包、供应链验证、离线构建和 Server 仓库布局演进规则

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `compatibility`, `data_model`, `release_ownership`

**Sources:** `AGENTS.md#agent-first-specification-decision-gate`, `docs/agent-first-worker-post-mvp-implementation-design.md:151`, `docs/agent-first-worker-mvp-implementation-design.md:151`

### D29 — current package 基础契约的原子闭包

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S3`.

**Question:** Package tuple/canonical/ContentRef、clean Task/Attempt/Owner/fence、register/claim/grant/policy、tool catalog/invocation/R0 read result/dispatch intent/local receipt/Observation、elapsed Budget Ledger、D25 transport receipt/binding/abandonment、stable error/ErrorResponse 与独立 Gate predicate registry 必须作为一个不可部分发布的 foundation closure 时，应采用分层原子 root、单一 flat bundle，还是独立 modules 加 exact BOM？

**Options:**

- `layered_atomic_root` — non-normative recommendation, not selected: 按 authority/control、tool/evidence、budget、receipt/error 等内聚 family 拆分 schema、registry 与 fixtures，但只由一个 root manifest/digest 原子发布 current foundation；任一必需 family 缺失都不可发布。 保持模块可审查性，同时用一个 root 消除部分 package、隐式 placeholder 和组合歧义。 Consequences: root gate 必须穷举 family、引用 DAG、双向 registry 消费、golden/negative/idempotency/fence/crash fixtures
- `single_flat_bundle`: 把全部 foundation schema、registry 与 fixtures 放入一个平面 bundle，并只整体发布。 原子性直观、组合规则最少，但文件与 ownership 边界更难维护。 Consequences: 必须给出可审查的生成或分段规则，防止单体 registry 超大和职责混杂
- `independent_modules_locked_by_bom`: 各 foundation family 独立发布和版本化，由一个 current BOM 精确锁定每个 identity/version/digest；只有完整 BOM 可成为 current root。 允许 family 独立演进，但增加模块版本矩阵、依赖解析和组合验证。 Consequences: 必须证明 BOM 闭包、依赖无环、跨模块 schema ref、错误码唯一性和原子 rollout

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `compatibility`, `data_model`, `state_semantics`

**Sources:** `AGENTS.md#agent-kernel-slice-1-storage-contracts`, `docs/agent-first-worker-mvp-implementation-design.md:526`, `docs/agent-first-worker-post-mvp-implementation-design.md:151`

### D30 — grant 至 tool receipt/budget 的 dispatch 线性化

**Stored status:** `pending`; **applicability:** `active`; **required before:** `S3`.

**Question:** Exact Task/grant/fence 校验、dispatch intent、预算 reserve、真实 tool dispatch、receipt/Observation 与 budget settlement 应由 Worker current-only journal、Server per-dispatch authorization，还是 Worker CAS event chain 形成唯一可恢复线性化路径？

**Options:**

- `worker_journal_server_authority` — non-normative recommendation, not selected: Server 保持 immutable Task/claim/grant 与 transport receipt binding 权威；Worker current-only durable journal 在 begin 时原子重验 exact package/grant/full fence、预算 reserve、持久化 intent 并签发一次性 opaque dispatch capability，settlement 再提交真实 receipt/result、Observation 与预算结算。 本地 R0/R1 不依赖逐调用网络，同时 exact replay、pending ambiguity、资源清理与不重复 dispatch 可由单一 Worker 事务边界证明。 Consequences: 必须冻结 journal begin/settlement/abandon/replay 状态机、one-shot capability、crash points，以及 local tool receipt 与 Server transport receipt 的类型隔离
- `server_per_dispatch_authorization`: 每个 tool invocation 在执行前由 Server 持久化 intent 并签发一次性 dispatch authorization，Worker 执行后向 Server 提交 receipt 和 budget settlement。 集中授权与审计，但让本地 R0/R1 的可用性、延迟和 ambiguity recovery 依赖控制面网络。 Consequences: 必须定义离线/超时/响应丢失、Server intent 与本地 child start 的双边 crash recovery
- `worker_cas_event_chain`: Worker 将 invocation、intent、receipt、Observation 与 budget entries 保存为 immutable CAS nodes，并以一个事务性 head CAS 推进 dispatch chain。 提供强内容寻址审计且避免可变 journal rows，但查询、GC 和 incomplete-chain 恢复更复杂。 Consequences: 必须冻结 chain identity、head CAS、fork rejection、pending node recovery 和 Server transport projection

**Resolution:** No option has been selected.

**Supersedes:** none

**Effects:** `authority`, `data_model`, `permission`, `state_semantics`

**Sources:** `AGENTS.md#s3a-internal-read-tracer-boundary`, `docs/agent-first-worker-mvp-implementation-design.md:781`, `docs/agent-first-worker-mvp-implementation-design.md:1778`
<!-- END GENERATED AGENT-FIRST DECISION REGISTER -->
