# Pullwise Worker 精简全仓审查引擎

## 目标

新引擎只做一件事：对一个不可变的全仓快照执行 Codex 审查，并且只把具有可审计证据的问题写入公开结果。证据可以是 runtime-command，也可以是经过结构化静态证据门校验的 static-proof。

它明确不做以下事情：

- 不以 diff 作为审查范围。
- 不把静态猜测或 worker 的启发式检查直接当成问题。
- 不启动并发的 `codex exec` 或多组 Codex CLI 进程。
- 不手工刷新、复制或轮换 ChatGPT OAuth token。
- 不引入第三方 Python 运行时依赖、图数据库、解析器或静态分析框架。

## 活跃架构

```text
Pullwise server job
        |
        v
worker protocol adapter
pullwise_worker/_main_part_04_graph_verified_review.py
        |
        v
simple full-repository engine
codereview/simple_review.py
        |
        +--> deterministic inventory + immutable snapshot
        |
        +--> one shared Codex app-server process
        |      +--> bounded discovery turns
        |      +--> Codex subagents inside a turn
        |      +--> bounded verification turns
        |
        +--> deterministic event evidence gate
        |
        v
legacy-compatible graphVerifiedReport/1 payload
```

旧 GraphVerified 实现仍保留在仓库中作为短期回滚路径，但 worker 的生产入口和 `python -m codereview` 已切换到精简引擎。稳定运行一个发布周期后，可以单独删除旧图构建、finder、judge 和 repair 模块，避免把一次架构切换和大规模文件删除绑在同一个发布中。

## 单向流程

### 1. Inventory 与 Snapshot

- 使用 `git ls-files -c -o --exclude-standard` 枚举当前全仓，而不是 diff。
- 对每个可审查文本文件记录路径、字节数、行数和内容哈希。
- 每个文件必须且只能进入一个 review unit。
- 复制到不可变快照，审查结束后再次校验源仓和快照哈希。

### 2. Discovery

- 按顶层目录、文件数和字节数形成稳定的 review units。
- 所有 units 都会被分配，预算只改变 turn 数和每个 turn 的工作量，不会偷偷跳过文件。
- Batches grow from file and byte limits; fast/standard/deep default to 40/80/100 files and 0.5/1.0/1.25 MB per discovery turn.
- 如果仓库在最大 turn 预算内仍装不下，引擎直接失败，不会把超大批次硬塞进模型上下文，也不会假装完成全仓覆盖。
- 每个 discovery turn 读取磁盘上的 assignment JSON，prompt 不内嵌源码，减少输入 token。
- prompt 明确要求 Codex 在可用时把 agent groups 分给子代理。
- discovery 只产生“待复现候选”，不会产生公开问题。

### 3. Runtime Verification

- 每个候选使用不可变快照的独立副本。
- workspace-write 只用于 `.codereview/repro/` 下的临时复现脚本。
- verifier 需要让一个子代理追踪行为、另一个子代理进行反证，协调代理执行最终命令。
- 最终证据必须来自至少一次真实执行的命令；推荐在 `.codereview/repro/` 下创建最小 harness，真实调用目标代码。
- 禁止使用 `echo`、`printf`、只打印预设文本的内联脚本充当证据。
- 网络关闭，不安装依赖，不修改仓库源码。

### 4. Deterministic Evidence Gate

confirmed 有两类证据路径。runtime-command 必须同时满足以下条件：

- App Server 的 `item/completed` 事件中存在真实 `commandExecution`。
- 至少一条真实 `commandExecution` 包含明确退出码。
- 声明的 output marker 出现在该命令的 stdout/stderr。
- 命令不是纯 `cat`、`grep`、`sed`、`echo`，也不能靠命令字符串夹带运行时名称蒙混过关。
- 命令工作目录位于隔离副本中。
- 命令、实际输出或其引用的复现脚本必须明确关联候选引用的源码路径。
- 被执行的源码文件存在，并覆盖候选的主要证据文件。
- 验证过程没有修改仓库源码。
- verifier 和 skeptic 都同意，且 expected/observed 行为完整。

static-proof 只适用于无法忠实用一个本地命令复现的仓库证据类缺陷，例如 config、workflow、lifecycle、concurrency/state-machine、security 或 documentation-contract。它必须满足：

- `reproduction_command` 和 `output_marker` 为空，不能伪装成 runtime evidence。
- `exercised_files`/inspected files 覆盖候选的每个 primary evidence file。
- 每个 inspected file 必须真实存在、不是 symlink，并位于隔离仓库副本内。
- `verification_steps` 必须说明如何从具体仓库文件验证 observed behavior。
- verifier 和 skeptic 都同意，且 expected/observed 行为完整。
- 公开展示时必须保留 `static-proof` 和 `model-self-certified` 标识，明确告诉用户这是模型自证的静态证明，不是 runtime reproduction。

任一条件不满足，候选只进入本地 diagnostics，不进入 `finalJson.confirmed`、公开 `rejected.json` 或用户报告。自然语言描述本身不是证据。

### 5. Report

对外继续使用现有协议：

- `version = graph-verified-code-review/1`
- `scope = full-repository`
- `finalJson.confirmed[]`
- `candidate.graph_evidence`
- `verification`、`repro`、`judge`
- `summary.finder`、`summary.candidates`、`summary.repro`、`summary.judge`、`summary.reports`

`graphVerifiedReport/1` 的问题结构不变，因此 web 和 admin 无需改动。server 只需要一个小型配套补丁，用于识别新的 Codex readiness 错误码、阻止无意义自动重试，并在认证、授权、订阅、额度或 CLI 版本故障时退还本次扫描额度。

## Auth 与 token 刷新规则

1. 每个 worker identity 只有一个长生命周期 `codex app-server`。
2. 新引擎每次 scan 只调用一次 `account/read`，并固定传入 `refreshToken: false`。
3. token 的自动刷新完全交给 Codex App Server，不在 worker 里实现第二套刷新器。
4. 不启动并发 CLI，因此不会出现多个进程同时消费同一个 refresh token。
5. Discovery outer turn concurrency defaults to 1 and is capped at 6 via `graphVerified.finderTurnParallel`; verification outer turn concurrency remains auto/plan controlled and capped at 6 by the simple engine.
6. 大部分并发通过同一 turn 内的 Codex 子代理完成，共享同一个 App Server 与认证状态。
7. 一旦出现 401、refresh token already used、failed to refresh token、quota exhausted、usage limit 等可缓存的 readiness 错误，当前 scan 立即失败并打开 cooldown，停止继续 claim，避免登录重试或额度重试风暴。
8. server 将这些 readiness 错误视为终止且可退款，不自动排队重试，并释放或回滚本次扫描额度。
9. 只有 token/登录态类错误会关闭当前 App Server 进程；普通超时或单 turn 错误不会无脑重启共享进程。
10. App Server 的 HOME、CODEX_HOME、SQLite 和缓存仍按 worker identity 隔离，禁止多实例共享。

## Token 预算

- prompt 只包含规则和 assignment 文件路径，不复制整个源码或大段 manifest。
- fast/standard/deep use baseline discovery turn counts of 1/2/3 and max discovery turns of 10/20/32; large repositories add sequential turns by the per-mode file and byte limits.
- Per-unit candidate caps default to fast/standard/deep = 1/1/2.
- Verification candidate caps default to fast/standard/deep = 4/8/12; explicit server config still overrides via the simple settings payload.
- Global scan deadline defaults to 14,400 seconds (4 hours) for every mode unless the server config overrides `scan_deadline_seconds`; candidates left after the deadline are internally rejected, not exposed as reviewed findings.
- Outer turn concurrency is capped at 6. `graphVerified.finderTurnParallel` controls discovery turn concurrency, and `graphVerified.finderMaxParallel` controls finder subagents batched inside one turn.
- 超出验证预算的候选不会公开，也不会伪装成已审查问题。

## 发布顺序

1. 先发布 `pullwise-server` 配套补丁，让新错误码、禁止重试和额度退款策略先就位。
2. 再发布 `pullwise-worker` 补丁，并只在一个 worker identity 上运行 smoke scan。
3. 检查 `summary.coverage.complete == true`、同一 identity 的 App Server 进程数为 1、公开结果只包含 confirmed。
4. 再扩大 worker 数量，每个实例使用独立 HOME/CODEX_HOME。
5. After one stable release cycle, remove the old GraphVerified modules separately.

web 和 admin 不需要发布补丁。

## 回滚

没有数据库迁移。先回滚 worker 到旧入口，再回滚 server 的 readiness 错误策略即可；web 和 admin 无需回滚。
