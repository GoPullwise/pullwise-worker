# Codex SDK + Reviewer Skill Worker 重构提案

状态：Proposal / Non-normative / 未授权实现

日期：2026-08-04

范围：`pullwise-worker` 及其与 Pullwise Server/Web 的代码审查任务边界

## 0. 文档地位

本文是架构重构提案，不是 current contract、ADR、实施授权或发布授权。

- 当前 `contracts/agent-first/spec-decision-register.json`、`AGENTS.md` 和既有规范性设计继续有效。
- 本文不修改任何已解决决策，不授权代码、schema、协议、部署、canary、cutover 或 legacy 删除。
- 若接受本提案，必须先新增 append-only 架构决策，明确 supersede 与本提案冲突的既有决策，再更新规范性设计和跨仓契约。
- 生产切换仍必须遵守单一 current contract、无双轨、无 fallback、无 downgrade 的 clean-break 约束。

## 1. 结论

用户提出的方向是合理的，而且对于 Pullwise 当前“单一用途、只读代码审查 Worker”比通用 Agent-First Kernel 更匹配。

但“Worker 只剩 Codex SDK + 一个纯文本 Skill”按字面实现仍然过度简化。Skill 是可维护的语义工作流，不是安全、租约、取消、预算、结构化结果和幂等发布的强制执行器。

建议目标为：

> **Thin deterministic Worker shell + pinned Codex Python SDK + explicitly bound, versioned reviewer Skill + minimal result validator/outbox**

也就是：

- 把变化快、需要人类判断的“如何审查”放进纯文本 Skill；
- 把必须 fail closed、可机械验证的“能做什么、何时停止、什么结果可发布”留在小型 Worker 壳；
- 删除面向未来通用 Agent 平台的 Task Owner、Quality Verifier、Requirement/Observation/Attestation/CAS 等机制，除非真实产品需求和基准数据重新证明它们必要。

这不是把安全交给 prompt，而是重新划分正确的抽象边界：**语义策略属于文本，系统不变量属于代码。**

## 2. 分析边界与当前事实

以下事实来自 2026-08-04 的本地仓库与固定依赖快照。

### 2.1 当前默认运行入口仍是 legacy Worker

`pullwise_worker/main.py` 通过 `build_review_worker(...)` 构建 Worker。默认未开启
`PULLWISE_AGENT_KERNEL_SHADOW_ENABLED` 时，返回的仍是 `ReviewWorkerV1`。

当前 Agent Kernel 接入点只是对 legacy active marker/outbox 的 shadow projection；截至本快照，
`agent_kernel*.py` 不直接调用 `openai_codex`、`thread_start` 或 `turn_start`。实际 Codex SDK
调用仍位于 `review_worker_v1.py`。

因此，当前正处在一个成本较低的架构转向窗口：大量 Agent Kernel 候选代码已经存在，但尚未成为唯一生产审查执行路径。

### 2.2 当前语义编排非常重

当前 `ReviewWorkerV1` 固定注册 30 个 phase，其中包括 repo map、risk routing、bundle planning、
reviewer fanout、clustering/voting、intent test、validator disproof 和 report rendering。

规模快照：

| 范围 | 文件数 | 物理行数 |
|---|---:|---:|
| `review_worker_v1.py` | 1 | 18,531 |
| `agent_kernel*.py` | 83 | 17,665 |
| 上述两部分生产代码合计 | 84 | 36,196 |
| `test_review_worker_v1.py` | 1 | 12,958 |
| `*agent_kernel*.py` 测试 | 51 | 11,356 |
| 上述两部分测试合计 | 52 | 24,314 |

这些数字不是“代码多就一定错误”的证明，但它们说明维护者面对的是一个通用任务平台，而不是一个容易整体理解的代码审查执行器。

### 2.3 当前 SDK 已具备提案所需的基本能力

仓库固定 `openai-codex==0.1.0b3`。本地已安装版本已经提供：

- `SkillInput(name, path)`，wire shape 为 `{type:"skill", name, path}`；
- `Thread.run(..., output_schema=...)`；
- read-only / workspace-write sandbox preset；
- thread、turn、interrupt 和事件流能力。

当前 `review_worker_v1.py` 只是把 turn input 固定为 text item；接入显式 Skill 不要求先发明新的 App Server 客户端。

初始实现应使用 `turn/start`，因为它已经同时承载显式 Skill input 和当前 turn 的
`outputSchema`。`review/start` 可以作为后续离线候选评测，但它主要提供 change-review target，
不能被假定为已经满足 Pullwise 的全仓、Skill binding 和结果契约。

官方文档也明确：Skill 用于可复用工作流和输出要求；需要确定性行为时应保留脚本或程序边界；App Server 推荐同时传 `$skill-name` 文本和 `skill` input item，`outputSchema` 只约束当前 turn 的最终消息。

参考：

- [Codex SDK](https://developers.openai.com/codex/sdk)
- [Codex Skills](https://developers.openai.com/codex/skills)
- [Codex App Server](https://developers.openai.com/codex/app-server)
- [Custom Code Review rules for Codex](https://developers.openai.com/blog/custom-code-review-rules-for-codex)

### 2.4 文本规则对审查质量有真实价值，但不是形式证明

OpenAI 对自定义 repository review rules 的公开评测中，rule-guided variant 找回了 98% 的指定自定义 finding，baseline 为 58.3%。这支持“审查知识应尽量文本化、靠近仓库”的方向。

但该数据只证明 scoped rules 能显著改善特定规则召回，不能证明：

- 一个 Skill 能保证审查了每一行；
- 一个 turn 能稳定覆盖任意规模仓库；
- 自报的 tool/evidence 就是真实执行事实；
- prompt 可以替代 sandbox、lease fence、schema validator 或 terminal outbox。

因此，本提案把它视为可验证假设，而不是跳过离线基准的理由。

## 3. 根因判断

当前方案的主要问题不是“实现水平不够”，而是产品边界和抽象层级错位。

### 3.1 Pullwise 当前需要专用 reviewer，而不是通用 Agent OS

当前目标任务具有很强的收敛性：

- 输入是一份仓库快照和 Server 签发的审查策略；
- 核心动作是读取、搜索、推理、可选地在隔离副本中验证；
- 不应写入原始 source；
- 不应产生外部 provider effect；
- 输出是一份 findings/coverage/report；
- Worker 丢失时可以从头重跑，不需要恢复一个有外部副作用的通用任务。

在这个范围内，通用 Task Owner incarnation、独立 Verifier session、Requirement Ledger、
Observation Manifest、Attestation、Content-addressed Object DAG、跨 lease semantic checkpoint、
Effect Ledger 和复杂 outcome matrix 的边际价值很低。

如果未来目标变成“可写代码、可审批、可调用外部系统、可跨 lease 接管的通用 Agent 平台”，上述机制会重新变得合理。但不应为了尚未进入产品范围的能力，让当前 reviewer 永久支付复杂度税。

### 3.2 当前方案把“证据存在”与“审查质量”混得过近

机械系统可以证明：

- 某个命令确实被 Worker 执行；
- 某个文件确实被读取或列入 inventory；
- 某份结果绑定了哪个 source/skill/runtime digest；
- 某个终态只提交了一次。

机械系统不能证明模型真正理解了每个文件，也不能把多个 schema/attestation 自动转化为更高缺陷召回率。

因此应把“运行事实完整性”和“语义审查质量”分开：

- 每次运行用小型 shell 保证事实完整性；
- Skill 负责审查方法；
- 离线 benchmark/eval 负责证明 Skill 的质量；
- 不用每次运行一个通用 Quality Verifier 来制造看似更强、但尚未由数据证明的保证。

### 3.3 维护成本集中在变化最频繁的层

审查策略会频繁变化：finding 结构、证据标准、严重性判断、框架注意点、误报抑制、报告语言。

当前这些变化横跨 prompt、phase registry、schema、helper、artifact、progress、Server/Web registry 和测试。一个语义变化容易变成跨模块协议变更。

将审查方法收敛到一个 versioned Skill 后，多数策略变化成为文本审查和离线 eval；只有真正改变安全、资源、wire 或发布不变量时才修改 Worker/Server 代码。

## 4. 三种方案对比

| 维度 | 当前 30-phase + Agent Kernel | 纯 SDK + SKILL.md | 推荐的薄壳 + SDK + Skill |
|---|---|---|---|
| 审查策略维护 | 低；策略分散在代码、prompt、schema、phase | 高；主要是文本 | 高；语义集中在文本 |
| 安全强制 | 高，但机制远超当前任务 | 低；prompt 不是权限边界 | 高；最小 sandbox/gateway 保留 |
| 租约/取消/超时 | 完整但状态复杂 | 容易遗漏 | 保留直接、可测试的硬路径 |
| 结果真实性 | 证据结构强，语义价值未必成比例 | 依赖模型自报 | schema + source/location/coverage 机械校验 |
| crash recovery | 通用 checkpoint/ledger | 通常没有 | 无副作用 run 从头重跑；只恢复 terminal outbox |
| full-repo 承诺 | inventory/phase 很强，仍不能证明“理解每一行” | 无法可信承诺 | 只承诺可证明的 inventory closure，并诚实报告限制 |
| 成本/延迟 | 多 turn、多 artifact、多 fanout | 最低 | 低；一个主 turn，最多一个格式修复 turn |
| 可演进性 | 新能力倾向新增 schema/state | 文本灵活但易漂移 | Skill 版本化 + digest + eval gate |
| 当前产品适配 | 偏通用平台，过重 | 过薄 | 最匹配 |

最终选择第三种。它不是折中主义，而是把强制性放在正确位置。

## 5. 推荐目标架构

```text
Pullwise Server
  ├─ job / lease / cancel / budget policy
  └─ idempotent result ingest
          │
          ▼
Thin Review Worker
  ├─ single active slot + heartbeat/cancel/deadline
  ├─ immutable source snapshot + disposable validation workspace
  ├─ pinned SDK/runtime + explicit Skill binding
  ├─ result/coverage/location/source validator
  ├─ logs + usage + redaction
  └─ durable terminal outbox
          │
          ▼
one Codex App Server / one root thread / one main review turn
          │
          ├─ Pullwise reviewer Skill：通用审查方法
          ├─ target repo AGENTS.md：仓库特有不变量
          └─ source/tools：受 sandbox 与 Worker policy 限制
```

### 5.1 组件职责

| 组件 | 应负责 | 不应负责 |
|---|---|---|
| Server | 全局队列、lease、取消、预算 grant、结果幂等接收、公开状态 | 审查 prompt、仓库语义 |
| Thin Worker | 单槽、隔离、deadline、Skill/runtime pin、校验、终态发布 | repo map、risk route、finding 判断 |
| Codex SDK/App Server | thread/turn 生命周期、工具执行、事件/usage、interrupt | Server lease authority、terminal truth |
| Reviewer Skill | 审查步骤、证据标准、严重性/置信度、误报抑制、输出要求 | 提升权限、选择预算、声明终态已提交 |
| target `AGENTS.md` | 当前仓库/目录特有的兼容性、安全和验证规则 | Pullwise Worker 传输和发布协议 |
| Result Validator | JSON/schema、路径/行号、inventory closure、source digest、大小/秘密检查 | 重新做一遍自然语言审查 |

### 5.2 运行约束

- 一个 Worker 最多一个 active job；Server 继续拥有队列，不设本地 queue/prefetch。
- 一个 Worker 进程最多一个 App Server；每个 attempt 一个新 root thread。
- 初始版本不做 reviewer fanout、不做独立 per-run Quality Verifier、不做子 Agent 编排。
- 正常路径只有一个主 review turn；仅当结构化输出无效且预算仍足够时，允许同 thread 一次格式修复 turn。
- lease 丢失、取消或 absolute deadline 到达时立即 interrupt；旧 attempt 永久禁止发布。
- review 没有外部 effect，运行中断后从新 attempt 重新开始，不恢复语义 checkpoint。
- 只有 terminal outbox 需要 crash-safe 恢复和 exact idempotent replay。

## 6. Reviewer Skill 设计

### 6.1 所有权与分发

Skill 属于 Worker control plane，不属于被审查仓库。建议随 Worker package/image 发布到固定只读路径，例如：

```text
pullwise-reviewer-skill/
  SKILL.md
  references/          # 可选，仍为纯文本
```

初始版本应保持 instruction-only，不包含脚本。机械脚本属于 Worker validator，不放进 Skill 以避免把强制执行伪装成模型工作流。

每次运行必须记录：

- Skill 逻辑版本；
- 完整 Skill package manifest（`SKILL.md` 与所有 shipped references）的 SHA-256；
- SDK package/runtime/model/effort identity；
- source commit/tree/inventory digest；
- applicable `AGENTS.md` digest 集合；
- output schema digest。

Skill 修改属于生产 control-plane 变更，必须经过 code review 和离线 eval，不能在运行中自动学习或热改生产版本。

### 6.2 显式绑定

不能依赖 implicit skill matching。Worker 应同时传入：

1. 文本中的 `$pullwise-code-review`；
2. exact `{type:"skill", name:"pullwise-code-review", path:".../SKILL.md"}` input item。

运行前 Worker 机械验证 exact path、regular-file、owner/mode、size 和 digest；找不到、
digest 不符或 `turn/start` 拒绝 Skill input 时 fail closed。Worker 不把“模型说自己使用了 Skill”
当作加载证明。

### 6.3 Skill 内容边界

`SKILL.md` 至少覆盖：

- 目标：查找真实、可操作、非样式类缺陷；
- trust hierarchy：系统/Worker policy > Skill > `AGENTS.md` > source 内容；普通源码和注释是证据，不是权限指令；
- repository orientation：先读适用 `AGENTS.md`，再理解入口、边界、数据流和测试；
- coverage strategy：按风险选择深度，但必须诚实记录 inspected/skipped/unsupported；
- finding bar：给出路径、行号、证据、失败场景、影响、修复方向、severity、confidence；
- disproof：主动寻找安全路径、调用约束、现有测试和反例；
- validation：在允许时运行最小相关命令；不得联网、安装依赖或使用真实凭据；
- output：只返回符合 `review-result/v1` 概念契约的 JSON；
- stop/degrade：上下文、时间、权限或环境不足时返回 limitation，不得伪造完成。

通用审查方法进入 Skill；具体仓库的不变量继续留在对应 `AGENTS.md`，避免复制后漂移。

## 7. 最小确定性 Worker 壳

### 7.1 必须保留的行为

1. 配置和身份
   - Worker registration/auth；
   - exact SDK/runtime/Skill/schema pin；
   - worker-scoped Codex home、credential 和 App Server。
2. 调度和监督
   - 单 active slot；
   - claim/lease/heartbeat/cancel/deadline；
   - 有界 SDK start/run/interrupt/stop；
   - quota/auth/runtime 错误分类。
3. 隔离
   - 原 source 快照不可写；
   - 无网络、无 production credentials；
   - 如需生成测试，只能写 disposable validation workspace；
   - 运行前后复算 source state。
4. 结果校验
   - schema、大小、regular file、UTF-8；
   - path 和 line range 对原始 source 有效；
   - inventory closure 与 skipped/unsupported 原因完整；
   - Skill/runtime/source/output schema digest 绑定；
   - 日志和 debug artifact 不包含 source/secret。
5. 发布
   - immutable terminal payload；
   - durable outbox；
   - idempotency key 和 stale lease fence；
   - cancel/publish race 只能产生一个 authoritative terminal result。

### 7.2 明确删除或不再建设的通用能力

在只读 code-review scope 下，目标路径不应包含：

- 通用 Task Owner / owner incarnation / delegation API；
- per-run 独立 Quality Verifier session 和 verifier slots；
- Requirement Ledger、Observation Manifest、Attestation、Evidence Closure；
- 通用 CAS object DAG 和多 schema availability graph；
- R0/R1/R3/R4 通用 capability/effect machinery；
- semantic checkpoint 和跨 lease owner/session resume；
- caller-independent 的复杂全局 outcome selector；
- 固定 reviewer fanout、voting 和 cluster orchestration；
- 为未来未知 Agent task 预留的状态、表和迁移。

这里删除的是通用抽象，不是安全行为。source snapshot、fence、budget、outbox、redaction 等行为可以从现有实现提取或重写为聚焦模块，但不应保留无实际消费者的通用接口。

## 8. 运行状态与失败语义

### 8.1 最小状态机

| 状态 | 含义 | 允许的下一状态 |
|---|---|---|
| `IDLE` | 无本地 active job | `CLAIMED` |
| `CLAIMED` | 已绑定 job/run/lease/attempt | `PREPARING`, terminal |
| `PREPARING` | source、Skill、SDK、预算已校验 | `REVIEWING`, terminal |
| `REVIEWING` | 唯一主 turn 执行中 | `VALIDATING`, terminal |
| `VALIDATING` | 机械校验 result/source/coverage | `PUBLISHING`, terminal |
| `PUBLISHING` | outbox 已冻结，幂等提交中 | terminal |
| terminal | `COMPLETED`, `PARTIAL`, `FAILED`, `CANCELLED` | 无 |

不为“未来也许会用”的中间状态建表。每个保留状态都必须对应一个可观察的外部边界和至少一个故障测试。

### 8.2 结果分类

- `COMPLETED`：结构有效、source 未变、inventory closure 完整，且没有未声明 coverage gap。
- `PARTIAL`：产生了有效 findings/report，但时间、上下文、权限或环境导致 coverage/validation 不完整。
- `FAILED`：没有可发布的有效报告，或 SDK/auth/sandbox/source-integrity/Skill binding 失败。
- `CANCELLED`：authoritative cancel 赢得竞态，旧 attempt 不得再发布其他终态。

“没有 finding”可以是成功，但“没有证明 coverage closure”不能冒充成功。

### 8.3 中断与恢复

- 主 turn 前丢 lease：不启动 Codex。
- 主 turn 中丢 lease/cancel/deadline：interrupt，kill 不健康 runtime，丢弃未冻结结果。
- Worker crash 且结果尚未冻结：Server 可签发新 attempt，从头运行。
- Worker crash 且 outbox 已冻结：只恢复 exact outbox submit；不得继续审查或改写结果。
- submit ACK 丢失：按 idempotency key 查询/重放同一 payload，不能生成第二结果。

只读任务允许“重算代替恢复”，这是本次简化最重要的前提之一。

## 9. 结果与 coverage 契约

以下是概念边界，不是本文授权的新 wire schema。

`review-result/v1` 至少包含：

- run binding：job/run/lease/attempt；
- control-plane binding：Worker、SDK/runtime、model/effort、Skill、output schema digest；
- source binding：repo identity、commit/tree/inventory、applicable `AGENTS.md` digests；
- findings：稳定 finding id、severity、confidence、path/line、evidence、failure scenario、impact、recommendation、rule refs、validation status；
- coverage：inventory digest、inspected scope、skipped/unsupported scope及原因；
- limitations：context、environment、dependency、permission、timeout 等；
- usage：wall time、turn count、token usage；
- terminal classification。

### 9.1 必须诚实定义“全仓审查”

应区分三种说法：

1. **Inventory complete**：Worker 机械枚举并绑定了全部纳入范围的文件；可以证明。
2. **Coverage accounted**：每个 inventory entry 被分类为 inspected/skipped/unsupported；可以机械闭合，但分类内容仍需审计。
3. **Semantic attention to every line**：模型真正理解了每一行；当前技术无法从最终 JSON 机械证明。

产品若继续展示“全仓扫描”，其定义必须限定为前两项，不得暗示第三项。大仓库超过 context/token/deadline 时必须返回 `PARTIAL` 或明确限制，而不是压缩报告后声称完整。

### 9.2 output schema 的边界

`outputSchema` 能约束最终消息形状，不能证明字段为真。Worker 仍需独立验证：

- path 存在且属于原 inventory；
- line range 有效且 evidence 与对应 source 区域一致到可接受程度；
- finding id 唯一；
- severity/confidence 枚举和范围有效；
- coverage 集合无未知、重复或遗漏路径；
- source state 和 control-plane digest 未漂移。

## 10. 30 个现有 phase 的重构归宿

| 当前 phase 组 | 目标归宿 |
|---|---|
| `prepare_workspace`, `start_codex_app_server`, `initialize_codex_connection`, `check_codex_auth` | 合并为内部 `PREPARING`；保留行为，不保留四个公共语义 phase |
| `bootstrap_helper_scripts` | 删除；确定性工具随 Worker package 发布并在启动时校验 |
| `inventory_repository`, `token_budget` | 保留为 `PREPARING` 的机械前置条件，不作为 Agent 语义阶段 |
| `repo_map`, `risk_routing`, `bundle_planning` | 方法论移入 Skill；不要求持久化中间 JSON |
| `bundle_packing`, `reviewer_fanout`, `clustering_and_voting` | 初始目标删除；只有 benchmark 证明单 turn 不足时才重新设计 |
| `reviewer_json_validation`, `location_validation` | 合并进一次确定性 `VALIDATING` |
| `intent_mining`, `intent_test_planning`, `intent_test_writing`, `intent_test_failure_analysis`, `validator_disproof` | 方法论移入 Skill；不再是固定 pipeline。允许时由同一 turn 在 disposable workspace 有界执行 |
| `intent_test_validation`, `validation_workspace_prepare`, `intent_test_running` | workspace/policy/command trace 由 Worker 机械控制；不是固定语义 phase |
| `final_report_json` | 主 turn 的结构化最终输出 |
| `render_markdown_report` | Server/Web 从结构化结果渲染，或用小型确定性 renderer；不再调用独立 Agent |
| `qa_gate` | 合并为 `VALIDATING` 的 schema/source/coverage/result gate |
| `hash_artifacts`, `upload_artifacts`, `submit_result_envelope` | 合并为 `PUBLISHING`，保留 outbox 和幂等性 |
| `cleanup_active_job` | terminal `finally` 行为，不是审查 phase |

公共进度由 30 个百分比阶段缩减为 `preparing/reviewing/validating/publishing/terminal`，Web 不再消费内部 prompt 拓扑。

## 11. 可复用资产与删除边界

### 11.1 应保留的能力资产

以下资产有独立产品价值，应优先复用其行为或测试，而不是因架构转向全部丢弃：

- 单槽、heartbeat、cancel/deadline 和 stale publish fence；
- worker-scoped SDK/Auth/App Server 隔离；
- source inventory/snapshot 和 source-change 检测；
- disposable validation workspace 的安全边界；
- SDK usage/event/error 分类；
- terminal outbox/idempotent publish；
- debug/log redaction；
- S8 benchmark、raw evidence、evaluator 和 release-gate 方法。

“保留能力”不等于“原文件原样保留”。若现有模块被通用类型污染，应通过行为测试提取到更小的专用模块。

### 11.2 应迁入 Skill 的知识

- 现有 repo mapper、risk router、reviewer、validator、reporter prompt 中仍有效的方法；
- `.codereview/prompts` 中经评测证明有效的审查提示；
- finding 证据标准、误报反证、严重性和 confidence 规则；
- framework/language 通用的 review checklist。

迁移后只能有一个生产语义来源。旧 prompt 不得继续被生产路径读取；有价值的旧案例转为 eval fixtures。

### 11.3 clean cutover 后应删除

- legacy 30-phase dispatch、phase-specific prompt/schema/helper 和中间 artifact contract；
- 无生产消费者的通用 Agent Kernel tables/migrations/types/gates/ledgers；
- Server/Web 对 30 phase、旧 artifact kinds 和旧进度计数器的依赖；
- shadow、compatibility adapter、fallback 和第二 runner；
- 重复表达同一审查规则的 prompt 文档。

删除前必须跨 Worker/Server/Web 生成可审计引用清单；不能只按文件名前缀批量删除。

## 12. 迁移方案

### Stage A：先改决策，不改生产

1. 新增 append-only 架构决策，明确代码审查是 read-only specialized task，而非通用 Agent platform。
2. 列出并 supersede 所有要求 Task Owner、Verifier、ledgers、固定 pipeline 和旧 current-contract 形态的冲突决策。
3. 冻结本提案的 scope、质量门、wire ownership 和 clean-cutover 规则。
4. 在决策前暂停继续扩展通用 Agent Kernel，避免增加 sunk cost。

### Stage B：离线 candidate spike

只实现无生产 authority 的最小候选：

- exact pinned SDK；
- 一个显式 Reviewer Skill；
- 一个主 thread/turn；
- output schema；
- inventory/location/source validator；
- 本地结果文件。

使用相同 source snapshot 对当前 pipeline 和候选方案做 paired benchmark。该比较是离线评测，不是生产双轨或 shadow authority。

当前固定 SDK 已暴露 SkillInput/output schema，因此 spike 首先验证行为和质量，不先升级 SDK。

### Stage C：构建最小生产壳

在 candidate 通过质量门后，再接入：

- claim/heartbeat/cancel/deadline；
- isolation/validation workspace；
- event/usage/log；
- durable outbox 和 idempotent result ingest。

按失败模式测试，不按 30 phase 重建旧架构。

### Stage D：跨仓 clean cutover

1. Server/Worker/Web 对同一个最小 current contract exact-pin。
2. Web 改用粗粒度进度和统一 review result。
3. 离线 attestation、rollback/stop-intake 演练通过。
4. 协调打开唯一新 intake；不把剩余流量送回旧 Worker。

### Stage E：删除旧路径

cutover slice 内或其紧随的强制 deletion slice 删除 legacy/Agent-Kernel 非目标路径，运行 absence gate。不能长期保留“备用实现”。

回滚只能回到实现同一最小 current contract 的 exact stable build；若不存在，只能 stop-intake/fence/reject，不能回旧协议。

## 13. 评测与接受门

架构简化必须由质量和运行数据证明，不能只用 LOC 证明。

### 13.1 benchmark corpus

至少覆盖：

- 历史真实缺陷及修复前快照；
- clean counterexamples 和合法例外；
- security、correctness、API/schema、state/concurrency/resource、test-gap；
- 小仓库、大仓库、monorepo、generated/vendor/binary/submodule 边界；
- nested `AGENTS.md` 和恶意源码 prompt injection；
- 缺依赖、测试不可运行、超 context/token/deadline；
- SDK timeout/crash、quota/auth error、cancel、lease loss、source mutation、publish ACK loss。

### 13.2 预注册指标

质量：

- oracle-positive finding recall，按 severity/concern 分层；
- false discovery / false verified；
- location accuracy、actionability、duplicate rate；
- clean counterexample restraint；
- known 与 unknown repository family 分开报告。

运行：

- schema-valid result rate；
- inventory closure honesty；
- source-integrity violation；
- stale/double terminal publish；
- p50/p95 wall time、token、cost；
- platform failure rate。

维护：

- 一个审查规则变更触及的生产文件和契约面；
- 生产语义来源数量；
- retained state/table/schema 是否各有当前消费者和故障测试。

### 13.3 推荐 go/no-go 门

正式决策应在揭示候选结果前冻结门槛。建议至少满足：

- source mutation、stale publish、double terminal、未声明 mandatory coverage gap 均为 0；
- schema/location/control-plane binding 有效率 100%；
- high/critical 加权召回相对 current stable 的 95% 置信下界不低于预注册 non-inferiority margin；建议 margin 不超过 5 percentage points；
- false discovery 相对 current 增加不超过 2 percentage points；
- clean counterexample 不因宽泛 Skill 规则产生系统性噪声；
- p95 wall time/cost 不超过预注册上限；
- 所有 indeterminate case 保持 `PARTIAL/FAILED`，没有假成功。

现有 S8 eval/release discipline 应尽量复用；应删除的是运行时通用内核，不是质量评测严谨性。

## 14. 会推翻本提案的证据

出现以下任一事实，应暂停或拒绝本次重构：

1. 已确认的近期产品需求要求 Worker 写代码、调用外部 provider、处理不可重放 effect 或执行人类审批。
2. paired benchmark 显示单 Skill 方案在关键缺陷召回、大仓库稳定性或误报上无法达到 non-inferiority 门。
3. 合规要求每次运行必须由独立 principal/session 做不可抵赖 attestation，而离线 eval 不足以满足。
4. 从头重跑的成本/SLO 不可接受，且跨 lease semantic resume 被数据证明为必要。
5. SDK/runtime 无法稳定 pin Skill bytes、sandbox 和 output contract，或 beta 兼容性测试不通过。
6. Server/Web 的最小契约无法在一次协调 clean cutover 中完成，而业务又不允许 stop-intake。

若这些事实不存在，继续建设通用 Agent Kernel 的收益目前没有足够证据。

## 15. 开放决策及推荐默认值

| 决策 | 推荐默认值 |
|---|---|
| “全仓审查”的产品定义 | inventory complete + coverage accounted；不承诺每行语义理解 |
| Skill 调用 | exact path + digest + explicit SkillInput；禁止只靠 implicit match |
| 每次 review 的 Agent 拓扑 | 1 root thread、1 main turn、最多 1 次格式修复；无 fanout |
| source 权限 | 原 source 只读；需要写入的验证只在 disposable copy |
| network/credential | 无网络、无 production credential、approval auto-deny |
| crash recovery | 语义从头重跑；只恢复 frozen terminal outbox |
| repo-specific rules | 放在 target repo/nested `AGENTS.md`，不复制进 Skill |
| 通用 review 方法 | 放在 Worker-owned、versioned、instruction-only Skill |
| progress | 5 个粗粒度状态，不暴露 prompt/phase 拓扑 |
| quality assurance | 离线 paired eval + 每次运行机械 validator；默认无 per-run Verifier Agent |
| rollback | 同 contract exact stable build；否则 stop-intake |

## 16. Definition of Done（提案级）

只有满足以下条件，才可称重构完成：

- 有新的 append-only 决策和规范性设计明确授权并 supersede 冲突决策；
- 生产路径只有一个 Worker current contract 和一个 reviewer Skill 语义来源；
- 每个 attempt 显式绑定 exact SDK/runtime/model/Skill/schema/source/AGENTS digest；
- 单槽、lease、cancel、deadline、sandbox、source integrity、budget 和 outbox 不变量全部通过故障注入；
- 结果 schema、path/line、coverage 和 terminal binding 全部由非 Agent 代码校验；
- benchmark/eval 满足预注册质量、运行和成本门；
- Server/Web 已切到粗粒度进度与最小 result contract；
- legacy 30-phase、非目标 Agent Kernel、shadow/fallback/compatibility 路径及跨端消费者已删除并通过 absence gate；
- operator 已完成 exact stable rollback 或 stop-intake/fence/reject 演练；
- 文档明确说明“全仓”的可证明边界和 `PARTIAL` 条件。

## 17. 最终建议

应接受这次架构方向调整，并立即把后续讨论聚焦到推荐的薄壳方案，而不是继续把通用 Agent Kernel 做完后再简化。

最小正确原则是：

> **用 Skill 维护审查智慧，用代码维护系统真相。**

下一步不是直接删代码，而是先用同一批仓库快照做离线 candidate spike 和 paired benchmark。只有质量不劣且运行门通过后，再通过新的 append-only 决策启动 clean-break 重构。
