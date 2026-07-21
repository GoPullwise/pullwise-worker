# Agent-First Worker S3a 内部只读 Tracer 证据

日期：2026-07-21

## 当前结论

本切片完成了 package-independent 的内部安全骨架：

1. Pullwise SourceState/SourceDiff 的确定性 typed facts。
2. Ubuntu 上 descriptor-rooted、no-follow、nonblocking 的源码扫描。
3. 固定顺序的 Gateway orchestration kernel。
4. 一个绑定完整 SourceState 和已打开 regular-file descriptor 的内部 R0 read tracer。

这不是生产 S3 完成证据，也没有接管 legacy Worker。它不生成任何 Server-owned
versioned contract，不写 Observation，不使用现有 Slice 2 shadow Task/Attempt/budget/
observations 表，也没有 production composition-root 接线。

## 已实现边界

### SourceState

- SourceSelectionPolicy 只接受 Pullwise all_repository_regular_files profile；
  控制根固定为 .git 与 .codex-review。调用方自选 exclusion 或 ephemeral pattern
  一律拒绝。
- file identity 绑定 raw bytes SHA-256、size 和 executable bit；symlink 只记录逻辑
  target 且不跟随。路径要求 canonical relative UTF-8/NFC，并拒绝 traversal、
  backslash、NUL、casefold component collision 和超出安全整数的 size。
- SourceStateID 只由 base revision、policy digest 和 UTF-8 byte-order entries 组成。
  SourceDiff 是内部 typed value，不携带 schema ID 或自引用 ContentRef。Pullwise
  任意非空 diff 均触发 SOURCE_MUTATION_FORBIDDEN。
- Ubuntu scanner 从固定 root dirfd 开始，逐组件 openat/O_NOFOLLOW，文件增加
  O_NONBLOCK 后才 fstat/read；读取前后比较 device/inode/mode/size/mtime/ctime，
  目录再次枚举以检测新增或删除。Windows fallback 只用于开发测试，并执行路径、
  reparse 和 before/open/after identity 检查。
- raw gitlink mapping 会被拒绝。精确 revision 的可信 Git catalog 尚未实现，因此
  SOURCE_GITLINK_CATALOG_UNVERIFIED 是明确生产阻断，不允许用调用方字典隐藏子树。

### Gateway

agent_kernel_gateway.py 只实现以下固定顺序：

1. exact-pinned codec 校验与 canonical invocation digest。
2. durable journal replay probe。
3. Task/Attempt/session/owner freshness。
4. outer lease/native epoch。
5. desired state/lifecycle。
6. descriptor resolution、capability 与 risk ceiling。
7. descriptor-safe prepare。
8. command/network/secret/approval controls。
9. budget reservation。
10. dispatch intent winner、真实 dispatch、fresh SourceState、truthful commit。

codec、authority、policy、budget、journal 和 committer 都是注入边界。任何
pre-dispatch 失败不 dispatch；prepare 后的控制/budget/intent 失败会关闭 descriptor，
并在已有 reservation 时释放它。exact completed replay 跳过全部 authority 和 dispatch；
pending replay 不重发。dispatch 后 SourceState 不可建立或出现 diff 时，正常 receipt
不会直接暴露，而是进入 injected unavailable/violation committer。

### R0 read

- ReadSourceFileInput 只保存已校验的 repository-relative path；PreparedDispatch 给
  dispatcher 的 handle 不含 unresolved path、shell、network client、approval channel
  或 secret handle。
- prepare 先取得完整 SourceState，再逐组件固定 leaf descriptor，并机械验证其
  size/hash/executable 与 before entry 完全一致。
- dispatcher 只能消费该 one-shot descriptor，读取时再次验证 identity、size 和 hash，
  成功或失败都会关闭 descriptor。路径在 prepare 后被替换也不能把读取重定向到新目标。
- capture_after 总是重新扫描；Gateway 对任何非空 diff 扣留正常结果。

## 明确未实现与阻断

- D23 Server-owned current package、package manifest/version/digest 和 Worker exact pin。
- source-selection-policy、source-tree-manifest、change-set、tool-dispatch-intent、
  observation 等 schema 的权威 validator/codec。
- exact-revision trusted gitlink catalog。
- current-only Task/Attempt/session/lease authority store。
- D7 elapsed-consumption budget ledger。
- crash-safe dispatch journal、two-phase unbound CAS publish/bind 和 automatic Observation。
- ExecutionState、R1、Agent session runtime、Completion/Verifier/Gate、S4-S8 与 D22 gate。

现有 AgentKernelDatabase 初始化 legacy_v1 Task schema，现有 observations 表也外键到
该 Task；它们不得被本 tracer 当成 current production journal。上述阻断关闭前，
不得加入 production feature flag、shadow route、fallback 或 legacy adapter。

## 验证

从 pullwise-worker 运行：

    python -m unittest \
      tests.test_agent_kernel_source_state \
      tests.test_agent_kernel_gateway \
      tests.test_agent_kernel_r0_read

本地 Windows 证据：40 tests passed，4 个 POSIX/host-capability 用例按平台跳过。
Ubuntu CI 必须实际覆盖 dirfd、FIFO race 和 case-sensitive component collision。

Decision gate：

    python scripts/agent_first_decision_register.py check \
      --repo-root . --require-slice S3

结果为 ready，26 resolved、0 applicable pending、D2 inactive。

## 文件尺寸证据

- agent_kernel_source_state.py：376 行，内部 identity/diff。
- agent_kernel_source_scan.py：300 行，Ubuntu dirfd scanner。
- agent_kernel_source_scan_windows.py：174 行，开发平台 fallback。
- agent_kernel_gateway.py：399 行，固定顺序 orchestration。
- agent_kernel_r0_read.py：354 行，held-descriptor R0 read。
- test_agent_kernel_source_state.py：375 行。
- test_agent_kernel_gateway.py：347 行。
- test_agent_kernel_r0_read.py：307 行。

AGENTS.md 的冻结基线为 1232 行，本切片仅增加 31 行 durable boundary notes；职责仍是
仓库级 guardrails。MVP implementation design 的冻结基线与当前均为 1875 行，只替换
一行非规范状态证据；两者都是已有 oversized 文档，没有新增实现职责。
