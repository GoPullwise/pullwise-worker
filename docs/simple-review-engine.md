# Pullwise Worker 精简全仓审查引擎

## 目标

新引擎只做一件事：对一个不可变的全仓快照执行 Codex 审查，并且只把具有真实运行命令证据的问题写入公开结果。

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
- 根据文件数和字节数自动增加顺序 discovery turns，默认每个 turn 最多约 120 个文件、1.5 MB 源码。
- 如果仓库在最大 turn 预算内仍装不下，引擎直接失败，不会把超大批次硬塞进模型上下文，也不会假装完成全仓覆盖。
- 每个 discovery turn 读取磁盘上的 assignment JSON，prompt 不内嵌源码，减少输入 token。
- prompt 明确要求 Codex 在可用时把 agent groups 分给子代理。
- discovery 只产生“待复现候选”，不会产生公开问题。

### 3. Runtime Verification

- 每个候选使用不可变快照的独立副本。
- workspace-write 只用于 `.codereview/repro/` 下的临时复现脚本。
- verifier 需要让一个子代理追踪行为、另一个子代理进行反证，协调代理执行最终命令。
- 最终命令必须至少重复运行两次；推荐在 `.codereview/repro/` 下创建最小 harness，真实调用目标代码。
- 禁止使用 `echo`、`printf`、只打印预设文本的内联脚本充当证据。
- 网络关闭，不安装依赖，不修改仓库源码。

### 4. Deterministic Evidence Gate

只有同时满足以下条件才确认：

- App Server 的 `item/completed` 事件中存在真实 `commandExecution`。
- 同一条精确命令至少真实执行两次，两次退出码一致。
- 声明的 output marker 同时出现在两次命令的 stdout/stderr。
- 命令不是纯 `cat`、`grep`、`sed`、`echo`，也不能靠命令字符串夹带运行时名称蒙混过关。
- 命令工作目录位于隔离副本中。
- 命令、实际输出或其引用的复现脚本必须明确关联候选引用的源码路径。
- 被执行的源码文件存在，并覆盖候选的主要证据文件。
- 验证过程没有修改仓库源码。
- verifier 和 skeptic 都同意，且 expected/observed 行为完整。

任一条件不满足，候选只进入本地 diagnostics，不进入 `finalJson.confirmed`、公开 `rejected.json` 或用户报告。

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
5. discovery 和 verification 的外层 turn 并发默认都是 1，硬上限都是 2。
6. 大部分并发通过同一 turn 内的 Codex 子代理完成，共享同一个 App Server 与认证状态。
7. 一旦出现 401、refresh token already used、failed to refresh token、quota exhausted、usage limit 等可缓存的 readiness 错误，当前 scan 立即失败并打开 cooldown，停止继续 claim，避免登录重试或额度重试风暴。
8. server 将这些 readiness 错误视为终止且可退款，不自动排队重试，并释放或回滚本次扫描额度。
9. 只有 token/登录态类错误会关闭当前 App Server 进程；普通超时或单 turn 错误不会无脑重启共享进程。
10. App Server 的 HOME、CODEX_HOME、SQLite 和缓存仍按 worker identity 隔离，禁止多实例共享。

## Token 预算

- prompt 只包含规则和 assignment 文件路径，不复制整个源码或大段 manifest。
- fast/standard/deep 的 discovery 基准 turn 数为 2/3/4；仓库较大时会按 120 文件或 1.5 MB 的批次上限自动增加顺序 turns，硬上限默认 48。
- 每个 unit 默认最多产生 2 个候选。
- runtime verification 默认 fast/standard/deep 最多 8/20/50 个候选。
- 外层 turn 并发硬限制为 2，避免同时压入多个大上下文。
- 超出验证预算的候选不会公开，也不会伪装成已审查问题。

## 发布顺序

1. 先发布 `pullwise-server` 配套补丁，让新错误码、禁止重试和额度退款策略先就位。
2. 再发布 `pullwise-worker` 补丁，并只在一个 worker identity 上运行 smoke scan。
3. 检查 `summary.coverage.complete == true`、同一 identity 的 App Server 进程数为 1、公开结果只包含 confirmed。
4. 再扩大 worker 数量，每个实例使用独立 HOME/CODEX_HOME。
5. 稳定一个发布周期后，再单独删除旧 GraphVerified 模块。

web 和 admin 不需要发布补丁。

## 回滚

没有数据库迁移。先回滚 worker 到旧入口，再回滚 server 的 readiness 错误策略即可；web 和 admin 无需回滚。
