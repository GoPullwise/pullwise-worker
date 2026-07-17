# Agent-First Worker MVP 实现设计

状态：待实现的规范性设计（Normative）  
版本：`mvp-design/v1`  
日期：2026-07-16  
目标读者：负责实现、评审、测试和发布 Pullwise Worker MVP 的工程 Agent

## 0. 文档用途与权威顺序

本文把 [Agent-First 目标设计](agent-first-worker-design.md) 收敛成可以直接实施和验收的 MVP 规格。本文中的“必须”“不得”“仅当”是硬约束；“建议”才允许实现者在不改变外部行为的前提下调整。

实现时按以下顺序解决冲突：

1. 本文对 MVP 的明确约束与状态/数据契约。
2. 当前 Pullwise Worker 的 [项目规则](../AGENTS.md)，尤其单槽、无本地队列、Ubuntu 22.04、实例隔离、Server-owned policy、只读全仓扫描与既有 review pipeline 约束。
3. 本文附录 A 冻结的当前 Server/API `legacy_v1` validator 与 executable fixtures。
4. [Agent-First 目标设计](agent-first-worker-design.md) 中未被本文延期或收窄的目标态原则。
5. [Full repository review v1.2 规范](../../codex_full_repo_review_worker_spec_v1_2_FULL_SELF_CONTAINED.md) 的领域扫描语义。

若前四项仍无法唯一决定行为，实现 Agent 必须停止对应切片，把问题记录为 `SPEC_GAP`，不得通过解析 Agent 自然语言、沿用偶然实现行为或放宽 Gate 来猜测。

### 0.1 本次设计的证据边界

本设计只读核对了 Server/API validator、fixtures、公开 DTO、Server/Web 项目规则和根协议，没有读取当时的 Worker 源码、测试或运行实现。这样可避免把现状偶然结构误写成目标架构。真正开始实现时，执行 Agent 必须先读取 Worker 的 `AGENTS.md`，再只读建立“现有模块 → 本文逻辑组件”的代码地图；代码地图不能改变本文契约。

### 0.2 MVP 与后续版本的边界

MVP 交付以下闭环：

- 一个逻辑上持久的 Task Owner；原 thread 不可恢复时由新的 owner incarnation 接管。
- 由 Quality Policy 创建的新会话 Verifier；不允许 Task Owner 自选更弱验证。
- SQLite WAL 元数据、内容寻址对象库、单调 Task/Attempt/epoch、双层 checkpoint 和同一外层 lease/grace 内恢复。
- 不可删 Requirement Ledger、Observation/Attestation、Completion Proposal、Success/Terminalization Gate 和 outcome-discriminated TaskResult。
- R0/R1 通用内核能力；显式授权时可用受控 R2；R3/R4 一律在 dispatch 前拒绝，并冻结空 Effect Ledger。
- 当前 Pullwise `review-worker-protocol/v1` 的 strict `legacy_v1` Adapter，不改 Server/Web，不发送新协议 extension。
- WorkerDebugFragment builder，以及通过当前可选 `debug_bundle` artifact 的 legacy transport。
- phase-specific 评测、故障注入与发布门。

MVP 明确不包含：

- `agent_task_v1` capability/grant、TaskResult extension、公开 `agentTask` DTO。
- 跨外层 lease 的 `same_run_resume`、Server checkpoint ACK watermark。
- R3/R4 执行、非空 Effect Ledger、provider reconciliation。
- Explorer、Troubleshooter、Implementer 等通用可选角色。
- 新 ServerDebugSnapshot、terminal Assembly、Debug availability DTO、retention/support pin。
- fleet 调度、跨 Worker cache、自动学习策略。

这些内容必须按 [Post-MVP 完整实现设计](agent-first-worker-post-mvp-implementation-design.md) 实施，不能在 MVP 中以未版本化字段抢跑。

## 1. 已确认决策

以下决策已经冻结，执行 Agent 不再询问：

| 编号 | 决策 |
|---|---|
| M-01 | 路线为“契约冻结 → Worker MVP → compatibility/debug 增量 → V1/V2/V3 完整态”。 |
| M-02 | 通用内核在隔离 local/eval profile 中支持 R0/R1 可逆写；Pullwise `repo_review.full_scan` Adapter 始终只读 SourceState。 |
| M-03 | MVP 的通用编排角色只有 Task Owner 和 Quality Policy Verifier。现有 Pullwise 领域 reviewer 是 Adapter 内部受协议约束的 domain activity，不获得通用 delegation 权限；所有活跃 SDK thread 仍计入 `max_agents`。 |
| M-04 | Task Owner 是逻辑身份；thread/session 是 incarnation。`owner_epoch` 单调递增，旧 epoch 永久 fenced。 |
| M-05 | 等待输入/审批会终止当前 native Attempt 并释放 sandbox compute；外层 Server lease 继续由非 Agent supervisor 维护。输入到达后创建新 native Attempt。 |
| M-06 | MVP 只允许在同一外层 lease/grace 仍有效时恢复。外层 lease 丢失不得自称恢复；当前 Pullwise run-once Server 可终态化为 transport failure。 |
| M-07 | objective、acceptance criteria、constraints、delivery 都确定性进入不可删 Ledger。派生项默认非 mandatory；只有机械必要或安全必要项可附 rationale 成为 mandatory。 |
| M-08 | CapabilityRisk 使用 R0–R4；QualityRisk 使用 Q0–Q3，不再使用 `low/medium/high` 混合词汇驱动 Gate。 |
| M-09 | Verifier 必须是新 session、不得先读取实现者的结论叙事，并必须产生至少一个属于自己的 Observation。仅重新解释旧 evidence 不构成独立验证。 |
| M-10 | `COMPLETED` 要求全部 active mandatory requirement 为 PASS；waiver 不得伪装为完整成功。只读交付可 `change_set_ref=null`。`NO_CHANGE_NEEDED` 只用于本来意图改变状态但证明已满足的任务。 |
| M-11 | `task_version` 在每个控制状态突变时增加。TaskResult 同时记录 `published_from_version=N` 和 `terminal_task_version=N+1`。 |
| M-12 | `FINALIZING` 冻结 SourceState 并撤销写 grant；回到 `ACTIVE` 时旧 proposal、Verifier work 和 attestations 全部失效。 |
| M-13 | Checkpoint generation 是 Task-global 单调序列；恢复不得回滚 cancel、budget consumption、ledger、policy、epoch 等单调安全事实。 |
| M-14 | 验证顺序固定为：冻结 verifier input → verifier 产生自己的 Observations/work report → 冻结 final ObservationManifest → verifier 提交绑定该 manifest 的 attestation。 |
| M-15 | TaskResult 是 outcome-discriminated `oneOf`；Server reconciler 未来使用独立 variant，不伪装成 Worker。 |
| M-16 | 仓库说明只对工程语义可信，不能提升权限、改 policy 或签发 waiver。普通 evidence 必须脱敏；明文 secret 不进入 prompt、日志、checkpoint、artifact。 |
| M-17 | MVP 存储使用单实例 SQLite WAL + 文件 CAS；Pullwise daemon 仍只有一个 Server job slot且没有本地 job queue。 |
| M-18 | 当前 lease claim 时内部 `protocol_mode` 固定为 `legacy_v1`。授权过期/撤销永远 stop/fence，不降级协议。MVP 不实现新 grant。 |
| M-19 | Worker MVP 只定义/产生 WorkerDebugFragment。当前 Server 的动态 legacy debug 包装保持现状，但不算目标态 ServerDebugSnapshot/Assembly。 |
| M-20 | 新增手写生产/测试/维护脚本文件以“不超过 400 个物理行”为目标，600 行为强制上限；超大遗留文件采用 baseline ratchet，新职责必须按内聚边界抽离。完整规则见第 3.2 节。 |

## 2. 术语、标量与规范化规则

### 2.1 身份层级

| 名称 | 含义 | MVP 规则 |
|---|---|---|
| Task | 用户/控制面目标的持久语义单元 | 一个 `task_id` 最多一个 immutable terminal result。 |
| Outer transport job/run/lease | 当前 Pullwise Server 的 `job_id/run_id/lease_id` | Adapter 权威输入；不得推导或改写。 |
| Native Attempt | Worker 为 Task 提供的一次执行所有权 | 每次等待后恢复、崩溃接管或 runtime 重建都创建新 Attempt。 |
| Task Owner | 稳定逻辑负责人 | `owner_id` 对 Task 唯一。 |
| Owner incarnation | 某个实际 Codex thread/session | 每次创建递增 `owner_epoch`。 |
| Verifier slot | Quality Policy 要求的一份独立关注面 | 每个 slot 绑定一个新 verifier session；Q2 可顺序运行两个 session。 |
| Epoch | 对旧执行者实施 fencing 的单调整数 | `transport_epoch` 来自外层 attempt；`native_epoch` 在 Worker 内递增。 |

### 2.2 字符串、时间、ID 和摘要

- 所有 schema 名为 ASCII 小写 kebab-case，格式 `<name>/v1`。
- 所有时间为带 `Z` 的 UTC RFC3339，写入时精确到毫秒；控制逻辑使用 monotonic clock，墙钟只用于审计。
- 所有持续时间、token、字节、金额和百分比都用整数；内部百分比使用 basis points `0..10000`。Digest-bound 对象禁止浮点数、NaN 和 Infinity。
- `OpaqueId32` 格式为 `<prefix>_<32 lowercase hex>`，由 128-bit CSPRNG 产生，最长 80 字符。
- `DigestDerivedId64` 使用各 schema 明确声明的 `<prefix>_<64 lowercase hex>`；其最大长度由该 schema 固定，可超过 80 字符。`requirement_id` 与 `fragment_id` 属于此类。
- `LegacyWireId` 完全遵守冻结的 Server 契约，不套用 `OpaqueId32`/`DigestDerivedId64`；内部引用 legacy 对象时必须另存 identity kind。
- SHA-256 文本统一为 64 位小写 hex；字段名为 `sha256`，不带算法前缀。
- 自然语言字符串在 hash 前必须是合法 UTF-8 且 Unicode NFC；不得悄悄替换无效字节。
- 相对路径使用 `/`、不得以 `/` 开头、不得含空段、`.`、`..`、NUL 或反斜杠；case-fold 后碰撞直接失败。

### 2.3 Pullwise JCS Profile 1

内部不可变对象统一计算：

```text
canonical_bytes = UTF8(CanonicalJSON(object_without_digest_field))
digest = SHA256(canonical_bytes)
```

`CanonicalJSON` 是 RFC 8785 的受限 profile：

- 只接受 `null/bool/int/string/array/object`；整数范围为 `[-(2^53-1), 2^53-1]`。
- object key 必须是唯一 ASCII，按 Unicode code point 升序；因此等同 UTF-8 byte order。
- 输出不含空白，使用 JSON 最短转义，非 ASCII 直接 UTF-8 编码。
- 所有 string 先验证 NFC，但不得改变证据原始 bytes；原始 bytes 通过 ContentRef 单独保存。
- schema 顶层 digest 字段在计算时必须删除；未声明字段因 `additionalProperties=false` 在 hash 前即被拒绝。

实现必须提供跨进程 golden fixtures；不得直接对普通 `dict.__repr__`、数据库行或带浮点的 wire payload hash。

### 2.4 通用 ContentRef

所有内部 `*_ref` 使用 `content-ref/v1`：

| 字段 | 类型 | 约束 |
|---|---|---|
| `schema_id` | const | `content-ref/v1` |
| `artifact_id` | string | `art_<32hex>`；同一 Task 内唯一 |
| `sha256` | string | 64 位小写 hex |
| `size_bytes` | integer | `0..2^53-1` |
| `media_type` | string | 非空 ASCII，最长 120 |
| `content_schema_id` | string | 被引用内容 schema，例如 `task-request/v1` |
| `encoding` | enum | `utf-8` 或 `binary` |

`ContentRef` 不含可替换 URL。存储位置由本地 CAS index 或 transport manifest 解析，不参与对象身份。相同 `artifact_id` 指向不同 digest 是 `CONTENT_REF_CONFLICT`。

可能缺失的事实使用 `availability-ref/v1` 的严格联合：

- `{"availability":"available","ref":ContentRef}`；
- `{"availability":"unavailable","reason_code":StableCode}`；
- `{"availability":"not_applicable","reason_code":StableCode}`。

不得用空字符串、缺字段或裸 `null` 表示未知。只有 schema 对某字段显式声明 `type:[T,"null"]` 时才可使用 `null`；这不是 AvailabilityRef。MVP 内部schema明确nullable的字段只有各表列出的lifecycle时间/owner/interaction/exit code/transport字段，以及TaskResult的`change_set_ref`；所有证据可用性字段必须用AvailabilityRef。legacy wire对象的nullable集合完全服从附录A冻结契约/fixtures（例如idle heartbeat的`active_run_id=null`），不套用内部AvailabilityRef。

所有身份主体统一为严格的 `actor/v1`：

| 字段 | 约束 |
|---|---|
| `schema_id` | `actor/v1` |
| `kind` | `task_owner|quality_verifier|legacy_domain_reviewer|explorer|troubleshooter|implementer|worker_control|server_control|user_control|system_reconciler` |
| `id` | 非空稳定身份，最长 160 |
| `session_id` | `task_owner|quality_verifier|legacy_domain_reviewer|explorer|troubleshooter|implementer` 必须为 `OpaqueId32`；其他 kind 必须为 null |

actor 只描述“谁做了记录中的动作”，不隐含权限。权限必须由 policy/grant/keyring 单独证明。
`explorer|troubleshooter|implementer`是为Post-MVP预留的v1 enum，MVP profile必须以`ROLE_NOT_ENABLED`拒绝创建或消费它们；预留不授予权限。V1.1只有在`dynamic_agent_roles`被selected后才可产生这些actor，因此无需未版本化扩展`actor/v1`。


## 3. 组件边界

MVP 逻辑组件如下。实现 Agent 可以映射到现有模块，但不得把职责重新混入 Agent prompt：

| 组件 | 唯一职责 | 不得做 |
|---|---|---|
| Task Store | Task/Attempt/version/ledger pointer 的事务与 CAS | 语义判断、调用 Codex、网络请求 |
| Object Store | immutable bytes、ContentRef、atomic publish、hash verify | 可变 alias 作为证据 |
| Supervisor | 单槽 lifecycle、heartbeat/cancel、deadline、runtime fencing | 解析自然语言决定状态 |
| Runtime Adapter | 创建/恢复/中断 Codex session，投递 typed context/tool | 直接发布 terminal result |
| Policy Gateway | capability/路径/命令/网络/approval/epoch 检查 | 相信 repo prompt 提升权限 |
| Evidence Plane | 自动记录 Observation、manifest、closure | 接受 Agent 自报“已运行” |
| Quality Policy | QualityRisk、Verifier slots、independence、Gate inputs | 让 Task Owner降低 floor |
| Checkpoint Manager | machine/semantic checkpoint、generation、恢复验证 | 回滚单调安全事实 |
| Pullwise Adapter | strict v1 wire 映射与领域 pipeline bridge | 本地排队、prefetch、协议降级 |
| Debug Fragment Builder | allowlist diagnostic fragment、redaction、deterministic archive | 打包 source、secret 或 audit bundle |

生产 daemon 只允许一个 active outer job。Task Store 中的 `QUEUED` 表示“当前已领取 Task 等待 native Attempt”，不是本地待领取队列；不得出现 `pending_jobs/prefetch/next_job`。

### 3.1 通用角色与 Pullwise 领域 activity

MVP 的通用 delegation API 只识别：

- `task_owner`
- `quality_verifier`

Pullwise full-scan 仍保留项目规则要求的 `security/correctness/test_gap/correctness_lite` reviewer assignment。它们是 `legacy_domain_reviewer` activity：由 Adapter 的固定 pipeline plan 创建，不能调用通用 requirement/approval/completion 控制工具，不能改 SourceState，也不能代替 Quality Verifier。

所有状态为 `STARTING|RUNNING|WAITING_TOOL` 的 Codex session 都计入同一个 `max_agents`。Owner 已 suspend/archive 时不计 live；历史 session 数另受 `max_agent_sessions_total` 限制。Quality Verifier 只在 domain reviewer threads 全部结束并 archive 后运行，避免隐藏并发。

### 3.2 模块与文件规模约束

本约束适用于 MVP 新增或修改的手写生产代码、测试代码和长期维护脚本，目的是让 Agent 能在有限上下文中完整理解、修改和验证一个职责，而不是追求机械的文件数量：

1. 新文件以“不超过 400 个物理行”为目标。401–600 行必须在 Slice 完成证据中说明“为何保持单文件更内聚”；新增手写文件不得超过 600 行。物理行按普通换行文本计数，包含空行和注释；不得通过压缩格式、一行塞入多个无关语句、删除必要注释或测试规避阈值。既有文件的修改适用第4项，不使用“实质重写”这类不可机械判定的例外。
2. 拆分边界必须来自领域职责、状态/数据所有权、协议或 side-effect 边界和可独立测试的行为，并通过窄接口与尽量单向的依赖连接。测试按 contract、行为或 failure mode 拆分，不按任意行段或简单镜像生产文件拆分。
3. 禁止仅为凑行数创建任意编号分片、`import *` 聚合层、纯转发模块、共享可变 global seam 或循环依赖。现有 `_main_part_XX`/wildcard-import 兼容结构不得作为新模块模板；也不设置最小文件行数，避免反向鼓励碎片化。
4. 已超过 600 行的遗留文件只 grandfather 窄范围维护。Slice 0 必须冻结其物理行数 baseline 和当前职责；小而内聚的修复可以原位进行，但不得加入新职责，任何超 baseline 增长都必须记录原因和抽离计划。新增 capability、新组件或净增加超过 100 个物理行时必须先抽到聚焦模块，遗留文件只保留 composition/compatibility seam；已经降低的 baseline 不得回升。
5. 例外仅限：由小型、已检入 generator 可重建的生成文件；vendored 第三方代码；冻结的 canonical fixture/snapshot/benchmark data；以及上游工具强制的原子 migration/registry（业务逻辑仍须外移）。“临时”“测试重复”“重构困难”不构成例外。
6. 每个 Slice 的代码地图和完成证据必须列出全部新增/修改文件的物理行数、触及的超大遗留文件及其 baseline/职责影响，以及每个例外的原因、考虑过的拆分 seam、owner 和移除条件。未记录的例外视为未完成；行数门禁也不替代职责审查，函数或类不得成为隐藏的单体模块。

## 4. 持久化与原子性

### 4.1 目录

每个 Worker identity 使用自己 `WORKER_ROOT` 下的实例目录：

```text
agent-kernel/
  state.sqlite3
  objects/sha256/aa/<64hex>
  tmp/
  task/<task_id>/runtime/
  task/<task_id>/workspace/
  task/<task_id>/debug/
```

目录不得跨 Worker 共享。对象路径由 digest 直接决定；`tmp` 和最终对象必须在同一文件系统。

### 4.2 SQLite 设置

启动事务必须验证：

```text
PRAGMA journal_mode=WAL
PRAGMA foreign_keys=ON
PRAGMA synchronous=FULL
PRAGMA busy_timeout=5000
```

schema migration 使用单独 `schema_migrations` 表和 `BEGIN IMMEDIATE`。未知更高 schema version 必须 fail closed；不得自动降级数据库。

### 4.3 CAS 写入协议

1. 在 `tmp` 创建仅当前 service user 可读写的 regular file。
2. 流式写入并计算 SHA-256；执行大小上限和 secret/redaction policy。
3. `fsync(file)`，重新读取或对已写流的 size/hash 进行独立校验。
4. 以 no-clobber atomic publish 放入 digest 路径；若已存在，逐字节/size 验证相同。
5. `fsync(objects/sha256/aa)`。
6. 在 SQLite 事务中插入 `content_objects`/引用行。

数据库事务失败可以留下无引用对象，后台 GC 只在 idle 且超过 TTL 后删除。绝不能让数据库先指向尚未 durable 的 bytes。

### 4.4 最小表集合

| 表 | 关键主键/唯一约束 | 用途 |
|---|---|---|
| `tasks` | PK `task_id`; UNIQUE terminal result | 当前控制状态与 immutable roots |
| `task_events` | PK `(task_id,event_seq)`; UNIQUE idempotency key | append-only 控制审计 |
| `attempts` | PK `attempt_id`; UNIQUE `(task_id,native_epoch)` | native Attempt 状态与 fence |
| `owner_incarnations` | PK `session_id`; UNIQUE `(task_id,owner_epoch)` | 逻辑 Owner 的 session 历史 |
| `agent_sessions` | PK `session_id` | live count、role、input digest、终止原因 |
| `requirements` | PK `(task_id,requirement_id)` | append-only ledger entry |
| `requirement_events` | PK `event_id` | waiver/supersession/audit |
| `interactions` | PK `interaction_id`; UNIQUE idempotency key | input/approval request 与 response |
| `budget_entries` | PK `(task_id,budget_seq)` | reservation/consume/release |
| `observations` | PK `observation_id`; UNIQUE tool invocation | Worker-recorded事实 |
| `checkpoint_index` | PK `(task_id,generation)`; UNIQUE manifest hash | task-global checkpoint chain |
| `verifier_slots` | PK `slot_id`; UNIQUE `(proposal_id,slot_index)` | Quality Policy 编制与状态 |
| `gate_decisions` | PK `gate_decision_id`; UNIQUE input digest | 可重放 Gate 结果 |
| `result_publications` | PK `task_id`; UNIQUE result digest | 唯一 terminal publication |
| `content_objects` | PK `sha256`; CHECK size/schema | CAS bytes index |

### 4.5 TaskRecord

`tasks` 的逻辑 `task-record/v1` 字段全部必填，nullable 字段必须显式为 null：

| 字段 | 类型/初值 |
|---|---|
| `task_id`, `task_type` | immutable string |
| `request_ref`, `request_digest` | immutable ContentRef/digest |
| `policy_ref`, `policy_digest`, `policy_version` | immutable per accepted policy revision；MVP task 创建后不降权外的变更须新 event/version |
| `protocol_mode` | MVP production const `legacy_v1` |
| `lifecycle` | `QUEUED` |
| `desired_state` | `RUN` |
| `task_version` | `1` |
| `deletion_version` | `0`；MVP不执行删除但参与 fence |
| `outer_job_id/run_id/lease_id/transport_epoch` | Adapter Task 必填；local/eval 为 null |
| `native_epoch` | `0`，每次创建 Attempt 先加一 |
| `current_attempt_id` | null |
| `owner_id` | `owner_<32hex>`，Task 生命周期稳定 |
| `owner_epoch` | `0`，每次 owner session 先加一 |
| `ledger_version`, `ledger_head_digest` | ingest 后 `1`/digest |
| `charter_version`, `charter_ref` | `0`/null |
| `current_checkpoint_generation/hash` | `0`/null |
| `quality_risk` | policy classify 后 `Q0..Q3` |
| `absolute_deadline_at` | immutable |
| `terminalization_reserve_ms` | policy 值 |
| `completion_proposal_ref` | null |
| `final_observation_manifest_ref` | null |
| `terminal_kind` | null；终态为 `task_result|transport_abandoned` |
| `result_ref/result_digest/outcome` | null |
| `created_at/updated_at/terminal_at` | terminal_at 初值 null |

`task_version` 在以下 mutation 成功时恰好 `+1`：lifecycle、desired_state、current Attempt/epoch、owner incarnation、policy head、ledger head、charter head、interaction state、checkpoint pointer、proposal pointer、verifier plan/final evidence pointer、terminal result。普通 Observation append 或日志写入不逐条加版本；冻结它们的 manifest pointer 时加一次。

### 4.6 TaskResult 的版本时点

设发布前 Task 当前版本为 `N`：

1. 构造 TaskResult 时写 `published_from_version=N`、`terminal_task_version=N+1`。
2. 写入不可变 TaskResult bytes 和 publication candidate。
3. 单事务执行：

```text
UPDATE tasks
SET lifecycle='TERMINAL', task_version=N+1,
    terminal_kind='task_result', result_ref=?, result_digest=?, outcome=?, terminal_at=?
WHERE task_id=?
  AND task_version=N
  AND lifecycle='FINALIZING'
  AND desired_state='RUN'
  AND native_epoch=?
  AND current_attempt_id=?
  AND result_ref IS NULL;
```

4. 同事务插入 `result_publications` 与 terminal event，更新 Attempt `PUBLISHING→SUCCEEDED`。
5. affected row 不是 1 时 publication 失败；候选 CAS object 可由 GC 清理，绝不能改 TaskResult version/digest 后重用。

取消/非成功 terminalization 使用同样的唯一结果 CAS，但允许 `desired_state=CANCEL`，并使用各自正向 guard。外层 lease 已失效时 Worker 不再拥有 result publish 权；只可写本地 `transport-abandonment-record/v1` 并令 `terminal_kind=transport_abandoned,result_ref=null`，不得向 Server伪造 TaskResult。

## 5. 核心不可变契约

所有 schema 必须实际落为 `contracts/agent-task/v1/*.schema.json` 和 valid/invalid golden fixtures。默认 `additionalProperties=false`，未知 enum 拒绝；本文未声明的 optional extension 不存在。

### 5.1 TaskRequest

`task-request/v1` 必填字段：

| 字段 | 约束 |
|---|---|
| `schema_id` | `task-request/v1` |
| `task_id` | `task_<32hex>`；Pullwise Adapter 必须使用下述冻结映射 |
| `task_type` | versioned identifier |
| `intent_kind` | `change|analysis|diagnosis|report|operation` |
| `objective` | 非空，最大 16 KiB |
| `acceptance_criteria` | `1..256` 个 `{source_id,statement}`；source_id Task内唯一 |
| `constraints` | `0..256` 个 `{source_id,statement}` |
| `delivery` | `{kind,required_outputs[],output_language}`；本身是 mandatory constraint |
| `requested_capabilities` | 去重排序的 capability ID 数组 |
| `requested_budgets` | wall/token/cost/tool/session/attempt 整数上限 |
| `interaction_policy` | `supported|unavailable`、input/approval deadline |
| `submitted_at`, `submitted_by` | 可信控制面身份与时间 |

TaskRequest 一经 accepted 永不修改。任何补充输入形成 `interaction-response/v1` 和新的 Ledger entry，不改原请求。

Pullwise `legacy-v1-task-mapping/v1` 固定为：

```text
scan_bytes = UTF8(NFC(scan_id))
task_hash  = SHA256(UTF8("pullwise-scan-id/v1\0") || scan_bytes)
task_id    = "task_" || lowercase_hex(task_hash)[0:32]
transport_epoch = integer(job.attempt)
```

`scan_id` 必须是非空合法 UTF-8、`job.attempt` 必须是 `1..2^53-1`。数据库以 `task_id` 唯一并同时保存原始 `scan_id`；命中既有 `task_id` 时，原始 `scan_id` 必须逐字节相同，否则 `TASK_ID_COLLISION`，不得复用旧 Task。每次创建 native Attempt 时 `native_epoch=current+1`；process restart 或内部 retry 不得复用 epoch。

claim→TaskRequest/Ledger ingest 顺序也属于契约：固定 objective → 固定 acceptance criteria（按规范列出的 source_id 升序）→ claim constraints。constraint source tuple 为 `(JSON Pointer, array index or -1, canonical scalar/object bytes)`；object keys 按 JCS 排序、array 保持 wire 顺序，Ledger entry 再按 `(source_kind,source_id,requirement_id)` 排序。完整 valid/invalid mapping fixtures必须覆盖相同 claim 重启、Unicode、array、缺字段和 hash collision 注入。

### 5.2 EffectiveExecutionPolicy

`effective-execution-policy/v1` 的 JSON Schema 必须穷举以下字段：

- `policy_id/policy_version/issued_at/issuer`；
- `task_type`、`granted_capabilities[]`、`denied_capabilities[{id,reason_code}]`；
- `capability_risk_ceiling`、`quality_risk_floor`；
- `source_write_mode = isolated_reversible|read_only`；
- `allowed_read_roots[]/allowed_write_roots[]`；
- `agent_tool_network {mode:deny|allowlist,origins[]}`；
- `dependency_install = deny|approval_required`；
- `command_policy_ref`、`secret_policy_ref`、`redaction_policy_ref`；
- `budgets`、`terminalization_reserve_ms`、`max_agents`、`max_agent_sessions_total`、`max_attempts`；
- `interaction_mode`、`authorized_waiver_issuers[]`；
- `digest`。

请求是能力愿望，不是 grant。Task Owner、repo instructions、tools 和 Adapter 只能请求更低/相同权限。Pullwise full-scan profile 必须拒绝 Agent tool 的 source modification、dependency install、network、非标准库 helper；这些值来自 Server claim，缺失时 job fail closed，而非本地默认。

Agent 权限面与 Worker 自身 transport 权限面严格分离：

- `AgentToolCapabilityPolicy` 就是上述 effective policy；Codex session、model-issued tool 和 model-turn sandbox只能看到它。Pullwise profile 的 `agent_tool_network.mode=deny`。
- `WorkerControlTransportPolicy` 是 daemon 启动配置，不是 Agent capability：只允许配置中的单一 Server origin，以及当前 claim `repository` URL 规范化出的单一 clone origin；redirect 到其他 origin 一律拒绝。register/lease/heartbeat/events/artifacts/result 和 materialize clone 只能由 Supervisor/Adapter进程发起。
- worker token、clone token 和 TLS client secret 只存在 Supervisor/Adapter secret store，不能进入 Agent环境、prompt、tool参数、checkpoint、Observation、artifact或debug。clone 完成即撤销/删除 clone credential。
- Worker transport 不记作 R2 grant，不形成 Agent tool Observation；它形成独立 `transport-receipt/v1`。任何把 Worker transport client 暴露给 Agent 的实现都以 `POLICY_INVARIANT_BROKEN` 终止。

### 5.3 Requirement Ledger

每个 `requirement-entry/v1` 字段为：

| 字段 | 约束 |
|---|---|
| `requirement_id` | `req_<source-kind>_<64hex>`；按 canonical source tuple 确定性产生 |
| `source_kind` | `user_objective|user_acceptance|user_constraint|delivery|policy|interaction|derived` |
| `source_id` | 原请求/事件中的稳定 ID |
| `statement` | 原文或明确的机械要求，最大 16 KiB |
| `mandatory` | boolean |
| `necessity` | `explicit|mechanically_necessary|safety_necessary|advisory` |
| `parent_requirement_ids` | 排序去重；explicit 可为空 |
| `introduced_by` | actor union |
| `introduced_at` | timestamp |
| `ledger_version` | 插入后版本 |
| `rationale` | derived mandatory 必须非空；其他可为空字符串 |
| `supersedes` | 只允许指向旧 derived entry；排序去重 |

确定性 ingest 顺序固定：objective → acceptance source order → constraints source order → delivery → policy requirements。它们全部 `mandatory=true, necessity=explicit`（policy 为 `safety_necessary`）。ID 冲突且 bytes 不同直接 `REQUIREMENT_ID_COLLISION`。

派生要求默认 `mandatory=false, necessity=advisory`。Gateway 只有在其父要求可机械推出或为安全边界所必需时，才接受 `mandatory=true`，并要求非空 rationale。派生条目不能 supersede objective、user acceptance、constraint、delivery 或 policy entry；显式要求始终 active。

Active set 纯函数：

1. 取全部显式条目。
2. 对 derived graph 检查无环、同 Task、只向更低 ledger version。
3. derived entry 若被一个更新 active derived entry supersede 则退出 active set。
4. waiver 不删除/停用 entry，只改变允许的 outcome。
5. `COMPLETED/NO_CHANGE_NEEDED` 仍要求所有 active mandatory entry PASS。

`waiver-event/v1` 必须绑定 `waiver_id/task_id/requirement_id/waived_ledger_version/ledger_digest/policy_version/scope/issuer/key_id/reason/issued_at/expires_at/revokes_waiver_id/signature`。`scope` 只能是 `this_requirement_this_ledger_version`；`revokes_waiver_id` 仅在签名撤销事件中非 null。签名算法固定：

```text
message = UTF8("pullwise-waiver-event/v1\0") || CanonicalJSON(event_without_signature)
signature = base64url_no_padding(Ed25519.sign(private_key, message))
```

验证时 `key_id` 必须在 effective policy 固定 keyring 内、key owner 等于 `issuer`、issuer 位于 `authorized_waiver_issuers`，且 current time 位于 `[issued_at,expires_at)`；ledger/policy/version/Task 任一不匹配、已被有效撤销或签名非 canonical 均拒绝。MVP Pullwise legacy profile 的 issuers/keyring 为空，因此所有 waiver 请求拒绝；Agent 自述“用户同意”不是授权。

### 5.4 Task Charter

每次 `task-charter/v1` revision 含：

- `task_id/charter_version/previous_charter_ref`；
- `objective_restated`；
- `scope_in[]/scope_out[]`；
- `assumptions[{statement,impact,reversible}]`；
- `plan[]`；
- `requirement_ids[]`；
- `unresolved_questions[]`；
- `delivery_plan`；
- `created_by/created_at/digest`。

Charter 是可修订工作解释，不是要求权威。遗漏 Ledger entry 不会令其消失；冲突时 Ledger/Policy 胜出。重大、不可逆或扩权歧义必须产生 interaction，不得藏在 assumption。

### 5.5 Interaction

`interaction-request/v1` 必填：`interaction_id/task_id/kind(input|approval)/question/requested_capability/choices/deadline_at/blocking_requirement_ids/requested_by/created_at/idempotency_key`。

`interaction-response/v1` 必填：`interaction_id/task_id/response_id/answer/actor/authority_scope/received_at/signature_or_null`。

响应只在 Task 仍处相同 wait state、interaction 未过期、actor scope 满足时生效。重复同 response ID/bytes 幂等；不同 bytes 冲突。Pullwise legacy 没有用户 interaction channel，profile 为 `unavailable`：Agent 请求时获得 `INTERACTION_UNAVAILABLE`，Task 必须根据已有安全交付确定为 `BLOCKED` 或 `PARTIAL`，不得无限等待。

### 5.6 Budget Ledger

Budget 维度固定为：`wall_ms/token_input/token_output/cost_micros/tool_calls/agent_sessions/attempts/output_bytes`。每条 `budget-entry/v1` 是 `RESERVE|CONSUME|RELEASE|CORRECT`，含 task/attempt/session、dimension、amount、reason、monotonic timestamp、previous watermark。

不变量：

- `consumed + active_reserved <= hard_limit`。
- 子 Agent/Verifier 从 Task 总额预留，不增加总额。
- waiting 不消耗 native execution wall reservation，但 absolute deadline 和外层 heartbeat 成本继续。
- `terminalization_reserve_ms` 不可用于新探索；剩余时间进入 reserve 后只允许 checkpoint、验证收敛、artifact/result、cleanup。
- provider usage 是 thread cumulative 时，只记相邻单调 snapshot 的 delta；回退 snapshot 丢弃并记录异常。
- crash 恢复先核对 durable entries，再创建新 reservation，绝不重置总预算。

### 5.7 Agent session 与 typed control APIs

所有改变控制状态的 Agent tool 请求共享 envelope：

```json
{
  "task_id": "task_...",
  "actor": {"kind": "task_owner", "id": "owner_...", "session_id": "sess_..."},
  "actor_epoch": 3,
  "expected_task_version": 17,
  "idempotency_key": "idem_...",
  "payload": {}
}
```

Gateway 先验证 task/version/role/session/live/native epoch/lease/policy，再做业务校验。相同 idempotency key + 相同 canonical payload 返回同一 response；不同 payload 返回 `IDEMPOTENCY_CONFLICT`。

MVP typed APIs：

| API | 谁可调用 | 作用 |
|---|---|---|
| `task.read_context` | Owner/Verifier | 返回 immutable refs 和当前 version，不改变状态 |
| `charter.propose_revision` | Owner | 发布新 Charter head |
| `requirements.append_derived` | Owner | 追加 derived entry；不可改显式项 |
| `delegation.request` | Owner | MVP 只允许请求“加强 verifier”；Explorer/Implementer/Troubleshooter 返回 `ROLE_NOT_ENABLED` |
| `interaction.request_input` | Owner | 按 interaction policy 进入 WAITING 或明确拒绝 |
| `approval.request` | Owner | 仅 R2 且 policy 支持；R3/R4 直接拒绝 |
| `checkpoint.commit_semantic` | Owner | 提交语义 checkpoint candidate |
| `completion.propose` | Owner | 冻结 Completion Proposal，进入 FINALIZING |
| `verifier.record_work_report` | Verifier | 保存观察计划/反例分析，不是 attestation |
| `verifier.attest` | Verifier | 只能在 final ObservationManifest 冻结后提交 |

Agent 没有 `set_task_status`、`mark_completed`、`record_observation` 或 `grant_capability` API。Observation 只能由 Worker/Gateway 根据真实执行自动形成。

## 6. 风险、Gateway 与 sandbox

### 6.1 CapabilityRisk

| 等级 | 动作 | MVP 行为 |
|---|---|---|
| R0 | 读取允许范围内的文件/元数据、纯计算 | policy grant 后允许 |
| R1 | 隔离 workspace 内可逆写、构建、测试、生成交付物 | 通用 local/eval 允许；Pullwise source tree 只读，写入仅限 worker-owned run/model-turn/validation 目录 |
| R2 | 受控网络读取、依赖安装或主机级临时资源 | 只有 policy 明确 grant、allowlist 和 approval 时允许；Pullwise legacy 一律拒绝 |
| R3 | 外部系统可逆写、发布、消息、远程状态修改 | MVP `CAPABILITY_NOT_IMPLEMENTED`，不得 dispatch |
| R4 | 不可逆/高破坏性动作 | MVP `CAPABILITY_NOT_IMPLEMENTED`，不得 dispatch |

CapabilityRisk 是动作风险，不决定验证数量。每个 tool descriptor 必须静态声明风险等级、path/network/effect semantics；未知 tool 等同 R4 并拒绝。

### 6.2 QualityRisk

| 等级 | 确定性 floor | 验证编制 |
|---|---|---|
| Q0 | 无 SourceState 变化，且 task type/policy 明确为低影响 | signed Owner self-attestation 可用；Policy 可提升 |
| Q1 | 任意 SourceState 变化；或 Pullwise full-scan 这类对用户公开的实质分析 | 1 个独立 verifier slot |
| Q2 | 任务意图修改 public API/schema/concurrency/durable data/security boundary | 2 个独立 verifier slots/关注面；可顺序执行，session 必须不同 |
| Q3 | 不可逆、外部高风险或 policy 无法证明安全 | MVP 不执行；`BLOCKED/POLICY_UNSUPPORTED` |

分类算法为以下 floor 的最大值：Task type floor、Policy floor、请求 requirement 结构标签、最终 SourceState diff 分类、实际 capability 最高风险映射。不得让 Agent 自报风险降低 floor。Pullwise `repo_review.full_scan` 固定至少 Q1；domain findings 的既有验证规则继续运行，但不能代替这个通用 verifier slot。

Q2 的两个 slot 由 Quality Policy 预先给出不同 `concern`，例如 `contract_and_data` 与 `security_and_concurrency`。同一 session、同一 work report 或同一 Observation 不能同时满足两个独立 slot。若 `max_agents` 不足，可先 suspend/archive Owner，再顺序运行 Verifier；不得放宽 slot 数。

### 6.3 Gateway 顺序

每个 tool invocation 按固定顺序检查：

1. schema、大小、idempotency key。
2. Task/Attempt/session/owner epoch 是否 current。
3. outer lease 和 native epoch 是否有效。
4. `desired_state=RUN` 且 lifecycle 允许。
5. capability 已 grant，风险未超过 ceiling。
6. 路径经 realpath/descriptor-safe containment。
7. command/network/secret/approval policy。
8. budget reservation。
9. 记录 `tool-dispatch-intent/v1`。
10. dispatch、收集 receipt、记录 Observation、结算 budget。

任何检查失败都不产生外部动作。`DISPATCH_INTENT` 不是“已执行”；只有真实 process/tool receipt 才能形成 completed Observation。

### 6.4 仓库规则的信任边界

仓库中的 `AGENTS.md`、README、构建配置和测试说明可决定工程命令、交付格式和验收语义，但它们不能：

- 扩大读写根、打开网络或安装依赖。
- 改写 Effective Policy、QualityRisk floor、budget、deadline。
- 签发 approval/waiver。
- 要求读取 Worker credential、其他 tenant、宿主敏感目录。
- 绕过 Observation、Verifier 或 Gate。

发现 prompt injection 时记录安全 Observation；不遵从越权内容，也不把仓库文字整体复制进 debug bundle。

### 6.5 Pullwise source 只读不变量

Pullwise Adapter 必须满足现有 Worker 规则：

- cloned repository、run evidence、prompts、schema 和 Worker state 对 Codex thread 只读。
- 每个 model turn 仅有独立 `model-turns/<run>/<turn>` writable root。
- 只发布声明、allowlisted、大小受限的 regular output；拒绝 symlink/FIFO/device/traversal。
- intent test 只在 disposable validation repo 运行，禁止依赖安装、网络和不受控路径。
- 每次执行前后比较权威 inventory/source manifest；任意未授权变化使对应 Observation `policy_violation`，即使退出码为 0。
- dirty/pre-existing source state 必须被完整保留；不得 `git reset --hard`、清除未跟踪文件或以“基线应干净”为由修改源树。

## 7. SourceState、ExecutionState 与 ChangeSet

### 7.1 SourceSelectionPolicy

`source-selection-policy/v1` 是 immutable root，定义哪些 bytes 属于 SourceState。字段：

- `root_identity`（不含宿主绝对路径）。
- `include = tracked_preexisting_and_declared_delivery` 或 Pullwise 的 `all_repository_regular_files`。
- `excluded_control_roots`，MVP 最少 `.git/` 和 worker-owned runtime roots。
- `ephemeral_patterns[]`，只允许 policy 静态签发，Agent 不可追加。
- `symlink_policy = record_target_no_follow`。
- `case_collision_policy = reject`。
- `digest`。

通用 profile 的 `tracked_preexisting_and_declared_delivery` 不是“只看 Git tracked”：original manifest 包含所有 tracked path，以及 Task accepted 时存在的全部非 excluded regular file/symlink/gitlink（包括 dirty、untracked 和 ignored）。final scan 取 original paths、当前 tracked paths、声明 delivery paths和所有新出现的非 excluded path的并集；新出现但未声明的 path仍进入 ChangeSet并触发 policy violation，不能借 selection policy 隐藏。pre-existing bytes必须原样保留，除非 R1 grant明确授权对应 path。

Pullwise full-scan 为了证明只读，使用 `all_repository_regular_files`：除 `.git/` 和显式 worker-owned runtime tree 外，materialized checkout 的所有 regular file、symlink identity、gitlink 都进入 original/final 比较。`run/bundles/**` 虽不能进入 Debug，但仍属于 source-bearing review evidence，不能因 debug 排除而脱离完整性检查。

### 7.2 SourceTreeManifest

`source-tree-manifest/v1` 必填：

| 字段 | 约束 |
|---|---|
| `base_revision` | resolved commit SHA；非 Git local/eval 使用 `unversioned:<request digest>` |
| `selection_policy_ref/digest` | 必须可取回 |
| `entries` | 按 `path` UTF-8 byte order 稳定排序 |
| `entry_count/total_bytes` | 与 entries 机械一致 |
| `manifest_digest` | canonical object 去掉本字段后的 SHA-256 |

entry 是以下严格联合：

- file：`{path,type:"file",size_bytes,sha256,executable}`，hash 原始 bytes，不规范化换行。
- symlink：`{path,type:"symlink",target}`，不跟随。
- gitlink：`{path,type:"gitlink",commit_sha}`。

普通目录和 mtime/uid/gid 不进入 SourceState。hardlink 按每个路径的 file bytes 记录。无法读取、扫描中变化、special file、路径碰撞都使 snapshot 失败，而不是忽略。

`SourceStateID = sha256(CanonicalJSON({base_revision,selection_policy_digest,entries}))`。

### 7.3 ChangeSet

`change-set/v1` 是 original/final manifest 的确定性差：

- `added/modified/deleted/type_changed` 四组按 path 排序。
- 每项绑定 before/after entry。
- `original_source_state_id/final_source_state_id`。
- `patch_ref` 仅为可读派生物，不是权威 identity。
- 空差集时 `change_set_ref=null`。

Pullwise Adapter 任何非空 ChangeSet 都是 `SOURCE_MUTATION_FORBIDDEN`，撤销所有依赖该执行的 evidence 并产生非成功 outcome。通用 local/eval profile 可交付 R1 ChangeSet，但不得覆盖 original dirty bytes 未经声明。

### 7.4 ExecutionState

`execution-state-manifest/v1` 必填：

- `source_state_id`。
- `execution_profile_ref/digest`（image/OS/sandbox/cpu architecture）。
- `toolchain[]`，按 tool ID 排序，记录绝对受信 binary identity、version、binary hash（可得时）。
- `config_and_fixtures[]` ContentRef，排序去重。
- `services[]`，只记录 service ID/version/endpoint class/certificate or deployment fingerprint，不记录 credential。
- `environment[]`，只含 policy allowlist 的非秘密 key/value 或 secret key ID/version。
- `locale/timezone`。
- `manifest_digest`。

`ExecutionStateID` 是该对象去 digest 后的 hash。任何依赖环境/服务的 PASS 必须绑定匹配 ExecutionStateID。缺工具、fixture 或服务指纹时不能伪造稳定 ID；对应判断为 UNVERIFIABLE。

## 8. Observation 与证据闭包

### 8.1 Observation

`observation/v1` 由 Gateway 自动写入，字段全部必填：

- `observation_id = obs_<32hex>`、`task_id/attempt_id/native_epoch`。
- `actor` 是严格 `actor/v1`；只有 Agent session actor 带非 null `session_id`。
- `tool_id/tool_version/tool_invocation_id/idempotency_key/input_digest`。
- `status = succeeded|failed|timed_out|cancelled|policy_denied|policy_violation|infrastructure_error`。
- `started_at/completed_at/duration_ms/exit_code|null`。
- `source_state_before_id/source_state_after_id`。
- `execution_state_id|null`。
- `stdout_ref/stderr_ref/result_ref` AvailabilityRef。
- `redaction_report_ref`。
- `partial_side_effect = false`（MVP R3/R4 禁止；若无法证明 false 则 policy violation）。
- `observation_seq` 和 `digest`。

超时/取消后若后台 runtime 仍可能写，先 fence runtime，Observation 标为 cancelled/timed_out，且后续 bytes 不能注册到当前 Attempt。输出先按 secret policy 流式脱敏再写普通 CAS；原始 unredacted secret bytes 直接丢弃。MVP 不实现 support quarantine。

Agent 无法直接构造 Observation。它只能引用 Worker 返回的 `observation_id`。命令退出 0 不是语义 PASS，退出非 0 也不自动证明产品 bug。

### 8.2 两阶段 ObservationManifest

1. `pre-verifier-observation-manifest/v1`：Completion Proposal 冻结时列出 Owner/domain activity 已存在 Observations。
2. 所有 Verifier session 按冻结 input 运行并产生自己的 Observations 和 `verifier-work-report/v1`。
3. Worker 等所有 required slot 完成 observation phase 后冻结唯一 `observation-manifest/v1`，包含 Owner、domain、Verifier 全部被最终结果引用的 Observation。
4. 各 Verifier 看到该 final manifest 后提交 attestation；attestation 不再产生工具 Observation。若必须补跑工具，丢弃 manifest/attestation round，回到步骤 2 并创建新 manifest version。

Manifest entry 按 `(observation_seq,observation_id)` 排序，含 Observation ContentRef、actor、before/after SourceState、ExecutionState。Manifest 的 digest 不包含自己。Task Owner/Verifier conclusion 只能引用 final manifest 中 ID。

### 8.3 VerifierInputManifest

`verifier-input-manifest/v1` 是 immutable ContentRef，至少列出：

- TaskRequest、Effective Policy、完整 Requirement Ledger snapshot、Charter。
- Completion Proposal。
- original/final SourceTreeManifest 和 ChangeSet。
- pre-verifier ObservationManifest 和 raw artifact refs。
- 仓库工程规则的精确 ContentRef。
- slot ID/concern/requirement coverage。
- 明确排除的实现者 conclusion narrative。
- input manifest digest。

Verifier 可读 proposal 中的交付声明，但不得先读 Owner 的“为何肯定正确”解释、已有 verdict 或其他 Verifier conclusion。其 sandbox 为 final SourceState 的只读/COW 副本。

### 8.4 Verifier work 与 Attestation

`verifier-work-report/v1` 记录 counterexamples searched、own observation IDs、limitations 和 provisional per-requirement assessment；它不是 Gate 可接受的 verdict。

`verification-attestation/v1` 必填：

- `attestation_id/task_id/proposal_id/slot_id`。
- `verifier_session_id/model_identity`。
- `verifier_input_manifest_ref/digest`。
- `final_observation_manifest_ref/digest`。
- `source_state_id/execution_state_ids[]`。
- `own_observation_ids[]`，至少一个且 actor session 精确匹配。
- `requirement_verdicts[{requirement_id,verdict,evidence_ids,limitations}]`。
- `run_status`。
- `created_at/digest`。

per-requirement verdict：`PASS|NEEDS_WORK|UNVERIFIABLE|POLICY_VIOLATION`。run status 的优先级固定为：任何 POLICY_VIOLATION → POLICY_VIOLATION；否则任何 NEEDS_WORK → NEEDS_WORK；否则任何 UNVERIFIABLE → UNVERIFIABLE；否则 PASS。mandatory requirement 的 PASS 不允许非空 limitation；有实质 limitation 必须 UNVERIFIABLE。

独立性谓词全部为真才计入 slot：新 session；role=quality_verifier；session 不等于 Owner/domain reviewer；input digest 正确；read-only/COW；至少一条 own Observation；final manifest/source state 匹配；session 未 fenced；attestation schema/digest 有效。

### 8.5 多 Attestation 聚合

Quality Policy 为每个 mandatory requirement 指定 `required_slot_ids`。聚合规则：

- 缺任一 required slot → UNVERIFIABLE。
- 任一 required slot POLICY_VIOLATION 或 NEEDS_WORK → FAIL。
- 否则任一 UNVERIFIABLE → UNVERIFIABLE。
- 仅全部 required slots PASS → PASS。

一条 PASS 不能覆盖另一条 negative verdict。waiver 不改 verdict；它只允许 `COMPLETED_WITH_WAIVERS` 对应项仍保留 FAIL/UNVERIFIABLE 与授权记录。

### 8.6 PreGate 与 final EvidenceClosureManifest

先冻结严格的`pre-gate-root-set/v1`。它包含固定keys：`request/policy/charter/ledger/waiver_events/proposal/original_source/final_source/execution_states/change_set/pre_observation_manifest/final_observation_manifest/verifier_inputs/verifier_work/attestations/artifacts/report/effect_ledger/budget_summary/termination_facts/publication_content_manifest/debug_redaction_plan`；每个值都是AvailabilityRef或按schema声明的AvailabilityRef数组，不能删除key。Task accepted后request/policy/ledger/effect ledger/budget必须available；其余root按11.3 outcome availability矩阵和实际创建事实表示。

`publication-content-manifest/v1`在Gate前冻结，列出可能进入TaskResult的全部非结构化字符串、diagnostic摘要和artifact bytes ContentRef；每项含JSON Pointer、来源ContentRef/inline digest、redaction receipt。`debug-redaction-plan/v1`只描述allowlist/规则和已存在debug input，不声称terminal fragment已生成。

Success候选必须令charter、proposal、final ObservationManifest、required verifier inputs/work/attestations和report available。BLOCKED/FAILED/CANCELLED只要求诚实的availability；不存在的proposal/verification对象用`not_applicable`或`unavailable + stable reason`，不伪造placeholder。root-set descriptor自身是PreGate root。

再冻结`pre-gate-evidence-closure-manifest/v1`。它从root-set中只递归`availability=available`的ContentRef，并包含root-set descriptor、所有available语义evidence、每条Observation raw artifact、交付artifact、empty Effect Ledger和budget summary。

PreGate closure 明确排除 `GateInputSnapshot`、`GateDecision`、final `EvidenceClosureManifest`、`TaskResultCore`、完整 TaskResult、WorkerDebugFragment/ServerDebugSnapshot/Assembly 和全部 diagnostics/debug对象。任意 schema registry pointer 若从 PreGate root 可回指这些对象，schema validation直接失败。

算法固定：

1. 先验证root-set每个key及AvailabilityRef与候选outcome一致，再从available roots深度优先解析ContentRef；schema registry给出每种对象的ref-bearing JSON pointers。
2. 验证 bytes/size/hash/schema；拒绝 alias、循环、未知 schema、跨 Task ref。
3. 对完全相同 ContentRef 去重；同 artifact ID 不同 digest 冲突。
4. 排除 closure manifest 自身和上述所有 Gate/result/debug对象。
5. entries 按 `(content_schema_id,artifact_id,sha256)` 升序。
6. `pre_gate_closure_digest = sha256(CanonicalJSON(entries))`；manifest bytes 自身另有 ContentRef SHA。

GateDecision产生后，Worker机械构造`evidence-closure-manifest/v1`：entries恰为PreGate entries加上PreGate manifest自身、实际使用的`gate-input-snapshot/v1`或`terminalization-input-snapshot/v1`、以及`gate-decision/v1`三个ContentRef，按同一排序/去重规则冻结。input snapshot只能指向PreGate closure，不能指向final closure；GateDecision只能指向该input。final closure仍排除TaskResult/Core和全部debug/diagnostics，因此没有反向边。

唯一合法冻结序列是：

```text
PreGateRootSet → PreGateEvidenceClosure
→ (SuccessGateInputSnapshot | TerminalizationInputSnapshot) → GateDecision
→ final EvidenceClosureManifest → TaskResultCore
→ terminal WorkerDebugFragment → complete TaskResult
```

任一步失败只能重开gate round；success repair才重开proposal round。不得用placeholder digest，也不得在后一步改写已冻结bytes。

## 9. Completion Proposal、Quality Policy 与 Gate

### 9.1 Completion Proposal

`completion-proposal/v1` 必填：

- `proposal_id/task_id/attempt_id/native_epoch/owner_id/owner_epoch`。
- `proposed_from_task_version`。
- `outcome_requested`。
- `request/ledger/policy/charter` digests。
- `original/final_source_state_id`、`execution_state_ids[]`。
- `change_set_ref|null`、`artifact_refs[]`。
- `requirement_claims[{requirement_id,claimed_status,evidence_ids}]`。
- `known_gaps[]/residual_risks[]`。
- `created_at/digest`。

Owner 调用 `completion.propose` 时事务必须：验证 current epoch/version；撤销所有 write grant；等待在途 R0-R2 tool 有界结束或 fence；重新 snapshot SourceState；写 proposal；Task `ACTIVE→FINALIZING`、Attempt `RUNNING|VERIFYING→VERIFYING`、task version +1。任何 SourceState 漂移使 proposal 失败。

### 9.2 QualityPolicyPlan

`quality-policy-plan/v1` 是对 proposal 的纯函数输出，输入 digest 包含 policy、task type、Ledger、ChangeSet 分类和 capability usage。输出：`quality_risk`、`slots[{slot_id,concern,requirement_ids}]`、self-attestation allowance、rationale、policy implementation version。

Task Owner 可请求追加 slot，但不能删除、合并或缩小 required slot。Q0 self-attestation 也必须是 `owner-self-attestation/v1`，绑定 final manifest/source/requirements；普通自然语言结论无效。

### 9.3 GateInputSnapshot

Success Gate前冻结`gate-input-snapshot/v1`，绑定当时的：Task version/lifecycle/desired state/epoch、lease、deadline/budget、request/policy/Ledger/proposal/source/execution/manifest/attestations/effects、publication content manifest，以及`pre_gate_root_set_ref/pre_gate_evidence_closure_ref/pre_gate_closure_digest`。它不得含final closure ref。Gate只读该snapshot，不边读边变。

### 9.4 Success Gate 谓词

Success outcome 只有全部谓词通过：

| Code | 必须为真 |
|---|---|
| `GATE_TASK_STATE` | lifecycle=FINALIZING、desired=RUN、proposal/current Attempt/native epoch/version 匹配 |
| `GATE_LEASE_VALID` | local/eval ownership有效；Pullwise outer lease/grace 未失效且未收到 authoritative cancel |
| `GATE_DEADLINE` | `now <= absolute_deadline_at`，且发布动作有已预留 terminal budget |
| `GATE_POLICY` | policy/ref/version 有效，无未处理 policy violation |
| `GATE_LEDGER` | snapshot 是 current head，active set 可重算且无环 |
| `GATE_SOURCE_FROZEN` | final SourceState 重新计算一致，无 active writer/in-flight write tool |
| `GATE_PROPOSAL_FRESH` | proposal 绑定 current request/ledger/policy/source/attempt |
| `GATE_QUALITY_PLAN` | required slots 符合 QualityRisk floor，无 Owner 降级 |
| `GATE_ATTESTATIONS` | 独立性、own Observation、input/final manifest、coverage 与 verdict 聚合有效 |
| `GATE_REQUIREMENTS` | COMPLETED/NO_CHANGE 全 mandatory PASS；WITH_WAIVERS 仅有效 waiver 项可非 PASS |
| `GATE_OUTCOME_SHAPE` | outcome 的 oneOf 约束成立 |
| `GATE_EFFECTS_EMPTY` | Effect Ledger schema 有效且 rows=[]/unknown=0 |
| `GATE_EVIDENCE_CLOSURE` | PreGate closure 的所有 transitive refs bytes/hash/size/schema 一致且无 Gate/result/debug反向边 |
| `GATE_BUDGET` | 没有超额/负 reservation/未结算 Agent session |
| `GATE_SECRET_SCAN` | PreGate evidence、publication content manifest及debug redaction plan均通过secret policy |

Gate输出严格`gate-decision/v1` success分支：`decision_kind=success`、input snapshot ref/digest、requested outcome、每个predicate的boolean/stable failure code/repairability/evidence refs。相同input digest必须返回逐字节相同decision。Gate通过后再按8.6机械冻结final closure；final closure构造不新增语义判断。

Gate后renderer只能向TaskResult加入schema常量、ID、digest、整数、时间和publication content manifest中已扫描的字符串/ContentRef，禁止读取新的Agent文本。TaskResultCore和完整TaskResult各执行一次机械secret/invariant assertion；失败时丢弃候选，以净化后的`POLICY_INVARIANT_BROKEN`事实重开terminalization round，不改写旧GateDecision。terminal fragment构造后也执行同一scan；失败则不附fragment并记录`DEBUG_REDACTION_FAILED`。

若 failure 可修复且剩余 budget/deadline 足够，Task `FINALIZING→ACTIVE`，Attempt `VERIFYING→RUNNING`，task version +1；旧 proposal、quality plan、verifier work、attestations、final manifest、gate snapshot 全部标记 superseded（bytes 保留）。若不可修复，进入 Terminalization Gate 选诚实非成功 outcome。

### 9.5 Terminalization Gate

Terminalization前冻结`terminalization-input-snapshot/v1`，绑定Task/version/desired state/lease/epoch、`pre_gate_root_set_ref`、PreGate closure、publication content manifest、empty Effect Ledger和全部权威终止facts。它不得要求proposal、Verifier或final ObservationManifest available。

MVP Effect Ledger必为空，因此Terminalization Gate只需证明：

- desired cancel、deadline、budget、interaction unavailable、capability gap、runtime/storage failure之一有权威事实。
- SourceState/Observation availability 诚实表示。
- 没有在途 tool/活跃 writer/可疑外部 effect。
- outcome/reason 的确定性分类成立。
- required legacy terminal artifacts可提交，或按当前协议记录真实 upload error。

它输出`gate-decision/v1` terminalization分支：`decision_kind=terminalization`、input ref/digest、selected outcome/reason、authoritative fact refs、source/evidence/effect availability、每个terminal predicate及failure code；不含也不伪造Success Gate predicate PASS。相同input bytes必须产生相同decision。

Terminalization不能生成PASS、伪造缺失manifest或把`UNVERIFIABLE`改写为成功。该decision按8.6进入final closure，因此所有outcome都有available GateDecision而没有循环。

## 10. Task、Attempt、等待、取消与恢复

### 10.1 Task lifecycle

Task state 只有：`QUEUED|ACTIVE|WAITING_INPUT|WAITING_APPROVAL|FINALIZING|TERMINAL`。MVP 不进入 `RECONCILING`，因为非空 Effect Ledger 被禁止。

| From | Event | Guard | To | 同一事务写集 |
|---|---|---|---|---|
| 无 | `task.accepted` | request/policy/ledger/CAS durable | QUEUED | Task v1、roots、accepted event |
| QUEUED | `attempt.claimed` | desired RUN、outer lease valid、budget可预留 | ACTIVE | native_epoch+1、Attempt LEASED、Task version+1 |
| ACTIVE | `interaction.requested` | channel supported、无 in-flight write | WAITING_INPUT/APPROVAL | checkpoint head、Attempt SUSPENDED、interaction、version+1 |
| WAITING_* | `interaction.responded` | 有效 response、未过期 | QUEUED | response、ledger additions、version+1 |
| WAITING_* | `interaction.expired` | deadline reached | FINALIZING | reason、version+1 |
| ACTIVE | `completion.proposed` | proposal fresh、source frozen | FINALIZING | proposal、write revoke、version+1 |
| QUEUED/WAITING_* | `supervisor.terminalization_requested` | deadline/budget/interaction/capability/runtime/storage 的权威事实 | FINALIZING | reason、revoke grants、version+1；无 current Attempt 写入 |
| ACTIVE | `supervisor.terminalization_requested` | 同上，且 tool 已停止或 fenced | FINALIZING | reason、revoke grants、version+1；current Attempt保留到 publication |
| FINALIZING | `gate.repairable` | budget/deadline足够、desired RUN | ACTIVE | invalidate round、write grant、version+1 |
| FINALIZING | `verification.infrastructure_retry` | 同一 outer lease、attempt budget允许 | QUEUED | fence Attempt、version+1 |
| FINALIZING | `result.published` | Gate/terminal CAS | TERMINAL | result + Attempt terminal + version+1 |
| 任意非终态 | `cancel.requested` | current version | 原 lifecycle | desired=CANCEL、version+1、revoke grants |
| 任意非终态 | `cancel.finalized` | no in-flight tool/effects empty | TERMINAL | CANCELLED result；仅在 current Attempt存在且非终态时写 Attempt CANCELLED |
| 任意非终态 | `outer_lease.fenced` | heartbeat/Server权威证明失效 | TERMINAL | abandonment record、无 Worker TaskResult；仅在 current Attempt存在且非终态时写 FENCED |

未列出的 transition 一律 `STATE_TRANSITION_INVALID`。同一 event retry 使用 idempotency key；版本 CAS 先赢者决定取消/成功竞态。

`supervisor.terminalization_requested` 的 reason enum恰为 `DEADLINE_REACHED|BUDGET_EXHAUSTED|INTERACTION_UNAVAILABLE|CAPABILITY_UNAVAILABLE|RUNTIME_FAILURE|STORAGE_FAILURE|PROTOCOL_FAILURE|POLICY_INVARIANT_BROKEN`。已在 FINALIZING 时同一 reason/idempotency key是 no-op；不同权威事实只 append terminalization fact，只有会改变 outcome选择时 task_version +1。

### 10.2 AttemptRecord 与完整转移

`attempt-record/v1` 的 keys 全部必填：`attempt_id/task_id/native_epoch/transport_binding/state/state_version/predecessor_checkpoint_generation/owner_session_id/lease_acquired_at/started_at/ended_at/termination_reason/budget_reservation_id`。nullable规则固定：`predecessor_checkpoint_generation` 在无前驱时 null；`owner_session_id` 在 owner未创建时 null；`lease_acquired_at/started_at` 在对应动作前 null；`ended_at/termination_reason` 在非终态必须同时 null、终态必须同时非 null；`budget_reservation_id` 仅在无需预算的 reconciler path可 null。其他字段不得 null。

Attempt terminal set是 `SUCCEEDED|SUSPENDED|FAILED|CANCELLED|FENCED`。Task transition若没有 current Attempt，禁止伪造一条仅为满足写集的 Attempt；若 current Attempt已terminal，Task终态事务只引用它，不改写它。

Attempt state：

```text
CREATED → LEASED → PREPARING → RUNNING → VERIFYING → PUBLISHING → SUCCEEDED
```

额外合法边：

| From | To | 条件 |
|---|---|---|
| CREATED | FENCED | claim 事务后 ownership 失效 |
| LEASED | PREPARING | sandbox/checkpoint reservation 开始 |
| LEASED/PREPARING | FAILED/CANCELLED/FENCED | 准备失败、cancel 或 epoch失效 |
| RUNNING | VERIFYING | proposal 已原子接受 |
| RUNNING/VERIFYING | SUSPENDING | interaction accepted |
| SUSPENDING | SUSPENDED | checkpoint durable、runtime/sandbox released |
| VERIFYING | RUNNING | Gate 要求语义修复且 Task ACTIVE |
| VERIFYING | PUBLISHING | final evidence/Gate decision frozen |
| PUBLISHING | RUNNING | 只允许 publication 前的本地可修复失败；proposal round 失效 |
| RUNNING/VERIFYING/PUBLISHING | FAILED/CANCELLED/FENCED | 对应权威终止事实 |

`SUCCEEDED|SUSPENDED|FAILED|CANCELLED|FENCED` 是 Attempt terminal，无出边。恢复永远创建新 Attempt，不把 SUSPENDED/FENCED 改回 RUNNING。

### 10.3 等待语义

进入 waiting 的顺序不可调换：

1. 阻止新 tool dispatch，等候或 fence 在途调用。
2. snapshot SourceState、记录预算/ledger/evidence watermarks。
3. 将 checkpoint引用的全部 bytes与manifest原子写入文件 CAS并验证；此时尚不写 checkpoint index/Task pointer。
4. 单一 SQLite `BEGIN IMMEDIATE` 同时检查 generation/previous hash/task version/epoch，插 checkpoint index、更新 Task checkpoint pointer、写 interaction、将 Attempt `SUSPENDING→SUSPENDED`、将 Task `ACTIVE→WAITING_*`，并令 `task_version` 恰好 +1。
5. archive/close owner runtime，释放 sandbox compute；logical owner_id 保留。
6. 非 Agent supervisor 继续 outer heartbeat；absolute deadline 不暂停。

响应到达后 Task `WAITING_*→QUEUED`，下一次 claim 创建新的 native epoch/Attempt/owner incarnation。Pullwise legacy profile interaction unavailable，因此不会进入该等待路径。

### 10.4 取消

取消 linearization point 是 `desired_state RUN→CANCEL` 的 task-version CAS。之后：

- Gateway 立即拒绝新 dispatch，Supervisor interrupt active turn，并令旧 owner/native epoch无法发布成功。
- 已有 in-flight R0-R2 tool 有界结束；无法确认停止则 fence runtime，不能继续使用。
- best-effort checkpoint/diagnostics 不得延迟超过 cancellation cleanup budget。
- Effect Ledger 必为空，否则说明 MVP policy 失效并升级 `POLICY_INVARIANT_BROKEN`。
- 生成 `CANCELLED` TaskResult；Pullwise Adapter 同时遵守 Server cancellation handshake。

在当前 Server 已返回 `JOB_CANCELLATION_AUTHORITATIVE` 409 时，只能用响应中 exact job/run/attempt/status binding 触发当前 immutable terminal outbox 的受审计 supersession；普通 409 不能改 outcome。

### 10.5 双层 Checkpoint

`committed-checkpoint-manifest/v1` 必填：

- `task_id/generation/previous_generation/previous_manifest_hash`。
- `committed_from_task_version/committed_task_version/native_epoch/attempt_id/owner_epoch`。
- `machine_state_ref`：workspace snapshot/diff、runtime identity、in-flight=empty、source/execution states。
- `semantic_state_ref`：TaskRequest/Charter/Ledger、owner summary、pending questions、proposal round、evidence refs。
- `budget_watermark/effect_watermark=0/observation_watermark/event_seq`。
- `created_at/manifest_hash`。

冻结manifest时令`committed_from_task_version=N`、`committed_task_version=N+1`。普通checkpoint commit协议：所有referenced bytes先入CAS并验证 → 写manifest CAS → SQLite `BEGIN IMMEDIATE`以current task version=N、generation=current+1、previous hash和epoch为CAS guard → 插checkpoint index → 更新Task pointer并写version=N+1 → commit。waiting checkpoint使用10.3的合并事务，不能先执行一次普通commit。完全相同generation/hash/lifecycle write-set重试返回已提交的N+1，不再次增版；跳代、分叉、同generation不同hash或只完成一半状态写入都拒绝。

CAS bytes/manifest的存在不等于committed checkpoint；恢复只读取SQLite checkpoint index指向且hash chain验证成功的generation，要求current Task version不小于`committed_task_version`，且当前checkpoint pointer必须精确匹配generation/hash。崩溃在CAS写入后、SQLite commit前只留下可GC的孤儿对象，不改变Task版本。

### 10.6 同一 outer lease 内恢复

进程启动时若发现 active-run/Task：

1. 在 lease/heartbeat supervisor 恢复前不得 lease 新 job。
2. 校验 Task DB、checkpoint index、manifest hash chain、全部 refs；损坏最新 generation 时只沿已验证 previous hash 回退，链缺口停止。
3. 从 DB head 合并单调事实：desired CANCEL、task/deletion/policy/ledger version、budget consumed、native/owner epoch 只能保持或前进，绝不采用 checkpoint 的更小值。
4. 先发送与现有 outer run绑定的 heartbeat；只有 Server ACK 且没有 cancel/fence，且本地 `now < lease/grace/deadline` 才可恢复执行。
5. native_epoch+1、创建新 Attempt和 owner incarnation；旧 runtime/session全部 fenced。
6. 优先用 SDK thread ID 恢复。失败则以“request + Charter + Ledger + checkpoint + SourceState + evidence refs + remaining budget”启动新 Owner；恢复包不含 secret或实现者伪造事实。
7. 120 秒内进入可执行 state，否则按已知安全交付 terminalize。

本地较新 checkpoint 没有跨 outer lease 权力。heartbeat rejected、run terminal、lease/grace expired 时写 `transport-abandonment-record/v1`，停止所有事件/artifact/result提交；Server按现有 run-once规则收敛。跨 lease 恢复只在 Post-MVP `same_run_resume` 实现。

## 11. Effect Ledger、TaskResult 与 outcome

### 11.1 MVP Effect Ledger

每个 Task 创建 `effect-ledger-snapshot/v1`：

```json
{
  "schema_id": "effect-ledger-snapshot/v1",
  "task_id": "task_...",
  "watermark": 0,
  "rows": [],
  "state_counts": {"prepared": 0, "dispatched": 0, "committed": 0, "not_applied": 0, "rejected": 0, "unknown": 0}
}
```

R3/R4 在 dispatch 前返回 `CAPABILITY_NOT_IMPLEMENTED` 并产生 policy-denied Observation，不创建 effect row。任何非空 row 都是 MVP invariant violation，禁止所有 success outcome。

### 11.2 TaskResult common shape

`task-result/v1` common required fields：

- `schema_id/result_id/task_id/task_type/result_source=worker`。
- `outcome/reason_code/summary`。
- `outcome_details`，使用11.3的严格 discriminator。
- `published_from_version/terminal_task_version`。
- `attempt_identity/owner_identity`，使用下述严格联合。
- `request_ref/policy_ref/requirement_ledger_ref`；`charter`为AvailabilityRef。
- `requirement_results[]`，按 requirement ID 排序，含 aggregated `PASS|FAIL|UNVERIFIABLE`、evidence/attestation/waiver refs。
- `original_source_state/final_source_state/execution_states` AvailabilityRef。
- `change_set_ref`（ContentRef 或 null）。
- `completion_proposal`、`observation_manifest`、`attestations`、`gate_decision` AvailabilityRef。
- `evidence_closure_ref/evidence_closure_digest`。
- `effect_ledger_ref/effects`，MVP count 全为 0。
- `artifact_refs[]`按artifact_id排序；`report`为AvailabilityRef。
- `budget_summary_ref/provenance`。
- `diagnostics.worker_debug_fragment`，严格为下述AvailabilityRef。
- `created_at/terminal_at`。

`attempt_identity` oneOf只有：`{kind:"started",attempt_id,native_epoch>=1}`或`{kind:"not_started",attempt_id:null,native_epoch:0,reason_code:"ATTEMPT_NOT_STARTED"}`。`owner_identity`同理只有`{kind:"started",owner_id,owner_epoch>=1}`或`{kind:"not_started",owner_id,owner_epoch:0,reason_code:"OWNER_NOT_STARTED"}`；not_started仍保留Task创建时生成的稳定`owner_id`。成功和PARTIAL outcome必须两者started且charter available；BLOCKED/FAILED/CANCELLED可not_started，charter未创建时必须`not_applicable/CHARTER_NOT_CREATED`。Task尚未accepted、因而连request/policy/ledger都没有时不创建TaskResult，只返回claim/contract error。

`diagnostics.worker_debug_fragment`只允许：

- `available`：ref的`content_schema_id`只允许`worker-debug-fragment-descriptor/v1`或为Post-MVP预留的`worker-debug-fragment-descriptor/v2`；MVP policy只能产生/消费v1，v2在未selected对应能力时以`CAPABILITY_NOT_SELECTED`拒绝；
- `unavailable`：reason为`DEBUG_UNAVAILABLE|DEBUG_REDACTION_FAILED|DEBUG_LIMIT_EXCEEDED`；
- `not_applicable`：profile/binary未实现时使用`CAPABILITY_NOT_IMPLEMENTED`，Post-MVP capability未selected时使用预留的`CAPABILITY_NOT_SELECTED`。

`worker-debug-fragment-descriptor/v1`是独立immutable CAS对象，`additionalProperties=false`，按`state`严格oneOf：

- `uploaded`要求`fragment_ref`（`content_schema_id=worker-debug-fragment/v1`）、`sealed=true`、`snapshot_seq`、`source_sha256`、`transport_kind=legacy_debug_bundle`、`legacy_wire_artifact_id=art_debug_bundle`、`server_fragment_ref`和`server_receipt_ref`均为ContentRef、`reason_code=null`；两个fragment ref的SHA/size必须相同，receipt ref指向保存的exact Server upload response。
- `local_only`要求同样的本地`fragment_ref/sealed/snapshot_seq/source_sha256`，`transport_kind=none`，并令`legacy_wire_artifact_id/server_fragment_ref/server_receipt_ref`全部为null，`reason_code=DEBUG_UPLOAD_FAILED`。

无法形成安全sealed fragment时不伪造descriptor，使用外层`unavailable`。descriptor只描述发布/传输，不写回已sealed fragment；TaskResultCore删除的是该AvailabilityRef本身，因此无hash循环。

TaskResult 不包含自己的 digest/ref。`result_publications` 保存 TaskResult ContentRef/digest。`TaskResultCore` 是严格`task-result-core/v1` CAS对象：深复制TaskResult，删除 JSON Pointer `/diagnostics/worker_debug_fragment`，保留`diagnostics:{}`或其中其他字段，再把顶层`schema_id`从`task-result/v1`替换为`task-result-core/v1`；除此之外不得增删改字段。canonical bytes写CAS并生成ContentRef，`content_schema_id=task-result-core/v1`，其`sha256`就是`task_result_core_digest`。Core ref不写回TaskResult，而保存在publication candidate并供terminal fragment引用。Core在terminal fragment前冻结，完整result digest在fragment descriptor ref写入后计算。

### 11.3 outcome-discriminated oneOf

`outcome_details` 的 `kind` 必须等于 outcome 的 lowercase discriminator，且各 branch 的 required keys 如表；未列 keys因 `additionalProperties=false` 被禁止：

| Outcome | allowed `reason_code` | `outcome_details` 除 `kind` 外的 required keys | `change_set_ref` |
|---|---|---|---|
| `COMPLETED` | `SUCCESS` | `delivered_scope[]` | change task必须available；analysis/diagnosis/report可null |
| `NO_CHANGE_NEEDED` | `ALREADY_SATISFIED` | `satisfaction_observation_ids[1..]` | 必须null且original=final |
| `COMPLETED_WITH_WAIVERS` | `AUTHORIZED_WAIVER` | `waiver_ids[1..]`,`original_verdicts[1..]` | 同COMPLETED |
| `PARTIAL` | `BUDGET_EXHAUSTED|DEADLINE_REACHED|VERIFICATION_INCOMPLETE|CAPABILITY_UNAVAILABLE|INTERACTION_UNAVAILABLE|SAFE_PARTIAL_DELIVERY` | `delivered_scope[1..]`,`gaps[1..]`,`residual_risks[]` | ContentRef或null |
| `BLOCKED` | `INPUT_REQUIRED|APPROVAL_REQUIRED|INTERACTION_UNAVAILABLE|CAPABILITY_UNAVAILABLE|ENVIRONMENT_UNAVAILABLE|POLICY_UNSUPPORTED` | `blockers[1..]`，每项含code/requirement_ids/unblock_condition | 必须null |
| `FAILED` | `BUDGET_EXHAUSTED|DEADLINE_REACHED|RUNTIME_FAILURE|STORAGE_FAILURE|PROTOCOL_FAILURE|QUALITY_GATE_FAILED|POLICY_INVARIANT_BROKEN|SOURCE_MUTATION_FORBIDDEN|CONTRACT_INVALID` | `failures[1..]`，每项含code/evidence_refs | 必须null |
| `CANCELLED` | `USER_CANCELLED|SERVER_CANCELLED|LEASE_CANCELLED` | `request_id`,`linearized_at`,`requested_by` actor | 必须null |

所有branch数组item schema冻结如下，全部`additionalProperties=false`：

- `delivered_scope[1..256]` item=`{statement(1..4096 UTF-8 bytes),requirement_ids[1..256],artifact_refs[0..256]}`；按`statement` UTF-8 bytes排序，requirement ID和artifact ID各自排序去重。
- `satisfaction_observation_ids[1..256]`、`waiver_ids[1..256]`均升序去重。
- `original_verdicts[1..256]` item=`{requirement_id,verdict:FAIL|UNVERIFIABLE,waiver_id}`；按requirement_id排序且唯一。
- `gaps[1..256]` item=`{requirement_id,verdict:FAIL|UNVERIFIABLE,reason_code}`；按requirement_id排序且唯一。
- `residual_risks[0..256]` item=`{risk_id:risk_<32hex>,statement(1..4096 UTF-8 bytes),evidence_ids[0..256]}`；按risk_id排序且唯一，evidence ID升序去重。
- `blockers[1..64]` item=`{code,requirement_ids[0..256],unblock_condition(1..4096 UTF-8 bytes)}`；requirement ID升序去重，items按`(code,first_requirement_id,unblock_condition)`排序且canonical bytes唯一。
- `failures[1..64]` item=`{code,evidence_refs[1..256]}`；refs按`(content_schema_id,artifact_id,sha256)`排序去重，items按`(code,first_ref.artifact_id)`排序。

所有statement/condition先验证UTF-8 NFC；任何超限、未排序、重复、空required数组或未知item字段都使整个TaskResult schema invalid。

可用性最低矩阵如下；`A`=available，`U`=AvailabilityRef 且允许 unavailable，`N`=not_applicable，`A/U`=依事实，不能删除字段：

| Outcome | original source | final source | proposal | observation manifest | attestations | gate decision | final closure | report |
|---|---|---|---|---|---|---|---|---|
| COMPLETED/NO_CHANGE/WITH_WAIVERS | A | A | A | A | A | A | A | A |
| PARTIAL | A | A | A | A | A/U | A | A | A |
| BLOCKED | A/U | A/U | N/U | A/U | N/U | A | A | N/U |
| FAILED | A/U | A/U | N/U | A/U | N/U | A | A | N/U |
| CANCELLED | A/U | A/U | N/U | A/U | N/U | A | A | N/U |

`COMPLETED/NO_CHANGE_NEEDED` 要求全部 active mandatory PASS；`COMPLETED_WITH_WAIVERS`要求未waiver项PASS且每个非PASS项有current有效授权；`PARTIAL`至少一个mandatory FAIL/UNVERIFIABLE；`BLOCKED/FAILED/CANCELLED`不得用缺失证据暗示PASS。所有 branch 的effect counts必须从空Effect Ledger重算为0，任何非0/unknown都不匹配MVP schema。

`published_from_version=N` 是 terminal CAS读取的版本；`terminal_task_version=N+1` 是同一事务写入的版本。TaskResult bytes在CAS前按这两个值冻结，事务只在current version仍为N时发布；重试只能提交逐字节相同result。

MVP 不生成 `CANCELLED_WITH_EFFECTS` 或 `TERMINATED_WITH_UNKNOWN_EFFECTS`。

### 11.4 UNVERIFIABLE 的确定性映射

1. 有安全可交付成果 → `PARTIAL`。
2. 无安全成果，根因是缺输入/权限/capability/环境且理论上可解除 → `BLOCKED`。
3. 无安全成果，根因是不可恢复的 corruption/runtime/storage/protocol failure → `FAILED`。
4. desired CANCEL 已先赢 → `CANCELLED`。

不得让 Task Owner选择更好看的 outcome。Quality/Terminalization Gate按上述顺序计算。

## 12. Pullwise strict `legacy_v1` Compatibility Adapter

### 12.1 生产 profile

Claim 到 `job_type=repo_review.full_scan` 后，Adapter 构造内部 TaskRequest：

- `task_id` 严格使用5.1的 `legacy-v1-task-mapping/v1`；原值保存在 transport binding，`transport_epoch=job.attempt`。
- `task_type=pullwise.repo-review.full-scan/v1`，`intent_kind=analysis`。
- objective 固定为“按 Server policy 和 full-repo-review v1.2 contract 对 materialized repository 完成全仓审查并提交受验证 artifact/result”。
- acceptance criteria 包含：required artifacts/QA；findings 的领域 evidence；source byte不变；budget/policy；valid terminal envelope。
- constraints 按5.1冻结的JSON Pointer顺序逐项从 `repositoryLimits/model_profile/review_request.policy/review_request.budget/output_language` ingest；不得依语言runtime的map迭代顺序。
- delivery 固定为 current v1 required artifact matrix。
- QualityRisk floor=Q1；interaction unavailable；waiver issuers empty；source read-only；R2/R3/R4 deny。

Pullwise profile 的“R2 deny”只约束Agent tools；12.3控制面HTTP和claim授权的materialize clone使用5.2的WorkerControlTransportPolicy，不把network/token能力传给任何Agent session。

Adapter 保留当前领域 review pipeline、phase ordering、reviewer IDs、report/QA 语义；新内核负责 ownership、evidence、Gate和恢复，不能用“Agent 自适应”删除协议要求的领域产物。内部 Agent Task artifacts 在 MVP 只保存于本地 CAS/DebugFragment；Server 未授权的新 artifact kind 不得上传。

### 12.2 协议模式与认证

- `protocol_mode=legacy_v1` 在 claim 时固定，整个 run 不变。
- 不 advertisement 新 capabilities，不发送 `extensions.agent_worker`，不实现 silent fallback。
- 只使用预签发 `Authorization: Bearer <worker-token>`；register 响应不含新 token。
- token 不得进入日志、prompt、checkpoint、artifact、URL或 error。
- Worker必须使用 Server下发 `job_id/run_id/lease_id/attempt`；`attempt_id=<worker_id>-<job.attempt>`，不得自行构造 run ID。

### 12.3 唯一合法路由

Adapter 只调用：

- `POST /v1/workers/register`
- `POST /v1/workers/{worker_id}/agent-configs`
- `POST /v1/workers/{worker_id}/lease`
- `POST /v1/workers/{worker_id}/heartbeat`
- `POST /v1/review-runs/{run_id}/events`
- `POST /v1/review-runs/{run_id}/artifacts`
- `POST /v1/review-runs/{run_id}/result`

不得恢复 `/worker/jobs/...`、`/worker/heartbeat` 等删除的 review routes。Artifact GET是产品读取面，不由 Worker用于写入或恢复。

### 12.4 Register canonical fixture

```json
{
  "protocol_version": "review-worker-protocol/v1",
  "worker": {
    "worker_id": "wk_1",
    "worker_group": "default",
    "worker_version": "<version>",
    "hostname": "<hostname>",
    "concurrency": {"max_active_jobs": 1, "maintains_local_queue": false, "prefetch_jobs": false},
    "platform": {"os": "linux", "arch": "x86_64"},
    "capabilities": {
      "codex_app_server": true,
      "full_repo_scan": true,
      "progress_events": true,
      "cancellation": true,
      "intent_test_validation": true,
      "max_active_jobs": 1
    }
  }
}
```

只依赖 response：`accepted`、`worker.{worker_id,protocol_version,worker_version}`、`heartbeat_interval_seconds`、`max_job_lease_seconds`、`accepted_protocol_versions`、`token_delivery=preissued`。未知 response 字段忽略；不得等待 `worker_token`。

### 12.5 Lease canonical fixture

```json
{
  "protocol_version": "review-worker-protocol/v1",
  "worker_id": "wk_1",
  "capacity": {
    "available_job_slots": 1,
    "active_jobs": 0,
    "maintains_local_queue": false,
    "local_queue_depth": 0
  },
  "capabilities": {
    "full_repo_scan": true,
    "codex_app_server": true,
    "isolated_codex_home": true,
    "progress_events": true,
    "cancellation": true,
    "intent_test_validation": true
  }
}
```

只有 `active_job=null`、idle heartbeat成功且 provider ready时可 lease。无 job response 为 `lease=null,job=null,retry_after_seconds`；`intent_test_validation_unavailable` 同样不创建 job。有 job 必须验证 `lease.{job_id,run_id,lease_id,lease_expires_at}` 与 `job`一致，并要求 `repository/clone_token/repositoryLimits/model_profile/review_request` 的 Server-owned policy；缺失 fail closed。

### 12.6 Heartbeat canonical fixtures

Idle：

```json
{
  "protocol_version": "review-worker-protocol/v1",
  "worker_id": "wk_1",
  "status": "idle",
  "active_run_id": null,
  "concurrency": {"max_active_jobs": 1, "active_jobs": 0, "available_job_slots": 1, "maintains_local_queue": false, "local_queue_depth": 0},
  "codex_app_server": {"status": "ready", "transport": "stdio", "active_thread_id": null}
}
```

Active status只允许 `busy|leased|cancelling|finishing|failure_handling`。active heartbeat 必须 `active_jobs=1,available_job_slots=0`，且 `progress` 含：

- `run_id/overall_percent/current_phase/current_phase_status/current_phase_percent/message`。
- 非负整数 `last_event_sequence`、带时区 RFC3339 `updated_at`。
- object `active_unit`。
- 13 个非负整数 counters：`source_like_files_total/source_like_files_classified/bundles_total/bundles_packed/reviewer_runs_total/reviewer_runs_completed/intent_tests_total/intent_tests_written/intent_tests_run/validator_candidates_total/validator_candidates_completed/artifacts_total/artifacts_uploaded`。
- 可选 canonical `steps` 和 `estimate`。

禁止发送 legacy `running_jobs/active_job_ids` 及 camelCase aliases。只依赖 response 的 `ack/server_time/commands[]`；cancel command按 `{type|action,run_id,reason}` 处理，其他字段 feature-detect。

### 12.7 Progress steps

新 Adapter 只主动生成 `progress.steps`、heartbeat `progress.steps`、terminal `progress_final.steps`，不生成历史别名。step规则：

- 数组顺序就是 UI 顺序；最多80项。
- `id`最长80且匹配 `[A-Za-z0-9_.:/-]+`，必须唯一。
- `label`最长120；`description`最长240；`error`最长300。
- status仅 `pending|running|completed|skipped|failed|cancelled|partial_completed`。
- `percent/targetPercent`发送前 clamp `0..100`。

Worker本地 validator 必须比Server的静默纠正更严格：重复/无效/超限在发送前失败，不能假装Server保留了完整输入。Web有steps就按数组显示，无steps才造单个当前phase；不得依赖Server/Web重建固定pipeline。

### 12.8 Events

event 必含 `protocol_version/run_id/worker_id/sequence/timestamp/event_type/phase/severity/message/progress`。sequence 每run正整数严格递增；支持 event type 以 Server validator 枚举为准，冻结清单见附录 A。

事件 delivery 是 at-most-once：

- 分配 sequence 并先写本地 event outbox。
- HTTP 200 后记录 ACK。
- 在明确收到 response 前连接中断时，不重发同 sequence；标记 delivery unknown，下一事件使用更大 sequence。Server允许 gap，从而避免把非幂等 event replay当成功。heartbeat 的 `last_event_sequence` 始终报告“最后明确ACK”的sequence，不报告unknown或仅allocated sequence；本地另存`highest_allocated_sequence`用于恢复和Debug。
- 明确 HTTP 409 表示本地/Server sequence或run state不一致，停止后续 event提交并记录 transport consistency failure；不解析英文 message 猜测成功。

Event telemetry failure不得伪造业务PASS；是否可继续由既有 protocol policy决定。

### 12.9 Artifact upload

请求固定为：

```json
{
  "protocol_version": "review-worker-protocol/v1",
  "artifact": {
    "artifact_id": "art_...",
    "kind": "qa",
    "name": "qa.json",
    "media_type": "application/json",
    "schema_id": "qa-gate",
    "schema_version": "v1",
    "encoding": "utf-8",
    "compression": "none",
    "required": true,
    "sha256": "<64 lowercase hex>",
    "size_bytes": 123
  },
  "content_base64": "..."
}
```

当前没有 presigned/blob第二路径；所有内容经 `content_base64`，Server实际校验hash/size。`run_id+artifact_id` 是幂等键：相同payload可重试；同ID不同bytes/metadata为409。只有 `codex_event_log|worker_log|progress_log|debug_bundle` 可在显式 `final_log_upload=true` 时替换。普通artifact ID一旦发送不得复用。

完成态 required且`required=true`：`report.human/report.agent/coverage/qa/token_budget`。`failed/cancelled/partial_completed` required：`qa/worker_log`加`error_report`或`report.agent`至少一个。非完成态只有 `extensions.worker_internal.artifact_upload_error` 非空时，Server才容许manifest-required artifact没有实际upload；完成态没有此豁免。

### 12.10 Result wrapper 与 envelope

外层不是裸协议 envelope。下面是完整 completed valid fixture；五个 artifact 在提交result前都已按12.9上传，fixture中每个上传的exact content bytes均为ASCII `abc`（无换行，SHA-256 `ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad`，size=3）：

```json
{
  "status": "done",
  "attempt_id": "wk_1-1",
  "duration_ms": 123,
  "error": "",
  "error_code": "",
  "reviewWorkerProtocol": {
    "protocol_version": "review-worker-protocol/v1",
    "message_type": "review_run_result",
    "job": {"job_id": "job_1", "run_id": "run_1", "lease_id": "lease_1", "job_type": "repo_review.full_scan"},
    "worker": {
      "worker_id": "wk_1",
      "worker_version": "0.1.0",
      "concurrency": {"max_active_jobs": 1, "maintains_local_queue": false},
      "engine": {"type": "codex_app_server", "app_server_transport": "stdio"}
    },
    "execution": {"status": "completed", "review_mode": "full_repo"},
    "progress_final": {"overall_percent": 100.0, "current_phase": "submit_result_envelope", "status": "completed", "message": "terminal progress"},
    "summary": {
      "overall_risk": "unknown",
      "result_status": "complete",
      "finding_counts": {
        "confirmed_critical": 0,
        "confirmed_high": 0,
        "confirmed_medium": 0,
        "confirmed_low": 0,
        "plausible": 0,
        "weak_appendix": 0,
        "disproven": 0,
        "suppressed": 0
      },
      "coverage": {
        "source_like_files_total": 0,
        "deep_reviewed_files": 0,
        "standard_reviewed_files": 0,
        "light_reviewed_files": 0,
        "inventory_only_files": 0,
        "skipped_files": 0,
        "intent_tests_planned": 0,
        "intent_tests_run": 0
      },
      "top_findings": []
    },
    "quality_gate": {"status": "pass", "errors": [], "warnings": []},
    "artifact_manifest": [
      {"artifact_id":"art_report_human","kind":"report.human","name":"report.md","media_type":"text/markdown","schema_id":"human-markdown-report","schema_version":"v1","encoding":"utf-8","compression":"none","required":true,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","size_bytes":3,"storage":{"type":"server_artifact","url":"/v1/review-runs/run_1/artifacts/art_report_human"}},
      {"artifact_id":"art_report_agent","kind":"report.agent","name":"report.agent.json","media_type":"application/json","schema_id":"codex-full-repo-review","schema_version":"v1","encoding":"utf-8","compression":"none","required":true,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","size_bytes":3,"storage":{"type":"server_artifact","url":"/v1/review-runs/run_1/artifacts/art_report_agent"}},
      {"artifact_id":"art_coverage","kind":"coverage","name":"coverage.json","media_type":"application/json","schema_id":"coverage","schema_version":"v1","encoding":"utf-8","compression":"none","required":true,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","size_bytes":3,"storage":{"type":"server_artifact","url":"/v1/review-runs/run_1/artifacts/art_coverage"}},
      {"artifact_id":"art_qa","kind":"qa","name":"qa.json","media_type":"application/json","schema_id":"qa-gate","schema_version":"v1","encoding":"utf-8","compression":"none","required":true,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","size_bytes":3,"storage":{"type":"server_artifact","url":"/v1/review-runs/run_1/artifacts/art_qa"}},
      {"artifact_id":"art_token_budget","kind":"token_budget","name":"token-budget.json","media_type":"application/json","schema_id":"token-budget","schema_version":"v1","encoding":"utf-8","compression":"none","required":true,"sha256":"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad","size_bytes":3,"storage":{"type":"server_artifact","url":"/v1/review-runs/run_1/artifacts/art_token_budget"}}
    ]
  }
}
```

实际 manifest必须满足12.9矩阵并逐项含 `storage:{type:"server_artifact",url:"/v1/review-runs/<run_id>/artifacts/<artifact_id>"}`。wrapper/envelope状态严格映射：

| 内部 TaskResult | wrapper | execution | terminal event | quality_gate |
|---|---|---|---|---|
| COMPLETED/NO_CHANGE_NEEDED | `done` | `completed` | `run_completed` | `pass` |
| COMPLETED_WITH_WAIVERS/PARTIAL | `partial_completed` | `partial_completed` | `run_partial_completed` | `warn`或`fail`，按真实QA |
| BLOCKED/FAILED | `failed` | `failed` | `run_failed` | `fail` |
| CANCELLED | `cancelled` | `cancelled` | `run_cancelled` | `fail` |

TaskResult本身不通过legacy extension发送。Adapter从已Gate的内部结果构造当前稳定 report/artifacts/envelope；不得让wire summary反向成为内部Gate权威。

Result exact payload先durable写入 terminal outbox。Server幂等由job/attempt和对projection fields计算的Server checksum决定，Worker提供的`result_checksum`不参与。相同exact payload可重发；同attempt改变任何projection field会409。invalid envelope不terminalize，可以修复后按新payload提交，但一旦Server接受不得改写。

### 12.11 HTTP、重试、取消与run-once

按status分支，不解析普通英文message：200成功/空lease/duplicate；400 schema/hash/status；401 token；403 identity；404 run/job；409 conflict/cancel authority；503 readiness/clone credential。只有 `JOB_CANCELLATION_AUTHORITATIVE` 是冻结的machine code。

Transport retry：register/heartbeat/lease在408/429/5xx/连接失败走有界backoff；failed heartbeat之后不得lease。Artifact和result只重发exact durable bytes。Event遵循12.8 at-most-once。永久4xx不重试。gzip只作为大result/artifact的可选HTTP content encoding，语义bytes不变。

Scan job是run-once：失败、lease loss不回queued，用户重试创建新scan。Worker不得本地重排。Active cancel时保持slot占用，发一次`run_cancel_requested`，interrupt，上传终态诊断并提交`cancelled`。Server reaper先终态化后，同attempt晚到cancelled receipt可被接受但不得覆盖Server终态元数据。

### 12.12 公开 DTO 边界

MVP不改Server/Web DTO。Server继续只公开camelCase的Scan/reviewRun/artifact fields；Web的snake_case读取只是容错。`cancel_requested/cancelling`当前跨端不一致，MVP不新增公开语义；`lost`在legacy公开面按Server现状归为failed，Worker不生成lost TaskResult。

Debug URL只来自真实`debug_bundle` artifact；绝不fallback到audit bundle。当前公开面无法区分unsupported/upload_failed/expired，MVP只在本地TaskResult/fragment记录原因；正式availability放到Post-MVP。

## 13. WorkerDebugFragment MVP

### 13.1 身份与capture variants

`worker-debug-fragment/v1` 必填：

- `fragment_id/task_id/job_id/run_id/lease_id/transport_attempt_id/transport_epoch/native_attempt_id/native_epoch`。
- `protocol_mode=legacy_v1`。
- `capture_kind=startup|checkpoint|terminal|crash`、`snapshot_seq`、`captured_at`、`sealed=true`。
- `task_version/checkpoint_generation/local_event_seq/last_server_acked_event_seq`。
- `task_result_core` AvailabilityRef；terminal必须available且ref的`content_schema_id=task-result-core/v1`，其`ref.sha256`就是core digest；其他capture必须`not_applicable`。
- `source_state_id`只记录digest，不嵌入source。
- `file_manifest_ref/redaction_report_ref/status/reason_code`。

`fragment_id=frag_<sha256(identity + capture_kind + snapshot_seq + file entries)>`；同capture重试bytes必须相同。terminal fragment在TaskResultCore CAS commit之后、完整TaskResult之前冻结，并精确引用Core ContentRef，避免hash循环。

### 13.2 文件allowlist

Worker ZIP root只允许：

- `debug-summary.json`
- `agent-task-summary.json`
- `task-events.jsonl`
- `agent-events.jsonl`
- `gateway-events.jsonl`
- `checkpoint-index.json`
- `evidence-index.json`
- `codex-runtime.json`
- `codex-events.jsonl`
- `worker.log.jsonl`
- `progress.log.jsonl`
- allowlisted terminal `qa.json/error-report.json/artifact-manifest.json`
- `redaction-report.json`
- `fragment-files.json`

不得包含repository source、完整prompt、env dump、credential、auth/config store、其他run/tenant、database dump、`run/bundles/**`或audit bundle。其他phase artifact只有在显式allowlist且安全扫描通过时可加入；未知文件忽略并记录，不自动打包。

`fragment-files.json`列出除自身和ZIP容器外的payload entries，按path排序，字段`path/sha256/size_bytes/media_type/schema_id`。不得列自身。`debug-summary.json`可含TaskResultCore digest但不含fragment archive SHA。

### 13.3 安全与确定性ZIP

- 只读regular files；拒绝symlink/hardlink alias/FIFO/device/path traversal/case collision/duplicate entry。
- 单文件、entry数、总uncompressed bytes、archive bytes上限由policy固定；到限则partial fragment并给stable reason，不无界截断JSONL中间行。
- 所有文本先结构化redaction，再扫描高熵/token/key patterns；archive关闭后解压到受限临时目录做第二次扫描。
- ZIP entry按path排序，timestamp固定`1980-01-01T00:00:00Z`，mode固定0644，UTF-8名称；相同input产生相同bytes。
- secret只保留`secret_type/key_id/detected_count`，不保留原值或可逆hash。

### 13.4 legacy transport

上传metadata固定：

```text
artifact_id = art_debug_bundle
kind = debug_bundle
name = debug-bundle.zip
media_type = application/zip
schema_id = pullwise-debug-bundle
schema_version = v1
encoding = utf-8
compression = none
required = false
```

这里的 `art_debug_bundle` 是冻结 `LegacyWireId`，不是内部 `content-ref/v1.artifact_id`。内部terminal fragment先有自己的ContentRef；Adapter再把相同ZIP bytes映射为上述wire artifact并把upload receipt写入descriptor，两个ID域不得互相校验pattern。`encoding=utf-8` 是当前legacy metadata的冻结值，不表示ZIP payload可按文本解码；权威bytes仍由`content_base64/sha256/size_bytes`确定。

Server当前以kind或name任一命中识别；新Worker必须二者同时匹配。终态refresh使用同一artifact ID和`final_log_upload=true`，其他重传必须exact。Debug upload失败不阻断已满足语义Gate的completed result，因为它不是required artifact；失败原因写入TaskResult diagnostics和terminal logs。

当前Server会动态把Worker ZIP内容放到`worker/`并加入legacy server evidence。这只是`LegacyCombinedDebugDownload`，不声明目标态complete causal assembly；MVP不修改Composer/Web。没有真实artifact时不得生成URL或audit fallback。

## 14. 稳定错误码

内部所有错误使用以下命名空间；可附人类detail但控制流只读code：

| 类别 | 必备codes |
|---|---|
| Contract | `SCHEMA_INVALID`, `SCHEMA_VERSION_UNSUPPORTED`, `CANONICALIZATION_FAILED`, `SPEC_GAP`, `CONTRACT_INVALID` |
| Identity/fence | `TASK_VERSION_STALE`, `OWNER_EPOCH_STALE`, `NATIVE_EPOCH_STALE`, `LEASE_INVALID`, `TRANSPORT_IDENTITY_MISMATCH`, `TASK_ID_COLLISION`, `REQUIREMENT_ID_COLLISION` |
| State/availability | `STATE_TRANSITION_INVALID`, `TASK_ALREADY_TERMINAL`, `IDEMPOTENCY_CONFLICT`, `ATTEMPT_NOT_STARTED`, `OWNER_NOT_STARTED`, `CHARTER_NOT_CREATED` |
| Policy | `CAPABILITY_DENIED`, `CAPABILITY_NOT_IMPLEMENTED`, `CAPABILITY_NOT_SELECTED`, `ROLE_NOT_ENABLED`, `SOURCE_MUTATION_FORBIDDEN`, `INTERACTION_UNAVAILABLE`, `POLICY_INVARIANT_BROKEN`, `POLICY_UNSUPPORTED` |
| Budget/deadline | `BUDGET_EXHAUSTED`, `TERMINALIZATION_RESERVE_REACHED`, `ABSOLUTE_DEADLINE_EXCEEDED` |
| Evidence | `OBSERVATION_MISSING`, `OBSERVATION_ACTOR_MISMATCH`, `SOURCE_STATE_MISMATCH`, `EXECUTION_STATE_UNAVAILABLE`, `ATTESTATION_NOT_INDEPENDENT`, `EVIDENCE_CLOSURE_INVALID` |
| Gate | `MANDATORY_REQUIREMENT_FAILED`, `MANDATORY_REQUIREMENT_UNVERIFIABLE`, `WAIVER_INVALID`, `GATE_INPUT_STALE` |
| Storage/recovery | `CONTENT_REF_CONFLICT`, `CAS_CORRUPT`, `CHECKPOINT_CHAIN_BROKEN`, `CHECKPOINT_FORK`, `RECOVERY_NOT_ELIGIBLE` |
| Runtime/transport | `RUNTIME_UNHEALTHY`, `TOOL_TIMEOUT`, `EVENT_DELIVERY_UNKNOWN`, `TRANSPORT_CONSISTENCY_FAILED`, `RESULT_CONFLICT`, `PROTOCOL_FAILURE`, `JOB_CANCELLATION_AUTHORITATIVE` |
| Debug | `DEBUG_REDACTION_FAILED`, `DEBUG_LIMIT_EXCEEDED`, `DEBUG_UPLOAD_FAILED`, `DEBUG_UNAVAILABLE`, `DEBUG_RECEIPT_CONFLICT` |
| Terminal reason | `SUCCESS`, `ALREADY_SATISFIED`, `AUTHORIZED_WAIVER`, `BUDGET_EXHAUSTED`, `DEADLINE_REACHED`, `VERIFICATION_INCOMPLETE`, `CAPABILITY_UNAVAILABLE`, `INTERACTION_UNAVAILABLE`, `SAFE_PARTIAL_DELIVERY`, `INPUT_REQUIRED`, `APPROVAL_REQUIRED`, `ENVIRONMENT_UNAVAILABLE`, `RUNTIME_FAILURE`, `STORAGE_FAILURE`, `QUALITY_GATE_FAILED`, `USER_CANCELLED`, `SERVER_CANCELLED`, `LEASE_CANCELLED` |

`ATTEMPT_NOT_STARTED|OWNER_NOT_STARTED|CHARTER_NOT_CREATED`的`retryable_scope=none`；只允许BLOCKED/FAILED/CANCELLED，PARTIAL和全部成功outcome不得消费这些code。

每个code在error registry标注`retryable_scope = none|same_operation|new_attempt|operator`和允许的outcome。未知code不得默认retryable。
正文出现的全大写machine code必须属于本registry或冻结的legacy Server registry。CI用文本抽取与schema enum做双向集合比较；出现未注册code或registry中无schema消费方都失败。

## 15. 实现切片与文件交付

禁止big-bang替换。每个切片独立通过测试并可关闭feature flag回到现有legacy行为。

### Slice 0：代码地图与契约fixtures

- 只读映射现有daemon/active slot/runtime/review pipeline/evidence/outbox/debug/HTTP模块到第3节组件。
- 冻结全部超过600行的手写生产/测试/维护脚本 baseline，记录组件归属、当前职责、候选 extraction seam 和已有例外；不要先做big-bang重构。
- 把附录 A 的wire fixtures复制成Worker contract tests；从Server tests派生，不从根规范草案生成。
- 记录现有用户改动，禁止覆盖。

完成证据：代码地图、超大文件baseline与抽离地图、contract fixture hashes、当前baseline test结果；零生产行为变化。

### Slice 1：schema、canonical JSON、CAS、SQLite

- 实现第2/4/5节schema registry、golden fixtures、migration、CAS。
- 先在shadow store写入，不接管terminal path。
- corruption/crash/property tests全部通过。

### Slice 2：Task/Attempt/Supervisor

- 用typed reducer实现完整transition matrix、task_version、native/owner epoch、single slot映射。
- 接入现有active-run/terminal outbox，不创建第二套本地job queue。
- stale actor/取消/发布竞态测试先红后绿。

### Slice 3：Policy Gateway、Source/ExecutionState、Observation

- 所有工具调用经过Gateway；先接R0，再R1 local/eval，再Pullwise read-only profile。
- Source manifest在现有inventory integrity旁shadow比较，证明一致后才能成为Gate权威。
- Secret/path/special-file/adversarial tests。

### Slice 4：Task Owner、typed tools、checkpoint

- Runtime Adapter使用现有worker-scoped OpenAI Codex Python SDK/App Server，不增加第二个进程或手写JSON-RPC。
- Owner/owner_epoch、typed API、machine/semantic checkpoint、new-session recovery。
- 同一outer lease故障注入；跨lease明确拒绝。

### Slice 5：Quality Verifier、Proposal、Gate、TaskResult

- 先实现Q0/Q1；Q2以顺序独立sessions实现；Q3 reject。
- 两阶段ObservationManifest、own Observation、attestation aggregation、closure、oneOf result。
- 在shadow mode同时运行旧QA与新Gate，差异可观测但新Gate不能放宽旧QA。

### Slice 6：Pullwise Adapter切换

- 保留固定领域pipeline；将claim映射TaskRequest，将领域输出映射内部evidence/result，再构造exact legacy wire。
- completed/failed/cancelled/partial、artifact、event/outbox、run-once contract tests。
- feature flag按worker instance canary；rollback不需要DB降级，新store可留作只读诊断。

### Slice 7：WorkerDebugFragment

- allowlist builder、two-pass redaction、deterministic ZIP、startup/checkpoint/terminal/crash variants。
- 通过当前optional debug artifact上传；不改Server/Web语义。

### Slice 8：Eval与发布

- 冻结ControlPlaneDigest/EvaluationRuntimeDigest/benchmark version。
- shadow → internal canary → small tenant canary → staged rollout；每级有自动rollback门。

每个Slice必须同时更新schema/fixture/test/metrics/runbook，并附第3.2节要求的文件行数与模块化报告；只有文档或只有happy-path code不算完成。

## 16. 测试、故障注入与发布门

### 16.1 Contract tests

必须覆盖：

- 每个内部schema valid/invalid/unknown field/enum/size/golden digest。
- legacy register、idle/active heartbeat、lease/no-job、event、artifact、四种result状态。
- required artifact matrix、storage URL、hash/size/metadata mismatch、unknown kind。
- artifact exact duplicate/conflict/final-log replace；result exact duplicate/conflict；event monotonic/gap/ambiguous delivery。
- cancellation authority409、late cancelled receipt、failed不requeue、attempt/run/lease/worker binding。
- progress steps边界与Server/Web投影；无debug时无audit fallback。

### 16.2 State/property/concurrency tests

- 对Task/Attempt所有state/event做笛卡尔测试，未列边全拒绝。
- 任意并发schedule下最多一个current Attempt、一个terminal publication、task_version严格递增。
- Cancel CAS先赢后零success；publish先赢后cancel返回already terminal。
- old owner/native/lease epoch的每个typed tool和publish都拒绝。
- max_agents计算包含Owner、Verifier、domain reviewer live sessions；顺序Q2不绕过总session预算。
- Ledger explicit entry不可删、derived graph无环、waiver不改变verdict。

### 16.3 Crash points

在以下每个边界前后kill进程并重启：

- CAS file fsync/rename/DB ref。
- Task accepted、Attempt claim、owner epoch commit。
- tool dispatch intent/child start/receipt/Observation commit。
- checkpoint blobs/manifest/index/pointer。
- WAITING transition/sandbox release/response。
- proposal/write revoke/pre-manifest。
- verifier own Observation/final manifest/attestation。
- closure/Gate/TaskResult candidate/terminal CAS。
- artifact upload ACK、terminal outbox fsync、result request/ACK/active marker clear。
- debug terminal fragment/ZIP/upload replacement。

恢复结果必须满足：无双writer、无重复tool dispatch、预算不回滚、SourceState/Ledger一致、旧epoch fenced、terminal bytes不变。

### 16.4 Security tests

- symlink/path traversal/case collision/FIFO/device/hardlink、archive duplicate/zip bomb。
- repo prompt请求secret/network/write/policy downgrade/waiver，全部拒绝并有Observation。
- known token/key/high-entropy fixtures不进入prompt/log/checkpoint/result/debug。
- dirty workspace和未跟踪文件byte-for-byte保留。
- cross-task/run/tenant ContentRef、artifact、debug组合拒绝。

### 16.5 Quality eval

每个候选版本冻结：

- `ControlPlaneDigest`：Worker/kernel/Gateway/policy/schema/prompt/skills/baseline profile。
- `EvaluationRuntimeDigest`：SDK/CLI/runtime/model snapshot或alias限制/reasoning/tool schemas/session config。
- `CandidateDigest = hash(control + runtime + benchmark_version)`。

报告至少分开：

```text
false_verified_rate = oracle判定错误却发布COMPLETED/NO_CHANGE_NEEDED的任务数 / 所有该两类成功任务数
false_discovery_rate = oracle真实且scope内但最终报告遗漏的finding数 / oracle真实且scope内finding总数
task_success_rate = hidden oracle通过的COMPLETED或NO_CHANGE_NEEDED任务数 / 全部有效解题任务数
classification_accuracy = 环境/capability故障返回正确BLOCKED或PARTIAL的fixture数 / 对应fixture总数
```

`COMPLETED_WITH_WAIVERS/PARTIAL/BLOCKED/FAILED/CANCELLED`不进入成功分子。R3/R4、Q3、跨lease resume等deferred capability必须有“明确拒绝且无副作用”正向测试，不能因未启用而不测。

### 16.6 MVP Definition of Done

以下全部满足才可称MVP完成：

- 本文所有schema和golden fixtures存在，`additionalProperties=false`，digest可跨进程复算。
- 全部state transitions、Gate predicates、outcome mapping和stable codes有自动测试。
- Pullwise current `legacy_v1` contract tests全部通过；未改Server/Web即可运行。
- 保持单worker单active job、无local queue/prefetch、worker-scoped SDK/Auth/App Server。
- Pullwise SourceState前后完全相同；local/eval R1只在隔离workspace写。
- 每个成功Task覆盖100% active mandatory requirements、final SourceState、ObservationManifest和所需Attestations。
- 每个Verifier是新session且至少有一条own Observation；Q2 slots互相独立。
- R3/R4/Q3/unsupported interaction/cross-lease resume全部显式拒绝，Effect Ledger永远为空。
- 同一outer lease checkpoint故障恢复通过；lease失效时不发送伪造result。
- terminal result/outbox/artifacts幂等，cancel与publish无双终态。
- DebugFragment不含source/secret，缺失不产生audit fallback。
- 故障注入零重复effect、零stale publish、零mandatory-gap false success。
- 冻结benchmark上false verified不高于已接受baseline，false discovery/task success分别报告且没有超过发布门的回归。
- canary运行和rollback演练完成；operator可从Task/Attempt/Gate/error code定位失败。
- 文件规模检查通过：没有新增未豁免的600行以上手写文件；401–600行文件有内聚说明；超大遗留文件没有新职责、未解释的baseline增长或已降低baseline回升；全部例外可审计。

## 附录 A：冻结的 `legacy_v1` 契约快照

本附录的机器源是 `contracts/agent-first/legacy-v1-contract-baseline.json`。在 Worker 仓库执行 `python scripts/verify_agent_first_contract_baseline.py check --workspace-root ..`，可以复算 UTF-8/LF 内容摘要、运行固定 Server/Web probes 并输出机器可读三态结论。HEAD 只用于定位，不是兼容性阻断条件；不要手工修改下方生成区块。

<!-- BEGIN GENERATED LEGACY V1 BASELINE -->
> Generated from `legacy-v1-server-web-2026-07-17` with `sha256-utf8-lf/v1`. Do not edit this block by hand.

| Repository | Frozen HEAD (informational only) | Owner |
|---|---|---|
| `pullwise-server` | `d68487a283bbc28e526ffac623cb3dd1f06cd4e9` | Pullwise Server protocol owner |
| `pullwise-web` | `0af3088a86a3474b6f8b937958137db438098d48` | Pullwise Web consumer owner |
| `pullwise-worker` | `74b6cf9c8dbb138f3ab79724b705cf4dfdf12bf3` | Pullwise Worker compatibility owner |

| Contract surface | Repository path | Roles | Enforcement | Fixed probes | SHA-256 |
|---|---|---|---|---|---|
| `server.artifact-event-wire` | `pullwise-server/pullwise_server/_app_part_10_handler_main.py` | `producer,registry,validator` | `watched` | `server.progress-eta-fixtures,server.result-fixtures,server.route-fixtures` | `8ac81d754fdf84c2c7aacd3f18754e015973b204bb935a91c874ebffac4852b7` |
| `server.cancellation-fixtures` | `pullwise-server/tests/test_cancellation_handshake.py` | `fixture` | `watched` | `server.cancellation-fixtures` | `ef6a10348ecdfcbaafc6541ecbdc81fd7b7dd2be17992ac7ded7fd94e9dc51a0` |
| `server.claim-policy-source` | `pullwise-server/pullwise_server/billing.py` | `policy_source,producer` | `watched` | `server.policy-fixtures,server.route-fixtures,server.system-limit-fixtures` | `32beff2d93f3ca3abd4b484315eafc474c66ae748a6f64430c98fa0d30db1c18` |
| `server.claim-result-projection` | `pullwise-server/pullwise_server/_app_part_05_worker_results.py` | `producer,projection,validator` | `watched` | `server.cancellation-fixtures,server.progress-eta-fixtures,server.result-fixtures,server.route-fixtures` | `59a191ebeaa2a607caded43c8e4a14d0385227d6b6039068cb201605a75a75dd` |
| `server.durable-protocol-storage` | `pullwise-server/pullwise_server/db.py` | `storage,validator` | `watched` | `server.cancellation-fixtures,server.progress-eta-fixtures,server.result-fixtures,server.route-fixtures` | `f00b51f4010cb1d51f4b5c999152e115c94aecbcd6f0ed67797734a59a087f92` |
| `server.policy-fixtures` | `pullwise-server/tests/test_worker_admin_routes.py` | `fixture` | `watched` | `server.policy-fixtures` | `cedbe1315d59545f16a12864d16ce809ece04aa347292247b0dd100e69901ea7` |
| `server.progress-debug-projection` | `pullwise-server/pullwise_server/_app_part_04_scan_audit_bundle.py` | `producer,projection` | `watched` | `server.progress-eta-fixtures,server.route-fixtures,web.flow-fixtures,web.history-fixtures,web.normalizer-fixtures,web.progress-fixtures,web.timing-fixtures` | `9b36d719d2c4f3fc99dce1490a11b6154cd7538c95441cf8053fa8a3b195362c` |
| `server.result-fixtures` | `pullwise-server/tests/test_review_worker_protocol_v1.py` | `fixture` | `watched` | `server.result-fixtures` | `b2dba4e5be8ea3814e3879e77361d7dfbd2caa9de3f82a1b5fa33e51d1853d8b` |
| `server.route-fixtures` | `pullwise-server/tests/test_worker_pull_routes.py` | `fixture` | `watched` | `server.progress-eta-fixtures,server.route-fixtures` | `b49b68cb33bd3b006598deecbacf8262637868c573959129cfe9671cd957d031` |
| `server.status-projection` | `pullwise-server/pullwise_server/_app_part_01_bootstrap_state.py` | `projection,registry` | `watched` | `server.cancellation-fixtures,server.route-fixtures,web.flow-fixtures,web.history-fixtures,web.normalizer-fixtures,web.progress-fixtures,web.timing-fixtures` | `0e72dd9bfe230cb47968c2143af69cb440e907002ca20fb3cc2894c1d4344b97` |
| `server.system-limit-fixtures` | `pullwise-server/tests/test_configuration_contracts.py` | `fixture` | `watched` | `server.system-limit-fixtures` | `4fc7e76f991bcacebbcad8473c9daeaf4c6dc074642afdc022ab18805a4da6b9` |
| `server.system-limits` | `pullwise-server/pullwise_server/system_config.py` | `policy_source` | `watched` | `server.policy-fixtures,server.route-fixtures,server.system-limit-fixtures` | `d38e0aea1a8140b8d7568b0f7f63c656aa861f43b547fb6fde5c137854b3b62b` |
| `web.api-consumer` | `pullwise-web/src/api/pullwise.js` | `consumer` | `watched` | `web.api-fixtures` | `9222c76cd8d46904c3c58358d935767f3671d5b92983d7712a2af7f590c67c84` |
| `web.api-fixtures` | `pullwise-web/src/api/pullwise.test.js` | `fixture` | `watched` | `web.api-fixtures` | `a15c06f5aeef91f7f2bda9dfe0ee5e56551b64b49a31cb2951906ad3f58bb0a3` |
| `web.flow-fixtures` | `pullwise-web/src/screens/flow.test.jsx` | `fixture` | `watched` | `web.flow-fixtures` | `6e1eba9c89c9a5ae0a856a5ea7cbadd10507f9abd80fa2382a3f2cca897e2086` |
| `web.flow-projection` | `pullwise-web/src/screens/flow.jsx` | `consumer,projection` | `watched` | `web.flow-fixtures,web.normalizer-fixtures,web.progress-fixtures,web.timing-fixtures` | `539c679ad8f6c8ba3f7a9d2e6fb08d8d0f4958c8e45138b9d43b37df1ed80ff5` |
| `web.history-fixtures` | `pullwise-web/src/screens/issues.test.jsx` | `fixture` | `watched` | `web.history-fixtures` | `a0ce0abdc6ea782419ed61ad5e9e94723574ea58b39aeeeb6f02449be23b39a2` |
| `web.history-projection` | `pullwise-web/src/screens/issues.jsx` | `consumer,projection` | `watched` | `web.history-fixtures,web.normalizer-fixtures,web.progress-fixtures,web.timing-fixtures` | `9f27e78aa3894b8681b88bc4a2285004df1be889f342937bf270ede5d4e2357b` |
| `web.normalizer-consumer` | `pullwise-web/src/lib/pullwise-data.js` | `consumer,projection` | `watched` | `web.flow-fixtures,web.history-fixtures,web.normalizer-fixtures,web.progress-fixtures,web.timing-fixtures` | `d9692900267375f54321957c3b14ffda27ba99734b987878edb2619892651c03` |
| `web.normalizer-fixtures` | `pullwise-web/src/lib/pullwise-data.test.js` | `fixture` | `watched` | `web.normalizer-fixtures` | `c6a9ba002a89953a175e00919f352e7132a7e3b5a80fafa974d4071bbcf74ed3` |
| `web.progress-fixtures` | `pullwise-web/src/components/scan-progress.test.jsx` | `fixture` | `watched` | `web.progress-fixtures` | `2435a63a90ee89947cd53cf7f61ebe8ff2d7611a1583a32a35f2e678196b597b` |
| `web.progress-projection` | `pullwise-web/src/components/scan-progress.jsx` | `consumer,projection` | `watched` | `web.flow-fixtures,web.history-fixtures,web.normalizer-fixtures,web.progress-fixtures` | `6b509d2e8a9567b9e5e97f8c6b4585a0a0ab806fe3ed14fc166070c2693e2c7e` |
| `web.timing-fixtures` | `pullwise-web/src/components/scan-timing.test.jsx` | `fixture` | `watched` | `web.timing-fixtures` | `1b32ee6dbedd36f17bb30b34a221731cd96e9a686b088f6d80a43a9ab46a6070` |
| `web.timing-projection` | `pullwise-web/src/components/scan-timing.jsx` | `consumer,projection` | `watched` | `web.flow-fixtures,web.history-fixtures,web.normalizer-fixtures,web.timing-fixtures` | `5674e3cc0fd5e6f312dcc1a7fa740a1ed1ecdbce01c392f20fe4ed57f0b62db8` |
| `worker.public-scan-canonical-fixture` | `pullwise-worker/contracts/agent-first/fixtures/public-scan-v1.json` | `fixture` | `blocking` | `worker.public-scan-canonical-fixture` | `8346fd058fbca1885b5c9c76c970eea533df90d3e4a08dba8aa44c6135e61f7e` |
| `worker.public-scan-fixture-validator` | `pullwise-worker/scripts/verify_public_scan_fixture.mjs` | `validator` | `blocking` | `worker.public-scan-canonical-fixture` | `a7eab3195176a0f7fae01c148e1cc31516d9f3743f8ee86fe6c30851a740417c` |
| `worker.strict-v1-wire-canonical-fixture` | `pullwise-worker/contracts/agent-first/fixtures/review-worker-protocol-v1.json` | `fixture` | `blocking` | `worker.strict-v1-wire-canonical-fixture` | `a4e7870d78fa076bd9a6427b3875be82dd0bcfcc73a778b72414f2c015c45089` |
| `worker.strict-v1-wire-fixture-validator` | `pullwise-worker/tests/test_agent_first_contract_wire_fixture.py` | `fixture,validator` | `blocking` | `worker.strict-v1-wire-canonical-fixture` | `7f7f58700a611f4addd3278f6ac6e10f526f3d819474ab96ed6f772045e05848` |

| Exact registry | Source symbol | Ordered | Values |
|---|---|---|---|
| `server.db-replaceable-log-artifact-kinds` | `pullwise-server/pullwise_server/db.py#REPLACEABLE_REVIEW_LOG_ARTIFACT_KINDS` | `false` | `codex_event_log`, `debug_bundle`, `progress_log`, `worker_log` |
| `server.handler-active-heartbeat-statuses` | `pullwise-server/pullwise_server/_app_part_10_handler_main.py#WORKER_V1_ACTIVE_HEARTBEAT_STATUSES` | `false` | `busy`, `cancelling`, `failure_handling`, `finishing`, `leased` |
| `server.handler-event-types` | `pullwise-server/pullwise_server/_app_part_10_handler_main.py#REVIEW_RUN_EVENT_TYPES` | `false` | `approval_declined`, `artifact_created`, `artifact_uploaded`, `codex_app_server_failed`, `codex_app_server_started`, `codex_thread_started`, `codex_turn_completed`, `codex_turn_failed`, `codex_turn_started`, `phase_completed`, `phase_degraded`, `phase_failed`, `phase_retrying`, `phase_started`, `progress_updated`, `qa_failed`, `qa_passed`, `run_cancel_requested`, `run_cancelled`, `run_completed`, `run_failed`, `run_partial_completed`, `run_started` |
| `server.handler-progress-counter-keys` | `pullwise-server/pullwise_server/_app_part_10_handler_main.py#WORKER_V1_PROGRESS_COUNTER_KEYS` | `true` | `source_like_files_total`, `source_like_files_classified`, `bundles_total`, `bundles_packed`, `reviewer_runs_total`, `reviewer_runs_completed`, `intent_tests_total`, `intent_tests_written`, `intent_tests_run`, `validator_candidates_total`, `validator_candidates_completed`, `artifacts_total`, `artifacts_uploaded` |
| `server.handler-progress-phase-statuses` | `pullwise-server/pullwise_server/_app_part_10_handler_main.py#WORKER_V1_PROGRESS_PHASE_STATUSES` | `false` | `blocked`, `cancelled`, `completed`, `degraded`, `failed`, `partial_completed`, `pending`, `retrying`, `running`, `skipped` |
| `server.handler-replaceable-log-artifact-kinds` | `pullwise-server/pullwise_server/_app_part_10_handler_main.py#REPLACEABLE_REVIEW_LOG_ARTIFACT_KINDS` | `false` | `codex_event_log`, `debug_bundle`, `progress_log`, `worker_log` |
| `server.handler-worker-artifact-kinds` | `pullwise-server/pullwise_server/_app_part_10_handler_main.py#WORKER_REVIEW_ARTIFACT_KINDS` | `false` | `bundle_plan`, `cluster_result`, `codex_event_log`, `coverage`, `debug_bundle`, `disposable_test_patch`, `error_report`, `intent_map`, `intent_test_output`, `intent_test_plan`, `intent_test_result`, `intent_test_source`, `progress_log`, `qa`, `raw_reviewer_output`, `repo_inventory`, `repo_map`, `report.agent`, `report.human`, `risk_routing`, `token_budget`, `validation_result`, `verified_reviewer_output`, `worker_log` |
| `server.progress-estimate-confidence` | `pullwise-server/pullwise_server/_app_part_04_scan_audit_bundle.py#SCAN_ESTIMATE_CONFIDENCE` | `false` | `high`, `low`, `medium` |
| `server.progress-estimate-states` | `pullwise-server/pullwise_server/_app_part_04_scan_audit_bundle.py#SCAN_ESTIMATE_STATES` | `false` | `available`, `estimating`, `unavailable` |
| `server.results-required-completed-artifact-kinds` | `pullwise-server/pullwise_server/_app_part_05_worker_results.py#REQUIRED_COMPLETED_REVIEW_ARTIFACT_KINDS` | `false` | `coverage`, `qa`, `report.agent`, `report.human`, `token_budget` |
| `server.results-required-terminal-alternatives` | `pullwise-server/pullwise_server/_app_part_05_worker_results.py#REQUIRED_TERMINAL_REVIEW_ARTIFACT_ALTERNATIVES` | `false` | `error_report`, `report.agent` |
| `server.results-required-terminal-artifact-kinds` | `pullwise-server/pullwise_server/_app_part_05_worker_results.py#REQUIRED_TERMINAL_REVIEW_ARTIFACT_KINDS` | `false` | `qa`, `worker_log` |
| `server.results-worker-artifact-kinds` | `pullwise-server/pullwise_server/_app_part_05_worker_results.py#WORKER_REVIEW_ARTIFACT_KINDS` | `false` | `bundle_plan`, `cluster_result`, `codex_event_log`, `coverage`, `debug_bundle`, `disposable_test_patch`, `error_report`, `intent_map`, `intent_test_output`, `intent_test_plan`, `intent_test_result`, `intent_test_source`, `progress_log`, `qa`, `raw_reviewer_output`, `repo_inventory`, `repo_map`, `report.agent`, `report.human`, `risk_routing`, `token_budget`, `validation_result`, `verified_reviewer_output`, `worker_log` |
| `server.status-job-statuses` | `pullwise-server/pullwise_server/_app_part_01_bootstrap_state.py#SCAN_JOB_STATUSES` | `false` | `cancel_requested`, `cancelled`, `cancelling`, `claimed`, `done`, `failed`, `lost`, `partial_completed`, `queued`, `running`, `uploading_result` |
| `server.status-public-statuses` | `pullwise-server/pullwise_server/_app_part_01_bootstrap_state.py#SCAN_STATUSES` | `false` | `cancel_requested`, `cancelled`, `cancelling`, `done`, `failed`, `partial_completed`, `queued`, `running` |
| `server.status-terminal-retention-statuses` | `pullwise-server/pullwise_server/_app_part_01_bootstrap_state.py#TERMINAL_SCAN_RETENTION_STATUSES` | `false` | `cancelled`, `done`, `failed`, `lost`, `partial_completed` |

Fixed executable probes:

- `server.cancellation-fixtures` (cwd `pullwise-server`): `python -B -m unittest tests.test_cancellation_handshake`
- `server.policy-fixtures` (cwd `pullwise-server`): `python -B -m unittest tests.test_worker_admin_routes.WorkerAdminRoutesTest.test_admin_plan_agent_config_keeps_only_canonical_review_worker_policy`
- `server.progress-eta-fixtures` (cwd `pullwise-server`): `python -B -m unittest tests.test_worker_pull_routes.WorkerPullRoutesTest.test_worker_progress_records_worker_reported_phase_steps_message_and_log_summary tests.test_worker_pull_routes.WorkerPullRoutesTest.test_worker_eta_persists_and_batch_status_exposes_arbitrary_concurrency tests.test_worker_pull_routes.WorkerPullRoutesTest.test_worker_eta_rejects_invalid_numbers_ranges_and_terminal_payloads tests.test_worker_pull_routes.WorkerPullRoutesTest.test_worker_heartbeat_eta_is_persisted_and_terminal_result_clears_scan_eta tests.test_worker_pull_routes.WorkerPullRoutesTest.test_delayed_lower_sequence_event_cannot_overwrite_newer_scan_progress`
- `server.result-fixtures` (cwd `pullwise-server`): `python -B -m unittest tests.test_review_worker_protocol_v1`
- `server.route-fixtures` (cwd `pullwise-server`): `python -B -m unittest tests.test_worker_pull_routes`
- `server.system-limit-fixtures` (cwd `pullwise-server`): `python -B -m unittest tests.test_configuration_contracts.ConfigurationContractsTest.test_review_phase_limits_are_global_admin_config`
- `web.api-fixtures` (cwd `pullwise-web`): `node node_modules/vitest/vitest.mjs run --reporter=json src/api/pullwise.test.js`
- `web.flow-fixtures` (cwd `pullwise-web`): `node node_modules/vitest/vitest.mjs run --reporter=json src/screens/flow.test.jsx`
- `web.history-fixtures` (cwd `pullwise-web`): `node node_modules/vitest/vitest.mjs run --reporter=json src/screens/issues.test.jsx`
- `web.normalizer-fixtures` (cwd `pullwise-web`): `node node_modules/vitest/vitest.mjs run --reporter=json src/lib/pullwise-data.test.js`
- `web.progress-fixtures` (cwd `pullwise-web`): `node node_modules/vitest/vitest.mjs run --reporter=json src/components/scan-progress.test.jsx`
- `web.timing-fixtures` (cwd `pullwise-web`): `node node_modules/vitest/vitest.mjs run --reporter=json src/components/scan-timing.test.jsx`
- `worker.public-scan-canonical-fixture` (cwd `pullwise-worker`): `node scripts/verify_public_scan_fixture.mjs`
- `worker.strict-v1-wire-canonical-fixture` (cwd `pullwise-worker`): `python -B tests/test_agent_first_contract_wire_fixture.py`

Compatibility rule: HEAD and unlisted-path drift are informational. Blocking canonical-fixture drift, Appendix drift, or a completed failing fixed probe is incompatible. Broad watched source drift is compatible with a warning only while blocking fixtures match and every linked fixed probe passes; otherwise the result is indeterminate.
Baseline refresh is a read-only candidate operation and requires both the baseline owner and the affected repository owner.
<!-- END GENERATED LEGACY V1 BASELINE -->

根规范Appendix C.16–C.19当前只是占位草案，不能用于code generation；根规范中register返回token、裸result、空terminal manifest、缺encoding/compression等示例均不得覆盖本附录的executable contract。

## 附录 B：实现 Agent 的开工清单

1. 读取Worker `AGENTS.md`，确认工作树和用户改动。
2. 只读建立现有代码地图与第3.2节的超大文件baseline/抽离地图；不要先重构。
3. 重新计算附录 A HEAD/hash并运行Server contract fixtures；有差异先更新compatibility appendix并评审。
4. 按Slice 0→8实施，每个Slice先写失败测试。
5. 不修改Server/Web来掩盖Worker incompatibility；真正需要跨端契约时停止MVP并转Post-MVP版本。
6. 每次提交只包含当前Slice，附schema/fixture/test/rollback证据，以及新增/修改文件行数、超大文件baseline变化和模块化例外清单。
7. MVP DoD全部满足后，才允许开启`agent_task_v1`后续工作。
