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
13. MVP第3.2节的模块与文件规模约束对Worker、Server、Web和contract package持续生效；每端都必须使用同一400行审查触发线、600行强制门禁、超大遗留文件baseline ratchet和显式例外机制。

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
- `agent-event/v1`、`effect-transition-event-payload/v1`及event response/prefix fixtures
- `agent-evidence-upload/v1`、`content-ref/v1`和evidence upload receipt
- `agent-worker-result-extension/v1`
- `task-record/v2`
- `task-result/v1`及全部引用schema
- `task-result-core/v1`
- `server-terminalization-input/v1`和`task-result-server-terminal/v1`
- `fragment-upload-receipt/v1`、`terminal-fragment-receipt/v1`、`task-result-receipt/v1`
- `worker-debug-fragment-descriptor/v1`
- `worker-debug-fragment-descriptor/v2`
- `agent-task-public/v1`
- `debug-bundle-public/v1`
- `worker-debug-fragment/v2`（V1.4启用；MVP v1保持只读兼容）
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

`supported` 只表示“本binary已通过该capability本版完成门且runtime feature可用”，禁止aspirational advertisement。`offer`只包含Server已部署且tenant可候选的能力，`selected`只包含本次Task真正授权的交集。能力依赖在schema中是有向无环图；任一依赖未selected时下游能力不能selected。V1.0只广告`agent_task_protocol`；动态角色、R3、resume、debug分别在V1.1/V1.2/V1.3/V1.4完成后才开始广告。

### 4.2.1 Schema major分配

| 启用点 | Worker Task record/result | Server Reconciler result | 约束 |
|---|---|---|---|
| MVP/legacy | `task-record/v1`、`task-result/v1` + `task-result-core/v1` | 不适用 | record protocol const legacy；result source const worker、Effect空 |
| C0/V1.0 | `task-record/v2`、`task-result/v1` + `task-result-core/v1` | `task-result-server-terminal/v1` | record v2预留agent mode与RECONCILING enum；V1.0禁止进入RECONCILING |
| V1.2 R3 | `task-record/v2`、`task-result/v2` + `task-result-core/v2` + `agent-worker-result-extension/v2` | `task-result-server-terminal/v2` | effect-aware branches；server schema source const server_reconciler |
| V2.1 R4 | `task-record/v2`、`task-result/v3` + `task-result-core/v3` + `agent-worker-result-extension/v3` | `task-result-server-terminal/v3` | R4 approval/profile/effect v2 refs |

`task-result/v1`、`task-result/v2`、`task-result/v3`始终是Worker-produced且`result_source=worker`。`task-result-server-terminal/v1`、`task-result-server-terminal/v2`、`task-result-server-terminal/v3`是独立strict schemas，公共字段复用生成代码但wire identity不同、`result_source=server_reconciler`、`worker_transport_envelope=null`；不得向任一既有major添加source enum。各server schema分别按V1.0、R3、R4章节开放branches。

Core major必须一一对应：对`task-result/vN`深复制、删除`/diagnostics/worker_debug_fragment`、把顶层schema替换为`task-result-core/vN`，除此之外不改字段。result major N只能引用/复算core major N；跨major ref原子拒绝。server-terminal schemas不绑定Worker terminal fragment，也不生成Worker Core。

`task-record/v2.protocol_mode=legacy_v1|agent_task_v1`，lifecycle enum预留`RECONCILING`，但只有selected `effect_ledger_r3|effect_ledger_r4`时该状态边才合法。Schema接受未来状态不等于 capability授权。旧v1 record永不原地改义。

Debug状态不扩展TaskResult enum：`diagnostics.worker_debug_fragment`从MVP起就是AvailabilityRef；其ref由`content_schema_id`选择v1/v2 descriptor。V1.0未selected时使用not_applicable，V1.4才允许v2 descriptor。

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
- grants、native events/event prefix、agent evidence objects/pins/upload receipts。
- resume operations、server terminalizations、debug captures/assemblies、effect records/transition receipts。
- artifact增加`validator_kind/schema_major/source_scope/immutable_revision`。

迁移采用expand → dual-read shadow → backfill/verify → enable write → switch read → 最后contract。C0不删除旧列、不改变legacy status。

启用V1.0 write前，Server先dual-read `task-record/v1`和`task-record/v2`。新Task直接建v2；既有v1 Task只有在`QUEUED`、无active lease/result且新claim将选择`agent_task_v1`时，才在claim同一事务CAS `record_schema_id:v1→v2`、`task_version:N→N+1`并append `task.schema_migrated` control event，随后签发grant。legacy claim保持v1。active legacy run永不在线迁移或改mode。

Rollback只关闭新agent grants；已有v2 Task由仍支持dual-read的binary完成或诚实fence。观察窗结束前不得部署不认识v2的旧Server，也不得v2→v1降级。backfill只校验候选，不替active Task换schema。

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
        {"id":"agent_task_protocol","major":1,"min_minor":0,"max_minor":0}
      ],
      "contract_bundle_digest":"<sha256>"
    }
  }
}
```

数组按ID排序、ID唯一、range合法。V1.0 binary不得提前列出`same_run_resume`或`worker_debug_fragment`；它们分别在V1.3/V1.4完成门通过后加入。`same_run_resume`依赖同major的`agent_task_protocol`，`worker_debug_fragment@2`同时依赖`agent_task_protocol@1`和`same_run_resume@1`。Register只返回offer，不签发执行权：

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
  "resume_until":null,
  "limits":{
    "agent_evidence_single_bytes":8388608,
    "agent_evidence_total_bytes":268435456,
    "agent_evidence_objects":4096,
    "schema_traversal_depth":64
  },
  "grant_digest":"<sha256>"
}
```

`grant_digest`对删除自身字段后的canonical object计算。`resume_until`只有`same_run_resume`被selected时才可为非null，且必须满足`issued_at < resume_until < absolute_deadline-terminal_reserve`；其他grant必须为null。Grant不是bearer secret，但所有写仍需Worker credential。Server durable row是权威；request自带grant object不能建立授权。

Grant bytes/digest/`expires_at`一经签发不可改写。heartbeat只续outer lease row，且新的`lease_expires_at <= grant.expires_at`；它不能延长/轮换grant，也不能延长`resume_until`。到达`expires_at-terminal_reserve`后Worker停止新dispatch并terminalize；过期后除明确的recovery upload grant外所有写拒绝。Resume若成功签发新grant，但其hard expiry仍不能超过Task absolute deadline。

选择不到`agent_task_protocol`时lease固定`protocol_mode=legacy_v1`且不发selected extension。一个run的mode不可变；新flag只影响新lease。安全撤销同时revoke grant、fence lease、记录audit，不能静默重解释payload。

### 5.3 严格extension门

`agent_task_v1`的每个native event、agent evidence upload、result，以及后续版本已selected的fragment/checkpoint/effect payload必须带：

- `grant_id/grant_digest`。
- `task_id/task_version/deletion_version`。
- `transport_attempt_id/transport_lease_epoch`。
- `native_attempt_id/native_epoch`。
- capability-specific payload/schema version。

Server在任何partial write前验证credential、scope、grant current、lease、epoch、version、deletion、selected capability。缺extension或不匹配是`PROTOCOL_DOWNGRADE_ATTEMPT`，原子拒绝，绝不能落入legacy validator。只有权威mode=legacy才走旧分支。

#### 5.3.1 原生事件与响应丢失

现有`POST /v1/review-runs/{run_id}/events`在`agent_task_v1`模式要求`extensions.agent_worker.event`为严格`agent-event/v1`：

```json
{
  "schema_id":"agent-event/v1",
  "event_id":"evt_<32hex>",
  "native_event_kind":"task.transition|attempt.transition|interaction.transition|effect.transition|server.cancel|server.policy_changed|server.deleted|agent.turn|tool.dispatch|tool.receipt|observation.committed|checkpoint.committed|gate.decided|task.terminalized",
  "task_id":"...",
  "observed_task_version":12,
  "deletion_version":0,
  "transport":{"attempt_id":"...","lease_epoch":2},
  "native":{"attempt_id":"...","epoch":4},
  "actor":{"schema_id":"actor/v1","kind":"task_owner","id":"owner_...","session_id":"sess_..."},
  "trace_id":"trace_<32hex>",
  "agent_id":"agent_<32hex>",
  "session_id":"sess_<32hex>",
  "tool_invocation_id":null,
  "payload_schema_id":"agent-turn-event-payload/v1",
  "payload_digest":"<sha256>"
}
```

event kind→nullable规则由`oneOf`关闭：task/control事件的`agent_id/session_id/tool_invocation_id`全为null；agent lifecycle/turn要求agent+session且tool为null；tool dispatch/receipt要求三者全非null；actor的session必须与顶层session相同。payload bytes要么是同一request内的strict payload，要么已作为agent evidence上传，Server必须重算digest。

控制事件payload是穷举联合：

- `task-transition-event-payload/v1`：`transition_id/from_lifecycle/to_lifecycle/from_desired_state/to_desired_state/previous_task_version/new_task_version/trigger/guard_digest`。
- `attempt-transition-event-payload/v1`：上述Task版本字段，加`attempt_id/previous_attempt_state/new_attempt_state/previous_attempt_state_version/new_attempt_state_version/native_epoch`。
- `interaction-transition-event-payload/v1`：上述Task版本字段，加`interaction_id/kind/previous_interaction_state/new_interaction_state/response_ref` AvailabilityRef。
- `effect-transition-event-payload/v1`：`effect_id/transition_seq/previous_effect_state/new_effect_state/previous_effect_version/new_effect_version/previous_task_version/new_task_version/previous_lifecycle/new_lifecycle/trigger/request_digest/receipt` AvailabilityRef。Effect-only变化要求Task version/lifecycle不变；若同事务令Task进入RECONCILING/FINALIZING，则要求`new_task_version=previous+1`并携带真实前后lifecycle。

`effect.transition`在C0 schema中预留但feature-gated；V1.2前或未selected`effect_ledger_r3|effect_ledger_r4`时以`CAPABILITY_DENIED`拒绝，不能产生event。它属于control event，顶层agent/session/tool字段全为null，actor只能`worker_control|system_reconciler`。

Worker的`task.transition|attempt.transition|interaction.transition`都要求`new_task_version=previous_task_version+1`。`effect.transition`按其payload分支：effect-only变化只递增effect version，Task version/lifecycle精确不变；同事务改变Task lifecycle时才要求Task version `+1`。Server依分支CAS current Task/effect/Attempt/interaction、append event并更新prefix；exact transition ID重试返回原receipt。普通telemetry不改变Task/Attempt，只允许`observed_task_version=current`。Task/Attempt/interaction未列出的边按共享state machine拒绝。

Cancel、policy mutation、deletion由Server控制面事务执行：先CAS并令task version +1（delete同时deletion version +1），再以`server_control` actor分配同一run的下一个event sequence并append`server.cancel|server.policy_changed|server.deleted`。Worker prepared的同sequence/version随后会409，response必须返回authoritative lifecycle/desired/task/deletion version、next sequence和prefix；Worker不得跳过Server事件或自行复用旧version。

event sequence由Server row串行化，Worker request携带base event的`sequence=expected_next_sequence`。Server-originated mutation和Worker event共享这一sequence/prefix空间，因此不存在两套不可比较watermark。

新模式的event delivery是exactly-once effect、at-least-once transport：`(run_id,event_id)`和`(run_id,sequence)`双唯一。相同event ID、sequence、canonical bytes和payload digest重发返回原receipt；同ID不同digest、同sequence不同ID或sequence小于next且无exact记录都409。成功response包含`accepted_event_id/last_accepted_event_sequence/next_event_sequence/event_prefix_digest`。

prefix固定为：

```text
prefix[0] = SHA256(UTF8("pullwise-agent-event-prefix/v1"))
prefix[n] = SHA256(prefix[n-1] || CanonicalJSON({sequence,event_id,native_event_kind,payload_digest}))
```

因此response丢失时Worker重发exact durable event，不沿用MVP的at-most-once gap策略。legacy mode仍保持MVP冻结行为。

#### 5.3.2 Agent evidence字节传输

V1.0在现有`POST /v1/review-runs/{run_id}/artifacts`增加仅`agent_task_v1`可用的`kind=agent_evidence`。这不是legacy required artifact，不进入旧artifact矩阵或公开下载列表。请求保留`content_base64`，并在`extensions.agent_worker.evidence`携带`agent-evidence-upload/v1`：grant/Task/transport/native binding、完整`content-ref/v1`、`content_schema_id`和`closure_role`。`closure_role` enum固定为`transitive|pre_gate_root_set|pre_gate_manifest|gate_input|gate_decision|final_closure|task_result_core_candidate`。Artifact metadata的artifact_id/sha256/size/media_type/encoding必须与ContentRef逐字段相等。

规则固定：

- Worker在result前上传final EvidenceClosure中的每一个对象及closure manifest自身，包括PreGate manifest、GateInput、GateDecision；TaskResult和全部debug/diagnostics对象按MVP无环规则排除。
`task_result_core_candidate`只允许当前ContractBundle已发布的`task-result-core/v1`、`task-result-core/v2`或`task-result-core/v3`，不进入PreGate/final closure；它在terminal fragment前上传到quarantine，供terminal receipt绑定，只有Result accept才与fragment一起pin。Server按4.2.1的N→N精确transform从候选TaskResult复算并校验major后才信任其ref。

- Server先验证size/hash/schema/tenant/task/scope；`(task_id,artifact_id)`相同exact bytes幂等，不同metadata或bytes冲突。单对象、单Task总量、对象数和schema递归深度使用grant `limits`；quarantine TTL由Server policy冻结并在upload receipt返回绝对`expires_at`。
- upload response返回immutable `agent-evidence-upload-receipt/v1`。未被result引用的对象只处于quarantine并按TTL GC；upload成功不等于terminal pin。
- result事务从Server evidence store解析全部ContentRef，按schema registry遍历，重算PreGate/final closure；任何missing、跨Task、未知schema、alias、cycle或hash/size不一致原子拒绝。
- result接受事务才把reachable对象pin到Task retention scope。Server不能相信Worker只提交的digest，也不能从Worker本地路径取bytes。

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
    "pre_gate_evidence_closure_digest":"...",
    "evidence_closure_digest":"..."
  },
  "terminal_event":{
    "event_id":"evt_<32hex>",
    "expected_sequence":42,
    "payload_schema_id":"task-terminal-transition-event-payload/v1",
    "payload_digest":"<sha256>"
  }
}
```

四个digest是TaskResult的sibling，不属于被hash对象。Server必须从已上传bytes自行重算PreGate/final EvidenceClosure、与TaskResult同major的严格Core transform和完整TaskResult，校验task/scan、run/job/lease、epochs、outcome→legacy status映射、Requirement覆盖和Effect状态。V1.0尚未selected debug时，`diagnostics.worker_debug_fragment`必须是`not_applicable/CAPABILITY_NOT_SELECTED` AvailabilityRef；V1.4 selected后才允许available descriptor并验证terminal receipt。

跨端冻结DAG必须与MVP一致：

```text
PreGateRootSet → PreGateEvidenceClosure
→ (SuccessGateInputSnapshot | TerminalizationInputSnapshot) → GateDecision
→ final EvidenceClosureManifest → TaskResultCore
→ [V1.4 terminal fragment → terminal receipt] → complete TaskResult
```

GateInput不得引用final closure；两种closure都排除TaskResult/Core和全部debug/diagnostics。Server schema tests必须对每条禁止的反向边提供negative fixture。

Result accept同一事务：

1. 验证legacy required artifacts和extension/grant。
2. 从quarantine解析/验证全部ContentRef、PreGate/final closure及`task_result_core_candidate`；尚不pin。
3. 依selected capabilities解析`diagnostics.worker_debug_fragment` AvailabilityRef；available ref必须指向`worker-debug-fragment-descriptor/v2`。其`uploaded`分支必须同时携带`server_fragment_ref`和`terminal_receipt_id`；`local_only`分支必须令二者同时为null；外层`unavailable|not_applicable`同样不得有任何Server ref/receipt。
4. 重算terminal event payload：`FINALIZING→TERMINAL`、previous/terminal Task version、desired state、outcome、result digest、actor和termination cause必须与TaskResult一致；校验Worker提供的event ID/expected sequence/payload digest。
5. insert immutable raw envelope/TaskResult/TaskResultCore；若是`uploaded`分支，对`terminal-fragment-receipt/v1.bound_task_result_digest`执行唯一`null → task_result_digest` CAS；然后insert result receipt binding，并在同一事务把reachable evidence、core candidate和receipt绑定的terminal fragment从quarantine转为Task retention pin。
6. CAS Task terminal version、review run和scan projection，同时以authoritative next sequence append terminal event、更新event prefix；任一version/sequence/cancel/delete冲突令整个事务回滚。
7. durable保存并返回`task-result-receipt/v1`；response包含accepted/duplicate/receipt、terminal versions和terminal event receipt。

完全相同attempt+四个digests+canonical envelope幂等；任一不同为409。成功response丢失后重发必须返回逐字节相同receipt。
Terminal receipt的相同`receipt_id/core_ref.sha256/full_digest/canonical envelope`重试沿用同一binding和result receipt；receipt已绑定不同full digest时409 `TERMINAL_RECEIPT_ALREADY_BOUND`。若结果事务在CAS前后任一步失败，整笔回滚，receipt仍未绑定，core与fragment都保持quarantine，不产生可下载terminal assembly。


terminal event属于authoritative prefix且只能由result事务写一次；Worker不得在普通event route预先发送`task.terminalized`。Server Reconciler使用相同payload schema但actor/result_source为`system_reconciler`。

### 5.5 Server Reconciler variant

V1.0该method只写`task-result-server-terminal/v1`；V1.2 selected R3时写`task-result-server-terminal/v2`，V2.1 selected R4时写`task-result-server-terminal/v3`。它绝不生成Worker的`task-result/v1`、`task-result/v2`或`task-result/v3`，也不携带agent-worker result extension。

新增内部、非HTTP `commit_server_terminal_result`：

- service identity，无Worker lease/grant，只能产生非成功outcome。
- 输入绑定latest task/deletion version、Server持久化request、`ack_checkpoint` AvailabilityRef、已知artifact/Observation/effect receipts。V1.0/V1.2固定`unavailable/CAPABILITY_NOT_SELECTED`；只有该run实际selected`same_run_resume`时才要求available并精确匹配watermark。
- 缺失事实用AvailabilityRef写`unavailable + reason_code`，不伪造digest/PASS/Worker envelope。
- `result_source=server_reconciler`、`worker_transport_envelope=null`。
- idempotency为`task_id + terminal_task_version + termination_cause + input_digest`。

空Effect Ledger且满足active-ownership谓词的`WORKER_LOST`仍是独立Server-owned transport terminal，无TaskResult；有任何effect row就不能用纯lost吞掉事实。

### 5.6 Public `agentTask` DTO

Server从权威Task/Attempt/interaction/event rows持续生成非终态allowlisted投影；terminal result接受后再从validated TaskResult填充终态字段。它不把raw Worker extension直接给Web：

```json
{
  "schemaVersion":"agent-task-public/v1",
  "taskId":"...",
  "lifecycle":"queued|active|waiting_input|waiting_approval|finalizing|reconciling|terminal",
  "desiredState":"run|cancel",
  "outcome":null,
  "attempt":{"nativeEpoch":2,"status":"running"},
  "interaction":null,
  "terminal":null,
  "requirements":{"mandatory":5,"pass":3,"fail":0,"unverifiable":2,"waived":0},
  "verification":{"qualityRisk":"Q1","requiredSlots":1,"passedSlots":0},
  "effects":{"total":0,"unknown":0},
  "evidence":{"availability":"not_started|partial|complete|unavailable","reasonCode":null},
  "debugBundle": {
    "schemaVersion":"debug-bundle-public/v1",
    "availability":"unsupported",
    "completeness":null,
    "reasonCodes":["CAPABILITY_NOT_SELECTED"],
    "downloadUrl":null,
    "sha256":null,
    "sizeBytes":null,
    "createdAt":null,
    "expiresAt":null
  },
  "transport":{"status":"running|lost|cancel_requested|cancelling|terminal"},
  "updatedAt":"...Z"
}
```

DTO是严格lifecycle `oneOf`；显式nullable字段只能按下表出现，不能靠缺字段表达：

| lifecycle | `attempt` | `interaction` | `terminal`/`outcome` | 额外不变量 |
|---|---|---|---|---|
| `queued` | null | null | null/null | transport非lost；没有current Attempt |
| `active` | object，status=`leased|preparing|running|verifying|publishing` | null | null/null | nativeEpoch/current Task一致 |
| `waiting_input|waiting_approval` | object且status=`suspended` | object且kind匹配lifecycle | null/null | interaction deadline/ID非空 |
| `finalizing` | object或显式null | null | null/null | success/terminal Gate正在运行 |
| `reconciling` | object或显式null | null | null/null | `effects.unknown>0`或有unresolved dispatched effect |
| `terminal` + `terminal.kind=task_result` | object或null | null | object/非null outcome | resultSource/digest/terminalTaskVersion非空 |
| `terminal` + `terminal.kind=worker_lost` | object或null | null | object/null | effects.total必须0，result/digest不存在，evidence unavailable reason=`WORKER_LOST` |

requirements/verification/effects counts从Server验证后的对象机械投影且必须非负、内部求和一致。`evidence.availability=complete`只在final closure全部pin并复算通过后允许；上传了一部分只能partial。公开对象不含prompt、session/thread ID、内部路径、raw ContentRef或Worker给出的外域URL。

`debugBundle`字段从C0起始终存在，严格使用`debug-bundle-public/v1`；其唯一规范路径是`reviewRun.agentTask.debugBundle`（列表项同构为`reviewRuns[].agentTask.debugBundle`）。不得另建`reviewRun.debugBundle`新对象；旧`reviewRun.debugBundleUrl`仅作为9.6规定的complete兼容alias。

旧scan `status`保留业务兼容；transport lost不再伪装为Worker FAILED TaskResult，而在`agentTask.transport.status=lost`表达。Web正式接受`cancel_requested/cancelling`，不得把它们normalize成queued。

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
- agent evidence每种schema的upload/hash/size/cross-task/TTL/pin/GC，以及missing/cycle/alias/未知schema negative fixtures全通过；Server不依赖Worker本地bytes。
- native event exact replay、ID/sequence冲突、prefix digest和response-loss测试全通过。
- 全部TaskResult outcome映射、PreGate/final closure DAG、digest、receipt、duplicate/conflict测试。
- Reconciler缺失证据诚实表达、幂等、与cancel/delete竞态。
- old/new三端矩阵全自动化；legacy bytes无回归。
- Web不读取raw Worker envelope，不泄露thread/secret/internal path。

## 6. V1.1：有界动态 Agent

V1.1完成门通过后Worker才广告`dynamic_agent_roles@1.0`；它依赖`agent_task_protocol@1`。Pullwise read-only profile即使binary支持，也可不在本次grant selected Implementer写能力。

MVP `actor/v1`已经预留`explorer|troubleshooter|implementer`，但预留kind在未selected时由Gateway拒绝。V1.1继续使用actor/v1，不新增enum；每个动态session的actor kind必须与DelegationRecord role相等，不能伪装成Task Owner或Verifier。

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

V1.2完成门通过后，Worker才广告`effect_ledger_r3@1.0`；它依赖`agent_task_protocol@1`。Grant未selected时任何R3 descriptor/transition都在dispatch前`CAPABILITY_DENIED`，不能只写本地row后继续。

### 7.0 Schema major与兼容

Effect-aware run必须使用`agent-worker-result-extension/v2`和`task-result/v2`；v2继承v1公共字段，但增加非空Effect Ledger约束与`CANCELLED_WITH_EFFECTS|TERMINATED_WITH_UNKNOWN_EFFECTS` branches。Server dual-read v1/v2；grant selected`effect_ledger_r3`后只接受v2，未selected时只接受v1且Effect rows必须空。不能用同一`task-result/v1`偷偷增加enum。

`agent-task-public/v1`在C0就预留`reconciling` lifecycle和上述两个outcome enum，但V1.2前的projection rule禁止产生它们；因此公开DTO无需改major。旧Web遇到未知outcome只显示legacy status，新Web按schema feature detection显示effect警告。
V1.2 rollout前必须先发布并由Server/Worker pin包含`agent-worker-result-extension/v2`、`task-result/v2`、`task-result-core/v2`、`task-result-server-terminal/v2`及全部valid/invalid/golden fixtures的新ContractBundleDigest；schema/storage → dual-read → Worker advertisement/grant的顺序与C0相同。


新branch严格字段：

- `CANCELLED_WITH_EFFECTS`：至少一条COMMITTED effect、UNKNOWN=0，绑定cancel linearization、每条effect/final receipt、post-action verification和residual risk。
- `TERMINATED_WITH_UNKNOWN_EFFECTS`：至少一条UNKNOWN，绑定reconciliation deadline、last query receipts、`requires_human_reconciliation=true`和人工恢复说明；任何成功/PASS汇总被禁止。

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
- `last_transition_seq/digest/server_effect_version`。

状态只允许：PREPARED→DISPATCHED/NOT_APPLIED/REJECTED；DISPATCHED→COMMITTED/NOT_APPLIED/REJECTED/UNKNOWN；UNKNOWN→COMMITTED/NOT_APPLIED/REJECTED。`COMMITTED|NOT_APPLIED|REJECTED`是Effect终态，无出边；`UNKNOWN`是未解决状态，不是Effect终态，但Task可用`TERMINATED_WITH_UNKNOWN_EFFECTS`终态化。TaskResult发布前Reconciler可解析UNKNOWN；发布后任何新事实只形成`EffectResolutionAddendum`，不改原Effect snapshot、TaskResult或outcome统计。

`PREPARED`机械表示“provider client尚未获准发送bytes”，因此可以确定无外部effect；`DISPATCHED`机械表示“exact request可能已经交给provider”，不声称成功。任何无法证明请求未离开进程的crash/timeout都不能停留PREPARED或映射NOT_APPLIED。

### 7.2 Dispatch原子边界

1. Gateway验证grant/approval/budget/epoch。
2. 生成provider idempotency key和request digest。
3. 单一SQLite事务写PREPARED、预算reservation、effect watermark、exact request ContentRef和transition outbox。
4. 将PREPARED exact transition写入Server Effect Ledger并取得ACK；ACK不明时只查询/重发exact transition，不调用provider。
5. 再验证cancel/revoke/deletion/grant expiry/epoch；若已失效，以PREPARED→NOT_APPLIED或REJECTED收敛。
6. 本地CAS PREPARED→DISPATCHED并durable写transition outbox，再把同一transition提交Server；只有Server明确ACK当前DISPATCHED version后才允许进入provider client。
7. 用已sealed exact bytes和同一idempotency key调用provider至多一次。Server ACK后、实际send前crash也保持DISPATCHED，由Reconciler查询，不能把它降回PREPARED。
8. provider response/timeout先原样落immutable receipt，再提交terminal/UNKNOWN transition；证明成功→COMMITTED，证明未应用→NOT_APPLIED，明确provider拒绝且无应用→REJECTED，固定deadline仍不明→UNKNOWN。

不得把HTTP timeout等同NOT_APPLIED，不得换idempotency key盲重试。

### 7.3 Server Effect Ledger协议

新增`POST /v1/agent-tasks/{task_id}/effects/{effect_id}/transitions`和read-only exact lookup。`effect-transition/v1`必须带grant/task/deletion/transport/native fence、`transition_seq`、expected Server effect version、from/to、idempotency key、request digest、相应receipt refs，以及`event_id/expected_event_sequence/effect-transition-event-payload/v1 digest`。Server在任何写前验证selected`effect_ledger_r3`、状态边和所有fence。

Server规则：

- 单一事务CAS Effect/必要的Task lifecycle+version、append `effect.transition`、更新shared prefix并保存transition/event receipt；任一步失败全回滚。response返回effect version、accepted/next event sequence和prefix digest。
- `(effect_id,transition_seq)`及`event_id` exact canonical bytes幂等；同seq不同bytes、跳seq、from/version不匹配、event sequence冲突为409且零partial write。response loss重发exact request返回原两类receipt。
- 首个PREPARED创建Server durable row；DISPATCHED ACK是provider dispatch的必要前置条件。Server不可用时能力fail closed。
- credential/cancel/delete在PREPARED时可拒绝并证明NOT_APPLIED；在DISPATCHED后只能fence新调用并进入reconciliation，不能删除row或宣称未应用。
- terminal result ingest要求Server Effect snapshot、Worker closure中的Effect snapshot和state counts逐项一致。Worker loss/reaper直接读取Server ledger，因此不能漏掉已可能dispatch的effect。
- Worker提交的provider receipt bytes也走agent evidence transport并由ContentRef绑定；Server只保存allowlisted provider identity/status/timestamps到公开投影。

Worker grant/lease失效后，受信Server Reconciler使用非HTTP service method `commit_effect_reconciliation_transition`。输入为service identity、task/deletion/effect current versions、effect ID、from/to、query operation ID、provider identity、receipt bytes+schema和input digest；它不需要也不接受Worker grant。

该method只允许`DISPATCHED→UNKNOWN|COMMITTED|NOT_APPLIED|REJECTED`、`UNKNOWN→COMMITTED|NOT_APPLIED|REJECTED`，以及TaskResult发布后的ResolutionAddendum；禁止PREPARED/DISPATCHED创建、provider action dispatch和权限扩大。receipt由Server直接写evidence CAS并生成ContentRef，source=`server_reconciler`。

幂等键为`effect_id + expected_effect_version + query_operation_id + receipt_digest + target_state`；event ID确定性为`evt_ + first32hex(SHA256(UTF8("effect-reconciler-event/v1\0" + idempotency_key)))`。同key exact重试返回原transition/event receipt，不同bytes冲突。单一事务分配authoritative next sequence，检查deletion/effect/Task version，写receipt/transition、按上述payload更新Task RECONCILING projection、append event并更新prefix。Delete/tombstone先赢则拒绝Task写并只留不含tenant payload的security audit；transition先赢后delete仍可随后删除bytes，历史digest不改。

### 7.4 Reconciliation

Reconciler是受信控制面actor：按provider query contract有界查询，所有query有budget/速率/credential scope，结果为immutable receipt。重启不得重置deadline。`DISPATCHED`没有确定证据时，在任何terminalization前CAS提升UNKNOWN。

Reconciler只能在provider contract明确`query_by_idempotency_key`或等价稳定identity时自动判断。重新发送exact request仅在provider contract声明“相同key replay不产生第二effect”且policy显式允许时可用；否则只query或交人工。query冲突按证据优先级规则进入UNKNOWN，不由Agent选择更好结果。

### 7.5 Effect-aware outcome

- 有确认effect且安全partial deliverable：`PARTIAL`或cancel时`CANCELLED_WITH_EFFECTS`，逐项披露receipt。
- deadline仍UNKNOWN：`TERMINATED_WITH_UNKNOWN_EFFECTS`，`requires_human_reconciliation=true`，优先级高于FAILED/CANCELLED/PARTIAL。
- 所有effect NOT_APPLIED/REJECTED且其他条件满足时可按普通outcome。
- `COMPLETED`要求所有预期effect COMMITTED且receipt/requirements PASS，无UNKNOWN。
- Worker loss只要Effect Ledger非空就不能使用纯`WORKER_LOST` terminal；必须fence Attempt并走Reconciler/Terminalization Gate。

Task lifecycle新增以下穷举边；未列出一律拒绝：

| From | Trigger | Effect guard | To | 结果/写集 |
|---|---|---|---|---|
| ACTIVE/FINALIZING | cancel/deadline/lease loss/runtime loss | 任一DISPATCHED或UNKNOWN | RECONCILING | desired/fence、Task version+1、Server Reconciler接管 |
| ACTIVE/FINALIZING | worker lost | Effect Ledger非空 | RECONCILING | fence Attempt；禁止pure WORKER_LOST |
| ACTIVE | cancel | 所有rows已终态，COMMITTED>0 | FINALIZING | desired=CANCEL、冻结effect snapshot、Task version+1；目标`CANCELLED_WITH_EFFECTS` |
| ACTIVE | cancel | rows为空或全部NOT_APPLIED/REJECTED | FINALIZING | desired=CANCEL、冻结effect snapshot、Task version+1；目标普通`CANCELLED` |
| RECONCILING | all effects COMMITTED/NOT_APPLIED/REJECTED | UNKNOWN=0 | FINALIZING | freezeeffect snapshot、Task version+1 |
| RECONCILING | reconciliation deadline | UNKNOWN>0 | TERMINAL | `TERMINATED_WITH_UNKNOWN_EFFECTS` result v2 |
| FINALIZING | cancel先赢 | COMMITTED>0且UNKNOWN=0 | TERMINAL | `CANCELLED_WITH_EFFECTS` result v2 |
| FINALIZING | cancel先赢 | COMMITTED=0且UNKNOWN=0 | TERMINAL |普通`CANCELLED` |
| FINALIZING | Success Gate | 所有expected COMMITTED、UNKNOWN=0 | TERMINAL | COMPLETED类result v2 |
| 任意非终态 | delete/tombstone | deletion CAS先赢 | tombstoned | 不接受新TaskResult/effect transition |

terminal outcome优先级固定：delete fence（不产生新result） > UNKNOWN effect → `TERMINATED_WITH_UNKNOWN_EFFECTS` > cancel且COMMITTED effect → `CANCELLED_WITH_EFFECTS` > cancel无effect → `CANCELLED` > safe PARTIAL > FAILED/BLOCKED > success。Agent、Worker或Reconciler都不能调换。

legacy wrapper映射固定：
ACTIVE cancel时若仍有PREPARED row，必须先用7.2的无dispatch证明把它CAS为NOT_APPLIED/REJECTED，再选择上述已收敛分支；不得跳过row或把PREPARED当作UNKNOWN。


| TaskResult v2 | legacy wrapper/execution | legacy error code | 新DTO |
|---|---|---|---|
| `CANCELLED_WITH_EFFECTS` | `cancelled/cancelled` | `CANCELLED_WITH_EFFECTS` | 保留effect counts/receipts投影 |
| `TERMINATED_WITH_UNKNOWN_EFFECTS` | `failed/failed` | `UNKNOWN_EFFECTS` | lifecycle terminal、human reconciliation=true |
| 其他v2 outcome | 沿用V1.0映射 | 对应稳定code | v1 allowlist projection |

### 7.6 R3审批

ApprovalEvent必须绑定task/version/ledger/policy/effect action/scope/expiry/actor authority/signature。approval只授权一个精确action scope，不能授权任意后续请求。撤销在dispatch前阻止动作；dispatch后进入Effect事实处理，不能假装撤销已回滚provider。

### 7.7 V1.2完成门

- result extension/task-result v1/v2 grant选择、双读、错误major和old Web兼容矩阵全通过。
- ACTIVE/FINALIZING/RECONCILING/TERMINAL每条合法边与每条非法边都有fixture；UNKNOWN outcome优先级不可被cancel/failed覆盖。
- 每个state/event组合、timeout/response-loss/query冲突和crash point测试。
- provider fake支持duplicate receipt、late commit、not-applied、unknown、credential revoke。
- cancel/lease loss/deadline/delete与PREPARED/DISPATCHED/COMMITTED/UNKNOWN笛卡尔矩阵。
- local/Server transition outbox和ACK response-loss、Server unavailable、DISPATCHED ACK→provider send之间crash全部证明零漏记effect。
- Server reaper在Worker消失时仅凭Server ledger即可选择WORKER_LOST或RECONCILING；任何DISPATCHED/UNKNOWN都不能被pure lost隐藏。
- service Reconciler在Worker grant失效后的每条允许transition、Addendum、response loss和delete竞态全通过；绝无新dispatch。
- 零盲重试、零effect被普通失败隐藏、reconciliation deadline不延长。
- Human reconciliation DTO和Addendum不改原TaskResult/outcome统计。

## 8. V1.3：Server-ACK Checkpoint 与 `same_run_resume`

V1.3完成门通过后，Worker才把`same_run_resume@1.0`加入advertisement；它依赖`agent_task_protocol@1`。只有Server offer且本次grant selected时，grant的`resume_until`才非null。V1.0/V1.2 Worker不能因本地checkpoint存在就请求resume。

### 8.1 Checkpoint watermark

Worker每次LocalCommittedCheckpoint后，在旧lease仍有效时只通过`agent-event/v1` route发送`native_event_kind=checkpoint.committed`；heartbeat只能回报最近ACK identity，不能提交或重试watermark。checkpoint event envelope的sequence为`S`，payload中的`last_local_event_sequence=S-1`，消除prefix自引用：

```json
{
  "generation":12,
  "manifest_hash":"...",
  "previous_manifest_hash":"...",
  "committed_from_task_version":30,
  "committed_task_version":31,
  "deletion_version":0,
  "transport_lease_epoch":4,
  "native_epoch":7,
  "last_local_event_sequence":413,
  "last_server_acked_event_sequence":407,
  "event_prefix_digest_at_ack":"..."
}
```

Server同事务验证grant选择`agent_task_protocol+same_run_resume`、scope/epoch/deletion、`generation=stored+1`、previous hash和`committed_from_task_version=current N/committed_task_version=N+1`；然后CAS Task version、append该checkpoint event、更新prefix、保存含`event_sequence=S`的`ServerResumeCheckpointWatermark`并ACK。任一步失败全回滚。完全相同event ID/sequence/canonical bytes重试幂等；跳代、分叉、旧epoch、同generation不同hash拒绝。只有ACK generation可跨lease恢复；checkpoint bytes仍留Worker，Server不解析语义。

Server返回authoritative `last_accepted_event_sequence=S/next_event_sequence=S+1/event_prefix_digest=prefix[S]`，使用5.3.1的event identity/prefix算法解决MVP event ambiguity。Worker恢复时采用Server watermark，不回退sequence；只允许exact event ID/sequence/canonical bytes重放，不同digest冲突。

Checkpoint ACK body本身作为`server-resume-checkpoint-watermark/v1` durable保存；event response丢失时Worker只在同一event route重发exact event，Server返回逐字节相同ACK。heartbeat携带checkpoint commit字段或同generation跨route重试均以`CHECKPOINT_ROUTE_INVALID`拒绝且零写入。若其间cancel/revoke/delete改变fence，Server返回相应拒绝且不把旧ACK升级为恢复授权；该code必须进入C0 ErrorResponse registry，HTTP 409、retry scope none。

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

Server创建`resume-operation/v1`，状态`PREPARED→COMMITTED|REJECTED|COMMITTED_THEN_REVOKED`。设request的`expected_task_version=N`；成功同一事务：锁Task/lease → 验证谓词 → 核销旧reservation → 预留新slice → fence旧attempt → 创建successor transport attempt/epoch和新grant → CAS Task `N→N+1` → append `attempt.transition`（旧Attempt fenced，successor=`PENDING_CONFIRM`）并更新event prefix → 保存同job/run lease、replay fence和canonical response bytes → commit。successor lease状态固定为`PENDING_CONFIRM`，grant authorization state固定为`pending_confirm`，response含`successor_task_version=N+1`和`confirm_deadline_at=min(now+60s,resume_until,grant.expires_at,absolute_deadline-terminal_reserve)`。

`replay_fence`至少分别绑定`resume_from_task_version=N/successor_task_version=N+1/deletion_version/desired_state/security_revocation_epoch/successor_grant_id/successor_lease_epoch/absolute_deadline`。相同operation ID的后续请求先锁行并重验fence：

- fence完全未变且successor仍current：返回逐字节相同success response。
- cancel/delete/security或credential revoke已赢：同一事务fence successor lease、revoke grant、释放未消费新slice，状态转`COMMITTED_THEN_REVOKED`，返回稳定`RESUME_CANCELLED_AFTER_COMMIT|RESUME_DELETED_AFTER_COMMIT|RESUME_REVOKED_AFTER_COMMIT`，绝不重放旧grant。
- operation字段不同、另一个successor已current或version不一致：409 `RESUME_OPERATION_CONFLICT`，不创建第二lease/budget。

这意味着idempotency保证“至多一个successor”，不保证在安全状态改变后重放一份已失效success。旧response bytes保留审计，但不能再次成为授权。

### 8.5 Successor confirm

Worker拿到successor后先验证response binding并加载/校验ACK checkpoint bytes，但不能恢复SDK thread或启动Agent/tool。它在本地pending记录预分配`native_attempt_id/native_epoch=current+1`和`owner_id/owner_epoch=current+1`，再通过heartbeat发送严格`resume-successor-confirm/v1`：

```json
{
  "schema_id":"resume-successor-confirm/v1",
  "confirm_id":"confirm_<32hex>",
  "operation_id":"resume_...",
  "grant_id":"grant_...",
  "grant_digest":"...",
  "task_id":"...",
  "expected_task_version":32,
  "deletion_version":0,
  "security_revocation_epoch":5,
  "transport":{"attempt_id":"...","lease_id":"...","lease_epoch":5},
  "native":{"attempt_id":"nat_...","epoch":8},
  "owner":{"owner_id":"owner_...","epoch":9},
  "checkpoint":{"generation":12,"manifest_hash":"..."}
}
```

在`PENDING_CONFIRM`期间，严格extension门除该confirm外拒绝event/evidence/checkpoint/effect/result/fragment，Server返回`RESUME_CONFIRM_REQUIRED`；Worker Gateway也拒绝所有Agent/tool dispatch。普通heartbeat不得把pending lease变active。

Confirm单一事务要求`expected_task_version=successor_task_version=N+1`，重验replay fence、Task/deletion/security versions、deadline/budget、successor identity和ACK checkpoint；然后绑定native/owner identity、令lease/grant authorization state变`ACTIVE/active`、CAS Task `N+1→N+2`，并按5.3.1 append `attempt.transition`（successor `PENDING_CONFIRM→PREPARING`）event/prefix，durable保存`resume-successor-confirm-receipt/v1`。只有ACK receipt返回后Worker才把本地pending Attempt变PREPARING并恢复runtime。示例因此固定为resume request 31、resume commit 32、confirm ACK 33。

`(operation_id,confirm_id)` exact重试返回同一ACK；同ID不同identity冲突。Cancel/Delete/Revoke在confirm事务前或并发先赢时，fence/revoke successor、释放未消费slice并返回稳定拒绝。超过confirm deadline未ACK时Server sweeper执行同一revoke/release事务并令operation`COMMITTED_THEN_REVOKED`，不得自动claim其他job。

### 8.6 拒绝分类

- cancel先赢 → `RESUME_CANCELLED`，交Terminalization。
- budget/deadline不足 → `RESUME_BUDGET_INSUFFICIENT`，诚实PARTIAL/FAILED。
- waiting/suspended/normal release → `RESUME_NOT_ACTIVE_OWNERSHIP`，保持等待策略。
- security/credential revoke或tombstone → permanent拒绝并audit。
- watermark/identity/version冲突 → permanent consistency failure。
- rollout无法保持mode →拒绝，不降legacy。

### 8.7 V1.3完成门

- watermark ACK/duplicate/gap/fork/old epoch/revoke/delete tests。
- resume success、每个拒绝、Cancel竞态、budget transfer、response loss；fence未变exact replay，fence改变稳定拒绝且旧grant不可用。
- 在旧reservation核销、新slice、新lease/new grant每个crash point注入故障，无双lease/双budget。
- success commit→response loss→cancel/revoke/delete→same operation replay，以及success response收到→confirm前撤销，全部零Agent/tool dispatch。
- PENDING_CONFIRM期间每个非confirm入口都拒绝；confirm exact replay、identity conflict、timeout sweeper、ACK loss和Task version event CAS全通过。
- native/owner pending identity在ACK前不启动runtime，ACK后只启动一次。
- event sequence不回退，checkpoint本地较新未ACK不恢复。
- WAITING/SUSPENDED/normal release不被sweeper误判WORKER_LOST。

## 9. V1.4：完整 Debug 诊断读模型

V1.4完成门通过后，Worker才广告`worker_debug_fragment@2.0`；它同时依赖`agent_task_protocol@1`和`same_run_resume@1`，Server必须实现ACK watermark、snapshot/composer/public projection。只会上传fragment但没有Server Composer，或不能签发resume/watermark依赖的部署，不得offer/select该能力。MVP的`worker-debug-fragment/v1`仅作只读迁移输入，不能由V1.4新capture产生，也不能单独满足complete条件。

### 9.1 三层对象

1. `WorkerDebugFragment`：Worker对某run/attempt的immutable、redacted分片。
2. `ServerDebugSnapshot`：Server在一个read-only transaction中冻结同scope权威状态。
3. `DebugAssembly`：Server Composer验证二者identity/causal cut/hash/redaction后生成的immutable terminal包。

Legacy dynamic download继续单独标`legacy_combined`，不能升级为complete。

### 9.2 Capture与receipt

`worker-debug-fragment/v2`的capture kind固定为`startup|checkpoint|terminal|crash|postlude`；v1只允许`startup|checkpoint|terminal|crash`。V1.4所有新capture必须写v2，v1只可由迁移reader读取并最多生成`partial/LEGACY_FRAGMENT_SCHEMA`。complete assembly的terminal fragment必须是v2。

普通capture必须有当前grant。所有upload成功先返回`fragment-upload-receipt/v1`，字段固定为`receipt_id/fragment_ref/task_id/deletion_version/run_id/transport attempt+epoch/native attempt+epoch/capture_kind/snapshot_seq/captured_at/accepted_at`。exact fragment identity/bytes重试返回同一receipt；同identity不同bytes为`DEBUG_RECEIPT_CONFLICT`。

startup/checkpoint/crash/postlude fragment的`task_result_core`必须是AvailabilityRef `not_applicable`。terminal fragment只能在TaskResultCore后生成，其`task_result_core`必须是available ContentRef，且`content_schema_id=task-result-core/vN`必须与待发布`task-result/vN`同major；Server要求该exact ref已作为`task_result_core_candidate`在quarantine。`terminal-fragment-receipt/v1`字段固定使用`task_result_core_ref`（完整ContentRef），不得再出现裸`task_result_core_digest`或把字符串写成`available`；receipt另绑定`task_result_schema_id`（非enum schema-ID字符串，必须存在于当前ContractBundle并与Core同major）、grant/task/deletion/epochs并初始化`bound_task_result_digest=null`。该字段只能由5.4 Result accept事务做一次性CAS。普通receipt不能冒充terminal receipt。

TaskResult引用的`worker-debug-fragment-descriptor/v2`是immutable CAS对象，`additionalProperties=false`，严格oneOf：

- `uploaded`：要求`fragment_ref`和`server_fragment_ref`均为完整ContentRef且`sha256/size_bytes/content_schema_id=worker-debug-fragment/v2`逐项相同，另要求`sealed=true`、`snapshot_seq`、`source_sha256`、`transport_kind=agent_terminal_fragment`、`terminal_receipt_id`、`terminal_receipt_ref`和`reason_code=null`。
- `local_only`：要求本地`fragment_ref`、`sealed=true`、`snapshot_seq/source_sha256`，并令`server_fragment_ref/terminal_receipt_id/terminal_receipt_ref`全部null，`transport_kind=none`，reason为稳定生成/上传失败code。

没有安全sealed fragment时，TaskResult外层AvailabilityRef使用`unavailable + stable reason`，不创建descriptor；capability未selected则使用`not_applicable/CAPABILITY_NOT_SELECTED`。Result accept校验的是descriptor ContentRef与receipt/core的整条链，不能相信内嵌字符串。

早期capture的core digest为not_applicable；terminal必须available。普通fragment不能包含最终含receipt的TaskResult bytes。Server接受result后，完整TaskResult digest进入ServerSnapshot，保持无环。

完整冻结顺序是：

```text
PreGate closure → GateInput → GateDecision → final closure
→ TaskResultCore → terminal fragment → terminal-fragment receipt
→ complete TaskResult → task-result receipt → ServerDebugSnapshot → DebugAssembly
```

final closure排除全部debug对象；terminal fragment不得包含complete TaskResult/result receipt；TaskResult可包含terminal fragment descriptor+terminal receipt；ServerSnapshot只在result接受后生成。任何反向ContentRef都按cycle拒绝。

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
- checkpoint generation/hash必须精确等于Server ACK watermark；不存在ACK、仅本地更新或所谓terminal successor都只能是`partial/CHECKPOINT_NOT_ACKED`。
- fragment、snapshot、result、receipt、file/component manifests全部hash/size/schema通过。
- 两次redaction scan通过，无source/secret。

任一不足只能`partial`并给穷举reason code；最小registry为`IDENTITY_MISMATCH|TASK_VERSION_MISMATCH|EVENT_PREFIX_MISMATCH|SERVER_BOUND_EVENT_MISSING|CHECKPOINT_NOT_ACKED|LEGACY_FRAGMENT_SCHEMA|FRAGMENT_MISSING|WORKER_FRAGMENT_MISSING|ORPHAN_CRASH_CAPSULE|RESULT_NOT_ACCEPTED|SNAPSHOT_MISSING|RESULT_RECEIPT_MISMATCH|COMPONENT_HASH_MISMATCH|REDACTION_FAILED`。多个reason按该固定顺序输出，不能凭时间接近、sequence大小或未建模的successor关系猜complete。

### 9.5 Assembly格式

外层`debug-assembly/v1`含contributors、scope、causal watermarks、component refs、file manifest和reason。`bundle-files.json`严格为`bundle-files/v1 {schema_id,entries,entries_digest}`；entry字段恰为`path/sha256/size_bytes/media_type/content_schema_id/component_id`，按path UTF-8 byte order排序并拒绝重复。它只列payload entries，排除自身、archive和任何manifest自身。contributors按`(kind,artifact_id,sha256)`排序。

`captured_at/generated_at`在首次capture/compose事务固定并进入identity；retry读取原值，不读取新墙钟。`assembly_id = asm_<64hex>`，hex为`SHA256(CanonicalJSON({scope,contributors,causal_watermarks,component_refs,entries_digest,captured_at,generated_at,completeness,reasons}))`。相同capture/result input必须产生逐字节相同manifest和archive。

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
  "reasonCodes":[],
  "downloadUrl":"/v1/review-runs/.../debug-bundle",
  "sha256":"...",
  "sizeBytes":123,
  "createdAt":"...Z",
  "expiresAt":"...Z"
}
```

Worker diagnostics→capture→assembly→public projection按以下首个命中规则执行；顺序本身是协议，不能重排：

| 优先级 | 条件 | availability | completeness | URL/hash/size | reasonCodes |
|---:|---|---|---|---|---|
| 1 | tombstone/delete | `deleted` | null | 必须null | `["DELETED"]` |
| 2 | retention已过期 | `expired` | null | 必须null | `["RETENTION_EXPIRED"]` |
| 3 | capability未selected | `unsupported` | null | 必须null | `["CAPABILITY_NOT_SELECTED"]` |
| 4 | 已验证complete assembly | `available` | `complete` | 指向该immutable assembly | `[]` |
| 5 | 已验证partial assembly | `available` | `partial` | 指向该immutable assembly | 9.4 ordered nonempty array |
| 6 | Composer已尝试且失败、没有可验证assembly | `composition_failed` | null | 必须null | 单一稳定composer code |
| 7 | terminal upload失败或terminal receipt无效、且没有partial assembly | `upload_failed` | null | 必须null | 单一稳定upload/receipt code |
| 8 | terminal diagnostics为`local_only`且Server没有可组装bytes | `absent` | null | 必须null | `["LOCAL_ONLY_NOT_UPLOADED"]` |
| 9 | terminal且Worker明确无fragment | `absent` | null | 必须null | `["FRAGMENT_NOT_CAPTURED"]` |
| 10 | selected且run非终态、尚未compose | `pending` | null | 必须null | `[]` |

机械补充规则：

- 普通fragment已上传但没有terminal receipt时只能作为partial contributor，绝不能complete；若有效ServerSnapshot存在，Composer必须产出`partial/RESULT_RECEIPT_MISMATCH`。
- result被拒时fragment保持quarantine且不公开；只有Task后来被Server Reconciler终态化后，Composer才可用权威snapshot生成`partial/RESULT_NOT_ACCEPTED`。
- 只有ServerSnapshot时生成`partial/WORKER_FRAGMENT_MISSING`；有效orphan crash capsule生成`partial/ORPHAN_CRASH_CAPSULE`。这些真实partial assembly优先于`local_only`、upload failure或“无fragment”的absent分支。

只有availability=available时URL/hash/size非null；partial也必须有真实、校验通过的immutable assembly，不能把fragment URL伪装成assembly URL。URL由Server生成，必须同源root-relative `/v1/...`；不允许Worker外域URL、`//`、`javascript:`、`data:`、反斜杠、CR/LF/NUL。Detail/List/batch status读取同一durable projection；Web只在detail展示action。

`reasonCodes`是必填、最多16项、按9.4 registry顺序去重；partial可多项，其余非成功availability恰一项，`pending`和complete恰为空数组。所有三端UI主原因取`reasonCodes[0]`，完整数组仍原样公开；不存在单数`reasonCode`兼容字段。

只有通过全部complete谓词的新Assembly可以填充旧Web的`debugBundleUrl`或legacy artifact alias；alias必须指向与新Assembly完全相同的bytes/SHA/size，并标`source=agent_assembly`。partial Assembly、Worker fragment、Server snapshot和orphan capsule永远不能进入旧alias。

legacy dynamic bundle仍只存在旧字段/路由并显式`kind=legacy_combined`；它不能写入`reviewRun.agentTask.debugBundle`、不能让new availability变available，也不能冒充`source=agent_assembly`。迁移期间Web可并列显示`legacyDebugBundle`，但必须标“legacy/非完整”。任何状态都不fallback audit bundle，audit URL也不能作为legacy alias。

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
- 普通receipt/terminal receipt类型不可互换；terminal receipt `null→full digest`唯一CAS、exact retry、different digest conflict与事务回滚全覆盖；terminal result/receipt无hash循环，相同capture/result重试bytes不变。
- v1/v2 fragment读取矩阵与`postlude` major边界通过；v1永不生成complete。
- debug ordered projection每格、`reasonCodes[]`顺序、唯一`reviewRun.agentTask.debugBundle`路径、`local_only`、普通fragment无terminal receipt、result被拒、server-only snapshot、orphan capsule、partial URL都有三端fixture。
- complete旧alias必须与新Assembly逐字节同一；partial/source fragment无alias且所有状态无audit fallback。
- delete/tombstone与所有写/读/retention/pin/crash upload逐项竞态。
- 无真实bundle时所有版本都没有audit URL；legacy与new complete指标分开。

## 10. V2.0：Fleet 与能力调度

### 10.1 Fleet模型

每个Worker仍单slot/单SDK process/独立root/auth/cache。Server scheduler在Worker之间水平分配，不向Worker预取。schema `worker-capability-snapshot/v1`记录当前可验证能力，不把技术栈标签当固定pipeline。

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

V2.1完成门通过后，binary才可广告`effect_ledger_r4@1.0`；它依赖`agent_task_protocol@1`和`effect_ledger_r3@1`，并以第3节的V2.0完成门作为版本级硬依赖。Server offer仍不等于授权；只有Task grant同时selected两个capability依赖、Server验证当前`worker-capability-snapshot/v1`，并绑定获批`r4_tool_profile_id/profile_digest`时，R4 Gateway才可能dispatch。

R4 contract/tool descriptors、审批、Effect Ledger、不可重试和人类恢复路径全部实现，但全局default deny。生产启用某个R4 tool需要单独versioned product policy和release approval；未知R4始终拒绝。

### 11.2 Contract major

V2.1 rollout前先发布并pin：

- `r4-tool-profile/v1`：provider/action allowlist、exact scope、dry-run/query/idempotency能力、single-shot policy、approval class、cooldown、limits和expiry。
- `approval-event/v2`：两个独立issuer/signature refs、exact request/profile/policy/task/deletion version、expiry/revoke和authority proofs。
- `effect-record/v2`、`effect-ledger-snapshot/v2`、`effect-transition/v2`：继承R3状态机，增加`capability_risk=R3|R4`、profile ref、approval refs、preview/impact refs、single-shot与human-reconciliation字段。
- `agent-worker-result-extension/v3`、`task-result/v3`、`task-result-core/v3`、`task-result-server-terminal/v3`：只引用effect v2对象，Core按4.2.1同major变换，outcome优先级沿用V1.2，不增加“成功但UNKNOWN”branch。
- `agent-task-public/v2`：显式增加`effects.highestRisk/r4ProfileId/requiresHumanReconciliation`；v1 projection继续服务旧Web并省略R4细节，但legacy status仍不得把UNKNOWN伪装成功。

Server dual-read R3 v2/R4 v3；grant selected R4时只接受result extension/task result v3和effect v2，selected仅R3时仍只接受v2/v1 effect objects。任何跨major混用、把R4写入effect-record/v1或task-result/v2、缺profile/双审批都在dispatch前`SCHEMA_VERSION_UNSUPPORTED|CAPABILITY_DENIED`且零partial write。

### 11.3 额外门禁

- two-person或等价强审批，issuer互不相同，均绑定exact action/scope/request digest。
- 强制dry-run/preview（provider支持时）形成Observation，但preview不当作commit。
- 不可逆动作必须有用户可理解impact/target/rollback impossibility和cooldown窗口。
- dispatch前再次验证scope、deletion/cancel、approval expiry、provider identity。
- 无provider idempotency/query contract的动作只允许明确single-shot，transport ambiguity立即UNKNOWN，不自动重试。
- Q3至少两个独立Verifier concerns；Verifier不能是approver/implementer。
- 完整receipt、human reconciliation和post-action verification。

### 11.4 V2.1完成门

- 默认off和所有绕过路径零dispatch。
- capability依赖、R3/R4 grant、effect/result/public DTO major矩阵与old Web投影全部通过；任何R4→v1/v2 downgrade零写入。
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
- 每个版本都有跨项目的新增/修改文件行数报告和超大文件baseline报告；没有新增未豁免的600行以上手写文件，没有向超大遗留文件加入未抽离的新职责，所有例外均可审计。

## 17. 每个版本的Agent执行模板

执行V1.0及之后任一版本时，Agent必须按顺序：

1. 读取涉及项目的`AGENTS.md`和本版本章节。
2. 重新盘点当前HEAD/dirty worktree/contract hashes，保护用户改动。
3. 把版本拆成tracer-bullet vertical slices；每slice跨必要端但保持可rollback，同时先给出模块所有权、文件行数预算，以及触及超大遗留文件时的职责抽离方案。
4. 先提交schema、fixtures和失败测试，再实现。
5. 运行unit/contract/property/crash/security/old-new matrix。
6. 记录ControlPlaneDigest、migration/rollback证据、指标门，以及MVP第3.2节要求的新增/修改文件行数、baseline变化和模块化例外。
7. 只在第3节依赖表列出的全部硬依赖完成门通过后启用本版能力；未列为硬依赖的编号中间版可以跳过。具体地，V1.2可在V1.0稳定后独立于V1.1交付，但不得使用任何未selected动态角色。
8. 若发现本文未闭合且会改变权限、外部行为、数据或兼容性的选择，停止该slice并请求新决策；局部模块命名等可逆选择由Agent记录ADR后继续。

该模板不授权Agent自动推送生产、发外部消息、启用R3/R4或删除旧数据；这些动作仍需对应发布/运维权限。
