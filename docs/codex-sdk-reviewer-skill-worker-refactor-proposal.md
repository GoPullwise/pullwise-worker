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

### 0.1 当前执行状态与状态词

决策注册表的 `ready` 只表示“当前注册表没有未解决的 active decision”，不表示本文提出的 D42–D47 已获授权，也不表示可以开始 candidate、benchmark 或生产实现。本文和后续 issue 只使用以下状态词：

| 状态 | 含义 |
|---|---|
| `NOT_AUTHORIZED` | 前置 append-only 决策或阶段授权缺失；禁止写入该阶段的实现代码或运行该阶段的真实操作 |
| `READY` | 前置授权与输入证据齐全，可以开始，但尚无完成声明 |
| `IN_PROGRESS` | 已开始且保留 red/green/命令/产物证据 |
| `PASS` | 所有适用验收项有当前、直接、可复算证据 |
| `FAIL` | 已证明至少一个适用门不满足 |
| `INDETERMINATE` | 缺证、证据损坏、样本不足、不可比或 gate 自身无法给出确定结论 |

截至本文版本，唯一可以进入 `READY` 的工作包是 `GOV-0A`；`GOV-0B` 及后续工作包全部是 `NOT_AUTHORIZED`。`FAIL` 与 `INDETERMINATE` 都不能被当作软通过；二者也不能通过修改 expected、排除项或删除历史证据转成 `PASS`。

Decision resolution 只设置允许到达的最大边界，不自动推进阶段。每次从 B→B2→C→D→E→F 还必须有 signed stage-advance record，引用前一阶段 exact PASS evidence、目标 release id/digest、允许的 work packages 和禁止项；前置 evidence stale 或 release digest 改变时授权自动失效。

### 0.2 规范性产物与证据包

本文是实施总规格，不取代决策注册表和生成契约。实施时按下列优先级解析冲突：

1. append-only decision register 中适用且未被 supersede 的 resolution；
2. Server-owned canonical schemas/registries 及其 signed manifest；
3. 由 canonical source 原子生成并 exact-pin 的 Worker/Web/Admin artifacts；
4. 本文、阶段 runbook、issue 与测试计划；
5. 非规范性说明和历史文档。

每个阶段使用同一个 `release_id`，输出不可覆盖的 `reviewer-refactor-evidence/<release_id>/<stage>/<work-package>/` 证据包。证据包至少包含：`inputs.json`、`commands.jsonl`、`tests.json`、`artifacts.json`、`environment.json`、`decision-bindings.json`、`result.json` 和 SHA-256 `manifest.json`。`result.json` 只能是上述六态之一，并逐项引用直接证据；重跑创建新 evidence generation，禁止原地改写旧失败。CI 保存同一 manifest 的 artifact，release attestation 只引用已签名的 exact generation。

所有尚不存在的目标命令、schema、registry 和 evidence writer 都是相应切片的交付物，不是当前已经可用的事实。文档中的“应运行”不能替代真实命令输出。

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
- Slice-0 当前失败：当前 generated wrapper 为 8,762 行、SHA-256 `9404c18b39afdb0ee6bd9d15fdbb3b24d9b85f1972a597a5919a868afe480697`，与 D41 记录一致；Slice-0 baseline 仍固定 D39 的 8,062 行、SHA-256 `bd099dd825c2b2340061b67500bc02f1bb4fee0a1ce7ff44138b36b8821a59fd` 及旧 producer。Stage 0A 必须证明二者的完整 provenance，再决定是既有生成链机械漏同步还是需要 D42 replacement；本文不预判结论。
- `tests/test_agent_first_decision_register_gate.py` 当前 405 行且未入 Slice-0 baseline。它不能以“新增 grandfathered 超限文件”的方式直接纳入；若现有语义确实要求收录，先按单一职责拆分/缩减到不超过 400 行并证明测试语义不变，否则记录为 Stage 0B replacement obligation。
- legacy contract baseline 为 `compatible`，14 组固定 probe 全部通过。
- 默认 absence ratchet 为 `ratchet_clean=true`、`legacy_absent=false`。
- 当前 `--require-absent` 既发现 live legacy，又因 `worker.004-frozen-contract-baseline` 自引用而 `indeterminate`；当前报告有 108 个 failure，首个确定阻塞原因为 `strict_catalog_self_reference`，不能证明 clean break。
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
  ├─ immutable source + instruction manifest（模型文件系统外）
  ├─ source/instruction/validation gateway + append-only receipt ledger
  ├─ scratch-only model filesystem + sanitized tool environment
  ├─ controlled CODEX_HOME/CWD + pinned Skill/SDK/CLI
  ├─ one thread/turn + optional format repair
  ├─ source/location/coverage/result validator
  └─ immutable terminal candidate outbox
          │ Worker-scoped provider channel
          ▼
Codex SDK / App Server
  ├─ explicit Reviewer Skill
  ├─ typed source/instruction/search tools
  ├─ bounded validation tool
  └─ optional generic shell（看不到 source/validation mount）
```

三条通道：

| 通道 | 允许 | 凭据 | Agent 可见 |
|---|---|---|---|
| SDK/provider | 仅 OpenAI/Codex 出站 | Worker 专属 auth | 否；工具读不到 auth/env |
| Worker↔Server/clone | lease/heartbeat/cancel/artifact/result/clone | Worker token/短期 token | 否 |
| Agent tools | instrumented source/instruction read/search、bounded validation profile | 无；使用 Worker 预建立的本地 capability channel | 是；只能看到工具返回值和 scratch，network disabled/deny-all |

“无网络、无生产凭据”专指 Agent tools，不是让 SDK 或 Worker control transport 失效。

### 5.1 模型可见文件系统与进程边界

仅设置 Worker-owned `CODEX_HOME`、turn `cwd` 或 `readOnly/workspaceWrite` preset 不能证明读取隔离。当前 [Codex App Server](https://developers.openai.com/codex/app-server) wire 支持显式 `readOnly.access` / `workspaceWrite.readOnlyAccess`，但固定的 `openai-codex==0.1.0b3` 公开 Python Sandbox 封装只提供 preset，不能表达 restricted readable roots。因此 exact tuple 必须通过运行时 capability probe，不能从 App Server 文档、版本号或 prompt 推断 Python SDK 路径已经安全。

每个 turn 的 model-visible filesystem 只能包含：

- Worker-owned model-turn scratch：读写、容量有界、attempt 后销毁，不含任何 source/instruction/control bytes。
- staged Reviewer Skill runtime assets：只读、manifest exact-bound；eval fixtures 和 Worker-only schema/control assets 不挂载。
- 受审计的 tool bridge/最小 binary/library/runtime：只读。bridge 本身没有 source 路径或生产凭据，只能使用 Worker 预建立、attempt-scoped 的本地 capability channel。

immutable source snapshot、target `AGENTS.md` bytes/instruction manifest、disposable validation copy、Worker source/control/runtime state、terminal outbox、logs、clone/control token、host/user home、其他 worker root、未列入 manifest 的 temp/cache，以及 `CODEX_HOME` auth/config/session **一律不进入模型可见文件系统**。Agent 只能通过 5.4 的工具结果看到获准 source/instruction bytes；validation copy 始终由 Worker-owned validation service 持有，模型和通用 shell 都不能直接遍历或修改它。

App Server/provider supervisor 可以使用 Worker-scoped auth，但 tool child process 只收到固定的 sanitized environment；不得继承 API key、token、proxy credential、`HOME`/`CODEX_HOME` credential path 或 Server transport credential。若运行时不能关闭通用 shell，shell 也只能看到 scratch 和最小 runtime，不能成为 source/read-receipt/validation 的旁路。

实现顺序：

1. 对当前 exact SDK/CLI/App Server tuple 运行公开 API capability probe，验证 SkillInput、strict output schema、精确工具配置、隐式 surface 枚举/关闭、interrupt/close/event、sandbox policy 实际 wire 和 tool child 隔离。
2. 优先使用能通过全部探针的受支持 Python SDK/API 路径。若 `0.1.0b3` 不能表达所需能力，只有在 D44 明确选择并 exact-pin 的兼容升级后才能采用新 SDK；不能把当前 App Server wire 的能力当作当前 Python SDK 已暴露的能力。
3. 允许保留 `0.1.0b3` 的唯一例外，是经验证的外部 Linux sandbox/mount namespace 已使 model/tool process 只能看到 scratch/tool bridge，并由 Worker 外部服务持有 source/validation。该隔离仍需 exact-pin image/kernel/policy evidence。
4. 两条受支持路径都不能满足时，Stage B `NO-GO`。不得手写 App Server JSON-RPC、猴子补丁 generated SDK types、依赖未公开字段，也不得把普通 `read-only` preset、文件 ACL 愿望或 prompt 当作证据。

Stage B 使用模型已知名称的 sentinel files/env/endpoints 覆盖 host home、其他 worker root、auth store、outbox、source、instruction、validation copy、source write、scratch write、网络和 approval。只有允许操作成功、所有拒绝路径稳定 fail closed，才可签发 capability evidence。

### 5.2 责任边界

| 组件 | 必须负责 | 不得负责 |
|---|---|---|
| Server | acceptance/lease/cancel/deadline/budget/CAS/quota/public projection | prompt/Skill/语义判断 |
| Worker | 读写根与 tool env 隔离、snapshot/instruction/SDK/deadline/validation/outbox | 全局终态/issue 生命周期/公共 DTO |
| SDK | thread/turn/tool/event/usage/interrupt | lease/terminal truth |
| Skill | 审查方法和输出纪律 | 权限/预算/终态/网络/凭据 |
| Validator | schema/binding/location/hash/coverage/redaction | 自然语言复审 |
| Web/Admin | 公共 DTO 和当前配置 | lease/attempt/Skill/source 私有事实 |

### 5.3 Runtime tuple 与 capability contract

每次 attempt 绑定一个 `evaluation_runtime_digest`。其 canonical document 至少包含 Worker wheel digest、Python 版本、`openai-codex` distribution/version/wheel digest、CLI/App Server executable digest、initialize metadata/protocol version、model/provider/effort/service tier、thread/turn config digest、sandbox/image/kernel/policy digest、Reviewer Skill manifest digest、Agent-output schema digest、tool manifest digest 和 redaction policy digest。字段缺失或实际值与预期不一致时，不启动 main turn。

`runtime-capability-report/v1` 必须由真实进程探针生成，不接受静态代码推断，并逐项给出 `PASS/FAIL/INDETERMINATE`：

- 显式 Skill 只加载 manifest 中的 exact bytes；
- strict output schema 被真实 turn 接受并拒绝附加/未知字段；
- 实际 tool/MCP/plugin/hook/approval surface 与 tool manifest 完全一致；
- source/instruction/validation 只能经 5.4 gateway，generic shell 无旁路；
- tool child 无 provider/control credential、无任意网络，scratch 是唯一可写根；
- provider 和 Worker control 通道在 tool sandbox 外正常工作；
- interrupt、deadline、event/usage、archive/close 有界且错误可分类；
- capability report 自身绑定 executable/config/image/kernel 和 probe fixture digests。

任何 SDK/CLI/App Server/model/sandbox mechanism 变化都会产生新的 runtime digest、使旧 capability report 失效，并触发 D46 的运行时可比性规则。

### 5.4 不可绕过的 read/validation gateway

Gateway 是 Worker-owned、attempt-scoped 的本地服务，运行在 model/tool sandbox 外。Codex 侧只配置受支持的 typed local tool bridge；bridge 通过预建立的 OS capability/IPC handle 调用 gateway，不能接受模型提供的 token、socket path 或 host absolute path。它不是第二 runner，也不是手写 App Server transport。

冻结的 `reviewer-tool-protocol/v1` 只提供以下工具：

| 工具 | 主要输入 | 主要输出 | 权威事实 |
|---|---|---|---|
| `source_inventory` | `inventory_id/cursor/limit` | canonical path、kind、size、digest、language、scope id | inventory 枚举与分页，不代表已审查 |
| `instruction_read` | inventory id、scope id、expected instruction-set digest | gateway 解析出的 ordered canonical paths、precedence、各文件 digest/有界完整 bytes | 对应 scope 的完整 instruction-set receipts |
| `source_read` | inventory id、canonical path、expected file digest、byte/line range | 实际返回范围、encoding、bytes、content digest | full/bounded read receipt |
| `source_search` | inventory id、literal/受限 regex、path filter、limit | 有界 match/span 列表、truncation | 只证明返回 match，不能生成 inspected |
| `validation_run` | validation profile id、inventory/source digest、schema-validated args、timeout | exit、bounded stdout/stderr、mutation summary、output digests | 在一次性 copy 中执行的机械验证事实 |

所有模型提供的 source path/filter 只能引用 inventory 中的 canonical relative path；拒绝 absolute path、`..`、alternate separator、case ambiguity、symlink/reparse/submodule escape、unknown id 和 stale digest。instruction paths 由 gateway 从 frozen manifest/scope 解析，模型不能自选或省略适用文件。`instruction_read` 完整成功前，gateway 自身拒绝该 scope 的首个 `source_read/source_search/validation_run`，不能只靠事后 validator 检查顺序。`validation_run` 只能选择 manifest 中冻结的 command/profile 和参数 schema；service 创建并销毁 disposable copy，执行时网络 deny-all、env allowlist、CPU/RSS/output/deadline 有界，绝不接受任意 shell 字符串。

每个响应包含 `protocol_version/attempt_id/turn_id/tool_call_id/sequence/status/receipt_id/receipt_digest`。Gateway 在模型不可写的 append-only ledger 中按单调 sequence 记录 request digest、resolved object identity、实际 byte/line range、returned-bytes digest、instruction-set digest、limit/truncation、started/finished time、exit/error code，并形成 previous-digest hash chain。Worker freeze result 前验证链、tool-call correlation 和 ledger fsync 边界；模型伪造同形 JSON 不会产生 ledger receipt。

工具错误使用 closed registry：`INVALID_REQUEST`、`STALE_BINDING`、`PATH_REJECTED`、`INSTRUCTION_REQUIRED`、`LIMIT_EXCEEDED`、`UNSUPPORTED_ENCODING`、`VALIDATION_DENIED`、`TIMEOUT`、`SERVICE_UNAVAILABLE`、`INTERNAL_INDETERMINATE`。未知错误、bridge/gateway crash、sequence gap、hash-chain mismatch 或 output truncation 未被结果声明时 fail closed。具体分页、单次/累计 bytes、search matches、validation profiles、CPU/RSS/output 限额在 Stage A 冻结并进入 benchmark binding。

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
- boot input 只内联有界 policy 与 opaque instruction-manifest id/digest，不暴露 host path；Skill 在审查 scope 前通过 `instruction_read` 读取适用 bytes。
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

### 7.4 Canonical package、字段矩阵与生成规则

Server canonical source 固定为：

```text
pullwise-server/contracts/reviewer-worker/v2/
  manifest.json
  registry.json
  schemas/*.schema.json
  fixtures/valid/*.json
  fixtures/invalid/*.json
  generator.lock.json
pullwise-server/contracts/public/review-run/v2/
  manifest.json
  registry.json
  schemas/*.schema.json
  fixtures/{valid,invalid}/*.json
```

所有 object schema 使用 `additionalProperties: false`；所有 integer 有范围；时间是 UTC RFC 3339；digest 为 lowercase SHA-256；ID 有长度/字符集限制；path 只允许 canonical POSIX relative path。每个 private message 有共同 envelope：`schema_id`、`schema_version`、`contract_digest`、`message_id`、`sent_at`。业务判断不得依赖 `sent_at`，而依赖 Server version/epoch/sequence。

最小字段矩阵如下；Stage A 可以增加有明确消费者和测试的字段，但不能删除、改义或把 trusted 字段交给 Agent：

| Schema | 必需字段（除共同 envelope） | 关键约束 |
|---|---|---|
| `worker-registration/v2` | worker/session id、build/runtime/capability/tool-manifest digests、slot capacity | capacity 必须为 1；Server 只接受 allowlist exact tuple |
| `review-task-claim/v2` | job/run/attempt id、lease id/epoch/version/expiry、source descriptor/digest、instruction/inventory digest、deadline、budget、cancel generation/digest | 全部由 Server/Worker preflight 注入；Agent 不可见 |
| `review-worker-heartbeat/v2` | claim identity、worker state、event sequence、observed lease/cancel generation、remaining deadline | sequence 单调；不能延长 lease/deadline |
| `review-run-event/v2` | claim identity、event id/sequence、event type、event payload digest | closed event type；`semantic_review_started` 仅一次且幂等 |
| `review-artifact-descriptor/v2` | artifact id/kind/media type/size/digest、storage generation、redaction class | descriptor 不含本地绝对路径或 raw source |
| `review-cancel-command/v2` | job/run/attempt、cancel generation/digest/reason/issued version | generation 单调；由 Server 签发 |
| `review-terminal-candidate/v2` | claim identity、expected nonterminal version、classification、result/artifact-set/outbox digests、idempotency key、execution/source bindings、可选 exact cancel binding | freeze 后 bytes 不变；`CANCELLED` 必须有 cancel binding |
| `review-terminal-receipt/v2` | idempotency key/candidate digest、accepted/rejected、global classification/version、accepted result ref、closed receipt code | 同 key/digest exact replay 返回同一 receipt |
| `review-error/v2` | scope、closed code、retry disposition、sanitized public code、evidence ref | message 仅诊断，不控制状态 |
| `normalized-review-result/v1` | run/execution/source binding、findings、coverage、limitations、usage、artifacts、candidate classification、normalization digest | 只由 Worker validator + Server normalization 产生 |
| `review-run/v2` | public id/status/progress、summary、findings、coverage、limitations、artifact metadata/safe URL、public error、created/updated/terminal times、etag/version | 私有字段 schema-level absent；public status 只来自 terminal/projection mapping |

`manifest.json` 对 registry、全部 schema/fixture 和 generator version 计算 path/size/SHA-256；`contract_digest` 是 canonical manifest content 的 domain-separated digest。`registry.json` 是所有 enum/reason/event/status 的唯一来源。Server generator 先写临时目录、校验 valid/invalid fixtures、生成语言 artifacts、再进行 no-clobber 原子发布；失败时四仓均不得留下部分更新。

生成目标固定为 Worker 的 private Python wrapper、Server validator/types，以及 Web/Admin 的 public TypeScript types/validators。Worker 不复制 public DTO，Web/Admin 不依赖 private package。每个仓的 `check` 在临时目录重生成并 byte-compare；任何手改 generated file、manifest mismatch、unknown consumer 或跨仓 digest 不一致均失败。一次 release change set 的 `release-manifest.json` exact-pin 四仓 commit、package/build digest 和 private/public contract digest。

### 7.5 Closed registries 与信任来源

`registry.json` 至少冻结：

- Worker state：`IDLE/CLAIMED/PREPARING/REVIEWING/VALIDATING/PUBLISHING/LOCAL_TERMINAL`；
- terminal classification：`COMPLETED/PARTIAL/FAILED/CANCELLED`；
- public status/progress：第 9.4 节固定映射和五级 progress；
- run events、artifact kinds、coverage status/reason、validation status；
- private error/receipt/cancel/deadline/sandbox/source/Skill/SDK/outbox/CAS reason；
- public error allowlist 和 private→public sanitization mapping；
- retry disposition：`NEVER/NEW_ATTEMPT/OPERATOR`，不得由 Agent 或自由文本决定。

初始 registry 必须覆盖本文出现的全部 code；未知值在所有边界 fail closed。free-text `message/detail` 必须有长度、字符和 redaction 上限，只作人类诊断，永不驱动 retry、billing、terminal、coverage 或 public projection。registry 变更等同 contract 变更，必须新 version/digest、fixtures、consumer parity 和适用 benchmark/cutover。

### 7.6 新存储模型与事务边界

Clean break 使用全新的 v2 tables/keys，不复用旧 row shape、trigger 或 reader：

| Table | 核心 key/内容 |
|---|---|
| `review_runs_v2` | PK run id；job/repository、current contract/source、state/version、current attempt、deadline/budget、cancel head、terminal head、projection state |
| `review_attempts_v2` | PK attempt id；run/worker/lease epoch、runtime/Skill/schema/source/instruction bindings、state/event sequence、started/frozen/finished time |
| `review_run_events_v2` | PK event id；unique(attempt, sequence)；event type/payload digest；billing marker |
| `review_cancel_commands_v2` | unique(run, generation)；command digest/reason/issued version |
| `review_terminal_receipts_v2` | unique(idempotency key)；candidate digest、CAS input/output version、global classification、accepted evidence refs |
| `review_results_v2` | immutable result/normalization digest、private payload ref、public projection input ref |
| `review_artifacts_v2` | immutable descriptor/storage generation/digest/redaction class |
| `review_projection_outbox_v2` | unique(run, terminal version, projection kind)；payload digest/delivery state |

四个事务是规范边界：

1. **claim**：只在 run nonterminal、无 active attempt、lease policy 满足时创建 attempt 并推进 run version；
2. **cancel**：递增 generation、写 command、推进 run version；不直接接受 Worker terminal；
3. **event/billing**：按 event id + attempt sequence 幂等落库；只有首次有效 `semantic_review_started` 消费 reservation；
4. **terminal CAS**：按 8.3 的固定顺序，在一个事务写 terminal receipt、run terminal head、accepted evidence/result、quota transition 和 projection outbox。

外键、unique/check constraints 和真实 SQLite 并发测试必须证明没有双 active attempt、sequence 回退、同 key 异 digest、stale side effect 或 terminal 改写。projection worker 只消费 outbox，可重复执行但不能更新 terminal truth。

Stage D 必须选择并记录“全新 v2 schema + pre-cutover task 清空/隔离”的 exact migration；旧表可因合规被保留为不可执行只读 archive，但新进程不得拥有 reader/import/trigger/view，也不得建立 old→v2 转换。没有已演练的 destructive-data 处置授权时只 stop-intake/fence，不擅自删除历史数据。

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

以下只是待确认 decision packets，不是 resolution。建议 ID 以实际 append 顺序为准；在 register 记录 option-anchored confirmation 和 provenance 前，状态一律为 `PENDING_EXPLICIT_RESOLUTION/NOT_AUTHORIZED`。

| Packet | 目标选项 A | 停止选项 B | A 只授权到 |
|---|---|---|---|
| RR-GOV（建议 D42） | 分离 immutable history 与 live forbidden catalog；用非自引用 Slice-0/absence v2 replacement，固定 exit 0/1/2 三态并机械迁移旧义务 | 保留当前 gate；本重构停在 GOV-0A | Stage 0B |
| RR-SCOPE（建议 D43） | 唯一任务 `repo_review.full_scan`；单 root thread/main turn、最多一次 format repair、无 fanout/verifier/sub-agent、失败从头重跑 | 不接受该专用边界；停止 candidate | Stage A 契约冻结 |
| RR-TRUST（建议 D44） | exact Skill/runtime；source/instruction/validation 仅经 5.4 gateway；scratch-only model FS；supported SDK 路径或经验证 external sandbox；能力不足 NO-GO | 不接受该 trust boundary；停止 candidate | Stage B 离线 trust/runtime slices |
| RR-TRUTH（建议 D45） | Agent payload 不可信；Worker 生成 immutable candidate；Server terminal CAS 是唯一全局权威；采用 7–9 节三层契约 | 保留现有 terminal authority；停止 v2 contract | Stage A schemas + Stage B offline fixtures；不自动授权 C |
| RR-EVAL（建议 D46） | 采用第 13 节 power-gated、task-clustered paired benchmark、runtime bridge、PASS/FAIL/INDETERMINATE 和签发职责 | 不接受该 release proof；禁止真实 benchmark/发布 | 真实离线 benchmark；不授权生产 |
| RR-CUT（建议 D47） | 采用第 12/16 节 stop-intake、无 mixed-version 的 clean cutover、pre-canary deletion、same-contract rollback | 业务不接受维护窗/隔离；禁止 cutover | Stage D release preparation；Stage E 仍需 D24/发布授权 |

每个 packet 的 canonical record 必须包含：

1. `packet_id/option_id/exact_confirmation_text`、decider、时间、provenance；
2. 被 supersede 的 decision id + digest + normative unit，逐项写“保留/替换/撤销”；
3. 只授权的 stage/work package 和明确禁止项；
4. normative artifact paths/digests、所需 failing/pass fixtures、replacement obligations；
5. rollback/expiry（如有）和下一个需要独立确认的决策。

有效确认必须明确 packet 与 option，例如：`确认 RR-GOV-A，仅授权 Stage 0B，继续禁止 candidate、benchmark、部署和流量。` “按文档做”“同意重构”“继续”或 issue/PR 合并都不是 option-anchored resolution。记录工具必须拒绝模糊确认、越级授权、缺 supersession digest 和把多个独立选项静默合并。

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

1. `S0A.1` 用当前 register/Slice-0 commands 复现 wrapper 8,762/8,062 和 digest 漂移，输出 `slice0-provenance.json`：D39/D41 record digest、producer version、Generate generation、expected/actual path/line/digest、首次出现 commit。只有能够证明是既有权威生成链/冻结语义内的机械同步遗漏时才可修复；若需要再次 Generate、改变 expected 语义或退休 gate，只产出 evidence packet，留给 Stage 0B 决策。
2. `S0A.2` 追溯 405 行 decision-register gate test 未入 baseline。只有现有 baseline 定义已要求收录时，先 split/reduce 到每个新手写文件 ≤400 行、运行原有 focused/full tests，再机械同步；否则记录 replacement obligation，不扩大 current baseline。
3. `S0A.3` 建含 Admin 的四仓 deletion inventory，记录 entrypoint/config/table/artifact/test/docs，明确它不是兼容承诺。
4. `S0A.4` 保持默认 absence ratchet 语义不变，补齐当前 self-reference/legacy-present/108 failures/indeterminate 的可重复证据和 CI 状态。
5. `S0A.5` 每个修改前后重跑 decision register、Slice-0、contract baseline、default absence，并保存 exit/stdout/stderr/digest；strict absence 只记录当前 `INDETERMINATE`，不宣称通过。

退出：每个 drift 被分类为“可在现有语义内机械修复”或“需新决策”；允许项已 PASS；四仓 inventory 可重复；gate/生产语义未改；`GOV-0A/result.json` 可复算且为 `PASS`。若 provenance 不足则 GOV-0A 为 `INDETERMINATE`，仍可提交 RR-GOV packet，但不得声称 current baseline 已修复。

### Stage 0B：有决策的治理 gate replacement

1. 先 resolve D42 等价 append-only 决策，冻结 Slice-0 保留/退休边界、immutable history storage、live forbidden catalog、absence v2 三态、self-reference 消除和 replacement tests。
2. 以 failing fixtures 证明当前 strict gate 对真正 absent/self-reference 无法给出正确确定性结果，再实现非自引用 verifier。
3. replacement 必须让 live legacy present=exit `1`/`FAIL`、真正 absent=exit `0`/status `absent`、缺证或历史损坏=exit `2`/`INDETERMINATE`，且 immutable history 不作为 live forbidden input。
4. CI 默认 ratchet 继续阻止新增 legacy；exact release artifact 在 Stage D 使用 strict gate。不得通过放宽 exclusion、删除历史或只改 expected 获得 PASS。

退出：D42 resolution/provenance PASS；Slice-0 或有决策的 replacement PASS；absence v2 fixtures/三态 PASS；生产行为未改。

### Stage A：决策与契约冻结

1. 在 D42 governance decision 已完成后，resolve D43-D47 等价决策。
2. 定义 7.4 的 Server canonical private/public source、manifest、valid/invalid fixtures 与 generator contract，但未获生成授权前不 Generate/激活。
3. 冻结 result、coverage、identity、status/error/reason registry 和 private→public sanitization mapping。
4. 冻结 terminal CAS/cancel binding/ACK/stale 和 untrusted payload/trusted envelope 边界。
5. 冻结 Skill/instruction/tool protocol、scratch-only model FS、tool env、gateway/receipt、validation profiles、限额和 exact-load evidence。
6. 冻结 7.6 DDL/constraints/四事务、migration/历史数据处置和 pre/post-activation rollback。
7. 冻结 D46 benchmark documents，包括 power calculation、统计单位、runtime comparison cells、paired estimator/CI 和 missing-run 规则。
8. 更新四仓 `AGENTS.md`，明确 superseded rules，避免 current 指令冲突。

退出：register history/provenance PASS；normative units 引用新 digest；schemas/registries/DDL/tool/benchmark policy 均有 valid+invalid fixtures；dependency/evidence ledger 已生成；新决策只授权 Stage B。

### Stage B：离线 candidate

```text
pullwise_worker/reviewer_runtime/
  types.py
  source_snapshot.py
  instruction_bundle.py
  model_fs_policy.py
  gateway_service.py
  tool_bridge.py
  validation_service.py
  receipt_ledger.py
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

先写 failing tests：`test_reviewer_skill_binding.py`、`test_reviewer_instruction_surface.py`、`test_reviewer_model_fs_policy.py`、`test_reviewer_gateway_service.py`、`test_reviewer_validation_service.py`、`test_reviewer_receipt_ledger.py`、`test_reviewer_candidate_runner.py`、`test_reviewer_result_validator.py`、`test_reviewer_coverage_codec.py`、`test_reviewer_runtime_policy.py`。

candidate 只读 fixture/snapshot 并写本地结果；不得调用生产 lease/result、写 production table、切 builder、shadow traffic 或部署。必须证明 exact-load、surface control、scratch-only model FS、sanitized tool env、instrumented AGENTS/source receipts、gateway 外 source 不可见、validation copy 仅 service 可写、tool 无网络/凭据、单 turn、有界 cancel/timeout/close、untrusted payload/trusted result、result/coverage/redaction、2,000-file bounded encoding。无法控制任一 surface、SDK supported path 或 receipt authority 即 `NO-GO`。

### Stage B2：paired benchmark

先通过第 13 节 sample-size/power preflight。若 candidate runtime digest 与 stable 相同，则同 source/model/effort/SDK/CLI/machine/budget 交错运行 stable/candidate。若不同，必须按 13.2 建立 candidate-runtime comparison cell，不能把 stable-native 与 candidate 直接称为 runtime-controlled pair。顺序预注册；每 task 3 seeds；独立 oracle 解盲；保存 raw samples/exclusions/bindings/可复算 report。只有 D46 全部门 PASS 才进入 C；缺证/不可比/超时/样本不足均按预注册 missing-run 规则计失败或 INDETERMINATE，不得静默排除。

### Stage C：最小生产壳候选，不激活

- 从 legacy 按行为测试提取 slot/supervisor/checkout/source/SDK/deadline/cancel/usage/redaction/outbox，不复制 30-phase。
- Worker 消费 private package，持久化 active marker/exact outbox。
- Server 小模块实现 claim/heartbeat/event/CAS/normalization；新表无生产入口。
- 注入 crash-before/after-freeze、cancel-before/after、ACK loss、replay conflict、stale、source drift、hung close。
- 本地 E2E：Server fixture → claim → candidate → CAS → public projection。

退出：单一终态、真实 SQLite 并发/recovery PASS、public/debug 无 source/secret、生产 builder/routes 未切。进入 Stage D 仍需独立 signed stage-advance record，Stage C PASS 本身不授权生成 release change set。

### Stage D：跨仓切换准备

- Server 原子生成 contract，接入但 intake disabled；改计费事件；完成 projection/debug/D24 barrier/v1 rejection 和 7.6 v2 schema migration。migration rehearsal 从生产形状的备份副本开始，禁止 old→v2 task conversion。
- 形成一个不可拆分的四仓 exact release change set：Worker builder 只指向新 runner，并删除 `ReviewWorkerV1`、非目标 Agent Kernel 和旧 outbox/result；Server 删除旧 phase billing/artifact/route/storage consumer；Web 删除旧 DTO/phase/artifact fallback；Admin 删除 reviewer/bundle/assignment 配置。不得用 flag/fallback 暂存第二路径。
- Worker exact-pin private package；Web/Admin 只 pin public DTO；doctor 校验 exact runtime tuple、tool surface 和 scratch-only filesystem capability evidence。
- 生成 production runbook，逐命令列出 stop-intake、drain/fence、停旧 Worker/Server、v2 migration、部署顺序、D24 activation、smoke、capacity gate、pre-activation abort、post-activation rollback 和 evidence capture。
- 对将部署的 exact commits/build artifacts 运行 strict absence v2、引用图、wheel/install、contract parity、DDL/CAS、四仓 local/CI。不可把“部署后再删除”当作 Stage D PASS。

退出：exact release build 内只有一个 current contract/runner/Skill，strict absence exit=`0` 且 status=`absent`；四仓 pins/fixtures/CI PASS；operator 在 production-like 环境完成完整部署、pre-activation abort、same-contract rollback 或 fence/reject 演练；release build 尚未部署/接流量；deletion manifest 全部关闭或有明确 immutable-history 处置。

### Stage E：clean cutover 与 capacity-only canary

Stage E 使用维护窗或无共享 authority 的 blue/green；禁止让 v1/v2 Server 或 Worker 同时连接同一生产 queue/current tables，禁止 mixed-version rolling deploy。

1. 核验 signed release/evidence digests，stop intake 并冻结 operator generation。
2. 等待 pre-cutover tasks 到权威终态；不能完成的 task tombstone/delete 或撤权隔离，不得迁移。导出 task/lease/outbox 清单并证明 active=0。
3. 停止全部旧 Worker，撤销旧 worker session/token；验证没有旧 heartbeat/claim。
4. 停止并从负载均衡/queue 移除全部旧 Server/consumer；取得 DB migration lock。此时仍未激活 v2，失败可按 16 节 pre-activation abort。
5. 应用已演练的 v2 schema/data-isolation migration；部署 v2 Server，保持 intake/claim disabled，并证明所有 v1 route/wire/event/result/replay fail closed。
6. 部署 v2 Worker，允许 registration/health 但 claim disabled；再部署只消费 public v2 DTO 的 Web/Admin。核验 exact pins、contract/runtime/capability digests。
7. 在隔离的 release-smoke namespace 运行 synthetic E2E，验证 claim/CAS/projection/debug/redaction；该任务不进入生产业务队列或 benchmark 分母。
8. 在一个 acceptance 事务中激活 D24/current contract generation，同时启用 v2 intake/claim；旧 generation 永久拒绝。
9. 新 current contract 开 5% capacity，其余容量保持关闭，而不是导向旧路径。
10. ≥24h 且 ≥200 accepted current tasks 后进 25%；≥72h 且 ≥1,000 tasks 后才考虑 full。

门失败即停止扩容；只可 rollback 到同 contract/schema/storage 的 signed stable，否则 stop-intake/fence/reject。

### Stage F：全量与证据收尾

Stage F 不再修改已 canary 的 runtime/schema/contract，也不在 canary 后才删除 legacy；否则新 build 没有被 canary 覆盖，必须退回 Stage D 并重跑 Stage E：

- 核验 5%/25% 的时窗、样本、质量、安全、成本和 operator stop evidence 后，按签发计划提升到 full capacity。
- 对 exact canary/full build 重跑 strict absence、引用图、四仓 pins 和 CI，确认无 flag/fallback/第二 runner/旧 consumer。
- 归档 release attestation、deletion manifest、operator evidence 和被 live catalog 隔离的 immutable decision history；关闭临时 issue/checklist，但不得删除审计要求保留的不可执行历史。

退出：full capacity 使用与 canary 相同的 exact contract/schema/runtime build；strict absence 仍为 exit=`0`/status=`absent`；引用图无未解释 consumer；四仓 local/CI PASS。

## 13. Benchmark 与发布门（D22 专业化）

### 13.1 corpus 与运行纪律

- `120` 只是继承 D22 的绝对下限，不是所有置信门的充分样本量。解盲和排程前，evaluator 必须根据每个门的冻结统计单位、置信方向、阈值和允许失败数计算 `n_required`；最终独立 task-cluster 数是 `max(120, 每个适用总体门 n_required, 每个适用 per-family 门 n_required)`。
- 对 Bernoulli 门，policy 保存公式、z 值/单双侧、严格不等号、最大允许 failures 和机器复算 fixture。例如零失败时要求 95% Wilson upper **严格小于** 2%：若冻结为单侧 95%，至少 133 个独立 clusters；若使用常见双侧 95% interval 的 upper endpoint，至少 189 个。D46 未明确单双侧前按 189 做容量规划，不能把 120 宣称为可发布样本。
- 至少 3 个 sealed unknown repository families，每 family 至少 15 tasks。
- 至少 50 个 oracle-positive in-scope findings。
- 覆盖 security、correctness、API/schema、state/concurrency/resource、test-gap。
- 每个适用核心簇对 real defect、bad/incomplete fix、clean counterexample、environment/capability failure、adversarial/prompt injection 各至少 3 tasks。
- 覆盖小/大仓、monorepo、generated/vendor/binary/submodule、nested `AGENTS.md`、依赖缺失、测试不可运行、context/token/deadline 限制。
- 每 task 3 个预注册 seed；所有计划 run 都必须保留。seed 在 task 内等权，但不是三个独立统计样本；不得为追 PASS 追加或替换运行。
- 只允许 policy 预列 infrastructure reason 排除，逐样本报告；解盲后不得改分母、权重、seed、baseline、阈值或 evaluator。
- `15 tasks/family` 只足以作为最小覆盖/报告门，不能支持 98% Wilson-style family claim。D46 必须逐指标标注 `overall/per-stratum/per-family` 适用范围；若 98% 或 <2% bound 适用于单个 family，该 family 也必须扩到其 `n_required`，否则该门 `INDETERMINATE`。
- task-cluster bootstrap 指标的样本量用冻结的 simulation/power procedure 预估；pilot 只能使用不含 sealed benchmark label 的历史/合成数据。procedure、effect/margin、相关结构、RNG 和目标 power 进入 signed policy。

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

运行时比较单元同样在 D46 冻结：

| Cell | 语义/build 输入 | Runtime | 用途 |
|---|---|---|---|
| `S-native` | exact signed stable release | stable runtime digest | 漂移锚点，不在 runtime 改变时直接作为 candidate pair |
| `S-candidate-runtime` | exact stable source/contract/Skill/prompt semantics；只替换被 D46 允许的 runtime tuple | candidate evaluation runtime digest | runtime-controlled baseline |
| `C-candidate-runtime` | candidate | 同一 candidate evaluation runtime digest | 与上一 cell 做 primary pair |

candidate runtime 与 stable 相同时只需 `S-native ↔ C`。不同时，三 cell 在同一 72h window、同 machine class/budget/source/task/seed 下按预注册顺序交错；primary comparison 只能是 `S-candidate-runtime ↔ C-candidate-runtime`，并把 `S-native ↔ S-candidate-runtime` 作为 runtime drift 报告。这里“stable”指 exact stable source/semantic assets，不能把替换了 SDK/CLI/model 的 cell 虚称为 byte-identical stable build。若 stable 语义不能在 candidate runtime 运行、需改业务代码/contract、或 runtime drift 超出 D46 上限，则结果 `INDETERMINATE`。

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
- `false_verified_rate` point estimate < 1%，且适用的 95% Wilson upper < 2%；numerator/denominator 和 critical/adversarial 子门按 D22/D46 冻结。
- false discovery rate ≤ 20%。
- location accuracy 的预注册 point/bound 规则不得低于 98%；若采用 confidence lower bound，样本量必须通过 13.1 preflight，不能只以 observed point 代替。
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
- runtime 不可比时按 13.2 三 cell 交错重跑；不能形成 candidate-runtime stable cell 或仍不可比则 INDETERMINATE。

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
| Gateway | typed tool schema、canonical path、instruction-first enforcement、pagination/limit/truncation、bridge crash、sequence/hash-chain/correlation、伪造 response 无 receipt |
| Sandbox | scratch-only model FS、host/other-worker/auth/outbox/source/instruction/validation sentinel、scratch write、network、sanitized credential path/env、approval |
| SDK | exact tuple capability probe、restricted-read/external-sandbox evidence、missing/wrong thread/turn id/status、notification failure、timeout、archive/close hang |
| Result | Agent 注入 trusted fields/classification、Worker binding injection、malformed schema、unknown enum、traversal、line OOB、evidence mismatch、duplicate |
| Coverage | claim/receipt intersection、0/1/2,000 files、gap/overlap/order/OOB、unknown reason、instruction binding |
| Lifecycle | bound/unbound/stale cancel、deadline/lease loss before/during/after turn、crash before/after freeze |
| Publish | exact replay before stale check、ACK loss、key conflict、stale、cancel vs result CAS concurrency |
| Contract | valid/invalid fixtures、additionalProperties、closed registries、generator no-clobber、跨仓 byte parity、private/public import 边界 |
| Server | DDL constraints、claim/cancel/event/terminal 四事务、cancel binding、quota event idempotency、normalization、DTO redaction、projection recovery |
| Web/Admin | dynamic five steps、partial/debug/ETA、private-field absence、配置删除、390px |
| Benchmark | task-cluster/strata、3-seed repeated measures、Wilson bounds、paired CI、single-side missing、exclusion cap、per-family failure |
| Deployment | 无 mixed v1/v2、stop/drain/revoke/migration/order、v1 reject、pre-activation abort、same-contract rollback、D24 atomic activation |

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

Stage A 必须交付目标 evidence aggregator：

```powershell
python scripts/check_reviewer_refactor_evidence.py check --workspace-root .. --release-id <id> --stage <0B|A|B|B2|C|D|E|F>
```

它只验证 signed inputs、dependency results、direct evidence、artifact digests、commands/CI 和适用门，不替代底层测试；exit 0=`PASS`、1=`FAIL`、2=`INDETERMINATE/NOT_AUTHORIZED`。Stage 0A 在该工具存在前直接使用当前治理命令和 0.2 evidence shape。

## 15. 文件所有权与实施切片

| 切片 | 主仓 | 主要目录/职责 | 独立验收 |
|---|---|---|---|
| GOV-0A | Worker | current decision/slice0/absence evidence、四仓 inventory | 不改变语义的 drift classification/current check |
| GOV-0B | Worker | D42、replacement slice0/absence scripts、contracts、docs | 三态/self-reference/true-absence fixtures |
| EVD-1 | Worker | evidence schema/writer/aggregator | tamper/missing/dependency/exit-code fixtures |
| SKILL-1 | Worker | `reviewer_skill/**` | package bytes/binding/eval fixtures |
| RUN-1 | Worker | source snapshot/instruction bundle/read gateway | source/instruction/receipt faults |
| RUN-2 | Worker | scratch-only filesystem/runtime policy/SDK session/runner | read/env/network sandbox、SDK/turn/capability |
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

### 15.1 依赖 DAG

```text
GOV-0A
  └─ RR-GOV-A → GOV-0B
       └─ RR-SCOPE/TRUST/TRUTH/EVAL/CUT-A → Stage A freeze + EVD-1
            ├─ CON-1 ───────────────┬─ SRV-1 → SRV-2 ───────────────┐
            ├─ SKILL-1 ─┐           │                               │
            ├─ RUN-1 ───┼─ RUN-2 ──┼─ RES-1 → PUB-1 ───────────────┤
            └─ BEN-1 policy         └─ Web/Admin public generation  │
                                  offline candidate + BEN-1 run     │
                                                └─ Stage C E2E ─────┤
                                                                   └─ CUT-1 → D → E → F
```

依赖只接受前置 work package 的 exact `PASS` manifest；工作树上“代码看起来已合并”不算依赖完成。可以并行的切片必须有互斥 write set 和共同 pinned input digest；任一共享契约变化会使所有下游证据 stale。

### 15.2 Work-package ledger 与验收证据

Stage A 生成 `work-package-ledger.json`，每项至少含 owner/reviewer、repository/write set、dependency evidence digest、decision/contract/runtime inputs、red command+failure、green command、focused/full/CI commands、output artifacts、line-count result、state 和 superseded-by。状态不能手工改成 PASS，只能由 evidence aggregator 从直接证据推导。

| Work package | Entry gate | PASS 必须直接证明 |
|---|---|---|
| GOV-0A | current D41 boundary | provenance、四仓 inventory、现有 gate 输出；零语义变化 |
| GOV-0B | RR-GOV-A | replacement 三态、true absence/live present/history damage/self-reference fixtures、旧义务映射 |
| EVD-1 | Stage A decisions | tamper/missing/stale dependency→exit 2，真实 fail→exit 1，完整 evidence→exit 0 |
| SKILL-1 | RR-SCOPE/TRUST-A | wheel/install bytes、transitive manifest、explicit SkillInput、无 eval/隐式 surface |
| RUN-1 | RR-TRUST-A | immutable inventory、instruction precedence、五工具协议、不可伪造 ordered receipt |
| RUN-2 | RUN-1 + SKILL-1 | exact runtime capability、scratch-only FS、env/network deny、single turn、bounded interrupt/close |
| RES-1 | RUN-1 + frozen schemas | untrusted injection rejection、trusted binding、location/evidence、closed coverage/classifier |
| PUB-1 | RUN-2 + RES-1 | freeze/no-clobber/fsync/replay/crash；无生产 submit |
| BEN-1 | RR-EVAL-A + candidate PASS | power preflight、sealed corpus、三 runtime cells（适用时）、全部门三态可复算 |
| CON-1 | RR-TRUTH-A | canonical schemas/registry/fixtures、atomic generation、四仓 exact parity |
| SRV-1 | CON-1 | 新 DDL constraints、四事务、SQLite concurrency/recovery、terminal CAS |
| SRV-2 | SRV-1 | intake disabled integration、billing/projection/debug/v1 rejection |
| WEB-1/ADM-1 | public generator + SRV-2 | 只依赖 public v2、旧 fallback/config absent、check + 390px QA |
| CUT-1 | 所有上游 PASS | exact release manifest、strict absence、完整 rehearsal、CI、signed runbook/attestation |

每个新手写 production source/test/script 默认 ≤400 行；401–600 行必须在 ledger 中有 cohesion rationale 和 review；>600 不得进入新架构。当前超大 legacy 文件只允许删除或添加调用 narrow seam 所需的最小变更，不得承接新职责。

## 16. 回滚与 operator runbook

回滚边界以 D24/current-contract activation transaction 为线：

- **激活前 abort**：只有在 v2 accepted task=0、D24/current generation 未改变、旧 schema/storage 未被破坏且旧 token/replica 可按 signed pre-cutover manifest 恢复时，才可整体恢复 exact v1 release。它是仍处于 v1 current contract 时的部署中止，不是 v2 fallback；任何条件不确定就保持 stop-intake。
- **激活后 rollback**：v2 已成为 current 后，允许回滚仅限已签发 v2 stable build，且同时保持：

  - 相同 private/public current contract identity/version/digest；
  - 相同 DB schema/storage semantics；
  - 相同 D24 barrier 与新 task population；
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
| Python SDK public surface 不足 | Stage B capability probe；受支持升级或外部隔离；否则 NO-GO |
| contract/registry 跨仓漂移 | Server canonical generator + temp regeneration + release manifest exact parity |
| benchmark 样本量不足/伪独立 | power preflight + task cluster + per-family applicability + 三态 |
| mixed-version 部署破坏 clean break | stop/drain/stop-old/deploy-disabled/atomic D24；禁止 rolling mixed authority |

估算必须按下面的 workload model 重新计算，不能仅按 Worker LOC：

```text
engineering_calendar =
  decision_wait
  + critical_path(GOV-0A, GOV-0B, Stage A, RUN/RES/PUB, Stage C, CUT)
  + max(contract+server+web+admin lane, corpus+oracle lane)
  + ceil(scheduled_benchmark_runs / safe_parallel_slots) * observed_run_p95
  + CI/review/rework
  + canary_minimum_96h
```

同 runtime 的 benchmark 最少约为 `N_clusters × 3 seeds × 2 cells`；runtime 改变时是 `×3 cells`。仅以 13.1 的保守 `N=189` 举例就分别是 1,134/1,701 planned runs，尚未计 per-family 扩样、无效基础设施重排禁止项和 canary。

在决策及时、SDK capability 不阻塞、corpus 可复用且单人连续投入的假设下，当前 planning range 是 12–25 工程周：

- Stage 0A–0B：1–2.5 周，不含授权等待；
- Stage A/EVD/契约冻结：1.5–3 周；
- Stage B Worker candidate：3–5 周；
- corpus/oracle/evaluator + 实际运行：3–7 周；
- Stage C Server/Worker E2E：2–4 周；
- Stage D–F 四仓 clean cutover：2–4 周。

这是区间而非承诺；每个阶段以 ledger 的 actual throughput/p95 更新 forecast。外部授权等待和 canary 至少 24h + 72h 的墙钟时间单列。corpus/oracle、SDK capability 和跨仓 release rehearsal 都可能成为关键路径。

## 18. Definition of Done

只有每项有当前直接证据时才完成：

- **DOD-01** append-only 决策已授权并逐项 supersede；无 current 指令冲突。
- **DOD-02** Slice-0/replacement gate 与非自引用 strict absence gate 可用。
- **DOD-03** 生产只有一个 current private contract、一个 runner、一个 Reviewer Skill。
- **DOD-04** attempt exact-bind SDK/CLI/runtime/model/effort/全部 runtime Skill assets/Agent-output schema/result schema/source/instruction/tool。
- **DOD-05** CWD/CODEX_HOME/scratch-only model FS/tool env/network/credential/approval 全部通过 sentinel/故障注入。
- **DOD-06** slot/lease/bound cancel/deadline/source/budget/outbox/Server CAS 通过并发/崩溃测试；Worker 无法自创 `CANCELLED`。
- **DOD-07** Agent payload 与 Worker trusted result 分离；binding/classification/identity/location/evidence/coverage/redaction 均由非 Agent 代码生成或验证。
- **DOD-08** D46 offline benchmark 所有适用门、power/sample gate 和 task-clustered paired statistics PASS，无缺证/INDETERMINATE。
- **DOD-09** billing 不再依赖旧 phase；三层 contract/generator/public redaction PASS。
- **DOD-10** Web 使用五级 progress/public DTO；Admin 旧配置已删除。
- **DOD-11** D24 barrier、legacy reject、完整 deployment、pre-activation abort、post-activation rollback/stop-intake 演练通过。
- **DOD-12** 30-phase、非目标 Agent Kernel、shadow/fallback/compatibility、旧配置/DTO/table/docs consumers 已在 exact release build/canary 前删除。
- **DOD-13** exact release build 的 strict absence 确定性 exit=`0`/status=`absent`，且 canary/full 对同一 build 重检仍通过。
- **DOD-14** 四仓 local checks 与对应 CI 全绿；CI 不可用不能完成生产 DoD。
- **DOD-15** canary 5%/25% 的样本、时窗、阈值 PASS 后才把同一 exact build 提升到 full；canary 后若改 runtime/schema/contract，必须重走 Stage D/E。
- **DOD-16** 四仓 `AGENTS.md` 记录 durable current rules，不把 superseded rules 留作 current。

### 18.1 Completion audit matrix

| DoD | Owner | 最小直接证据 |
|---|---|---|
| 01 | architecture/governance owner | register check、provenance、normative-unit digest、四仓 instruction conflict scan |
| 02 | GOV-0B owner | replacement fixtures + strict true-absence/live-present/history-damage reports |
| 03 | CUT-1 owner | exact release reference graph、builder/routes/package inventory |
| 04 | Worker owner | attempt binding fixture、wheel/install/runtime capability/tool manifest report |
| 05 | Worker security owner | sentinel matrix raw results、sandbox/image/config digests |
| 06 | Worker + Server owner | crash/ACK/stale/cancel/deadline/SQLite concurrency reports |
| 07 | RES-1 owner | injection/schema/location/coverage/classifier/redaction tests |
| 08 | benchmark owner + release operator | signed D46 policy、raw schedule、power output、evaluator PASS report |
| 09 | CON/SRV owner | generator byte parity、registry/fixtures、billing idempotency、DTO redaction |
| 10 | Web/Admin owners | generated public digest、checks、browser/mobile QA、absence inventory |
| 11 | deployment operator | production-like runbook transcript、D24 transaction、v1 rejects、abort/rollback evidence |
| 12 | four-repo owners | deletion manifest closed、strict catalog/reference graph no unexplained consumer |
| 13 | release operator | exact artifact strict-absence reports at D/E/F，均绑定同一 release digest |
| 14 | four-repo owners | local command logs + immutable CI run ids/artifacts for exact commits |
| 15 | release operator | 5%/25% windows、accepted counts、quality/safety/cost gates、full promotion signature |
| 16 | governance owner | 四仓 AGENTS digest、current-rule renderer/check、superseded text audit |

最终 `release-attestation.json` 必须逐个列出 DOD-01..16 的 evidence URI/digest/owner/result；缺任一项时 aggregator 只能返回 `INDETERMINATE`，不能用总括性“全部测试通过”替代。

## 19. 立即下一步

1. 当前只开 `GOV-0A`：复现并分类 Slice-0/absence drift，生成 provenance/evidence 和四仓 deletion inventory；只修复能证明不改变现有语义的机械遗漏。
2. 将无法在现有语义内修复的项目写入 RR-GOV packet，不改 expected、不 Generate、不替换 gate。
3. 取得 `RR-GOV-A` 的 option-anchored 确认并 append resolution；然后按 TDD 实现 GOV-0B。未确认则重构停在 0A。
4. 逐项准备并显式确认 RR-SCOPE/TRUST/TRUTH/EVAL/CUT；尤其在 RR-TRUTH 中明确 supersede D5/D9 的 terminal authority。
5. Stage A 冻结 canonical private/public contract、Agent-output/result schema、registry/DDL、gateway/tool/receipt、scratch-only model FS、`semantic_review_started`、D46 power/statistics 和 evidence ledger。
6. 仅在决策授权后按 DAG/TDD 建 Skill 和无生产 authority candidate；SDK capability 不能证明就 `NO-GO`。
7. exact-load/surface/env/gateway-receipt/coverage/terminal faults 未全绿，不开始真实 benchmark。
8. power preflight 与 paired benchmark 未 PASS，不开始 Stage C–F、部署、流量、canary 或删除。

最小正确原则：

> **由 Skill 维护审查智慧，由代码维护系统真相，由 Server CAS 维护全局终态。**
