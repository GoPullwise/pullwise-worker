# Codex SDK + Reviewer Skill Worker 重构执行规格

状态：Implementation Specification / Governance-gated / 未授权生产切换

版本：2026-08-05

范围：`pullwise-worker`、`pullwise-server`、`pullwise-web`、`pullwise-admin`

## 0. 文档地位与授权边界

本文把原架构 Proposal 和 2026-08-05 的补充评审意见合并为可执行的跨仓重构规格，规定目标架构、契约、工作包、测试、阶段门、切换、回滚和删除标准。

- `contracts/agent-first/spec-decision-register.json` 仍是当前决策权威。冲突的生产实现必须先由 append-only 新决策逐项 supersede。
- 当前 D41 仍禁止 D24 激活、部署、生产流量、真实 benchmark、canary、cutover 和 legacy 删除。
- Stage 0A 可立即修复不改变既有语义的证据漂移；Stage 0B 若替换 gate 或改变 completion 语义，必须先有 append-only 决策。Stage A 需架构所有者参与；Stage B 需新决策明确授权离线 candidate；Stage C–F 在前序门和显式授权前均为 `NO-GO`。
- 生产始终只有一个 current contract；不得增加 shadow authority、fallback、双写/双读、协议协商、downgrade 或 compatibility mode。

| 阶段 | 当前可执行 | 额外授权 |
|---|---|---|
| Stage 0A 证据修复 | 是 | 只能恢复既有权威生成链/基线语义，不得借修复改规则 |
| Stage 0B gate replacement | 否 | append-only 决策先冻结 history/live catalog、三态和替换义务 |
| Stage A 决策/契约冻结 | 有条件 | Stage 0B governance decision 已完成，D43-D47 等价决策逐项 resolution |
| Stage B 离线 candidate | 否 | 新决策取代 D41 停止边界 |
| Stage C 最小生产壳候选 | 否 | Stage B PASS + 实施授权；仍不接生产 |
| Stage D 跨仓切换准备 | 否 | Stage C PASS + 契约生成授权 |
| Stage E clean cutover/canary | 否 | 离线 attestation + strict absence + D24 + 发布授权 |
| Stage F 全量/收尾 | 否 | canary PASS |

## 1. 执行结论

采用：

> **Thin deterministic Worker shell + pinned Codex Python SDK + explicitly bound versioned Reviewer Skill + mechanical validator + durable terminal candidate outbox**

- Skill 维护审查方法、证据标准、严重性、置信度、误报抑制和输出纪律。
- Worker 维护单槽、snapshot、受控指令面、SDK/runtime pin、deadline、cancel、sandbox、coverage、redaction 和 outbox。
- Server 是队列、租约、取消、预算和全局终态 CAS 的唯一权威。
- 离线 benchmark 证明语义质量；每次运行的 validator 证明 wire/source/location/coverage/发布事实。
- 删除无当前产品消费者的通用 Agent OS 抽象：Task Owner、独立 Quality Verifier、Requirement/Observation/Attestation 图、通用 capability/effect、语义 checkpoint、固定 fanout/voting。

这不是纯 prompt Worker；安全、权限、时间、资源、事实和发布仍由代码强制执行。

## 2. 当前证据快照

2026-08-05 当前四仓复核：

- 默认 builder 仍返回 `ReviewWorkerV1`；Agent Kernel 只是可选 shadow projection。
- `review_worker_v1.py` 仍注册 30 个生产 phase，SDK 调用仍在该路径。
- Worker 固定 `openai-codex==0.1.0b3`，已有 `SkillInput`、`Thread.run(..., output_schema=...)`、thread/turn/interrupt。该版本公开 Sandbox 封装只表达预设模式，不能单独证明受限读取根；Stage B 必须先对 exact SDK/CLI/runtime tuple 做 capability probe。探针通过时可不升级；否则只能使用经验证的外部 sandbox、经显式决策的兼容性升级，或 `NO-GO`。
- Server/Web 已接收动态 `progressSteps`；旧依赖主要在 billing/quota phase、artifact kind、测试、Admin 配置和文案。
- Server quota 仍识别 `repo_map`、`risk_routing`、`reviewer_fanout`、`clustering_and_voting`、`validator_disproof`、`final_report_json`。
- Admin 仍编辑 `reviewerConcurrency`，并消费 `maxBundles/maxReviewerAssignments`。
- 决策注册表为 `ready`：40 resolved、D2 inactive、无 active decision。
- Slice-0 当前失败：generated wrapper 8,762/8,062 行不一致、digest 不一致、`tests/test_agent_first_decision_register_gate.py` 未入 baseline。
- legacy contract baseline 为 `compatible`，14 组固定 probe 全部通过。
- 默认 absence ratchet 为 `ratchet_clean=true`、`legacy_absent=false`。
- 当前 `--require-absent` 既发现 live legacy，又因 `worker.004-frozen-contract-baseline` 自引用而 `indeterminate`，不能证明 clean break。
- 四仓取证时工作树均干净；远端 CI 未纳入本地证据，实施阶段必须复查。

因此必须从 Stage 0A 开始；未完成 Stage 0B 所需决策时，不能替换 gate、写 candidate 或删旧代码。

## 3. 范围、目标与非目标

唯一产品任务是 `repo_review.full_scan`：输入为 Server 授权的不可变仓库快照与政策；source 只读；受控验证只写 disposable copy；输出 findings/coverage/limitations/usage/terminal candidate；中断后从头重算，只恢复冻结 outbox。

目标：

- 语义来源收敛为一个 Worker-owned Reviewer Skill。
- 正常路径一个 root thread、一个 main turn；最多一次纯格式修复 turn。
- 公开进度为 `preparing/reviewing/validating/publishing/terminal`。
- 使用三层最小契约，不向浏览器泄露 Worker 私有字段。
- 以 D22 专业化后的 paired benchmark 证明质量不劣。
- cutover 后删除 30-phase、非目标 Agent Kernel、shadow/fallback/compatibility 和旧消费者。

非目标：

- 写回原仓库、自动修复/PR、外部 tool/provider effect、人工 approval、通用 Agent task。
- reviewer fanout、投票、per-run Verifier、子 Agent、跨 lease semantic resume。
- 证明模型“理解每一行”；产品只承诺 inventory complete + coverage accounted。
- 兼容旧任务/wire/table/DTO/数据。历史留存需单独的只读、不可执行、隔离合规决策。

## 4. 系统不变量

1. 每 Worker identity 最多一个 active job、一个 SDK/App Server、一个 auth store。
2. Server 拥有 lease/cancel/budget/global terminal authority；Agent 不创造 authority。
3. source snapshot 只读；写入只在 disposable validation copy。
4. Skill 及其全部 runtime-reachable bytes、SDK、CLI、model、effort、schema、source、instruction manifest 均 exact-pin。
5. Agent tool 的可读根、可写根、命令、环境和网络均为显式 allowlist；它无 provider/control credential、无 approval。provider OpenAI 出站是独立通道。
6. 缺失、漂移、超限、未知 schema/reason、stale attempt、source mutation 均 fail closed。
7. 无 finding 可以成功；无 coverage closure 不能成功。
8. Worker outbox 是不可变 attempt candidate；Server CAS 才是全局终态。
9. 不存在第二 runner/authority/current contract/fallback。
10. 新手写文件遵守 400/600 行门，不向现有超大文件增加新职责。

## 5. 目标架构与信任边界

```text
Pullwise Server
  ├─ queue / acceptance / lease / cancel / budget / deadline
  ├─ private Worker control + terminal CAS
  ├─ normalized review result + issue dedupe
  └─ public Web/Admin DTO
          │ authenticated control/clone transport
          ▼
Thin Review Worker
  ├─ one slot + supervisor
  ├─ immutable source + instruction manifest
  ├─ restricted model-visible filesystem + sanitized tool environment
  ├─ controlled CODEX_HOME/CWD + pinned Skill/SDK/CLI
  ├─ one thread/turn + optional format repair
  ├─ source/location/coverage/result validator
  └─ immutable terminal candidate outbox
          │ Worker-scoped provider channel
          ▼
Codex SDK / App Server
  ├─ explicit Reviewer Skill
  ├─ read-only source/search
  └─ bounded local validation in disposable copy
```

三条通道：

| 通道 | 允许 | 凭据 | Agent 可见 |
|---|---|---|---|
| SDK/provider | 仅 OpenAI/Codex 出站 | Worker 专属 auth | 否；工具读不到 auth/env |
| Worker↔Server/clone | lease/heartbeat/cancel/artifact/result/clone | Worker token/短期 token | 否 |
| Agent tools | instrumented source read/search、disposable copy allowlist 命令 | 无 | 是；restricted read/write roots、network disabled/deny-all |

“无网络、无生产凭据”专指 Agent tools，不是让 SDK 或 Worker control transport 失效。

### 5.1 模型可见文件系统与进程边界

仅设置 Worker-owned `CODEX_HOME`、turn `cwd` 或 `readOnly/workspaceWrite` preset 不能证明读取隔离。当前 [Codex App Server](https://developers.openai.com/codex/app-server) 的 `readOnly.access` / `workspaceWrite.readOnlyAccess` 在未显式限制时默认是 `fullAccess`；固定的 `openai-codex==0.1.0b3` 公开 Sandbox 封装也没有受限读取根参数。因此 exact tuple 必须通过运行时 capability probe，不能从版本号或 prompt 推断安全性。

每个 turn 的 model-visible filesystem 只能包含：

- Worker-owned model-turn 目录：读写，仅用于允许的临时输出。
- immutable source snapshot：只读。
- staged model-visible Skill/runtime assets、instruction manifest/bytes：只读。Agent-output schema 由 Worker 通过 SDK 传入；Worker result schema、control manifest/outbox 不进入 tool readable roots。
- disposable validation copy：仅在 policy 允许验证时读写。
- 执行 allowlist 命令所必需的最小系统 binary/library/runtime：只读、显式枚举或由经审计的外部镜像固定。

必须拒绝模型读取 Worker source、Worker control/runtime state、terminal outbox、logs、clone/control token、host/user home、其他 worker root、未列入 manifest 的 temp/cache，以及 `CODEX_HOME` 中的 auth/config/session。App Server/provider supervisor 可以使用 Worker-scoped auth，但 tool child process 只能收到最小 sanitized environment；不得继承 API key、token、`HOME`/`CODEX_HOME` credential path 或 Server transport credential。

实现顺序：

1. 若 exact SDK/CLI tuple 能表达并实际执行 restricted `readableRoots`/`readOnlyAccess`，使用该能力并 exact-pin wire/runtime evidence。
2. 否则把 App Server 工具执行置于 Worker-owned external sandbox/container/mount namespace，只挂载上述 allowlist roots；provider/control 通道留在工具 sandbox 外。
3. 若两者都无法隔离读取或清除 tool environment，Stage B `NO-GO`。不得写手工 JSON-RPC 绕过 SDK，也不得把普通 `read-only` preset、文件权限愿望或 prompt 当作证据。

Stage B 使用模型可知路径的 sentinel files、sentinel env、host home、其他 worker root、auth store、outbox、source write、validation-copy write 和 network endpoint 做正反故障注入。只有允许路径成功、所有拒绝路径稳定 fail closed，才可签发 capability evidence。

### 5.2 责任边界

| 组件 | 必须负责 | 不得负责 |
|---|---|---|
| Server | acceptance/lease/cancel/deadline/budget/CAS/quota/public projection | prompt/Skill/语义判断 |
| Worker | 读写根与 tool env 隔离、snapshot/instruction/SDK/deadline/validation/outbox | 全局终态/issue 生命周期/公共 DTO |
| SDK | thread/turn/tool/event/usage/interrupt | lease/terminal truth |
| Skill | 审查方法和输出纪律 | 权限/预算/终态/网络/凭据 |
| Validator | schema/binding/location/hash/coverage/redaction | 自然语言复审 |
| Web/Admin | 公共 DTO 和当前配置 | lease/attempt/Skill/source 私有事实 |

## 6. Reviewer Skill 与受控指令面

### 6.1 唯一语义资产

- `pullwise_worker/reviewer_skill/SKILL.md`
- `pullwise_worker/reviewer_skill/manifest.json`
- `pullwise_worker/reviewer_skill/review-agent-output-v1.schema.json`
- `pullwise_worker/reviewer_skill/review-result-v1.schema.json`
- `pullwise_worker/reviewer_skill/eval-fixtures/**`

`manifest.json` 固定 Skill name/version、Skill/schema digests、工具 allowlist、最大 turn 数、最小 SDK/runtime tuple，并把文件分为 model-visible runtime assets 与 Worker-only control assets。`review-agent-output-v1.schema.json` 由 Worker 作为 `outputSchema` 传入，`review-result-v1.schema.json` 只供 Worker validator 使用；二者都不需要作为 tool-readable 文件挂载。wheel check 必须证明安装 bytes 一致。旧 prompt 的有效知识经有来源的迁移表进入 Skill；迁移后生产不再读取旧 prompt。

`manifest.json` 还必须列出按 canonical relative path 排序的全部 runtime-reachable Skill files 及其 size/SHA-256。若 `SKILL.md` 引用 `references/`、`scripts/`、`assets/` 或其他文件，它们全部进入同一 manifest；未列入、重复、越界或 digest 漂移的文件拒绝 staging。Stage B 推荐 runtime Skill 只含 `SKILL.md` 和明确必要的只读引用。生产语义只来自这组 runtime assets；`eval-fixtures/**` 仅是离线评测证据，永不 staging、挂载或暴露给模型。

### 6.2 显式绑定与防 TOCTOU

每个 attempt：

1. 从已安装 package 按 manifest 将 exact Skill runtime assets 复制到 model-visible read-only staging，将 schemas/control assets 复制到独立 Worker-only validation staging；两者都不在 source 内且都不可由模型写入。
2. 拒绝 symlink/reparse/non-regular/multi-link/越界路径，生产 POSIX 使用私有权限。
3. 对每个 runtime file 记录 path/device/inode/size/SHA-256；`Thread.run` 同时传 `$pullwise-reviewer` 文本和指向 staged `SKILL.md` 的显式 `SkillInput`。
4. turn 后重检全部 staged 对象；任何增删、替换或 bytes 漂移使 attempt `FAILED`。
5. result 绑定 staged Skill digest 和 package manifest digest。

Stage B 必须故障注入 swap、symlink、truncate、turn 中修改。

### 6.3 关闭隐式 surface

- 使用 Worker-owned 空白/生成式 `CODEX_HOME`，不继承 user/admin/system skills、插件、MCP、hooks 或全局会话。它只控制 Codex discovery/state，不替代 5.1 的文件读取隔离。
- turn `cwd` 位于 Worker-owned model-turn 目录，不在 target repo/Worker source，避免父级 `AGENTS.md` 自动发现。
- 只装 allowlist Skill；启动后枚举实际 skill/tool/MCP/plugin/hook surface，与 manifest exact compare。
- 无法证明非允许 surface 被关闭时，Stage B `NO-GO`；prompt 声明不是隔离证据。

### 6.4 target `AGENTS.md`

不依赖 Codex 默认 32 KiB merge，也不把“记录 digest”当成模型已看到内容。Worker 在 snapshot 上生成 `instruction-manifest/v1`：

- 机械发现 root/nested exact `AGENTS.md`，只接受 regular file。
- canonical POSIX path；浅到深优先级，nested 只作用其子树。
- 记录 path/scope/depth/size/SHA-256/precedence。
- 禁止截断。初始上限：128 files、单文件 256 KiB、总计 1 MiB；越限 `PARTIAL/FAILED`。
- boot input 只内联有界 policy 与 manifest path/digest；Skill 在审查 scope 前读取适用 bytes。
- source 与 instruction bytes 只能通过 Worker-owned instrumented read/search gateway 形成 coverage authority。SDK tool events 只用于 call correlation/diagnostics，不是文件读取证明。
- gateway 在 model 不可写 ledger 中记录 receipt：attempt/turn/tool-call、单调序号、inventory id、canonical path、byte/line range、returned-bytes SHA-256、完整/部分读取类型、适用 instruction-set digest。search receipt 只能证明返回的匹配范围，不能冒充整文件读取。
- instruction receipt 必须在对应 scope 的首个 source-read receipt 之前；Worker 按 ledger 时序验证。coverage entry 绑定适用 instruction-set digest；缺失、晚到、digest 不符或只有 search receipt 时不得标 `inspected`。
- gateway 或不可伪造 ledger 无法实现时，Stage B `NO-GO`；不得从 shell command text、stdout、模型自报或普通 SDK event 合成 receipt。

限额必须在 Stage A 冻结，benchmark 解盲后不得修改。

### 6.5 Agent 拓扑

- 每 attempt 一个新 root thread，一个 main turn。
- 仅最终消息 schema 无效且预算充足时，同 thread 允许一次 format repair；不得重扫或新增 finding。
- 禁止 fanout、独立 verifier、子 Agent、并行 turn、第二 App Server。
- archive/close 有界；失败标记 runtime unhealthy，下一 attempt 使用新 runtime。

## 7. 三层契约

### 7.1 Server↔Worker 私有 wire

Server-owned canonical source：`pullwise-server/contracts/reviewer-worker/v2/**`，至少含：

- `worker-registration/v2`
- `review-task-claim/v2`
- `review-worker-heartbeat/v2`
- `review-run-event/v2`
- `review-artifact-descriptor/v2`
- `review-cancel-command/v2`
- `review-terminal-candidate/v2`
- `review-terminal-receipt/v2`
- `review-error/v2`

它包含 job/run/lease/attempt/epoch、deadline/budget、authoritative cancel generation/digest、runtime/Skill/schema/source/instruction binding、outbox generation/digest/idempotency。Web/Admin 不得看到。Server 生成，Worker exact-pin Python wrapper；生成遵守一次性原子跨仓 parity，禁止手改。

### 7.2 Server 内部 normalized result

`normalized-review-result/v1` 不是 wire/浏览器 DTO；它把 accepted candidate 转为 findings/coverage/limitations/usage/artifacts/public state，生成 finding instance id/issue key，并保存私有 audit facts。

建议模块：

- `pullwise_server/review_worker_contract.py`
- `pullwise_server/review_terminal_cas.py`
- `pullwise_server/review_result_normalization.py`
- `pullwise_server/review_public_projection.py`

现有 `_app_part_XX`/`db.py` 只留 composition seam 和冻结行数，不承接新职责。

### 7.3 Server→Web/Admin 公共 DTO

`review-run/v2` 仅含 public run/scan id、公开 status、五级 progress/message/counters/ETA、summary/findings/coverage/limitations、artifact metadata/安全 URL、公开错误与时间。

禁止输出 Worker token、lease、attempt epoch、Skill/instruction 私有 binding、provider auth、thread id、raw env/source、CAS internals。Web 继续动态渲染；Admin 只消费 health/quota 和仍存在的配置。

## 8. 状态机与终态线性化

### 8.1 Worker 本地状态

| 状态 | 含义 | 允许下一状态 |
|---|---|---|
| `IDLE` | 无 active slot | `CLAIMED` |
| `CLAIMED` | job/run/lease/attempt 已持久化 | `PREPARING`、terminal candidate |
| `PREPARING` | checkout/snapshot/Skill/runtime/instruction/budget 校验 | `REVIEWING`、terminal candidate |
| `REVIEWING` | main turn 或一次 format repair | `VALIDATING`、terminal candidate |
| `VALIDATING` | 非 Agent validator | `PUBLISHING`、terminal candidate |
| `PUBLISHING` | outbox 已 freeze，等待 Server CAS/ACK | local terminal |
| local terminal | Server receipt 已持久化 | `IDLE` |

每个状态必须对应可观察边界和故障测试；不得为假想能力建状态。

### 8.2 Worker terminal candidate

Worker 在同一锁内：

1. 读取最新 lease/deadline，并验证可见的 Server cancel command generation/digest。
2. 验证 result、artifact snapshot 和 binding。
3. canonicalize terminal candidate。
4. 临时写入、`fsync`、atomic no-clobber publish、目录 `fsync`。
5. 记录 candidate digest/idempotency key。
6. 进入 `PUBLISHING`，之后 payload 永不可变。

只有 exact-bound Server cancel command 才能让 Worker freeze `CANCELLED` candidate。local operator stop、deadline、interrupt 或模型自报均不是 cancel authority，必须按 closed reason registry 进入 `FAILED/PARTIAL`。freeze 前 authoritative cancel 获胜时 candidate 为 `CANCELLED`；freeze 后不得改写、补 finding 或重跑。

### 8.3 Server 唯一终态 CAS

Server 以 `(job_id, run_id, attempt_id, lease_epoch, expected_nonterminal_version)` 在一个事务中线性化。校验顺序也是契约：

1. 先按 idempotency key 查询既有 terminal receipt。同 key + 同 digest 直接返回原 receipt，即使当前 lease/version 已推进；同 key + 不同 digest 为 `IDEMPOTENCY_CONFLICT`。
2. 无既有 receipt 时验证 job/run/attempt/lease/version、candidate digest/schema/binding。stale attempt/lease 拒绝且无任何终态/projection side effect。
3. candidate 自报 `CANCELLED` 时必须携带与 Server 当前 authoritative cancel record 完全一致的 generation/digest；无 cancel、缺 binding 或 binding stale 时以 `CANCEL_BINDING_INVALID` 拒绝，不得接受 Worker 自创取消。
4. CAS 前无 authoritative cancel 时，只接受 Worker 计算的 `COMPLETED/PARTIAL/FAILED` classification。
5. CAS 前 authoritative cancel 已提交时，全局终态为 `CANCELLED`；已冻结的非取消 candidate 只作 attempt evidence，不得投影为成功/部分/失败。
6. terminal CAS 已先提交时，后到的不同 result/cancel 不改变终态；只有第 1 步的 exact replay 可返回原 receipt。

事务同时保存 terminal state、accepted/attempt evidence pointer、适用的 normalized result pointer、quota transition、receipt、projection-pending marker。公共 projection 可幂等收敛，不能改写终态。

| 场景 | 结果 |
|---|---|
| turn 前 lease 丢失 | 不启动 turn；旧 attempt 永久禁发 |
| turn 中 cancel/deadline | bounded interrupt，放弃未冻结输出 |
| crash，未 freeze | 新 attempt 从头重跑 |
| crash，已 freeze | 只重放 exact outbox |
| ACK 丢失 | 相同 key/digest 查询/重放，返回原 receipt |
| bound cancel 在 freeze 前 | Worker candidate cancelled，携带 exact cancel binding |
| cancel 在 freeze 后、CAS 前 | Server CAS 选择 cancelled |
| cancel 在 CAS 后 | 终态不变 |
| Worker 自报 cancel、Server 无匹配记录 | `CANCEL_BINDING_INVALID`，无终态/projection side effect |
| stale submit | fail closed，无 projection |

## 9. Agent output、`review-result/v1` 与 coverage

### 9.1 不可信 Agent payload 与可信 Worker result

`Thread.run(..., output_schema=...)` 的目标是 `review-agent-output/v1`，它是不可信模型 payload，只含：

- findings 的语义字段与可校验 location/evidence components，不含稳定 ID。
- limitations。
- 非权威 coverage claims：inventory id/scope、模型声明的 disposition/reason；它不能制造 read receipt。

Agent payload 禁止包含或覆盖 job/run/attempt/lease/cancel、Worker/SDK/CLI/runtime、model/effort/Skill/schema/source/instruction digest、usage、artifact identity、candidate classification、terminal/public status。即使模型输出同名字段，strict schema 也必须拒绝。

Worker 在模型 payload 通过 schema/semantic/source/location/coverage 验证后生成 `review-result/v1`：

- `run_binding`：Worker 从 active attempt 注入 job/run/attempt/lease epoch。
- `execution_binding`：Worker 从实际 runtime 注入 Worker/SDK/CLI/runtime/model/effort/Skill/output schema digest。
- `source_binding`：Worker 从 immutable snapshot/inventory/instruction ledger 注入 repository/commit/tree/inventory/instruction digest。
- `findings`：验证并由 Worker 计算 instance identity 后的 findings。
- `coverage`：Worker 对 Agent claims 与 instrumented receipts 做交集后机械生成。
- `limitations`：模型声明与 Worker 检测的限制合并，Worker 检测项不可被模型删除。
- `usage`、`artifacts`：只来自 SDK events 和 Worker artifact snapshot。
- `candidate_classification`：只由 Worker closed classifier 计算。

`outputSchema` 只约束当前 turn 的 `review-agent-output/v1` 最终消息 shape；它不证明 trusted binding，也不允许 Agent 生成 terminal candidate。Worker validator/runner 才能组装 `review-result/v1` 和 frozen candidate。

### 9.2 finding identity 与证据

模型不得决定稳定 id，只输出可校验 identity components。

- Worker 计算 `finding_instance_id`：domain separator + inventory digest + concern/rule ref + canonical path + span + evidence hash。
- Server 计算 `issue_key`：repository lineage + concern/rule ref + path + primary symbol（如有）+ normalized failure signature。
- 组件不足时不猜测跨扫描同一性；创建新 issue。
- 同扫描按 instance id 去重；跨扫描只按 exact issue key。路径移动默认新 issue，除非未来有独立 alias 决策。

finding 必须含 title、severity、numeric confidence `[0,1]`、failure scenario、impact、recommendation、false-positive risk、rule refs、validation status、location。location 含 inventory path、有效行范围、`evidence_sha256` 和有界 span；文件/行/hash 不匹配不得进入 main findings。

### 9.3 紧凑 coverage（最多 2,000 文件）

Inventory 按 canonical path UTF-8 byte order 排序并编号 `0..N-1`。coverage 用有序、互斥、无空洞的 inclusive ranges：

```json
{"start": 0, "end": 12, "status": "inspected", "reason": null}
```

- status 仅 `inspected/skipped/unsupported`。
- skipped/unsupported 必须有 closed reason code。
- inspected 必须同时有 Agent coverage claim、在适用 instruction receipt 之后产生的 source full/bounded-read receipt，以及 instruction-set digest。search-only、晚到、缺 receipt 或 digest 不符的 claim 不得自报为 inspected，必须机械降为 closed skipped/unsupported reason 并使适用结果进入 `PARTIAL/FAILED`。`inspected` 只表示该文件有受约束的语义处理和可核验返回范围，不表示每个 byte 都被读取或理解；receipt 必须保留实际范围。
- ranges 必须精确分区 inventory，不能 gap/overlap/OOB/unknown。
- document 绑定 inventory digest、entry count、encoding version、ranges digest。
- 超过 2,000 个纳入文件时 preflight 拒绝，不抽样伪装全仓。

“全仓”只表示 inventory complete + coverage accounted，不表示每行语义理解。

### 9.4 Worker classification 与 debug

classification 由 Worker 代码基于 validated result、receipt ledger、deadline/runtime facts 和 bound cancel command 计算，Agent 无输入权：

- `COMPLETED`：结构/source/coverage 全闭合，无未声明 mandatory gap。
- `PARTIAL`：有可发布结果，但 context/permission/dependency/deadline/instruction/coverage 限制已声明。
- `FAILED`：无有效可发布结果，或 SDK/auth/sandbox/source/Skill/binding 失败。
- `CANCELLED`：Worker candidate 必须绑定 authoritative cancel generation/digest；Server CAS 仍会以自己的 cancel record 决定全局终态。

Server 只投影 CAS 选定的全局终态；`COMPLETED/PARTIAL` 可引用 accepted normalized result，`FAILED/CANCELLED` 只使用 sanitized terminal facts。映射固定且不可由 Worker/Agent 覆盖：

| Server terminal classification | public status | public progress |
|---|---|---|
| `COMPLETED` | `completed` | `terminal` |
| `PARTIAL` | `partial` | `terminal` |
| `FAILED` | `failed` | `terminal` |
| `CANCELLED` | `cancelled` | `terminal` |

非终态只由 Server 按已接受的 lifecycle event 投影为 `preparing/reviewing/validating/publishing`；未知私有状态或 reason 不透传，projection fail closed 并记录 sanitized internal error。

debug bundle 与 audit bundle 分离；不含 source、`run/bundles/**`、auth/token/raw env/其他用户数据。只含 sanitized SDK events/progress/runtime/Skill/source/instruction digests、validator、outbox/receipt metadata 和 Server scoped evidence。所有日志/event/stdout/stderr 有 redaction 与大小上限。

## 10. 30 phase、计费、Admin 与 Web

| 当前职责 | 目标归宿 |
|---|---|
| checkout/workspace/SDK init/auth/inventory/budget | Worker `PREPARING` |
| repo map/risk routing/bundle planning | Skill 方法，不作生产中间协议 |
| packing/fanout/clustering/voting | 删除；数据证明单 turn 不足前不重建 |
| JSON/location/QA | Worker `VALIDATING` |
| intent/disproof | Skill 可选方法；执行仍受 Worker policy/sandbox |
| validation workspace/test running | disposable validation boundary |
| final report JSON | main turn 结构化输出 |
| Markdown render | Server/Web 或小型确定性 renderer |
| hash/upload/submit | Worker `PUBLISHING` |
| cleanup | terminal finally，不是公开 phase |

计费删除旧 phase 列表，改为 durable `semantic_review_started`：

- main turn 获得有效 thread/turn id 并开始语义工作后才发送。
- Server 验证 run/attempt/lease 并幂等持久化；首次事件消费 reservation，此前失败/取消释放。
- 后续 progress id 不参与计费；迟到/恢复从 durable event store 收敛。

Admin 在协调切换时删除 Plan `reviewerConcurrency`、system `reviewWorker.maxBundles/maxReviewerAssignments` 及默认值/schema/API/UI/tests/copy。保留 model、effort、turn timeout、scan deadline、repository limits、output language；不得换名保留 fanout。

Web 已支持动态 steps：切到五级 steps 与 `review-run/v2`，保留 partial/ETA/debug URL 安全规则，删除旧 artifact/phase fallback，不显示私有 binding，并保持移动端/390px gate。

## 11. Append-only 决策计划

| 建议 ID | 主题 | 冻结内容 |
|---|---|---|
| D42 | 治理 evidence gate | Slice-0/replacement 边界、immutable history 与 live forbidden catalog、absence v2 三态/自引用消除 |
| D43 | 专用 reviewer 边界 | full_scan、单 thread/turn、无 fanout/verifier/sub-agent、从头重跑 |
| D44 | Skill/instruction/runtime trust | exact transitive Skill assets、CWD/CODEX_HOME、restricted read/write roots、sanitized env、instrumented receipts、AGENTS、tool/network/credential、TOCTOU |
| D45 | 三层契约/终态 | untrusted Agent payload/trusted Worker result、private wire、normalized/public DTO、Server CAS、cancel binding/ACK/stale |
| D46 | reviewer benchmark/release | D22 专业化 corpus、task-clustered paired statistics、三态、canary/rollback |
| D47 | clean cutover/deletion | exact release build 在 canary 前完成四仓 live legacy 删除、absence v2、same-contract rollback |

实际 ID 由 register 顺序生成。既有决策处理：

| 处理 | 决策 |
|---|---|
| 保留核心 | D1、D23、D24 barrier、D26、D27、D28、D31 |
| 专业化 supersede | D22：保留样本/seed/owner/三态/可比性/canary，替换通用 Agent 指标 |
| 明确 supersede | D6、D8、D10-D12、D14-D20、D25、D29-D30、D32-D33、D41 的冲突部分 |
| 保留安全不变量、替换实现 | D3 的 R0/R1/无 effect；D7 deadline 不延长；D13 cancel precedence；D21 单 current contract |
| 明确替换 | D5 通用 Task version → Server terminal CAS version；D9 Worker TaskResult authority → Server global terminal authority |

每个 supersession 列出旧 digest、保留不变量、撤销义务、normative units、新测试证据；本文不得静默覆盖。

## 12. 分阶段实施计划

### Stage 0A：不改变语义的治理证据修复

1. `S0A.1` 复现并追溯 wrapper 8,762/8,062 漂移。只有能够证明是既有权威生成链/冻结语义内的机械同步遗漏时才可修复；若需要再次 Generate、改变 expected 语义或退休 gate，只产出 evidence packet，留给 Stage 0B 决策。
2. `S0A.2` 追溯 decision-register gate test 未入 baseline。只有现有 baseline 定义已要求收录时才机械同步；否则记录 replacement obligation，不扩大 current baseline。
3. `S0A.3` 建含 Admin 的四仓 deletion inventory，记录 entrypoint/config/table/artifact/test/docs，明确它不是兼容承诺。
4. `S0A.4` 保持默认 absence ratchet 语义不变，补齐当前 self-reference/legacy-present/indeterminate 的可重复证据和 CI 状态。

退出：每个 drift 被分类为“可在现有语义内机械修复”或“需新决策”；允许项已 PASS；四仓 inventory 可重复；gate/生产语义未改。

### Stage 0B：有决策的治理 gate replacement

1. 先 resolve D42 等价 append-only 决策，冻结 Slice-0 保留/退休边界、immutable history storage、live forbidden catalog、absence v2 三态、self-reference 消除和 replacement tests。
2. 以 failing fixtures 证明当前 strict gate 对真正 absent/self-reference 无法给出正确确定性结果，再实现非自引用 verifier。
3. replacement 必须让 live legacy present=exit `1`/`FAIL`、真正 absent=exit `0`/status `absent`、缺证或历史损坏=exit `2`/`INDETERMINATE`，且 immutable history 不作为 live forbidden input。
4. CI 默认 ratchet 继续阻止新增 legacy；exact release artifact 在 Stage D 使用 strict gate。不得通过放宽 exclusion、删除历史或只改 expected 获得 PASS。

退出：D42 resolution/provenance PASS；Slice-0 或有决策的 replacement PASS；absence v2 fixtures/三态 PASS；生产行为未改。

### Stage A：决策与契约冻结

1. 在 D42 governance decision 已完成后，resolve D43-D47 等价决策。
2. 定义 Server canonical wire source/fixtures，但不 Generate/激活。
3. 冻结 result、coverage、identity、status/error/reason registry。
4. 冻结 terminal CAS/cancel binding/ACK/stale 和 untrusted payload/trusted envelope 边界。
5. 冻结 Skill/instruction/tool/read roots/tool env/read gateway/限额/exact-load evidence。
6. 冻结 D46 benchmark documents，包括统计单位、paired estimator/CI 和 missing-run 规则。
7. 更新四仓 `AGENTS.md`，明确 superseded rules，避免 current 指令冲突。

退出：register history/provenance PASS；normative units 引用新 digest；fixtures 完整；新决策只授权 Stage B。

### Stage B：离线 candidate

```text
pullwise_worker/reviewer_runtime/
  types.py
  source_snapshot.py
  instruction_bundle.py
  model_fs_policy.py
  read_gateway.py
  runtime_policy.py
  sdk_session.py
  runner.py
  coverage_codec.py
  result_validator.py
  terminal_candidate.py
pullwise_worker/reviewer_skill/
  SKILL.md
  manifest.json
  review-agent-output-v1.schema.json
  review-result-v1.schema.json
scripts/run_reviewer_candidate.py
```

先写 failing tests：`test_reviewer_skill_binding.py`、`test_reviewer_instruction_surface.py`、`test_reviewer_model_fs_policy.py`、`test_reviewer_read_gateway.py`、`test_reviewer_candidate_runner.py`、`test_reviewer_result_validator.py`、`test_reviewer_coverage_codec.py`、`test_reviewer_runtime_policy.py`。

candidate 只读 fixture/snapshot 并写本地结果；不得调用生产 lease/result、写 production table、切 builder、shadow traffic 或部署。必须证明 exact-load、surface control、restricted model-visible roots、sanitized tool env、instrumented AGENTS/source receipts、source read-only、validation-copy scoped write、tool 无网络/凭据、单 turn、有界 cancel/timeout/close、untrusted payload/trusted result、result/coverage/redaction、2,000-file bounded encoding。无法控制任一 surface 或 receipt authority 即 `NO-GO`。

### Stage B2：paired benchmark

同 source/model/effort/SDK/CLI/machine/budget 交错运行 legacy 与 candidate；顺序预注册；每 task 3 seeds；独立 oracle 解盲；保存 raw samples/exclusions/bindings/可复算 report。只有 D46 全部门 PASS 才进入 C；缺证/不可比/超时/样本不足均按预注册 missing-run 规则计失败或 INDETERMINATE，不得静默排除。

### Stage C：最小生产壳候选，不激活

- 从 legacy 按行为测试提取 slot/supervisor/checkout/source/SDK/deadline/cancel/usage/redaction/outbox，不复制 30-phase。
- Worker 消费 private package，持久化 active marker/exact outbox。
- Server 小模块实现 claim/heartbeat/event/CAS/normalization；新表无生产入口。
- 注入 crash-before/after-freeze、cancel-before/after、ACK loss、replay conflict、stale、source drift、hung close。
- 本地 E2E：Server fixture → claim → candidate → CAS → public projection。

退出：单一终态、真实 SQLite 并发/recovery PASS、public/debug 无 source/secret、生产 builder/routes 未切。

### Stage D：跨仓切换准备

- Server 原子生成 contract，接入但 intake disabled；改计费事件；完成 projection/debug/barrier/legacy reject。
- 形成一个不可拆分的四仓 exact release change set：Worker builder 只指向新 runner，并删除 `ReviewWorkerV1`、非目标 Agent Kernel 和旧 outbox/result；Server 删除旧 phase billing/artifact/route/storage consumer；Web 删除旧 DTO/phase/artifact fallback；Admin 删除 reviewer/bundle/assignment 配置。不得用 flag/fallback 暂存第二路径。
- Worker exact-pin private package；Web/Admin 只 pin public DTO；doctor 校验 exact tuple 和 model-visible filesystem capability evidence。
- 对将部署的 exact commits/build artifacts 运行 strict absence v2、引用图、wheel/install、contract parity、四仓 local/CI。不可把“部署后再删除”当作 Stage D PASS。

退出：exact release build 内只有一个 current contract/runner/Skill，strict absence exit=`0` 且 status=`absent`；四仓 pins/fixtures/CI PASS；operator 完成 stop-intake、same-contract rollback 或 fence/reject 演练；release build 尚未部署/接流量；deletion manifest 全部关闭或有明确 immutable-history 处置。

### Stage E：clean cutover 与 capacity-only canary

1. stop intake。
2. pre-cutover tasks 权威终态/tombstone/delete，或撤权隔离；不得迁移。
3. 部署 Stage D 已通过 strict absence 的四仓 exact builds；部署物内不得含可执行 legacy runner/route/schema consumer。
4. acceptance 事务激活 D24 barrier。
5. 验证旧 lease/event/result/replay fail closed。
6. 新 current contract 开 5% capacity，其余 intake 暂停。
7. ≥24h 且 ≥200 accepted current tasks 后进 25%。
8. ≥72h 且 ≥1,000 tasks 后才考虑 full。

门失败即停止扩容；只可 rollback 到同 contract/schema/storage 的 signed stable，否则 stop-intake/fence/reject。

### Stage F：全量与证据收尾

Stage F 不再修改已 canary 的 runtime/schema/contract，也不在 canary 后才删除 legacy；否则新 build 没有被 canary 覆盖，必须退回 Stage D 并重跑 Stage E：

- 核验 5%/25% 的时窗、样本、质量、安全、成本和 operator stop evidence 后，按签发计划提升到 full capacity。
- 对 exact canary/full build 重跑 strict absence、引用图、四仓 pins 和 CI，确认无 flag/fallback/第二 runner/旧 consumer。
- 归档 release attestation、deletion manifest、operator evidence 和被 live catalog 隔离的 immutable decision history；关闭临时 issue/checklist，但不得删除审计要求保留的不可执行历史。

退出：full capacity 使用与 canary 相同的 exact contract/schema/runtime build；strict absence 仍为 exit=`0`/status=`absent`；引用图无未解释 consumer；四仓 local/CI PASS。

## 13. Benchmark 与发布门（D22 专业化）

### 13.1 corpus 与运行纪律

- 至少 120 个 known-gold tasks。
- 至少 3 个 sealed unknown repository families，每 family 至少 15 tasks。
- 至少 50 个 oracle-positive in-scope findings。
- 覆盖 security、correctness、API/schema、state/concurrency/resource、test-gap。
- 每个适用核心簇对 real defect、bad/incomplete fix、clean counterexample、environment/capability failure、adversarial/prompt injection 各至少 3 tasks。
- 覆盖小/大仓、monorepo、generated/vendor/binary/submodule、nested `AGENTS.md`、依赖缺失、测试不可运行、context/token/deadline 限制。
- 每 task 3 个预注册 seed；所有计划 run 都必须保留。seed 在 task 内等权，但不是三个独立统计样本；不得为追 PASS 追加或替换运行。
- 只允许 policy 预列 infrastructure reason 排除，逐样本报告；解盲后不得改分母、权重、seed、baseline、阈值或 evaluator。

### 13.2 统计单位、缺失运行与置信区间

D46 必须在解盲前冻结可执行统计契约：

- `task_id` 是质量比较的 primary cluster，repository family/known-vs-unknown 是预注册 strata。legacy/candidate 在同一 `(task_id, seed)` 内配对并按预注册顺序交错运行。
- 三个 seed 是 task 内重复测量。task-level 指标先在 task 内聚合，再按 task 等权进入总体；finding、seed、tool event 和 coverage entry 不得被当作相互独立样本扩大有效样本量。
- recall/FDR 等 finding-level 指标保留 finding 权重，但 bootstrap/variance 必须以 task 为 cluster，将一个 task 的全部 seeds/findings 一起重采样。unknown family 同时逐 family 出具结果。
- D46 为每个指标冻结 numerator、denominator、oracle mapping、severity/concern weight、tie/rounding、undefined case 和通过方向。`task success`、location accuracy、actionability、false verified 等术语没有这些定义时不能运行 benchmark。
- 真正以 task/attempt Bernoulli 为冻结单位的绝对门复用 D22/D41 的 exact Wilson 计算：success 等使用预注册的下置信界，error 等使用上置信界；不能只比较 point estimate。location/FDR/recall 等 observations 嵌套于 task 的指标使用 task-cluster bound，禁止对 findings/seeds 直接套独立样本 Wilson。零容忍门还要求 observed count=`0` 和最小样本满足，但不表述为总体风险等于零。
- paired non-inferiority 使用确定性的、按 strata 分层的 task-cluster bootstrap，计算 `candidate - stable` 的单侧 95% 下置信界；重采样次数、RNG/seed derivation、quantile、small-sample 和 p95 wall-time/cost 算法全部进入 signed policy。若另选统计方法，必须在 D46 中命名、证明 paired/cluster handling 并重新冻结 evaluator，不能在结果后切换。
- 系统被测自身产生的 timeout、SDK/auth/sandbox/result failure 是评分结果，不是 infrastructure exclusion。只有 policy 预列、同时影响可比双方且有外部证据的基础设施故障可排除；单侧缺样、无法配对、超出排除上限或样本不足均使适用相对门 `INDETERMINATE`，不得补跑替代。
- 所有适用 absolute/relative/per-family 门取交集，必须全部 PASS；不得用总体平均覆盖失败 strata，也不得解盲后选择 primary metric。

报告必须包含 raw scheduled runs、排除证据、task-level aggregates、cluster/strata membership、bootstrap inputs/seed、每个 confidence bound 和可复算 evaluator output。

### 13.3 质量、安全和相对门

零容忍：

- source mutation = 0。
- unauthorized network/credential/tool/approval = 0。
- stale/double/conflicting terminal publish = 0。
- critical/adversarial false verified = 0。
- `COMPLETED` 中未声明 mandatory coverage gap = 0。

绝对门：

- schema/location/evidence/control/source/Skill binding valid rate = 100%。
- known task success ≥ 70%，unknown ≥ 50%。
- environment/capability classification accuracy ≥ 95%。
- false discovery rate ≤ 20%。
- location accuracy 预注册下限不得低于 98%。
- clean counterexample 不出现系统性规则噪声。

相对 current stable paired baseline：

- high/critical 加权 recall 的 95% non-inferiority 下界不低于预注册 margin，默认 margin = -5 percentage points。
- false discovery 增加 ≤ 2 percentage points。
- location/actionability 下降 ≤ 2 percentage points。
- verified-success p95 wall time/cost 增加均 ≤ 20%。
- unknown family 单独报告，不能被 known 平均值掩盖。

运行诚实性：

- inventory/coverage partition valid rate = 100%。
- indeterminate case 保持 `PARTIAL/FAILED`，不得假成功。
- SDK/auth/quota/timeout/cancel/source mutation/ACK loss 分类与 oracle 一致。
- debug/log 不含 source/secret。
- runtime 不可比时交错重跑 exact stable build；仍不可比则 INDETERMINATE。

### 13.4 evaluator 与职责分离

- benchmark owner 冻结 dataset/oracle。
- CI/eval owner 产生 raw samples/可复算 report，无 promote 权。
- release operator 冻结 policy、核验 report、签发 attestation。
- deployment operator 只能执行已签发 canary/rollback plan。

Evaluator 只允许 exit 0 PASS、exit 1 FAIL、exit 2 INDETERMINATE；只有 exact PASS report 可签发 attestation。

## 14. 测试与验证矩阵

| 领域 | 必测场景 |
|---|---|
| Skill | explicit input、transitive runtime manifest、unlisted/eval asset exposure、digest drift、swap/symlink/truncate、隐式 skill/plugin/MCP/hook |
| Instructions | root/nested precedence、超 32 KiB、总量超限、manifest drift、instruction-before-source ordering、search-only/缺失/伪造 receipt |
| Sandbox | restricted readable/writable roots、host/other-worker/auth/outbox sentinel、source write、validation-copy write、network、sanitized credential path/env、approval |
| SDK | exact tuple capability probe、restricted-read/external-sandbox evidence、missing/wrong thread/turn id/status、notification failure、timeout、archive/close hang |
| Result | Agent 注入 trusted fields/classification、Worker binding injection、malformed schema、unknown enum、traversal、line OOB、evidence mismatch、duplicate |
| Coverage | claim/receipt intersection、0/1/2,000 files、gap/overlap/order/OOB、unknown reason、instruction binding |
| Lifecycle | bound/unbound/stale cancel、deadline/lease loss before/during/after turn、crash before/after freeze |
| Publish | exact replay before stale check、ACK loss、key conflict、stale、cancel vs result CAS concurrency |
| Server | cancel binding、quota event idempotency、normalization、DTO redaction、projection recovery |
| Web/Admin | dynamic five steps、partial/debug/ETA、private-field absence、配置删除、390px |
| Benchmark | task-cluster/strata、3-seed repeated measures、Wilson bounds、paired CI、single-side missing、exclusion cap、per-family failure |

每个 feature/bug slice 必须保存：

1. 实现前最小 failing test 命令与 failure。
2. 同一 test 实现后通过。
3. focused suite。
4. 项目全套 check。
5. 文件行数和超大文件 frozen baseline。
6. 对应 CI run/check。

纯文档/schema 无运行行为时，可用 schema fixture/generator/check 作 red/green，但需说明例外。

当前/目标命令：

Worker：

```powershell
python scripts\agent_first_decision_register.py check --repo-root .
python scripts\agent_first_slice0_baseline.py check --repo-root .
python scripts\verify_agent_first_contract_baseline.py check --workspace-root ..
python scripts\verify_agent_first_legacy_absence.py --workspace-root ..
python scripts\check_output_contracts.py
python -m unittest discover -s tests -p "test_*.py"
```

exact release slice 使用 Stage 0B 决策并实现的非自引用 verifier `--require-absent`；Stage 0B 完成前不得把现有 strict command 当通过证据。

Server：

```powershell
python -m pytest
```

Web/Admin：

```powershell
npm run check
```

发布前还需 Worker wheel/install probe、Server generator/parity、跨仓 exact-pin、真实 SQLite concurrency 和对应 CI。未运行/超时必须报告缺证，不能写“通过”。

## 15. 文件所有权与实施切片

| 切片 | 主仓 | 主要目录/职责 | 独立验收 |
|---|---|---|---|
| GOV-0A | Worker | current decision/slice0/absence evidence、四仓 inventory | 不改变语义的 drift classification/current check |
| GOV-0B | Worker | D42、replacement slice0/absence scripts、contracts、docs | 三态/self-reference/true-absence fixtures |
| SKILL-1 | Worker | `reviewer_skill/**` | package bytes/binding/eval fixtures |
| RUN-1 | Worker | source snapshot/instruction bundle/read gateway | source/instruction/receipt faults |
| RUN-2 | Worker | model filesystem/runtime policy/SDK session/runner | read/write/env/network sandbox、SDK/turn |
| RES-1 | Worker | Agent output/result schemas、coverage codec/result validator | trusted-field injection/schema/location/coverage |
| PUB-1 | Worker | terminal candidate/active marker/outbox | crash/replay |
| BEN-1 | Worker/eval | D46 policy/sample/evaluator/report | clustered paired statistics/三态复算 |
| CON-1 | Server | `contracts/reviewer-worker/v2/**`/generator | schema/parity/exact-pin |
| SRV-1 | Server | terminal CAS/normalization/projection/DB | SQLite concurrency/recovery |
| SRV-2 | Server | routes/quota/debug | route/integration |
| WEB-1 | Web | normalizer/flow/detail/history | check + browser QA |
| ADM-1 | Admin | plan/settings/worker copy | check + mobile |
| CUT-1 | 四仓 | builder/routes/config/deletion/CI/docs | coordinated checklist |

两个切片不得同时修改同一超大 legacy 文件；需触及时先提取 narrow seam，并记录所有权/合并顺序。

## 16. 回滚与 operator runbook

允许回滚仅限已签发 stable build，且同时保持：

- 相同 private/public current contract identity/version/digest。
- 相同 DB schema/storage semantics。
- 相同 D24 barrier 与新 task population。
- 不重新开放旧 route/task/data/Worker。

无此 stable build 时只能 stop-intake、fence/reject 并保留证据。

禁止回滚：

- 回 `ReviewWorkerV1` 或打开 shadow/fallback/legacy flag。
- 恢复旧 protocol/schema/table reader。
- 将 current task 交给旧 Worker。
- 通过 Admin 选择旧 mode。
- 用旧 baseline/QA 覆盖新 release gate。

operator 必须演练：

- stop/reopen intake。
- barrier 前 task 清点与隔离。
- stale lease/event/result rejection。
- ACK loss/outbox recovery。
- same-contract stable rollback。
- 无 stable 时 fence/reject。
- canary auto-stop/证据导出。
- debug bundle 无 source/secret 审计。

演练使用非生产环境和 exact release artifacts，结果进入 attestation evidence。

## 17. 风险、停止条件与估算

出现任一事实即停止或回 Stage A：

1. 近期产品需要写代码、外部 effect、人工 approval 或通用 Agent task。
2. SDK/runtime 无法关闭非允许 instruction/tool/filesystem/env surface，无法形成可信 read receipt，或无法 pin Skill/sandbox/output。
3. paired benchmark 无法通过关键召回、误报、大仓稳定性或成本 non-inferiority。
4. 单 turn 系统性无法诚实 coverage，且小规模拓扑扩展也无数据支持。
5. terminal CAS/clean cutover 无法协调完成，业务又不允许 stop-intake。
6. 从头重跑 SLO 不可接受，且数据证明需 semantic resume。
7. 合规要求每次运行独立 principal/不可抵赖 attestation，离线 release gate 不足。

| 风险 | 缓解 |
|---|---|
| Skill 混入全局 surface | controlled CWD/CODEX_HOME + surface inventory + fail closed |
| 模型读取 auth/control/host state | restricted readable roots 或 external sandbox + sanitized tool env + sentinel faults |
| 大 AGENTS 静默截断 | 自有 manifest/receipt/限额，不依赖默认 merge |
| coverage/read receipt 自报不实 | instrumented gateway ledger + claim/receipt intersection + partial |
| Server/Worker 双终态 | Worker candidate + Server CAS + concurrency/ACK/stale tests |
| 简化降低召回 | paired benchmark + unknown families + 3 seeds + oracle |
| 漏 Admin/计费 | 四仓 inventory + 专属工作包 |
| gate 自引用 | Stage 0A 只取证；D42 授权后由 Stage 0B 分离 history/live catalog |
| 超大文件继续增长 | 小模块 + 400/600 门 + frozen baseline |

单人连续投入估算 8–15 工程周：

- Stage 0A–0B：0.5–1.5 周，不含 D42 授权等待。
- Stage A：1–2 周。
- Stage B：2–3 周。
- corpus/oracle/eval：2–5 周。
- Stage C：1.5–3 周。
- Stage D–F：1.5–3 周。

不含 canary 至少 24h + 72h 的日历时间和外部授权等待。corpus/oracle 通常是关键路径，不能只按 Worker LOC 估算。

## 18. Definition of Done

只有每项有当前直接证据时才完成：

- append-only 决策已授权并逐项 supersede；无 current 指令冲突。
- Slice-0/replacement gate 与非自引用 strict absence gate 可用。
- 生产只有一个 current private contract、一个 runner、一个 Reviewer Skill。
- attempt exact-bind SDK/CLI/runtime/model/effort/全部 runtime Skill assets/Agent-output schema/result schema/source/instruction。
- CWD/CODEX_HOME/restricted read-write roots/tool env/network/credential/approval 全部通过 sentinel/故障注入。
- slot/lease/bound cancel/deadline/source/budget/outbox/Server CAS 通过并发/崩溃测试；Worker 无法自创 `CANCELLED`。
- Agent payload 与 Worker trusted result 分离；binding/classification/identity/location/evidence/coverage/redaction 均由非 Agent 代码生成或验证。
- D46 offline benchmark 所有适用门和 task-clustered paired statistics PASS，无缺证/INDETERMINATE。
- billing 不再依赖旧 phase；三层 contract/public redaction PASS。
- Web 使用五级 progress/public DTO；Admin 旧配置已删除。
- D24 barrier、legacy reject、rollback/stop-intake 演练通过。
- 30-phase、非目标 Agent Kernel、shadow/fallback/compatibility、旧配置/DTO/table/docs consumers 已在 exact release build/canary 前删除。
- exact release build 的 strict absence 确定性 exit=`0`/status=`absent`，且 canary/full 对同一 build 重检仍通过。
- 四仓 local checks 与对应 CI 全绿；CI 不可用不能完成生产 DoD。
- canary 5%/25% 的样本、时窗、阈值 PASS 后才把同一 exact build 提升到 full；canary 后若改 runtime/schema/contract，必须重走 Stage D/E。
- 四仓 `AGENTS.md` 记录 durable current rules，不把 superseded rules 留作 current。

## 19. 立即下一步

1. 开 Stage 0A issue：复现并分类 Slice-0/absence drift；只修复不改变现有语义的机械遗漏。
2. 生成含 Admin、billing、DTO、table、tests、docs 的四仓 deletion inventory。
3. 准备并 resolve D42 governance packet，再以 TDD 实现 Stage 0B 的非自引用 absence/replacement gate。
4. 准备 D43-D47 resolution packet；在 D45 中明确 supersede D5/D9 的 terminal authority，并冻结 cancel binding 与 trusted envelope。
5. 冻结三层契约、Agent-output/result schema、coverage/read gateway、model-visible filesystem、`semantic_review_started` 和 D46 统计政策。
6. 新决策授权后，按 TDD 建 Skill 和无生产 authority candidate。
7. exact-load/read-root/env/instruction-receipt/coverage/terminal faults 未全绿，不开始真实 benchmark。
8. paired benchmark 未 PASS，不开始 Stage C–F、部署、流量、canary 或删除。

最小正确原则：

> **由 Skill 维护审查智慧，由代码维护系统真相，由 Server CAS 维护全局终态。**
