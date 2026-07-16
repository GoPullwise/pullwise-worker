# Agent-First Worker Post-MVP 完整实现设计

状态：MVP 之后的规范性执行路线（Normative）  
版本：`post-mvp-design/v1`  
日期：2026-07-16  
前置文档：[MVP 实现设计](agent-first-worker-mvp-implementation-design.md)  
目标文档：[Agent-First Worker 目标设计](agent-first-worker-design.md)

## 0. 目的与“完全实现”的定义

本文用于 MVP 完成之后继续实施，不是愿景清单。每个版本都给出前置条件、跨端契约、数据迁移、实施顺序、回滚边界和可执行验收。执行 Agent 不得从 V2/V3 挑一个孤立功能直接落地，也不得用 feature flag 代替缺失的底层契约。

本文所称“完全实现”指：

- MVP 的持久 Task Owner、typed control、证据闭包和发布 Gate 已在生产稳定。
- Worker/Server 正式协商 `agent_task_v1`，Server可验证、保存并公开TaskResult的小型投影。
- 有界动态 Agent、R3 Effect Ledger/reconciliation、同run跨lease恢复和双端Debug Assembly全部实现。
- fleet/能力调度、高风险R4控制基础和eval驱动的质量自适应达到目标态验收。
- 旧Worker、旧Server、旧Web的滚动兼容、删除/撤销/retention、安全和可观测性均有机器契约。
- [目标设计](agent-first-worker-design.md) 的每项目标要么由追踪矩阵证明实现，要么作为明确非目标保留；不能用“暂未启用”掩盖未实现的安全路径。

“完全实现”不等于默认开放所有危险能力。R4 的完整实现包括受信审批、Effect Ledger、不可重试/对账语义和默认拒绝；生产没有获批R4 tool profile时保持off是正确目标态。

## 1. 权威顺序与不可破坏不变量

冲突优先级：本Post-MVP文档的版本契约 → [MVP 文档](agent-first-worker-mvp-implementation-design.md) 的基础不变量 → 各项目 `AGENTS.md` → [目标设计](agent-first-worker-design.md) 未细化部分 → 根full-repo-review协议的领域语义。

所有版本必须保持：

1. 一个Task只有一个逻辑Task Owner和一个immutable terminal TaskResult。
2. 每个Worker identity仍只有一个active outer job；横向扩展靠多个隔离Worker，不靠本地queue/prefetch。
3. Server拥有租约、task policy、capability grant、删除代次和公开投影；Agent不能自授权。
4. `task_version/transport_epoch/native_epoch/owner_epoch/deletion_version`均单调，旧actor永久fenced。
5. Requirement Ledger显式项不可删；waiver不等于PASS；success只来自机械Gate。
6. SourceState、Observation、Attestation、Effect receipt和EvidenceClosure均为内容寻址不可变证据。
7. `FINALIZING`无writer；任何语义修订都使旧proposal/attestation失效。
8. R3/R4先持久PREPARED effect和idempotency key再dispatch；不确定结果绝不盲重试。
9. 同run恢复不延长absolute deadline、不重置总budget、不复活cancel/revoke/delete scope、不降级protocol mode。
10. Debug不是Audit，不包含source/secret/完整prompt；没有真实bundle绝不生成fallback URL。
11. 旧客户端兼容只能发生在新lease的协商点；一个已固定`agent_task_v1`的lease不得运行中降为legacy。
12. 所有跨端新字段先有schema、valid/invalid fixture、error code和兼容矩阵，再写业务实现。

## 2. 开始 Post-MVP 前必须具备的证据

只有MVP Definition of Done全部通过才可开始。发布负责人还必须冻结以下输入：

| 输入 | 必需内容 |
|---|---|
| `MvpReleaseManifest` | Worker commit/image、schema registry digest、policy/prompt/tool digest、SDK/CLI/runtime版本 |
| `MvpContractBaseline` | legacy_v1 Server executable fixtures和Worker adapter golden payloads |
| `MvpEvalBaseline` | false verified、false discovery、task success、classification accuracy、成本/延迟原始分母 |
| `MvpRecoveryReport` | 同outer lease crash-point矩阵、stale epoch/cancel/publish结果 |
| `MvpSecurityReport` | source integrity、secret redaction、path/archive、cross-tenant tests |
| `MvpOperationsReport` | canary、rollback、active-slot/outbox恢复、debug缺失诊断 |

任何基础不变量仍靠日志人工判断而无schema/fixture时，先补MVP，不得把债务转嫁给新协议。

## 3. 版本与依赖总图

| 版本 | 核心结果 | 硬依赖 | 不包含 |
|---|---|---|---|
| C0 | 跨端schema包、稳定ErrorResponse、additive DB字段 | MVP | 新行为grant |
| V1.0 | `agent_task_v1`协商、TaskResult ingest、公开agentTask投影 | C0 | 动态通用角色、R3、resume、完整debug |
| V1.1 | Explorer/Troubleshooter/Implementer有界动态编排 | V1.0 | R3/R4 |
| V1.2 | R3 Effect Ledger、Reconciler、effect-aware terminalization | V1.0；建议V1.1稳定 | R4、same-run resume |
| V1.3 | Server-ACK checkpoint watermark和跨lease same-run resume | V1.0、V1.2的loss semantics | Debug Assembly |
| V1.4 | WorkerFragment + ServerSnapshot + immutable Assembly +公开availability | V1.0、V1.3 identity/watermark | fleet |
| V2.0 | Fleet、BaselineProfile、能力调度、隔离cache | 全部V1 | eval自学习 |
| V2.1 | R4高风险控制基础，默认off | V1.2、V2.0 policy/ops | 无审批的R4 tool |
| V3.0 | eval驱动Quality Policy、specialized verifier、受控skill演进 | V2.0/2.1 | 在线无门学习 |

同一版本内部按Server schema/storage → Worker producer → Server ingest/projection → Web feature detection → rollout的顺序。Web不能先猜Worker extension，Worker不能先发送Server未授权字段。

## 4. C0：跨端契约基础

### 4.1 交付物

建立由Server仓库发布、Worker/Web消费的versioned contract package，至少含：

- `agent-worker-capabilities/v1`
- `agent-worker-offer/v1`
- `agent-worker-grant/v1`
- `agent-worker-event-extension/v1`
- `agent-worker-result-extension/v1`
- `task-result/v1`及全部引用schema
- `server-terminalization-input/v1`和server-reconciler TaskResult variant
- `agent-task-public-dto/v1`
- `debug-bundle-public-dto/v1`
- `error-response/v1`
- valid、invalid、unknown-optional、unknown-major、golden-digest fixtures

Contract package本身生成`ContractBundleDigest`。三个项目在build/test中pin digest；运行时Server在register响应暴露实现的bundle版本，Worker记录但不能把不匹配当自动降级许可。

### 4.2 版本规则

- capability identity为`id + major + minor`。
- major不兼容；未知major不选择、不映射、不猜。
- minor只允许新增明确optional、旧接收端可忽略的字段；required、enum语义、hash算法、状态机变化必须升major。
- Schema使用`additionalProperties=false`；可扩展点只能是显式`extensions` map，key须命名空间，值有独立schema/size cap。
- grant选择Worker advertisement、Server实现、task/tenant rollout policy的交集；minor取双方范围内最高兼容值。
- Contract fixture的canonical bytes和digest属于API，不能由语言serializer偶然决定。

### 4.3 稳定ErrorResponse

所有新/扩展route错误统一：

```json
{
  "error": {
    "code": "AGENT_GRANT_INVALID",
    "message": "human readable",
    "retryable": false,
    "retry_scope": "none",
    "request_id": "req_...",
    "details": {}
  }
}
```

`details`按code使用strict subschema。HTTP status仍有意义，但Worker控制流优先读code/retry scope。旧legacy route保持现有响应；新Worker必须能同时处理旧无code响应和新ErrorResponse，不能解析英文message。

### 4.4 Additive存储迁移

Server先增加nullable字段/新表，不改旧read path：

- lease：`protocol_mode/grant_id/grant_digest/transport_epoch/resume_until/deletion_version/task_version`。
- task/run：`agent_task_projection_json/task_result_ref/core_digest/full_digest/checkpoint_watermark`。
- grants、resume operations、server terminalizations、debug captures/assemblies、effect receipts。
- artifact增加`validator_kind/schema_major/source_scope/immutable_revision`。

迁移采用expand → dual-read shadow → backfill/verify → enable write → switch read → 最后contract。C0不删除旧列、不改变legacy status。

### 4.5 C0完成门

- 三端从同一contract bundle运行golden tests。
- unknown major、required field缺失、额外字段、digest变化有negative fixtures。
- DB migration在生产规模snapshot上可重入、可中断恢复、旧binary仍可读写。
- ErrorResponse code registry穷举且每个code有HTTP/retry/Attempt transition。
- feature flags全off时所有legacy contract bytes和公开DTO不变。

## 5. V1.0：`agent_task_v1` 协议与公开投影

### 5.1 Capability advertisement与offer

Worker在现有register request附加：

```json
{
  "extensions": {
    "agent_worker": {
      "supported": [
        {"id":"agent_task_protocol","major":1,"min_minor":0,"max_minor":0},
        {"id":"worker_debug_fragment","major":1,"min_minor":0,"max_minor":0},
        {"id":"same_run_resume","major":1,"min_minor":0,"max_minor":0}
      ],
      "contract_bundle_digest":"<sha256>"
    }
  }
}
```

数组按ID排序、ID唯一、range合法。`worker_debug_fragment`和`same_run_resume`依赖同major的`agent_task_protocol`。Register只返回offer，不签发执行权：

```json
{
  "extensions": {
    "agent_worker_offer": {
      "recognized":[{"id":"agent_task_protocol","major":1,"min_minor":0,"max_minor":0}],
      "server_contract_bundle_digest":"<sha256>"
    }
  }
}
```

旧Server忽略advertisement时Worker继续发exact legacy request；不得在没有lease grant时发送新event/result字段。

### 5.2 Lease grant

Server在claim同一事务中固定：

```json
{
  "schema_id":"agent-worker-grant/v1",
  "grant_id":"grant_...",
  "protocol_mode":"agent_task_v1",
  "scope":{"tenant_id":"...","task_id":"...","job_id":"...","run_id":"...","worker_id":"..."},
  "transport":{"attempt_id":"...","lease_id":"...","transport_epoch":1},
  "selected":{"agent_task_protocol":{"major":1,"minor":0}},
  "task_version":1,
  "deletion_version":0,
  "issued_at":"...Z",
  "expires_at":"...Z",
  "resume_until":"...Z",
  "grant_digest":"<sha256>"
}
```

`grant_digest`对删除自身字段后的canonical object计算。Grant不是bearer secret，但所有写仍需Worker credential。Server durable row是权威；request自带grant object不能建立授权。

选择不到`agent_task_protocol`时lease固定`protocol_mode=legacy_v1`且不发selected extension。一个run的mode不可变；新flag只影响新lease。安全撤销同时revoke grant、fence lease、记录audit，不能静默重解释payload。

### 5.3 严格extension门

`agent_task_v1`的每个event/result和普通fragment descriptor必须带：

- `grant_id/grant_digest`。
- `task_id/task_version/deletion_version`。
- `transport_attempt_id/transport_lease_epoch`。
- `native_attempt_id/native_epoch`。
- capability-specific payload/schema version。

Server在任何partial write前验证credential、scope、grant current、lease、epoch、version、deletion、selected capability。缺extension或不匹配是`PROTOCOL_DOWNGRADE_ATTEMPT`，原子拒绝，绝不能落入legacy validator。只有权威mode=legacy才走旧分支。

### 5.4 Result extension与完整性

legacy wrapper和required artifact仍保留；`reviewWorkerProtocol.extensions.agent_worker`新增：

```json
{
  "grant_id":"grant_...",
  "grant_digest":"...",
  "task_result": {"schema_id":"task-result/v1"},
  "task_result_integrity": {
    "task_result_core_digest":"...",
    "task_result_digest":"...",
    "evidence_closure_digest":"..."
  }
}
```

三个digest是TaskResult的sibling，不属于被hash对象。Server必须自行重算TaskResultCore（只删`/diagnostics/worker_debug_fragment`）、完整TaskResult、EvidenceClosure，校验task/scan、run/job/lease、epochs、outcome→legacy status映射、Requirement覆盖、Effect状态、terminal receipt。

Result accept同一事务：

1. 验证legacy required artifacts和extension/grant。
2. pin全部ContentRef和closure bytes。
3. 验证optional terminal fragment receipt；缺失走明确`unavailable`分支，不伪造。
4. insert immutable raw envelope/TaskResult/TaskResultCore/receipt binding。
5. CAS Task terminal version、review run和scan projection。
6. 写terminal event/outcome labels。
7. 返回含accepted/duplicate/receipt的同一response。

完全相同attempt+digests幂等；任一不同为409。成功response丢失后重发必须返回同一receipt。

### 5.5 Server Reconciler variant

新增内部、非HTTP `commit_server_terminal_result`：

- service identity，无Worker lease/grant，只能产生非成功outcome。
- 输入绑定latest task/deletion version、Server持久化request、ACK checkpoint identity、已知artifact/Observation/effect receipts。
- 缺失事实用AvailabilityRef写`unavailable + reason_code`，不伪造digest/PASS/Worker envelope。
- `result_source=server_reconciler`、`worker_transport_envelope=null`。
- idempotency为`task_id + terminal_task_version + termination_cause + input_digest`。

空Effect Ledger且满足active-ownership谓词的`WORKER_LOST`仍是独立Server-owned transport terminal，无TaskResult；有任何effect row就不能用纯lost吞掉事实。

### 5.6 Public `agentTask` DTO

Server只从validated TaskResult保存allowlisted小投影，不把raw extension直接给Web：

```json
{
  "schemaVersion":"agent-task-public/v1",
  "taskId":"...",
  "lifecycle":"active|waiting_input|waiting_approval|finalizing|terminal",
  "desiredState":"run|cancel",
  "outcome":null,
  "attempt":{"nativeEpoch":2,"status":"running"},
  "requirements":{"mandatory":5,"pass":3,"fail":0,"unverifiable":2,"waived":0},
  "verification":{"qualityRisk":"Q1","requiredSlots":1,"passedSlots":0},
  "effects":{"total":0,"unknown":0},
  "evidence":{"availability":"partial"},
  "transport":{"status":"running|lost|cancel_requested|cancelling|terminal"},
  "updatedAt":"...Z"
}
```

DTO按lifecycle discriminated schema规定nullability。旧scan `status`保留业务兼容；transport lost不再伪装为Worker FAILED TaskResult，而在`agentTask.transport.status=lost`表达。Web正式接受`cancel_requested/cancelling`，不得把它们normalize成queued。

### 5.7 V1.0兼容矩阵与rollout

| Worker | Server | Web | 行为 |
|---|---|---|---|
| old | old/new | old/new | exact legacy；新Server可生成legacy projection但不声称agent evidence complete |
| new | old | 任意 | advertisement被忽略后exact legacy；无grant不发extension |
| new | new grant off | 任意 | mode=legacy_v1 |
| new | new grant on | old | Server ingest extension；旧Web忽略新增DTO |
| new | new grant on | new | 完整agentTask投影 |

Rollout：Server schema/storage → Server parse shadow → Worker advertisement → grant仅内部tenant → Server strict ingest → Web feature detection → 分批grant。Rollback只能关闭新lease grant；已grant run继续同mode或被明确fence/terminalize，绝不降级。

### 5.8 V1.0完成门

- grant dependency/major/minor/expiry/revoke/deletion/unknown field fixtures全通过。
- grant-on省略extension、错scope/epoch/version、legacy downgrade均零partial write。
- 全部TaskResult outcome映射、digest、closure、receipt、duplicate/conflict测试。
- Reconciler缺失证据诚实表达、幂等、与cancel/delete竞态。
- old/new三端矩阵全自动化；legacy bytes无回归。
- Web不读取raw Worker envelope，不泄露thread/secret/internal path。

## 6. V1.1：有界动态 Agent

### 6.1 新角色

在MVP的Task Owner/Quality Verifier之外启用：

| 角色 | 权限 | 典型输出 |
|---|---|---|
| Explorer | R0，只读、并行发现 | observation-backed exploration report |
| Troubleshooter | R0/R1隔离sandbox，复现和最小化 | hypothesis/experiment/observation |
| Implementer | R1独立worktree/branch，单任务写 | candidate change set |

Quality Verifier仍由Policy启动，不能作为普通delegation。Pullwise read-only Adapter继续不启用Implementer；现有领域reviewers保持Adapter activities。

### 6.2 Delegation contract

`delegation-request/v1`：`goal/requirement_ids/role/capabilities/input_refs/output_schema/budget/deadline/dependency_ids/merge_policy/idempotency_key`。Worker生成`DelegationGrant`，取请求与Effective Policy交集；子Agent不能再扩大权限。

每个Agent返回结构化`agent-work-result/v1`：status、observations、artifacts、change set、unresolved questions、budget usage。自然语言消息只作artifact，不能改变Task state。

### 6.3 并发与单writer

- `max_agents`统计所有live sessions，包括Owner、通用子Agent、Quality Verifier和legacy domain reviewer。
- Task Owner是主workspace唯一writer。Implementer只写独立worktree；Owner通过typed merge operation审查/合并。
- 同一worktree最多一个write grant；Gateway持久化`writer_lease`并受native epoch fence。
- dependency DAG必须无环，fanout宽度/深度/总session受policy硬限。
- 子Agent失败显式回Owner；可重派、串行或诚实降级，不能静默丢失mandatory scope。

### 6.4 调度价值门

Policy只在预计并行收益/独立性价值超过启动成本时接受可选Agent。输入使用机械特征（scope count、independent components、blocked hypotheses、budget），不按语言标签分配固定角色。

每类任务做同budget paired ablation：Owner-only与dynamic组拥有相同总token/wall/tool预算和相同required Quality Verifier。动态组必须在适合子集达到：成功率+5个百分点，或p50墙钟-15%且成本+≤10%；同时false verified/越权不增加。否则该类默认Owner-only。

### 6.5 V1.1完成门

- DAG cycle、budget laundering、live count、writer conflict、stale child epoch tests。
- 不同worktree合并冲突不覆盖original dirty state。
- 子Agenttimeout/cancel/runtime loss不阻断Supervisor，且Observation归属正确。
- Verifier input不继承Implementer结论叙事；独立性仍满足。
- paired ablation过门，不能只证明“能启动多Agent”。

## 7. V1.2：R3 Effect Ledger 与 Reconciler

### 7.1 EffectRecord

每个外部动作在dispatch前创建`effect-record/v1`：

- `effect_id/task_id/attempt_id/native_epoch`。
- `provider/action/capability_risk=R3`。
- `scope`、sanitized request摘要、request ContentRef。
- provider-stable `idempotency_key`；无provider幂等能力的动作默认不支持。
- `state=PREPARED|DISPATCHED|COMMITTED|NOT_APPLIED|REJECTED|UNKNOWN`。
- `prepared_at/dispatched_at/resolved_at`。
- `dispatch_receipt/query_receipts/final_receipt` AvailabilityRefs。
- `reconciliation_deadline_at`，创建后不可延长。
- `last_transition_seq/digest`。

状态只允许：PREPARED→DISPATCHED/REJECTED；DISPATCHED→COMMITTED/NOT_APPLIED/REJECTED/UNKNOWN；UNKNOWN→COMMITTED/NOT_APPLIED/REJECTED。终态无出边；后到事实只形成`EffectResolutionAddendum`，不改已发布TaskResult。

### 7.2 Dispatch原子边界

1. Gateway验证grant/approval/budget/epoch。
2. 生成provider idempotency key和request digest。
3. SQLite事务写PREPARED、预算reservation、effect watermark。
4. durable写dispatch intent。
5. 调provider。
6. 在知道request已交给provider前后分别记录状态：确定未发送→REJECTED/NOT_APPLIED；已发送或响应不明→DISPATCHED。
7. receipt/query证明成功→COMMITTED；证明未应用→NOT_APPLIED；固定deadline仍不明→UNKNOWN。

不得把HTTP timeout等同NOT_APPLIED，不得换idempotency key盲重试。

### 7.3 Reconciliation

Reconciler是受信控制面actor：按provider query contract有界查询，所有query有budget/速率/credential scope，结果为immutable receipt。重启不得重置deadline。`DISPATCHED`没有确定证据时，在任何terminalization前CAS提升UNKNOWN。

### 7.4 Effect-aware outcome

- 有确认effect且安全partial deliverable：`PARTIAL`或cancel时`CANCELLED_WITH_EFFECTS`，逐项披露receipt。
- deadline仍UNKNOWN：`TERMINATED_WITH_UNKNOWN_EFFECTS`，`requires_human_reconciliation=true`，优先级高于FAILED/CANCELLED/PARTIAL。
- 所有effect NOT_APPLIED/REJECTED且其他条件满足时可按普通outcome。
- `COMPLETED`要求所有预期effect COMMITTED且receipt/requirements PASS，无UNKNOWN。
- Worker loss只要Effect Ledger非空就不能使用纯`WORKER_LOST` terminal；必须fence Attempt并走Reconciler/Terminalization Gate。

### 7.5 R3审批

ApprovalEvent必须绑定task/version/ledger/policy/effect action/scope/expiry/actor authority/signature。approval只授权一个精确action scope，不能授权任意后续请求。撤销在dispatch前阻止动作；dispatch后进入Effect事实处理，不能假装撤销已回滚provider。

### 7.6 V1.2完成门

- 每个state/event组合、timeout/response-loss/query冲突和crash point测试。
- provider fake支持duplicate receipt、late commit、not-applied、unknown、credential revoke。
- cancel/lease loss/deadline/delete与PREPARED/DISPATCHED/COMMITTED/UNKNOWN笛卡尔矩阵。
- 零盲重试、零effect被普通失败隐藏、reconciliation deadline不延长。
- Human reconciliation DTO和Addendum不改原TaskResult/outcome统计。

## 8. V1.3：Server-ACK Checkpoint 与 `same_run_resume`

### 8.1 Checkpoint watermark

Worker每次LocalCommittedCheckpoint后，在旧lease仍有效时通过heartbeat或event extension上报：

```json
{
  "generation":12,
  "manifest_hash":"...",
  "previous_manifest_hash":"...",
  "task_version":31,
  "deletion_version":0,
  "transport_lease_epoch":4,
  "native_epoch":7,
  "last_local_event_sequence":413,
  "last_server_acked_event_sequence":407,
  "event_prefix_digest_at_ack":"..."
}
```

Server同事务验证grant选择`agent_task_protocol+same_run_resume`、scope/version/epoch/deletion、`generation=stored+1`和previous hash，保存`ServerResumeCheckpointWatermark`并ACK。完全相同重试幂等；跳代、分叉、旧epoch、同generation不同hash拒绝。只有ACK generation可跨lease恢复；checkpoint bytes仍留Worker，Server不解析语义。

Server还返回authoritative `last_accepted_event_sequence/next_event_sequence/event_prefix_digest`，解决MVP event ambiguity。Worker恢复时采用Server watermark，不回退sequence；相同event ID/digest幂等，不同digest冲突。

### 8.2 Resume request

使用现有lease endpoint的完整idle v1 request附加：

```json
{
  "extensions": {
    "agent_worker_resume": {
      "operation_id":"resume_...",
      "prior_grant_id":"...",
      "prior_grant_digest":"...",
      "run_id":"...",
      "prior_transport_attempt_id":"...",
      "prior_transport_lease_epoch":4,
      "checkpoint_generation":12,
      "checkpoint_manifest_hash":"...",
      "deletion_version":0,
      "expected_task_version":31
    }
  }
}
```

Resume请求不能在同一次失败后顺便claim别的job。

### 8.3 Resume正向谓词

全部为真才成功：

- prior grant选择agent task+resume，未security revoke；Worker credential有效。
- tenant/task/job/run/worker/attempt/epoch完全匹配，scope未tombstone，deletion version精确相等。
- `now < resume_until < absolute_deadline-terminal reserve`，run未terminal。
- Task lifecycle为ACTIVE或FINALIZING、desired RUN；不是WAITING/SUSPENDED/normal release。
- 旧Attempt原为active ownership且已fenced，无新active lease。
- request checkpoint精确等于Server ACK watermark。
- expected task version未被Cancel/Policy改变。
- Effect状态允许；有UNKNOWN时只允许Reconciler，不恢复语义执行。
- 旧budget reservation可核销，剩余wall/token/cost/tool满足minimum resume window/budget。
- 当前rollout仍能保持同一protocol mode和major。

### 8.4 ResumeOperation事务

Server创建`resume-operation/v1`，状态`PREPARED→COMMITTED|REJECTED`。同一事务：锁Task/lease → 验证谓词 → 核销旧reservation → 预留新slice → fence旧attempt → 创建successor transport attempt/epoch和新grant → 保存同job/run lease → commit response bytes。response丢失后相同operation ID返回相同response；冲突请求409。

Worker拿到successor后再验证response binding，载入ACK checkpoint，native/owner epoch继续递增。新lease不能延长absolute deadline或总budget。

### 8.5 拒绝分类

- cancel先赢 → `RESUME_CANCELLED`，交Terminalization。
- budget/deadline不足 → `RESUME_BUDGET_INSUFFICIENT`，诚实PARTIAL/FAILED。
- waiting/suspended/normal release → `RESUME_NOT_ACTIVE_OWNERSHIP`，保持等待策略。
- security/credential revoke或tombstone → permanent拒绝并audit。
- watermark/identity/version冲突 → permanent consistency failure。
- rollout无法保持mode →拒绝，不降legacy。

### 8.6 V1.3完成门

- watermark ACK/duplicate/gap/fork/old epoch/revoke/delete tests。
- resume success、每个拒绝、Cancel竞态、budget transfer、response loss exact replay。
- 在旧reservation核销、新slice、新lease/new grant每个crash point注入故障，无双lease/双budget。
- event sequence不回退，checkpoint本地较新未ACK不恢复。
- WAITING/SUSPENDED/normal release不被sweeper误判WORKER_LOST。

## 9. V1.4：完整 Debug 诊断读模型

### 9.1 三层对象

1. `WorkerDebugFragment`：Worker对某run/attempt的immutable、redacted分片。
2. `ServerDebugSnapshot`：Server在一个read-only transaction中冻结同scope权威状态。
3. `DebugAssembly`：Server Composer验证二者identity/causal cut/hash/redaction后生成的immutable terminal包。

Legacy dynamic download继续单独标`legacy_combined`，不能升级为complete。

### 9.2 Capture与receipt

capture kind：startup/checkpoint/terminal/crash/postlude。普通capture必须有当前grant；terminal fragment在TaskResultCore后生成，upload descriptor带source SHA/size/schema/identity/core digest。Server校验后签发`terminal-fragment-receipt/v1`，绑定fragment和TaskResultCore。

早期capture的core digest为not_applicable；terminal必须available。普通fragment不能包含最终含receipt的TaskResult bytes。Server接受result后，完整TaskResult digest进入ServerSnapshot，保持无环。

### 9.3 ServerDebugSnapshot

一个数据库read transaction冻结：

- tenant/scan/task/job/run/transport/native identities、versions、deletion。
- scan/job/attempt/task lifecycle、cancel/lease/recovery records。
- event high watermark和prefix digests、progress/phase/error。
- artifact metadata/immutable revisions、result/core/full digests、receipt。
- requirement/verification/effect/budget counts和reconciliation状态。
- quota只含该run相关sanitized状态；无token、其他用户、全DB。

Snapshot生成后为ContentRef，不随后续数据库变化重写。

### 9.4 Causal completeness

Assembly标`complete`必须全部成立：

- tenant/task/job/run/attempt/epochs/core digest/terminal Task version一致。
- Worker `A=last_server_acked_seq <= L=local_seq`，Server `H>=A`。
- 双方在A的event prefix digest相同。
- 对A之后所有声明server-bound的Worker events，Server存在相同event ID/digest且`H>=L_bound`；worker-local-only诊断明确分类且不影响Server状态。
- checkpoint generation/hash等于Server ACK watermark或明确terminal successor关系。
- fragment、snapshot、result、receipt、file/component manifests全部hash/size/schema通过。
- 两次redaction scan通过，无source/secret。

任一不足只能`partial`并给穷举reason code；不能凭时间接近或sequence大小猜complete。

### 9.5 Assembly格式

外层`debug-assembly/v1`含contributors、scope、causal watermarks、component refs、file manifest和reason。`bundle-files.json`只列payload entries，排除自身、archive和任何manifest自身。entries/contributors按规范排序；capture timestamp在首次冻结时固定，重试不重生成。

Composer安全门：

- 同scope/tenant且authorization当前有效。
- entry count、单entry/总解压/archive大小、压缩比上限。
- 拒绝absolute/traversal/backslash/NUL/duplicate/case collision/symlink/special file。
- schema-specific allowlist、recursive field redaction、文本secret scan。
- 使用隔离临时目录、bounded CPU/time/memory；失败生成状态，不生成假成功ZIP。

### 9.6 Public Debug DTO

公开分离availability和completeness：

```json
{
  "schemaVersion":"debug-bundle-public/v1",
  "availability":"pending|available|absent|unsupported|upload_failed|composition_failed|expired|deleted",
  "completeness":"complete|partial|null",
  "reasonCode":null,
  "downloadUrl":"/v1/review-runs/.../debug-bundle",
  "sha256":"...",
  "sizeBytes":123,
  "createdAt":"...Z",
  "expiresAt":"...Z"
}
```

只有availability=available时URL/hash/size非null。URL由Server生成，必须同源root-relative `/v1/...`；不允许Worker外域URL、`//`、`javascript:`、`data:`、反斜杠、CR/LF/NUL。Detail/List/batch status读取同一durable projection；Web只在detail展示action。任何状态都不fallback audit bundle。

### 9.7 Retention、删除与support pin

- Task/fragment/snapshot/assembly/artifact/download全部带`deletion_version` fence。
- Delete先CAS tombstone/version，再阻止新heartbeat/event/artifact/result/resume/composition/download，最后异步删bytes。
- support pin有issuer/scope/reason/expiry/audit，不能覆盖security delete或tenant policy。
- 下载中的authorization和deletion在stream开始前检查；长stream按policy决定是否中断，但新请求必拒绝。
- expired/deleted保留小型tombstone projection和reason，不保留secret/source bytes。

### 9.8 Orphan crash capsule

lease fenced后的唯一上传例外使用一次性`recovery_upload_grant`：prior grant曾选debug、非security revoke、credential有效、identity/deletion精确、无新active lease。Grant绑定`purpose=crash_capsule/kind/sha/size<=4MiB/TTL<=5min/nonce`，只能追加内容寻址capsule；不能写event/progress/checkpoint/effect/result或生成terminal receipt。它只能令debug partial并给`orphan_crash_capsule`原因。

### 9.9 V1.4完成门

- fragment缺失/损坏/过大/secret/source、snapshot竞态、composer limits、cross-scope tests。
- complete causal predicate逐项negative fixture；partial reason稳定。
- terminal result/receipt无hash循环，相同capture/result重试bytes不变。
- delete/tombstone与所有写/读/retention/pin/crash upload逐项竞态。
- 无真实bundle时所有版本都没有audit URL；legacy与new complete指标分开。

## 10. V2.0：Fleet 与能力调度

### 10.1 Fleet模型

每个Worker仍单slot/单SDK process/独立root/auth/cache。Server scheduler在Worker之间水平分配，不向Worker预取。`WorkerCapabilitySnapshot/v1`记录当前可验证能力，不把技术栈标签当固定pipeline。

### 10.2 BaselineProfile

Baseline profile是不可变、内容寻址、可复现环境描述：

- OS/image digest、cpu/memory/disk class。
- worker/SDK/CLI/runtime versions。
- available tool identities/versions/sandbox features。
- network/secret/provider capability classes，不含credential。
- supported contract/capability ranges。
- health/doctor evidence和captured_at/expiry。

Task Requirement由Policy编译成capability predicates；scheduler只在满足hard predicates的ready Worker中选择，再按queue age/cost/cache affinity等软评分。Worker claim后仍验证Server policy，不能因scheduler选择而跳Gateway。

### 10.3 Cache

- Repo mirror、runtime/image/tool cache都按tenant安全策略和immutable key命名。
- credential/token不进cache identity、path、log。
- checkout为隔离worktree/copy，SourceState仍独立snapshot；cache hit不能省略integrity验证。
- mutable semantic evidence、TaskResult、Effect Ledger、auth、Codex sessions不得跨Task复用。
- cache eviction只在idle，保护active/checkpoint/outbox/retention pin。

### 10.4 调度与失效

- Worker heartbeat暴露可验证ready/degraded/capability snapshot digest；Server不靠Worker自报任意标签授权。
- stale snapshot/doctor failure/provider quota unavailable使Worker不可claim，但idle heartbeat shape保持协议要求。
- lease后capability消失：fence新dispatch，按checkpoint/resume或BLOCKED/PARTIAL处理，不迁移活跃effect到另一Worker盲重放。
- same-run resume到另一Worker只有checkpoint bytes在受信共享CAS且加密/tenant隔离时才可作为后续minor能力；V2.0默认仍恢复同Worker identity。

### 10.5 V2.0完成门

- 多Worker并发claim无duplicate；每Worker始终一slot。
- capability mismatch、stale health、quota、cache poison/cross-tenant、worker loss tests。
- scheduler deterministic tie-break和可解释reason；不按语言硬编码pipeline。
- fleet rollout/rollback不破坏已grant run和protocol mode。

## 11. V2.1：R4 高风险控制基础

### 11.1 支持含义

R4 contract/tool descriptors、审批、Effect Ledger、不可重试和人类恢复路径全部实现，但全局default deny。生产启用某个R4 tool需要单独versioned product policy和release approval；未知R4始终拒绝。

### 11.2 额外门禁

- two-person或等价强审批，issuer互不相同，均绑定exact action/scope/request digest。
- 强制dry-run/preview（provider支持时）形成Observation，但preview不当作commit。
- 不可逆动作必须有用户可理解impact/target/rollback impossibility和cooldown窗口。
- dispatch前再次验证scope、deletion/cancel、approval expiry、provider identity。
- 无provider idempotency/query contract的动作只允许明确single-shot，transport ambiguity立即UNKNOWN，不自动重试。
- Q3至少两个独立Verifier concerns；Verifier不能是approver/implementer。
- 完整receipt、human reconciliation和post-action verification。

### 11.3 V2.1完成门

- 默认off和所有绕过路径零dispatch。
- approval伪造/重放/过期/撤销、identity drift、cancel/delete、response loss矩阵。
- UNKNOWN永不被普通终态隐藏；operator runbook可安全查询但不重复动作。
- red-team评审和人工演练签字；没有合格product profile时版本仍可发布但R4保持unsupported。

## 12. V3.0：Eval 驱动质量自适应

### 12.1 Specialized Verifier

Quality Policy可按结构化risk/scope选择specialized verifier profile，例如security、data/concurrency、contract/API、UI behavior、performance。它们仍遵循新session、冻结input、own Observation、final manifest attestation，不可降低Q floor。

### 12.2 Policy候选与离线学习

- 生产evidence先脱敏聚合，不能把tenant source/prompt/secret当训练资料。
- 每次policy/prompt/skill/tool schema变化形成新ControlPlaneDigest和CandidateDigest。
- frozen benchmark + hidden holdout + concurrent previous stable对照。
- 只有预先冻结threshold通过才进入canary；不得看结果后改分母/门槛。
- 自动发现的skill/prompt只生成候选artifact，需schema/security/eval/code review，不能在线自改生产Worker。

### 12.3 质量与成本目标

同时优化：task success、false verified、false discovery、unaided completion、verified-success总成本、p50/p95 wall、Verifier修回率。Abstain/waiver/partial不进入success；环境fixture单独评classification。

自适应策略必须有hard guardrails：mandatory coverage=100%、source/epoch/effect invariants=0违反、R3/R4审批不变、Verifier floor不降低。质量提升不能用更高越权率或隐性预算换取。

### 12.4 V3.0完成门

- candidate/stable完全可复算；无法获得model immutable snapshot时同时间窗重跑stable并披露限制。
- policy选择有reason trace，可离线replay。
- benchmark未见技术栈/对抗/脏基线/大repo覆盖。
- adaptive策略回滚只影响新Task；active Task继续冻结policy version。
- 无任何在线无门自修改。

## 13. 跨版本数据、协议和删除策略

### 13.1 Schema兼容

- 所有持久对象保存schema ID/version和raw immutable bytes；projection可重建。
- reader支持当前major和明确列出的旧major；未知major不投影。
- migration不重写旧TaskResult/Attestation/Effect receipt；需要解释修正时追加Addendum。
- hash/canonicalization变化必须新schema major，不能原地重算旧digest。

### 13.2 Expand/contract发布模板

1. Expand DB/contract reader。
2. Shadow parse/validate但不写新行为。
3. Dual-write新projection，比较差异。
4. 启用少量新lease grant。
5. Web feature-detect新DTO。
6. 达观察窗后扩大。
7. 旧版本低于保留阈值且rollback窗口结束后，才contract dead code/columns。

任何版本rollback都不得删除新schema数据或让active新mode run降级。必要时fence并由Reconciler诚实terminalize。

### 13.3 Tombstone优先级

delete/tombstone的`deletion_version`参与grant、heartbeat/event/artifact/result/checkpoint/resume/effect query/debug composition/download/retention。Delete CAS先赢时所有旧版本写拒绝；其他terminal CAS先赢后delete继续删除，但不改变历史result digest。测试必须覆盖每个route/内部入口，而不是只测result。

## 14. 运营、可观测性与回滚

### 14.1 必备指标

- Task lifecycle/Attempt termination/recovery eligibility和latency。
- stale fence、downgrade、grant/revoke、checkpoint fork、resume拒绝分原因。
- Requirement覆盖、Verifier slots/verdict/repair rounds。
- Effect state age、UNKNOWN count/reconciliation deadline。
- Debug availability/completeness/reason、redaction/composition failures。
- false verified/false discovery/task success/classification/cost，均带原始分母。
- legacy/new mode占比和三端版本矩阵。

高基数task/tenant ID只进受控trace，不做无界metric label。日志不含secret/source/full prompt。

### 14.2 Runbooks

每版必须提供：grant/downgrade conflict、stale epoch、checkpoint corruption、resume denial、result conflict、UNKNOWN effect、debug partial/failed、tombstone race、old-client rollback。Runbook只允许查询/恢复，不得指示operator重发未知effect。

### 14.3 自动rollback门

任一条件触发暂停新grant/新version rollout：

- stale/cancel后成功发布>0。
- Effect duplicate或UNKNOWN被错误隐藏>0。
- cross-tenant/source/secret泄漏>0。
- schema/digest/receipt无法复算。
- false verified超过冻结门或显著回归。
- legacy control组出现契约回归。

停止新grant不改变active run mode。安全事件可revoke/fence；普通质量回归让active run按冻结版本完成，除非release policy明确要求终止。

## 15. 全目标追踪矩阵

| 目标设计能力 | 实现版本 | 完成证据 |
|---|---|---|
| Thin deterministic kernel、persistent Owner、typed tools | MVP | schema/state/Gateway/recovery tests |
| Requirement Ledger、Observation/Attestation、双Gate | MVP | mandatory coverage和false-green fixtures |
| 同outer lease checkpoint恢复 | MVP | crash matrix |
| `agent_task_v1` grant/strict mode/TaskResult ingest | V1.0 | old/new兼容和digest/receipt tests |
| Public agentTask DTO、Server Reconciler | V1.0 | lifecycle oneOf、source audit、idempotency |
| Explorer/Troubleshooter/Implementer | V1.1 | bounded DAG/single-writer/ablation |
| R3 Effect Ledger和effect-aware terminalization | V1.2 | provider/reconciliation/cancel-loss矩阵 |
| Server-ACK watermark、same-run跨lease恢复 | V1.3 | resume/budget/event sequence crash矩阵 |
| WorkerFragment/ServerSnapshot/Assembly/Public Debug | V1.4 | causal/security/retention/delete矩阵 |
| Fleet/Profile/capability scheduling/cache | V2.0 | multiworker/cross-tenant/capability tests |
| R4安全控制基础 | V2.1 | default deny/two-person/UNKNOWN runbook |
| Eval-adaptive Quality/Verifier/skills | V3.0 | frozen candidate/holdout/canary/rollback |

## 16. 完全实现验收

只有以下全部成立才可关闭总体项目：

- 追踪矩阵每行有可定位commit、schema、fixture、test run和生产观察窗。
- 所有协议模式、Task/Attempt/Effect/Resume/Debug状态均是穷举state machine，无“其他情况按经验”。
- old/new Worker×Server×Web矩阵和grant on/off/revoked/partial组合自动化。
- stale lease/epoch、cancel后success、重复effect、secret/source/cross-tenant泄漏均为0。
- 所有success 100%覆盖mandatory Ledger、final Source/ExecutionState、ObservationManifest和Quality Policy要求Attestations。
- UNKNOWN effect全部在固定deadline前收敛或以专门outcome披露；restart不延长deadline。
- same-run resume不降mode、不延deadline/budget、不复活wait/cancel/revoke/delete，response loss幂等。
- Debug complete全部满足causal/hash/redaction；partial/absent/failed/expired/deleted不伪装URL，永不fallback audit。
- fleet维持单Worker单slot和instance isolation；cache不改变SourceState或tenant边界。
- R4默认deny；任何启用profile通过独立security/recovery审批。
- V3 candidate指标带原始分母、stable并发对照和rollback，生产策略不在线无门自改。
- 文档、runbook、schema registry与实现同步；没有用TODO或feature flag代替必需安全路径。

## 17. 每个版本的Agent执行模板

执行V1.0及之后任一版本时，Agent必须按顺序：

1. 读取涉及项目的`AGENTS.md`和本版本章节。
2. 重新盘点当前HEAD/dirty worktree/contract hashes，保护用户改动。
3. 把版本拆成tracer-bullet vertical slices；每slice跨必要端但保持可rollback。
4. 先提交schema、fixtures和失败测试，再实现。
5. 运行unit/contract/property/crash/security/old-new matrix。
6. 记录ControlPlaneDigest、migration/rollback证据和指标门。
7. 只在前版完成门通过后启用下一版能力。
8. 若发现本文未闭合且会改变权限、外部行为、数据或兼容性的选择，停止该slice并请求新决策；局部模块命名等可逆选择由Agent记录ADR后继续。

该模板不授权Agent自动推送生产、发外部消息、启用R3/R4或删除旧数据；这些动作仍需对应发布/运维权限。
