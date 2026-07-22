# Agent-First Worker Post-MVP 实施基线与 Roadmap

状态：MVP 之后的 implementation baseline 与 roadmap 分层（Normative）
版本：`post-mvp-design/v1`  
日期：2026-07-16  
前置文档：[MVP 实现设计](agent-first-worker-mvp-implementation-design.md)  
目标文档：[Agent-First Worker 目标设计](agent-first-worker-design.md)

<!-- BEGIN AGENT-FIRST DECISION REFS: POST_AUTHORITY_SCOPE -->
<!-- D1@sha256:ab117e7c86472b7ce57bf2433978df0efe1299353ad747b7eabbff723fec469a -->
<!-- D27@sha256:f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e -->
<!-- END AGENT-FIRST DECISION REFS: POST_AUTHORITY_SCOPE -->

<!-- BEGIN AGENT-FIRST DECISION REFS: POST_CLOSURE -->
<!-- D8@sha256:e895f73c3a0962937cbab61b4c8037f9ccba9daa6e6de89d5004005dd830b98a -->
<!-- D23@sha256:cecd60a0f27d18240d3222eb6aa117dc588b06ba3f9581c83af3d292dd4254e2 -->
<!-- D24@sha256:8e9b8ee728dabd8e8f07e3b6ce8057a6e3e11707d07bbaf4e5d1e67f7dfc3806 -->
<!-- D25@sha256:03564c29030767d552a5759828970f30ed10c11bbd46c42c51f16a08c3e2f2d0 -->
<!-- D26@sha256:ce8a907836b3b8209f12f7c48f66878e9534d7cac667532c2899f3d74c86602f -->
<!-- D27@sha256:f3ef27ad6318d4da20d4750cdde9387b66045f1708a909b57aba1c6e48ec2b0e -->
<!-- D28@sha256:0a9c7e47ab03c92e5d48003ee3d7dc1b5df1cd68031fdd97dda7f85520297204 -->
<!-- END AGENT-FIRST DECISION REFS: POST_CLOSURE -->

## D27 clean-break override（Normative）

Post-MVP 从同一套 current Agent-First contract 继续演进，不承担 legacy→new
兼容迁移。后文的 old/new 三端矩阵、expand/dual-read/backfill、legacy mode、
旧 DTO alias、shadow/fallback 与关闭 grant 回旧路径等内容均为删除清单，不得实施。
未来 current contract 自身的显式 major-version 演进、数据保全与安全部署回滚仍需
版本化，但不能借此复活 pre-Agent-First 协议或第二生产权威。D9-D21 与 D23 已按
machine register 解决；D20 要求协调切换后新 Gate 立即成为唯一生产权威。D21 将
`server_claim_bound_mode` 单值特化为唯一 current contract 的不可变 Server
claim/grant 绑定，Agent Kernel 权威是固有语义而非可选择 mode；Worker 只验证并
执行，缺失、未知或不匹配时 fail closed。Worker config、deployment、单个 job 均
不得换轨，授权失效只能 stop、fence 或 reject；同 contract 分批发布及旧 build
安全回滚仍允许。D23 冻结 Server 仓库为跨端 current contract package 唯一发布真源，
Worker/Web 只消费精确 pin；其 compatibility matrix 仅证明同一 current package 的
协调发布组合，不授权旧协议共存、运行时多 major 协商、fallback 或 downgrade。D28 进一步
选择 `logical_bundle_generated_wrappers`：Server 维护一份 canonical content bundle/root
manifest，并从相同 canonical bytes 生成 Python/npm 薄包装；两个 wrapper 共享一个逻辑
package identity/version/content digest，Worker/Web 分别 exact-pin wrapper version 与逻辑
digest，不得复制或重定义 schema。D24 已将
`new_tasks_only` 单值特化为 D27-compatible 的受审计协调切换屏障：屏障生效后只有按唯一
current TaskRecord schema 与 current Agent-First contract 新提交的 Task 可以创建和执行；
pre-cutover Task、旧数据形状、旧协议与旧生产权威均不得越过屏障。D25 冻结
`immutable_receipt_mutable_binding`：terminal upload/transport receipt 的 bytes 与 ContentRef
始终不可变；Server 以独立 binding/index row 一次性绑定 exact `transport_envelope_digest`，
不得回写 receipt、重绑或清空。D24/D25 只冻结规范决策，不授权任何 runtime、schema、
protocol 或 deployment 实现变更。

## D26 roadmap maturity overlay（Normative）

D26 已选择 `roadmap_separate_designs`（resolution digest
`ce8a907836b3b8209f12f7c48f66878e9534d7cac667532c2899f3d74c86602f`）。本文不再把
所有远期版本宣称为已经闭合的 implementation design。D22 已按用户确认的
`absolute_plus_baseline` current-contract 单值特化解决（resolution digest
`94ec57c0b72801dc37d8a7de08b16cc78b8ffc8bdb69b39f0eb0b56cf80d6e96`）。D22/D26
原始闭合点上的机器 decision register 无 active decision，规范状态为 ready；当前
append-only register 中 D28 已 resolved，D29-D30 仍 pending，状态为 `valid_pending`，
D29 是唯一 active question，D30 仍 dependency-blocked，S3-S8 因而 blocked。D2 仍是 inactive pending，
不适用且不得据此恢复旧生产权威。D26 只确定文档成熟度与开工边界，不授权 runtime、
schema、protocol 或 deployment 变更。

本文按以下 maturity 分类：

| Maturity | 章节 | 权威含义 |
|---|---|---|
| implementation baseline | C0/V1.0（第4-5节）、V1.2-V1.4（第7-9节） | 可作为后续独立实施设计的详细输入；仍须满足适用 decision gate、测试、发布权限与逐切片证据，不表示已实现或获准部署 |
| roadmap | V1.1（第6节）、V2.0（第10节）、V2.1（第11节）、V3.0（第12节） | 只冻结目标、约束与未来验收意图；每版开工前必须另写完整 implementation design，闭合 schema/state/storage/wire/fixtures/rollout/rollback/DoD，并取得适用独立决议 |
| cross-cutting guardrail | 第1-3、13-14节 | 只约束所有未来设计；不能把示例中的旧兼容或多轨机制提升为实现授权 |

任何 roadmap 或其后续独立 implementation design 都不得复活 dual-read、运行时 multi-major
negotiation、old-Web compatibility、protocol downgrade、legacy fallback 或第二生产轨道。未来
current-contract 演进必须先取得独立决议，再以协调切换使唯一 current package
identity/version/digest 生效；旧 package、schema、protocol 或入口不得作为并行生产权威。

## 0. 目的与“完全实现”的定义

D26 将这里的“完全实现”严格收窄为下述 implementation baseline 的相应实施范围；它不再
表示本文所有 Roadmap 版本都已闭合、已实现或进入当前 DoD。

本文用于 MVP 完成之后的实施规划，并诚实区分 implementation baseline 与 roadmap。执行
Agent 不得从 roadmap 挑一个孤立功能直接落地，也不得用 feature flag 代替缺失的底层契约。
implementation baseline 也不是完成声明：MVP 前置证据、适用 decision gate、跨端验证、发布权限
和逐切片回滚证据缺一不可。

D8 已冻结 lease-loss 的 Task/Attempt 分层。D9 已选择内部 TaskResult CAS 为唯一语义终态
线性化点，Server ACK 只是可恢复 transport projection；D10 已选择全局 safety-first 穷举矩阵；
D20 已冻结新 Gate 在协调切换后立即成为唯一生产权威的边界；D21 已冻结唯一 current contract
的不可变 Server claim/grant 绑定与 Worker fail-closed 验证；D23 已冻结 Server-owned contract
package 真源与 Worker/Web exact pin；D24 已冻结受审计 cutover barrier 与 pre-cutover Task 的
fail-closed 隔离边界；D25 已冻结 immutable receipt、独立 mutable binding/index 与
core/transport 双 digest 的无环关系；D26 已冻结上述 maturity 分流；D22 已冻结签名
release-gate 制品、职责分离、绝对与 stable-relative 门、三态 CI、baseline/bootstrap 规则，
以及 D24 barrier 后的 capacity-only canary。当前决策前缀 D1 与 D3-D28 已 resolved，D2
保持 inactive，不得被实现或发布流程激活；append-only D29-D30 仍 pending，D29
active，S3-S8 blocked。当前 `valid_pending` 只证明登记结构有效，不表示规范完整，也不表示
实现、评测或部署证据已存在。

R4 roadmap 的目标包括受信审批、Effect Ledger、不可重试/对账语义和默认拒绝；生产没有获批
R4 tool profile 时保持 off。该目标是未来独立 implementation design 的约束，不是当前能力或
部署授权。

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
11. 生产 graph 只能包含 current Agent-First protocol；任何 legacy fallback、downgrade、dual mode 或兼容入口均须被 composition/contract test 拒绝。
12. 所有跨端 current 字段先有schema、valid/invalid fixture和error code，再写业务实现；三端只验证同一 current contract。
13. MVP第3.2节的模块与文件规模约束对Worker、Server、Web和contract package持续生效；每端都必须使用同一400行审查触发线、600行强制门禁、超大遗留文件baseline ratchet和显式例外机制。
14. 外层 lease 丢失只终止旧 ownership：一个 Task 控制事务原子记录独立 transport abandonment 事实、永久 fence 精确旧 transport/native Attempt 与 actor/session并令 Task version恰好 `+1`，精确重试不再增版；Task 保持 `ACTIVE|FINALIZING` 且 terminal/result 不变。只有满足显式恢复资格谓词的 successor 可接管；lease loss 本身不是 Task terminalization authority。

## 2. 开始 Post-MVP 前必须具备的证据

只有MVP Definition of Done全部通过才可开始。发布负责人还必须冻结以下输入：

| 输入 | 必需内容 |
|---|---|
| `MvpReleaseManifest` | Worker commit/image、schema registry digest、policy/prompt/tool digest、SDK/CLI/runtime版本 |
| `MvpCurrentContractBaseline` | current Agent-First schema、三端 executable fixtures、negative fixtures 与 legacy-path absence 证据 |
| `benchmark-bundle/v1` | benchmark owner 在候选结果揭示前签发的 task/oracle/rubric inventory、environment/image digest、预声明 seed、重复次数与统计实现 |
| `release-gate-policy/v1` | release operator 在候选结果揭示前签发的 candidate/stable identity、absolute/relative threshold、task-profile budget 和 canary plan |
| `release-gate-report/v1` | CI/eval owner 产生的可复算原始分母、排除 reason、Wilson 上界、全部绝对/相对门和三态 verdict |
| `release-gate-attestation/v1` | release operator 对 exact exit-0 report 的核验签名；policy 最长有效 30 天，attestation 最长有效 7 天 |
| `StableBaselineRecord` | 仅由通过 offline gate 与 canary 的 candidate 晋升而成的 immutable record；bootstrap 时明确不存在 |
| `MvpRecoveryReport` | 同outer lease crash-point矩阵、stale epoch/cancel/publish结果 |
| `MvpSecurityReport` | source integrity、secret redaction、path/archive、cross-tenant tests |
| `MvpOperationsReport` | canary、rollback、active-slot/outbox恢复、debug缺失诊断 |

任何基础不变量仍靠日志人工判断而无schema/fixture时，先补MVP，不得把债务转嫁给新协议。

## 3. 版本与依赖总图

| 版本 | 核心结果 | 硬依赖 | 不包含 |
|---|---|---|---|
| C0 | 跨端schema包、稳定ErrorResponse、clean DB schema | MVP | 新行为grant |
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

D23 冻结 Server 仓库为该 package 的唯一发布真源；Worker/Web 必须消费精确
version/digest pin，不得另行生成、分叉或重打包权威 schema。D23 所称 compatibility
matrix 只验证三端在一次协调发布中使用同一 current package tuple。未知或不匹配必须
fail closed；major 演进必须协调切换，不得形成运行时旧包 fallback、多 major 协商或双轨。

D28 冻结该发布物为 `logical_bundle_generated_wrappers`（resolution digest
`0a9c7e47ab03c92e5d48003ee3d7dc1b5df1cd68031fdd97dda7f85520297204`）：Server
只维护一份 canonical content bundle/root manifest，并从相同 canonical bytes 生成 Python
与 npm 薄包装。两个 wrapper 必须共享同一逻辑 package identity/version/content digest；
Worker/Web 分别 exact-pin wrapper version 与逻辑 digest，且不得复制或重定义 schema。
package conformance 必须证明两个 wrapper 的 canonical content/digest 完全一致，并验证两个
consumer 的 exact lock。该决议只冻结发布物与 pin 语义，不代表 package 已实现或获准发布。

### 4.2 版本规则

D21/D23 的 current-contract 规则优先：协调发布固定唯一 package identity、exact
version 与 digest，claim/grant 只不可变绑定该精确值，缺失或不匹配时 fail closed，
不得在运行时用 range/minor 交集选择基础协议。以下版本规则仅可描述同一 exact
current package 内的可选 capability authorization，或未来协调发布的离线验证；
不得产生基础 protocol mode、旧包 fallback、multi-major 协商或双轨。

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

### 4.4 D24 受审计协调切换屏障（Normative）

D24 的 `new_tasks_only` 是 D27-compatible 单值特化，不包含“所有旧任务走完 v1”、
legacy drain 或 legacy retention 期限。Server 受审计的协调切换屏障是唯一线性化边界：
只有 Task acceptance/TaskRecord creation 事务在该屏障生效后，按唯一 current
TaskRecord schema 与 current Agent-First contract 成功提交的 Task，才可创建并执行。

屏障生效前必须暂停 intake。所有 pre-cutover Task 必须在屏障生效前完成权威终态或
tombstone/delete 处置，或者撤销执行授权并被 stop、fence 或 reject 后隔离为不可执行
状态；stop、fence 或 reject 只撤销 authorization/Attempt ownership，不得冒充 Task
terminalization 或 TaskResult。屏障生效后，任何 pre-cutover Task 均不得再被 claim、
grant、resume、replay、drain、写入、发布或执行；迟到的旧 lease、event、result 或
replay 必须 fail closed。pre-cutover `submission_idempotency_key` 的重放不得被重新创建
或归类为新 Task。

不得为 pre-Agent-First/旧 TaskRecord 实施 lazy migration、batch backfill、dual
read/write、compatibility reader 或运行时 schema/protocol negotiation；不得保留 legacy
Adapter/shim、production shadow、legacy fallback、protocol downgrade、compatibility
rollback 或 old/new schema/contract 双轨。D24 不授予旧数据留存例外；仅在另行获得明确
的审计或合规留存授权时，旧数据才可隔离为 immutable、read-only、non-executable 的审计
归档。该归档必须与 current control plane、operational tables/readers 和 DTO projection
分离，不得成为 current TaskRecord 的输入、授权、恢复或执行来源。

任何 Task、TaskRecord 或 claim/grant 的 schema/contract identity/version 缺失、未知、
旧版或与唯一 current schema/contract 不匹配时，create、claim、grant、resume、replay、
写入、发布和执行都必须 fail closed。安全回滚只可回到 exact-pin 同一 current package
identity/version/digest、实现同一 current TaskRecord schema、storage semantics 与 current
Agent-First contract 的先前 build，不得重新开放旧 Task、旧数据形状、旧协议、旧入口或
第二生产权威。

同一 current contract 的 clean initialization/rebuild、current-version upgrade、分批部署，
以及未来经独立决议协调切换的 current-contract 演进仍可设计；这些路径不得引入
pre-Agent-First 兼容层、运行时协商或并行生产轨道。D24 只记录上述 policy，不授权对
production runtime、schema、protocol 或 deployment 的实现变更。

### 4.5 C0完成门

- Server 仓库发布唯一 current contract bundle；三端从同一 exact version/digest pin 运行 golden tests，Worker/Web 不得独立定义或发布跨端 schema。
- unknown major、required field缺失、额外字段、digest变化有negative fixtures。
- clean schema 初始化、重建与 current-version upgrade 可重入、可中断恢复；不存在 legacy table/column 双读写。
- ErrorResponse code registry穷举且每个code有HTTP/retry/Attempt transition。
- composition/contract tests证明生产 graph 不含 legacy Adapter/route/schema/fallback，三端只接受 current protocol。

## 5. V1.0：`agent_task_v1` 协议与公开投影

### 5.1 Capability advertisement与offer（D21/D27 已退役的基础协议协商示例）

生产替代规则是协调部署预先固定 Server-published current contract 的 identity、exact
version 与 digest；Worker request 只证明匹配，Server response/claim/grant 只回显并
不可变绑定该事实。可选 capability 仍可在这套 exact contract 内由 Server 授权，
但不能选择基础协议或执行轨道。以下 advertisement/offer 与旧 Server fallback 示例
仅是删除清单，不得实现。

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

### 5.2 Lease grant（D21/D27 已退役的 multi-mode 示例）

Current grant 仍可携带 lease、policy、fence、limit 与 contract 内 capability
authorization，但基础 contract binding 必须是协调发布预先固定的 exact identity、
version 与 digest。Worker 缺失、不认识或不匹配时 fail closed；授权失效只能 stop、
fence 或 reject。以下 `protocol_mode`、基础协议 `selected` 与旧轨 fallback 分支均为
删除清单，不得进入 current grant。

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

Grant bytes/digest/`expires_at`一经签发不可改写。heartbeat只续outer lease row，且新的`lease_expires_at <= grant.expires_at`；它不能延长/轮换grant，也不能延长`resume_until`。到达`expires_at-terminal_reserve`后Worker停止新dispatch，并只在当前 ownership 仍有效时进入受权 FINALIZING 流程；若 lease/grant 先失效，则按 D8 只提交 abandonment/fence，保持 Task 与 terminal/result 不变。过期后除明确的recovery upload grant外所有写拒绝。Resume若成功签发新grant，但其hard expiry仍不能超过Task absolute deadline。

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
    "transport_envelope_digest":"...",
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

四个digest是TaskResult的sibling，不属于被hash对象。`task_result_core_digest`只绑定严格的语义 `TaskResultCore`；`transport_envelope_digest`绑定包含immutable terminal receipt ContentRef的完整不可变transport envelope，两者不得混用。Server必须从已上传bytes自行重算PreGate/final EvidenceClosure、与TaskResult同major的严格Core transform和完整transport envelope，校验task/scan、run/job/lease、epochs、outcome→legacy status映射、Requirement覆盖和Effect状态。D9的内部TaskResult CAS仍是唯一语义终态线性化点；receipt binding/index与ACK只证明transport/index关系，不能选择、覆盖或替代outcome CAS。V1.0尚未selected debug时，`diagnostics.worker_debug_fragment`必须是`not_applicable/CAPABILITY_NOT_SELECTED` AvailabilityRef；V1.4 selected后才允许available descriptor并验证terminal receipt。

跨端冻结DAG必须与MVP一致：

```text
PreGateRootSet → PreGateEvidenceClosure
→ (SuccessGateInputSnapshot | TerminalizationInputSnapshot) → GateDecision
→ final EvidenceClosureManifest → TaskResultCore
→ [V1.4 terminal fragment → immutable terminal upload/transport receipt]
→ complete immutable transport envelope
(terminal receipt identity, transport_envelope_digest) → Server-owned binding/index
```

GateInput不得引用final closure；两种closure都排除TaskResult/Core和全部debug/diagnostics。binding/index不进入任何被hash对象，transport envelope也不得反向引用它。terminal receipt、binding/index、双digest schema及每条禁止反向边的negative/crash fixture都由D23的Server-owned current contract package发布，Worker/Web只消费exact pin。

Result accept同一事务：

1. 验证legacy required artifacts和extension/grant。
2. 从quarantine解析/验证全部ContentRef、PreGate/final closure及`task_result_core_candidate`；尚不pin。
3. 依selected capabilities解析`diagnostics.worker_debug_fragment` AvailabilityRef；available ref必须指向`worker-debug-fragment-descriptor/v2`。其`uploaded`分支必须同时携带`server_fragment_ref`和`terminal_receipt_id`；`local_only`分支必须令二者同时为null；外层`unavailable|not_applicable`同样不得有任何Server ref/receipt。
4. 重算terminal event payload：`FINALIZING→TERMINAL`、previous/terminal Task version、desired state、outcome、`task_result_core_digest`、actor和termination cause必须与TaskResultCore一致；校验Worker提供的event ID/expected sequence/payload digest。
5. insert immutable raw transport envelope/TaskResult/TaskResultCore；若是`uploaded`分支，只在独立Server-owned binding/index row上执行唯一`bound_transport_envelope_digest: null → exact transport_envelope_digest` CAS，绝不修改terminal receipt bytes或ContentRef；然后insert result transport ACK，并在同一事务把reachable evidence、core candidate和该immutable receipt关联的terminal fragment从quarantine转为Task retention pin。
6. 以D9语义TaskResult CAS推进Task terminal version、review run和scan projection，同时以authoritative next sequence append terminal event、更新event prefix；binding/index CAS或ACK不能替代该outcome CAS。任一version/sequence/cancel/delete冲突令整个事务回滚。
7. durable保存并返回`task-result-receipt/v1` transport ACK；response包含accepted/duplicate/receipt、terminal versions和terminal event receipt，但该ACK不构成第二语义终态。

完全相同attempt+四个digests+canonical envelope幂等；任一不同为409。成功response丢失后重发必须返回逐字节相同transport ACK。
相同immutable terminal receipt identity、`core_ref.sha256`、`transport_envelope_digest`与canonical envelope的重试沿用同一binding/index和transport ACK；binding/index已绑定相同digest则幂等，已绑定不同digest时409 `TERMINAL_RECEIPT_ALREADY_BOUND`。binding一旦成功不得清空或重绑，terminal receipt bytes/ContentRef在全部路径都不得变化。若结果事务在binding CAS前后任一步失败，整笔回滚，binding/index仍为unbound，core与fragment都保持quarantine，不留下半绑定、语义终态或可下载terminal assembly。


terminal event属于authoritative prefix且只能由result事务写一次；Worker不得在普通event route预先发送`task.terminalized`。Server Reconciler使用相同payload schema但actor/result_source为`system_reconciler`。

### 5.5 Server Reconciler variant

V1.0该method只写`task-result-server-terminal/v1`；V1.2 selected R3时写`task-result-server-terminal/v2`，V2.1 selected R4时写`task-result-server-terminal/v3`。它绝不生成Worker的`task-result/v1`、`task-result/v2`或`task-result/v3`，也不携带agent-worker result extension。

下述 `commit_server_terminal_result` 仍只是候选接口轮廓：它只有在完整 D10 矩阵选择 outcome 后，通过 D9 要求的内部 TaskResult CAS 才能形成语义终态，不能把 Server ACK 当作线性化点。D8 只允许 lease/worker loss 在一个 Task 控制事务中原子记录独立 transport abandonment、永久 fence 精确旧 transport/native Attempt 与 actor/session并令 Task version恰好 `+1`，同时保持 Task `ACTIVE|FINALIZING` 及 terminal/result 不变；精确重试返回原版本且不再次增版。

新增内部、非HTTP `commit_server_terminal_result`：

- service identity，无Worker lease/grant，只能产生非成功outcome。
- 输入绑定latest task/deletion version、Server持久化request、`ack_checkpoint` AvailabilityRef、已知artifact/Observation/effect receipts。V1.0/V1.2固定`unavailable/CAPABILITY_NOT_SELECTED`；只有该run实际selected`same_run_resume`时才要求available并精确匹配watermark。
- 缺失事实用AvailabilityRef写`unavailable + reason_code`，不伪造digest/PASS/Worker envelope。
- `result_source=server_reconciler`、`worker_transport_envelope=null`。
- idempotency为`task_id + terminal_task_version + termination_cause + input_digest`。

Lease/worker loss 本身不得调用该方法或产生 `WORKER_LOST` Task terminal。空 Effect Ledger 时，`WORKER_LOST` 只可保留为“恢复资格已机械判定不存在或耗尽”之后的候选矩阵输入；只有完整 D10 矩阵选择该 outcome，且事务成功 CAS 一个内部 TaskResult 时才可终态化。有任何 effect row 时更不能用 pure lost 吞掉事实。资格仍待判定时必须保持 Task `ACTIVE|FINALIZING`、terminal/result 不变，并让公开 transport projection 表达 lost。

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
| `active` | object，status=`leased|preparing|running|verifying|publishing|fenced` | null | null/null | active ownership 时 nativeEpoch/current Task一致；lease loss 后只允许旧 Attempt=`fenced` 且 transport=`lost`，Task/result 不变并等待 successor 资格判定 |
| `waiting_input|waiting_approval` | object且status=`suspended` | object且kind匹配lifecycle | null/null | interaction deadline/ID非空 |
| `finalizing` | object或显式null；lease loss 时旧 Attempt 可为 `fenced` | null | null/null | success/terminal Gate正在运行，或 transport lost 后等待 successor/后续 authority；lease loss 不产生 terminal/result |
| `reconciling` | object或显式null | null | null/null | `effects.unknown>0`或有unresolved dispatched effect |
| `terminal` + `terminal.kind=task_result` | object或null | null | object/非null outcome | resultSource/digest/terminalTaskVersion非空 |
| `terminal` + `terminal.kind=worker_lost` | object或null | null | object/null | 仅在恢复资格已证明不存在/耗尽、完整全局矩阵选择该 outcome 且内部 TaskResult CAS 成功时产生；effects.total必须0，evidence unavailable reason=`WORKER_LOST` |

requirements/verification/effects counts从Server验证后的对象机械投影且必须非负、内部求和一致。`evidence.availability=complete`只在final closure全部pin并复算通过后允许；上传了一部分只能partial。公开对象不含prompt、session/thread ID、内部路径、raw ContentRef或Worker给出的外域URL。

`debugBundle`字段从C0起始终存在，严格使用`debug-bundle-public/v1`；其唯一规范路径是`reviewRun.agentTask.debugBundle`（列表项同构为`reviewRuns[].agentTask.debugBundle`）。不得另建`reviewRun.debugBundle`新对象；旧`reviewRun.debugBundleUrl`仅作为9.6规定的complete兼容alias。

旧scan `status`保留业务兼容；transport lost不再伪装为Worker FAILED TaskResult，而在`agentTask.transport.status=lost`表达，同时 Task lifecycle 保持 loss 前的 `active|finalizing`、旧 Attempt 显示 fenced，直到合法 successor 接管，或完整全局矩阵选择 outcome 且内部 TaskResult CAS 成功。Web正式接受`cancel_requested/cancelling`，不得把它们normalize成queued。

### 5.7 V1.0 唯一 current contract 发布序列（Normative）

发布矩阵只有一种合法组合：Server 发布并 exact-pin 同一 current package
identity/version/digest，Worker 与 Web 消费该 exact pin；三端任何 identity、digest 或
`CandidateDigest` 不一致都 fail closed。版本切换不得依赖第二 control path、并行生产权威
或 runtime mode selection。

发布必须严格按以下顺序：

1. benchmark owner 与 release operator 在候选结果揭示前分别签发
   `benchmark-bundle/v1` 和 `release-gate-policy/v1`。
2. CI/eval owner 对同一 exact package/`CandidateDigest` 完成 offline benchmark；
   evaluator exit 必须为 `0`，release operator 才能对 exact report 签发 attestation。
3. 在同一 current package 上完成 rollback 演练后，Server 原子启用 D24
   Task acceptance/TaskRecord creation barrier；不满足屏障的 intake 一律暂停、fence 或 reject。
4. canary 仅按 current-contract capacity 扩大：先到 5% target capacity（至少一台 Worker），
   并同时满足 24 小时与 200 个 accepted current Tasks；再到 25% capacity，并同时满足
   72 小时与 1000 个 accepted current Tasks；两阶段都通过后才可 full capacity。

未分配给 canary 的 capacity 不得路由到另一套 contract，剩余 intake 只能暂停或留在
Server current control plane。样本或窗口不足不得晋级。存在已签发 stable build 时，rollback
只能 exact-pin 回实现同一 current package/schema/storage semantics 的 stable build；bootstrap
没有此对象时，只能 stop intake、fence 或 reject。

### 5.8 V1.0完成门

- grant dependency/major/minor/expiry/revoke/deletion/unknown field fixtures全通过。
- grant-on省略extension、错scope/epoch/version、legacy downgrade均零partial write。
- agent evidence每种schema的upload/hash/size/cross-task/TTL/pin/GC，以及missing/cycle/alias/未知schema negative fixtures全通过；Server不依赖Worker本地bytes。
- native event exact replay、ID/sequence冲突、prefix digest和response-loss测试全通过。
- 全部TaskResult outcome映射、PreGate/final closure DAG、digest、receipt、duplicate/conflict测试。
- Reconciler缺失证据诚实表达、幂等、与cancel/delete竞态。
- lease/worker loss 将独立 abandonment、旧 transport/native Attempt/actor fence 与 Task version恰好 `+1` 原子提交，精确重试不再增版；公开 Task 保持 active/finalizing、terminal/result 不变，且 stale actor 全入口拒绝。无恢复资格证明时不得投影 `terminal.kind=worker_lost`。
- current-protocol 三端 exact-pin 组合、offline gate、D24 barrier、capacity-only canary 与
  rollback/stop-intake 路径全部自动化；生产 composition 中不存在第二 contract authority。
- Web不读取raw Worker envelope，不泄露thread/secret/internal path。

## 6. Roadmap — V1.1：有界动态 Agent

本节是 roadmap，不是当前 implementation authority。V1.1 开工前必须另写完整
implementation design，闭合动态角色的 schema、state、storage、wire、fixtures、rollout、
rollback 与 DoD，并取得适用独立决议；以下内容只保留目标、约束和未来验收意图。

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

### 6.5 V1.1 Roadmap 验收意图（非当前完成门）

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

状态只允许：PREPARED→DISPATCHED/NOT_APPLIED/REJECTED；DISPATCHED→COMMITTED/NOT_APPLIED/REJECTED/UNKNOWN；UNKNOWN→COMMITTED/NOT_APPLIED/REJECTED。`COMMITTED|NOT_APPLIED|REJECTED`是Effect终态，无出边；`UNKNOWN`是未解决状态，不是Effect终态。`TERMINATED_WITH_UNKNOWN_EFFECTS` 是必须纳入完整全局矩阵的候选 Task outcome，不得由 lease loss 或本文未冻结的候选顺序直接实现。TaskResult发布前Reconciler可解析UNKNOWN；发布后任何新事实只形成`EffectResolutionAddendum`，不改原Effect snapshot、TaskResult或outcome统计。

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
- Worker loss 必须先在独立 lease-loss Task 控制事务中记录 transport abandonment、永久 fence 精确旧 transport/native Attempt 与 actor/session并令 Task version恰好 `+1`；精确重试不再增版，Task 保持 `ACTIVE|FINALIZING` 及 terminal/result 不变。Effect Ledger 非空时不能使用 pure `WORKER_LOST`；fence 完成后，只能由独立且 effect-authorized 的 Reconciler transition 决定是否进入 `RECONCILING`，不能把 lease loss 本身当成该 Task transition 或 Terminalization Gate authority。

Task lifecycle新增以下穷举边；未列出一律拒绝：

| From | Trigger | Effect guard | To | 结果/写集 |
|---|---|---|---|---|
| ACTIVE/FINALIZING | outer lease/worker loss | 任意 | 原 lifecycle | 同一控制事务写独立 abandonment、fence 精确旧 transport/native Attempt 与 actor/session、Task version+1；terminal/result 不变，精确重试不再增版 |
| ACTIVE/FINALIZING | cancel/deadline/runtime loss | 任一DISPATCHED或UNKNOWN | RECONCILING | desired/fence、Task version+1、Server Reconciler接管 |
| ACTIVE/FINALIZING | effect reconciliation activated after ownership fence | Effect Ledger非空且 effect-authorized 谓词成立 | RECONCILING | 引用既有 abandonment/fence，不重复 transition 旧 Attempt；禁止 pure WORKER_LOST，Task version+1 |
| ACTIVE | cancel | 所有rows已终态，COMMITTED>0 | FINALIZING | desired=CANCEL、冻结effect snapshot、Task version+1；目标`CANCELLED_WITH_EFFECTS` |
| ACTIVE | cancel | rows为空或全部NOT_APPLIED/REJECTED | FINALIZING | desired=CANCEL、冻结effect snapshot、Task version+1；目标普通`CANCELLED` |
| RECONCILING | all effects COMMITTED/NOT_APPLIED/REJECTED | UNKNOWN=0 | FINALIZING | freezeeffect snapshot、Task version+1 |
| RECONCILING | reconciliation deadline | UNKNOWN>0 | TERMINAL | `TERMINATED_WITH_UNKNOWN_EFFECTS` result v2 |
| FINALIZING | cancel先赢 | COMMITTED>0且UNKNOWN=0 | TERMINAL | `CANCELLED_WITH_EFFECTS` result v2 |
| FINALIZING | cancel先赢 | COMMITTED=0且UNKNOWN=0 | TERMINAL |普通`CANCELLED` |
| FINALIZING | Success Gate | 所有expected COMMITTED、UNKNOWN=0 | TERMINAL | COMPLETED类result v2 |
| 任意非终态 | delete/tombstone | deletion CAS先赢 | tombstoned | 不接受新TaskResult/effect transition |

现有 terminal outcome 顺序候选为：delete fence（不产生新result） > UNKNOWN effect → `TERMINATED_WITH_UNKNOWN_EFFECTS` > cancel且COMMITTED effect → `CANCELLED_WITH_EFFECTS` > cancel无effect → `CANCELLED` > safe PARTIAL > FAILED/BLOCKED > success。D10 已选择全局 safety-first 穷举矩阵，但完整 fact×state×availability×effect 表尚未在本单元冻结，因此该候选仍不是 precedence 实现授权；任何选中 outcome 都必须由 D9 规定的内部 TaskResult CAS 线性化。任何实现都不得把 lease loss 插入候选顺序并直接终态化 Task。

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
- Server reaper在Worker消失时只可原子记录 abandonment、fence 精确旧 ownership、令 Task version恰好 `+1` 并保持 Task active/finalizing；精确重试不再增版。随后可仅凭 Server ledger 触发独立 effect reconciliation 资格判断。任何 DISPATCHED/UNKNOWN 都不能被 pure lost 隐藏；恢复资格未耗尽、完整矩阵未选择 WORKER_LOST，或内部 TaskResult CAS 未成功时均不得形成该终态。
- service Reconciler在Worker grant失效后的每条允许transition、Addendum、response loss和delete竞态全通过；绝无新dispatch。
- 零盲重试、零effect被普通失败隐藏、reconciliation deadline不延长。
- Human reconciliation DTO和Addendum不改原TaskResult/outcome统计。

## 8. V1.3：Server-ACK Checkpoint 与 `same_run_resume`

V1.3完成门通过后，Worker才把`same_run_resume@1.0`加入advertisement；它依赖`agent_task_protocol@1`。只有Server offer且本次grant selected时，grant的`resume_until`才非null。V1.0/V1.2 Worker不能因本地checkpoint存在就请求resume。

跨 lease resume 是 D8 所称 successor 的唯一正向能力：旧 lease 丢失时先独立原子提交 abandonment/fence/Task version `+1` 并保持 Task `ACTIVE|FINALIZING`；resume 请求随后只能消费该既有事实，不能在 successor 事务里第一次 fence 或再次 transition 旧 Attempt。`transport-abandonment-record/v1` 的字段、identity、幂等 contract 仍是 `SPEC_GAP`，本节不得就地发明。

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
- 已有 durable transport abandonment 事实；精确旧 transport/native Attempt 与 actor/session 原为 active ownership且均已永久 fenced，无新 active lease。Resume 只验证并幂等断言该既有 fence，不能再 transition 一次。
- request checkpoint精确等于Server ACK watermark。
- expected task version未被Cancel/Policy改变。
- Effect状态允许；有UNKNOWN时只允许Reconciler，不恢复语义执行。
- 旧budget reservation可核销，剩余wall/token/cost/tool满足minimum resume window/budget。
- 当前rollout仍能保持同一protocol mode和major。

### 8.4 ResumeOperation事务

Server创建`resume-operation/v1`，状态`PREPARED→COMMITTED|REJECTED|COMMITTED_THEN_REVOKED`。设request的`expected_task_version=N`；成功同一事务：锁Task/lease → 验证全部恢复资格谓词和既有 abandonment/fence，幂等断言精确旧 transport/native Attempt 与 actor/session 仍 fenced（不再次 transition）→ 核销旧reservation → 预留新slice → 创建successor transport attempt/epoch和新grant → CAS Task `N→N+1` → append只描述successor=`PENDING_CONFIRM`并引用既有旧 fence 的`attempt.transition`，更新event prefix → 保存同job/run lease、replay fence和canonical response bytes → commit。successor lease状态固定为`PENDING_CONFIRM`，grant authorization state固定为`pending_confirm`，response含`successor_task_version=N+1`和`confirm_deadline_at=min(now+60s,resume_until,grant.expires_at,absolute_deadline-terminal_reserve)`。

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

Sweeper 只 fence/revoke 未确认的 successor 并释放未消费 slice；它不能因 successor 超时就终态化 Task。若仍要尝试另一个 successor，必须重新满足显式恢复资格与版本/fence；若资格耗尽，后续终态仍须由完整全局矩阵选择并以内部 TaskResult CAS 线性化。

### 8.6 拒绝分类

- cancel先赢 → `RESUME_CANCELLED`，交给完整全局矩阵选择后续 outcome；resume 拒绝本身不写 Task terminal/result。
- budget/deadline不足 → `RESUME_BUDGET_INSUFFICIENT`，记录恢复资格拒绝；诚实 PARTIAL/FAILED 只能由完整全局矩阵选择，并以内部 TaskResult CAS 线性化。
- waiting/suspended/normal release → `RESUME_NOT_ACTIVE_OWNERSHIP`，保持等待策略。
- security/credential revoke或tombstone → permanent拒绝并audit。
- watermark/identity/version冲突 → permanent consistency failure。
- rollout无法保持mode →拒绝，不降legacy。

### 8.7 V1.3完成门

- watermark ACK/duplicate/gap/fork/old epoch/revoke/delete tests。
- resume success、每个拒绝、Cancel竞态、budget transfer、response loss；既有 abandonment/fence 未变时 exact replay，fence改变时稳定拒绝且旧grant不可用，任何路径都不重复 transition 旧 Attempt。
- 在旧reservation核销、新slice、新lease/new grant每个crash point注入故障，无双lease/双budget。
- success commit→response loss→cancel/revoke/delete→same operation replay，以及success response收到→confirm前撤销，全部零Agent/tool dispatch。
- PENDING_CONFIRM期间每个非confirm入口都拒绝；confirm exact replay、identity conflict、timeout sweeper、ACK loss和Task version event CAS全通过。
- native/owner pending identity在ACK前不启动runtime，ACK后只启动一次。
- event sequence不回退，checkpoint本地较新未ACK不恢复。
- WAITING/SUSPENDED/normal release不被sweeper误判WORKER_LOST；successor confirm timeout 也只 fence successor，不直接终态化 Task。

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

startup/checkpoint/crash/postlude fragment的`task_result_core`必须是AvailabilityRef `not_applicable`。terminal fragment只能在TaskResultCore后生成，其`task_result_core`必须是available ContentRef，且`content_schema_id=task-result-core/vN`必须与待发布`task-result/vN`同major；Server要求该exact ref已作为`task_result_core_candidate`在quarantine。`terminal-fragment-receipt/v1`字段固定使用`task_result_core_ref`（完整ContentRef），不得再出现裸`task_result_core_digest`或把字符串写成`available`；receipt还固定`task_result_schema_id`（非enum schema-ID字符串，必须存在于当前ContractBundle并与Core同major）、grant/task/deletion/epochs。Server一经接受upload即冻结receipt bytes与ContentRef，receipt本体没有可回填的result/envelope digest字段。独立Server-owned binding/index row以receipt identity为键并初始化`bound_transport_envelope_digest=null`，只允许5.4 Result accept事务做一次`null → exact transport_envelope_digest` CAS；相同digest重试幂等，不同digest、清空或重绑都拒绝。普通receipt不能冒充terminal receipt。上述receipt、binding/index、双digest schema和crash fixtures均由D23 Server package发布。

TaskResult引用的`worker-debug-fragment-descriptor/v2`是immutable CAS对象，`additionalProperties=false`，严格oneOf：

- `uploaded`：要求`fragment_ref`和`server_fragment_ref`均为完整ContentRef且`sha256/size_bytes/content_schema_id=worker-debug-fragment/v2`逐项相同，另要求`sealed=true`、`snapshot_seq`、`source_sha256`、`transport_kind=agent_terminal_fragment`、`terminal_receipt_id`、`terminal_receipt_ref`和`reason_code=null`。
- `local_only`：要求本地`fragment_ref`、`sealed=true`、`snapshot_seq/source_sha256`，并令`server_fragment_ref/terminal_receipt_id/terminal_receipt_ref`全部null，`transport_kind=none`，reason为稳定生成/上传失败code。

没有安全sealed fragment时，TaskResult外层AvailabilityRef使用`unavailable + stable reason`，不创建descriptor；capability未selected则使用`not_applicable/CAPABILITY_NOT_SELECTED`。Result accept校验的是descriptor ContentRef与receipt/core的整条链，不能相信内嵌字符串。

早期capture的core digest为not_applicable；terminal必须available。普通fragment不能包含最终含receipt的transport envelope bytes。Server接受result后，`transport_envelope_digest`进入ServerSnapshot，保持无环。

完整冻结顺序是：

```text
PreGate closure → GateInput → GateDecision → final closure
→ TaskResultCore → terminal fragment → immutable terminal upload/transport receipt
→ complete immutable transport envelope → binding/index CAS + task-result transport ACK
→ ServerDebugSnapshot → DebugAssembly
```

final closure排除全部debug对象；terminal fragment不得包含complete transport envelope/result ACK；transport envelope可包含terminal fragment descriptor+immutable terminal receipt ContentRef，但不得包含或反向引用binding/index；ServerSnapshot只在result接受后生成。任何反向ContentRef都按cycle拒绝。

### 9.3 ServerDebugSnapshot

一个数据库read transaction冻结：

- tenant/scan/task/job/run/transport/native identities、versions、deletion。
- scan/job/attempt/task lifecycle、cancel/lease/recovery records。
- event high watermark和prefix digests、progress/phase/error。
- artifact metadata/immutable revisions、result core/transport envelope digests、immutable receipt与binding/index identity。
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
- result被拒时fragment保持quarantine且不公开；只有 Task 后来由完整全局矩阵选择 outcome、并经内部 TaskResult CAS 完成终态事务后，Composer才可用权威snapshot生成`partial/RESULT_NOT_ACCEPTED`。Lease loss/abandonment 本身不满足该条件。
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
- 普通receipt/terminal receipt类型不可互换；immutable terminal receipt bytes/ContentRef永不变化，独立binding/index的`null→exact transport_envelope_digest`唯一CAS、禁止清空/重绑、exact retry、different digest conflict与事务回滚/无半绑定全覆盖；core/transport双digest与receipt无hash循环，相同capture/result重试bytes不变。schema、binding、digests和crash fixtures全部来自D23 Server package exact pin。
- v1/v2 fragment读取矩阵与`postlude` major边界通过；v1永不生成complete。
- debug ordered projection每格、`reasonCodes[]`顺序、唯一`reviewRun.agentTask.debugBundle`路径、`local_only`、普通fragment无terminal receipt、result被拒、server-only snapshot、orphan capsule、partial URL都有三端fixture。
- complete旧alias必须与新Assembly逐字节同一；partial/source fragment无alias且所有状态无audit fallback。
- delete/tombstone与所有写/读/retention/pin/crash upload逐项竞态。
- 无真实bundle时所有版本都没有audit URL；legacy与new complete指标分开。

## 10. Roadmap — V2.0：Fleet 与能力调度

本节是 roadmap，不是当前 implementation authority。V2.0 开工前必须另写完整
implementation design，闭合 fleet ownership、scheduler state/storage/wire、capability fixtures、
rollout、rollback 与 DoD，并取得适用独立决议；以下内容不得直接驱动生产实现。

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

### 10.5 V2.0 Roadmap 验收意图（非当前完成门）

- 多Worker并发claim无duplicate；每Worker始终一slot。
- capability mismatch、stale health、quota、cache poison/cross-tenant、worker loss tests。
- scheduler deterministic tie-break和可解释reason；不按语言硬编码pipeline。
- fleet rollout/rollback不破坏已grant run和protocol mode。

## 11. Roadmap — V2.1：R4 高风险控制基础

本节是 roadmap，不是当前 implementation authority。V2.1 开工前必须另写完整
implementation design，闭合 R4 schema/state/storage/wire、审批与 effect fixtures、rollout、
rollback 与 DoD，并取得适用独立决议；当前 policy-only 描述不能授权任何 R4 dispatch。

### 11.1 支持含义

V2.1完成门通过后，binary才可广告`effect_ledger_r4@1.0`；它依赖`agent_task_protocol@1`和`effect_ledger_r3@1`，并以第3节的V2.0完成门作为版本级硬依赖。Server offer仍不等于授权；只有Task grant同时selected两个capability依赖、Server验证当前`worker-capability-snapshot/v1`，并绑定获批`r4_tool_profile_id/profile_digest`时，R4 Gateway才可能dispatch。

未来 V2.1 独立 implementation design 必须完整覆盖 R4 contract/tool descriptors、审批、Effect
Ledger、不可重试和人类恢复路径，并保持全局 default deny。生产启用某个 R4 tool 仍需要单独
versioned product policy 和 release approval；未知 R4 始终拒绝。

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

### 11.4 V2.1 Roadmap 验收意图（非当前完成门）

- 默认off和所有绕过路径零dispatch。
- capability依赖、R3/R4 grant、effect/result/public DTO major矩阵与old Web投影全部通过；任何R4→v1/v2 downgrade零写入。
- approval伪造/重放/过期/撤销、identity drift、cancel/delete、response loss矩阵。
- UNKNOWN永不被普通终态隐藏；operator runbook可安全查询但不重复动作。
- red-team评审和人工演练签字；没有合格product profile时版本仍可发布但R4保持unsupported。

## 12. Roadmap — V3.0：Eval 驱动质量自适应

本节是 roadmap，不是当前 implementation authority。V3.0 开工前必须另写完整
implementation design，闭合 policy/state/storage/wire、eval fixtures、rollout、rollback 与 DoD，
并取得适用独立决议；以下候选指标与门槛不能被当作已批准的生产 policy。

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

### 12.4 V3.0 Roadmap 验收意图（非当前完成门）

- candidate/stable完全可复算；无法获得model immutable snapshot时同时间窗重跑stable并披露限制。
- policy选择有reason trace，可离线replay。
- benchmark未见技术栈/对抗/脏基线/大repo覆盖。
- adaptive策略回滚只影响新Task；active Task继续冻结policy version。
- 无任何在线无门自修改。

## 13. 跨版本数据、协议和删除策略

### 13.1 Schema兼容

- 所有持久对象保存schema ID/version和raw immutable bytes；projection可重建。
- production reader 只接受 exact-pin current package 规定的 schema；未知或不匹配 major 不投影。
- future current-contract major 必须先有独立 implementation design 与 release-gate 决议，再经
  协调切换成为新的唯一 current package；不得保留运行时多 major 生产选择。
- current-package upgrade 不重写已接受的 TaskResult/Attestation/Effect receipt；需要解释修正时追加Addendum。
- hash/canonicalization变化必须新schema major，不能原地重算旧digest。

### 13.2 协调切换与 capacity-only 发布模板

1. Server 发布候选 current package，Worker/Web exact-pin 同一 identity/version/digest。
2. 在结果揭示前冻结并签发 benchmark bundle 与 release policy。
3. 对 exact `CandidateDigest` 运行 offline gate；只有 exit `0` report 可获得 attestation。
4. 在同一 current package 上完成 rollback 演练，然后原子启用 D24 acceptance barrier。
5. 依次完成 5% 与 25% target capacity 的签发 canary 窗口，再扩至 full capacity。
6. 全部 accepted Task 保持 current contract；非 canary intake 留在 Server control plane 或暂停。
7. canary 通过后签发 immutable baseline record；future package 只能对该 accepted stable object 比较。

rollback 不得改变已接受 Task 的 package/schema/storage semantics，也不得删除已提交的新
schema 数据。必要时只先 fence 失效 ownership；Reconciler 的后续诚实 terminalization 必须由
完整全局矩阵选择 outcome，并以内部 TaskResult CAS 线性化，不能由 rollback、lease loss 或
Server ACK 隐式授权。没有可用 stable build 时只能 stop intake、fence 或 reject。

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
- exact package/`CandidateDigest` 一致性、offline verdict、D24 barrier 状态、canary capacity/stage 与 rollback target。

高基数task/tenant ID只进受控trace，不做无界metric label。日志不含secret/source/full prompt。

### 14.2 Runbooks

每版必须提供：package/digest conflict、stale epoch、checkpoint corruption、resume denial、
result conflict、UNKNOWN effect、debug partial/failed、tombstone race、barrier/canary 停止和
exact stable rollback。Runbook 只允许查询/恢复，不得指示 operator 重发未知 effect 或改写签发门值。

### 14.3 自动rollback门

任一条件触发停止 capacity 扩大：

- stale/cancel后成功发布>0。
- Effect duplicate或UNKNOWN被错误隐藏>0。
- cross-tenant/source/secret泄漏>0。
- schema/digest/receipt无法复算。
- false verified 超过冻结绝对/相对门。
- `platform_failure_rate >= 2%`，或相对 accepted stable baseline 增加超过 2 percentage points。
- verified-success p95 wall time 或 p95 cost 相对 stable 增加超过 20%。

样本或窗口不足同样不得晋级。触发后自动回到 exact-pin、实现同一 current package/schema/storage
semantics 的已签发 stable build；没有该对象时 stop intake、fence 或 reject。安全事件可
revoke/fence；其他 active Tasks 仍按其冻结 current package 完成，除非已签发 release policy
明确要求终止。rollback 不得重开 D24 barrier 已排除的 intake 或建立第二生产轨道。

## 15. 按成熟度分流的目标追踪矩阵

“完成证据”只说明对应能力未来如何验收，不宣称证据已经存在。Roadmap 行不得进入当前 DoD，
也不得作为 implementation 授权；它们只能成为各自独立 implementation design 的输入。

| 目标设计能力 | 版本 | Maturity | 完成证据或未来验收意图 |
|---|---|---|---|
| Thin deterministic kernel、persistent Owner、typed tools | MVP | MVP baseline | schema/state/Gateway/recovery tests |
| Requirement Ledger、Observation/Attestation、双Gate | MVP | MVP baseline | mandatory coverage和false-green fixtures |
| D22 signed absolute + stable-relative release gate | MVP/Post cross-cutting | implementation baseline | canonical policy/report/attestation、三态 CI、offline benchmark、D24 barrier 与 capacity-only canary |
| 同outer lease checkpoint恢复 | MVP | MVP baseline | crash matrix |
| lease-loss Task/Attempt分层与 qualified successor | D8；正向 successor 在 V1.3 | implementation baseline | abandonment/fence/version 原子矩阵、stale actor 拒绝、resume predicate/crash tests |
| current Agent-First grant/TaskResult ingest | V1.0 | implementation baseline | exact-pin current-contract、legacy absence和digest/receipt tests |
| Public agentTask DTO、Server Reconciler | V1.0 | implementation baseline | lifecycle oneOf、source audit、idempotency |
| Explorer/Troubleshooter/Implementer | V1.1 | roadmap | bounded DAG/single-writer/ablation |
| R3 Effect Ledger和effect-aware terminalization | V1.2 | implementation baseline | provider/reconciliation/cancel-loss矩阵 |
| Server-ACK watermark、same-run跨lease恢复 | V1.3 | implementation baseline | resume/budget/event sequence crash矩阵 |
| WorkerFragment/ServerSnapshot/Assembly/Public Debug | V1.4 | implementation baseline | causal/security/retention/delete矩阵 |
| Fleet/Profile/capability scheduling/cache | V2.0 | roadmap | multiworker/cross-tenant/capability tests |
| R4安全控制基础 | V2.1 | roadmap | default deny/two-person/UNKNOWN runbook |
| Eval-adaptive Quality/Verifier/skills | V3.0 | roadmap | frozen candidate/holdout/canary/rollback |

## 16. Implementation baseline 验收与 Roadmap 排除

### 16.1 当前 implementation baseline DoD

只有 implementation baseline 行的以下条件全部成立，才能关闭相应实施范围；Roadmap 行明确排除
在当前 DoD 之外：

- 对应 implementation baseline 行有可定位 commit、schema、fixture、test run 和生产观察窗。
- `benchmark-bundle/v1`、`release-gate-policy/v1`、`release-gate-report/v1` 与
  `release-gate-attestation/v1` 均 exact-bind package/candidate/stable identity、control/runtime/
  benchmark/task/oracle/environment digest、seed、重复次数、统计实现、threshold 与 canary plan，
  并由 trust registry 中未撤销 key 对 RFC 8785 JCS bytes 作 Ed25519 签名。benchmark owner、
  CI evidence producer 与最终 release operator 是不同 principal；deployment operator 无改门或 promote 权。
- policy 未超过 30 天、attestation 未超过 7 天；missing、过期、撤销、签名或 scope/digest
  不匹配、证据不新鲜均得到 CI exit `2`，而不是 fail-open。
- offline benchmark 至少有 120 known-gold tasks、3 个各不少于 15 tasks 的 sealed
  unknown-stack families；每个适用核心簇五类 fixture 各不少于 3 tasks，全局至少 50 个
  oracle-positive findings。每 task 按 3 个预声明 seed 独立运行 3 次并等权计入冻结分母；
  invalid exclusion 只接受 policy 预列 reason，false-verified 95% 上界使用 Wilson。
- 全部绝对门通过：安全越权、stale publish、duplicate effect/result 与 critical/adversarial
  false verified 为 0；mandatory Ledger/final SourceState coverage 为 100%；总体 false verified
  点估计 < 1% 且 Wilson 上界 < 2%；known/unknown task success ≥ 70%/50%，unaided
  completion ≥ 60%/40%；false discovery ≤ 20%；environment/capability classification ≥ 95%；
  每个 task profile 的 wall/token/cost cap 均为正数、有限且无 wildcard。
- 已有 accepted stable baseline 时，false verified 不恶化；known/unknown task success、
  unaided completion 与 classification 下降分别不超过 2 percentage points，false discovery
  增加不超过 2 percentage points，verified-success p95 wall time/cost 增加不超过 20%。
  zero-tolerance/absolute safety gate 无 waiver；其他门值、分母或统计变更在结果揭示前另取决议或 ADR。
- stable baseline 只由通过 offline gate 与 canary 的 candidate 晋升并形成 immutable record；
  不自动刷新或事后选择。comparison 使用 exact stable package/`ControlPlaneDigest` 和相同
  benchmark/task/oracle/statistics/`EvaluationRuntimeDigest`；runtime/model 不可 exact-pin 时，
  在同一 72 小时窗口以 candidate runtime 交错重跑 stable，并绑定 comparison report digest。
  bootstrap 仍通过全部绝对门，相对门为 `not_applicable`，canary 后才形成首个 baseline。
- 唯一 current protocol 下的 Task/Attempt/Effect/Resume/Debug 状态均是穷举 state machine，无“其他情况按经验”或运行时 mode negotiation。
- Worker、Server、Web 对同一 exact-pin current package 的 offline gate、D24 barrier、
  capacity-only canary、stable rollback 与无 stable 时的 stop-intake/fence/reject 路径全部自动化；
  CI 只有 exit `0/1/2` 三态，且仅 exit `0` exact report 可签发 attestation。
- D24 barrier 后 canary 先满足 5% capacity、至少一台 Worker、24 小时和 200 accepted current
  Tasks，再满足 25% capacity、72 小时和 1000 Tasks，之后才 full capacity。每阶段
  `platform_failure_rate < 2%`，且存在 stable 时增加不超过 2 percentage points；样本/窗口
  不足不晋级，任一 zero-tolerance、platform 或 p95 门失败都停止扩容并执行签发 rollback plan。
- stale lease/epoch、cancel后success、重复effect、secret/source/cross-tenant泄漏均为0。
- 所有success 100%覆盖mandatory Ledger、final Source/ExecutionState、ObservationManifest和Quality Policy要求Attestations。
- UNKNOWN effect全部在固定deadline前收敛或以专门outcome披露；restart不延长deadline。
- lease/worker loss 原子记录独立 abandonment、永久 fence 精确旧 transport/native Attempt 与 actor/session，并按控制事件规则推进 Task version；Task 保持 `ACTIVE|FINALIZING`、terminal/result 不变，stale actor 零写入。
- same-run resume 只由全部显式恢复资格谓词成立的 successor 接管，不重复 transition 旧 Attempt，不降mode、不延deadline/budget、不复活wait/cancel/revoke/delete，response loss幂等；资格耗尽或 successor timeout 均不凭 D8 直接终态化 Task。
- Debug complete全部满足causal/hash/redaction；partial/absent/failed/expired/deleted不伪装URL，永不fallback audit。
- 文档、runbook、schema registry与实现同步；没有用TODO或feature flag代替必需安全路径。
- 每个版本都有跨项目的新增/修改文件行数报告和超大文件baseline报告；没有新增未豁免的600行以上手写文件，没有向超大遗留文件加入未抽离的新职责，所有例外均可审计。

### 16.2 Roadmap 未来验收意图（非当前 DoD）

- V1.1 的动态角色必须证明 bounded DAG、single-writer、预算与 ablation 价值门。
- V2.0 的 fleet 必须维持单 Worker 单 slot 与 instance isolation，且 cache 不改变 SourceState 或 tenant 边界。
- V2.1 的 R4 必须默认 deny；任何启用 profile 都须通过独立 security/recovery 审批。
- V3.0 candidate 必须保留原始分母、stable 并发对照与 rollback，且生产策略不得在线无门自改。

这些条目只供各 roadmap 版本的独立 implementation design 完善和取证，未完成不会阻塞当前
implementation baseline 的 DoD，也不能据此宣称 roadmap 已闭合、已实现或获准部署。

## 17. 按成熟度分流的 Agent 执行规则

### 17.1 Implementation baseline

执行 C0/V1.0 或 V1.2-V1.4 的 implementation baseline 时，Agent 必须按顺序：

1. 读取涉及项目的`AGENTS.md`和本版本章节。
2. 重新盘点当前HEAD/dirty worktree/contract hashes，保护用户改动。
3. 把版本拆成tracer-bullet vertical slices；每slice跨必要端但保持可rollback，同时先给出模块所有权、文件行数预算，以及触及超大遗留文件时的职责抽离方案。
4. 先提交schema、fixtures和失败测试，再实现。
5. 运行 unit/contract/property/crash/security、exact-pin current-package 组合与 production
   composition absence checks。
6. 在候选结果揭示前冻结签名 benchmark bundle 与 release policy；按 D22 运行完整 offline
   absolute + stable-relative gate，保留原始分母、排除 reason、Wilson 计算与三态 report。
7. 记录 `ControlPlaneDigest`、`EvaluationRuntimeDigest`、`CandidateDigest`、签名
   attestation、rollback/stop-intake 演练、指标门，以及 MVP 第3.2节要求的新增/修改文件行数、
   baseline 变化和模块化例外。
8. 只有 exit `0` report 才能进入 D24 barrier；按 5%→25%→full 的 capacity-only canary
   取证，失败时执行 exact stable rollback 或 stop-intake/fence/reject。
9. 只在第3节依赖表列出的全部硬依赖完成门通过后启用本版能力；未列为硬依赖的编号中间版可以跳过。具体地，V1.2可在V1.0稳定后独立于roadmap V1.1交付，但不得使用任何未由独立设计和决议授权的动态角色。
10. 若发现本文未闭合且会改变权限、外部行为、数据或兼容性的选择，停止该slice并请求新决策；局部模块命名等可逆选择由Agent记录ADR后继续。

### 17.2 Roadmap

V1.1、V2.0、V2.1 或 V3.0 开工时，Agent 必须先停止功能实现，为该版本单独编写完整
implementation design，至少闭合 schema、state machine、storage、wire contract、valid/invalid/
crash fixtures、rollout、rollback、DoD、owner 与数值门。设计必须接受独立 implementation
与 release-gate 决议，明确协调切换边界并重新签发适用 D22 policy，之后才能把获批范围转为
implementation baseline 并按第17.1节执行。

独立设计不得复活 dual-read、运行时 multi-major negotiation、old-Web compatibility、downgrade、
legacy fallback 或第二生产轨道。第17.1和17.2节都不授权 Agent 自动推送生产、发外部消息、启用
R3/R4、修改 runtime/schema/protocol/deployment 或删除旧数据；这些动作仍需对应决策与发布/运维权限。
