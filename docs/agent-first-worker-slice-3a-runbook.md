# Agent-First Worker S3a 内部只读 Tracer 与 capture window 证据

日期：2026-07-21

## 当前结论

本切片实现并验证了 package-independent 的内部安全骨架：

1. Pullwise SourceState/SourceDiff 的确定性 typed facts。
2. Ubuntu 上 descriptor-rooted、no-follow、nonblocking 的源码扫描。
3. 与 checkout top-level、Git 版本、object mode/OID 和物理 gitlink topology 绑定的
   exact-revision catalog。
4. 必填 CheckoutAcquisitionBounds、独立的 CheckoutWriterCoordinator、共享 canonical
   lock domain 的 POSIX cooperating-writer lock，以及覆盖 fresh catalog/scan
   before/after 的 capture window。
5. 固定顺序的 Gateway orchestration kernel。
6. 由同一个 materialized-source capture provider 提供 checkout root 与 before/after
   SourceState、并跨 dispatch 持有 lease 的内部 R0 read tracer。

这不是生产 S3 完成证据，也没有接管 legacy Worker。它不生成任何 Server-owned
versioned contract，不写 Observation，不使用现有 Slice 2 shadow Task/Attempt/budget/
observations 表，也没有 production composition-root 接线。D28-D30 当前仍为 pending，
S3 decision gate 明确 blocked；本文中的 injected interfaces 和内部 tracer 不选择或
替代这些生产决议。

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
  任意非空 diff 均触发 SOURCE_MUTATION_FORBIDDEN。全局 entry invariant 同时拒绝
  重复 path 和任意 ancestor/descendant prefix 冲突，不能同时表示例如 file
  `vendor` 与 gitlink `vendor/sub`。
- Ubuntu scanner 从固定 root dirfd 开始，逐组件 openat/O_NOFOLLOW，文件增加
  O_NONBLOCK 后才 fstat/read；只读取 initial size 再探测一个额外 byte，避免持续
  append 导致无界读取；读取前后比较 device/inode/mode/size/mtime/ctime，
  目录再次枚举以检测新增或删除。Windows fallback 只用于开发测试，并执行路径、
  reparse 和 before/open/after identity 检查。
- raw gitlink mapping 会被拒绝。可信 catalog 只能由固定 absolute Git executable
  对精确 40-hex revision 生成；每次子进程前后都重验 executable identity，Git
  replace refs 与 lazy fetch 同时由命令行和环境关闭。`git --version` 必须证明
  Git >=2.45，`rev-parse --path-format=absolute --show-toplevel` 的 canonical 结果
  必须等于 supplied checkout root，因此嵌套目录不能借父仓库生成 catalog。
- catalog 使用 `ls-tree -rzt --full-tree`，校验每个 node 的 mode/type/OID、tree
  prefix topology 与 gitlink 40-hex object ID；catalog 绑定 checkout device/inode，
  scanner 打开的 root 必须匹配。每个物理 gitlink leaf/ancestor 必须是现存的
  non-symlink、non-reparse directory，gitlink 目录不会被递归扫描。

### Checkout capture window

- CheckoutCaptureCoordinator 与独立 CheckoutWriterCoordinator 都只在 POSIX 可用；
  Windows 显式 fail closed。capture 的 checkout root 与二者的 control root 都先
  strict-resolve 为现存 canonical directory，并拒绝 checkout/control 相等、嵌套或
  symlink-alias 后的物理重叠。writer 可在 checkout root 尚未 materialize 时先取得锁。
  使用同一 canonical control root 的 capture/writer（包括 symlink alias）共享唯一
  process mutex 与 flock lock domain；旧的 capture-owned writer API 已删除。
- control root 必须由当前 uid 拥有且 group/world 不可写。lock leaf 使用
  O_NOFOLLOW/O_NONBLOCK 打开，必须是当前 uid 拥有、0600、single-link regular file；
  control/lock identity 与 metadata 在 acquire 以及 capture/writer 边界重复检查。
- 每个 coordinator 必须收到 CheckoutAcquisitionBounds：finite process-local monotonic
  caller deadline，加上 trusted、nonblocking 且返回 exact bool 的 cancellation callback。
  它不解释 wall time、不持久化 authority、也不定义 schema。每次 acquire 只计算一次
  min(caller deadline, local lock cap)，同一 effective deadline 贯穿 process mutex 与
  POSIX flock；每次 contention wait request 最多 50 ms，且 callback 不在
  process-mutex condition lock 内执行。scheduler delay 与 blocking OS call 不构成
  hard 50 ms cancellation latency 保证。不存在零参数或无界 acquire fallback。
- capture session 在同一锁内重新生成 before catalog/snapshot，跨真实 dispatch 持有
  lease，再重新生成 after catalog/snapshot；catalog 不会逃出该 window。writer 成功
  body 的退出顺序是 lock/control integrity、cancellation/caller deadline；local cap
  只约束 acquisition。
  body 原始错误始终优先于取消、完整性或 release 故障，所有清理步骤仍会被尝试。
  cancellation callback 自身失败也会 fail closed 并释放 lease。
- 这只是 cooperating-writer primitive：advisory flock 不会阻止绕过 coordinator 的
  写者。生产 materializer 必须保证同一 checkout 的所有 clone/copy/reset/cleanup/
  mutation writer 都经由同一锁协议，并提供多进程、崩溃恢复和部署证明。
- 当前 bounds 只约束 lock acquisition，并在 writer body 正常返回后再 checkpoint；
  不会强制中断已在运行的 Git、scanner、capture 或 writer body。长 writer body 必须
  主动轮询 yielded bounds。生产 composition 仍须从 checkout 前已启动的唯一 current
  invocation 派生 bounds，并把同一 deadline/cancellation 贯穿 checkout/Git/scans；
  D7 与 current package 未闭合前不得接线。

### Gateway

agent_kernel_gateway.py 只实现 package-independent 的 injected sequencing seam：

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

codec、authority、policy、budget、journal 和 committer 都是注入边界。journal
begin 必须原子复核 authority ticket 并绑定唯一 opaque dispatch capability；该
capability 由 dispatcher 与所有 settlement 路径消费。任何
pre-dispatch 失败不 dispatch；prepare 后的控制/budget/intent 失败会关闭 descriptor，
并在已有 reservation 时释放它；取消同样清理资源但不伪造 terminal settlement。
exact completed replay 跳过全部 authority 和 dispatch；
pending replay 不重发。dispatch 后 SourceState 不可建立或出现 diff 时，正常 receipt
不会直接暴露，而是进入 injected unavailable/violation committer。D30 仍需决定生产
grant/intent/receipt/budget 的唯一 durable linearization；该 seam 不是 D30 resolution。

### R0 read

- `R0ReadPreparer` 只接受 closed `MaterializedSourceCaptureProvider`；checkout root、
  before SourceState 与 fresh after SourceState 必须来自同一个 provider，不再接受独立
  root/policy、raw gitlink catalog 或 stage hook。ReadSourceFileInput 只保存已校验的
  repository-relative path；PreparedDispatch 给 dispatcher 的 handle 不含 unresolved
  path、shell、network client、approval channel 或 secret handle。
- prepare 先在 provider capture lease 内取得完整 SourceState，在任何 leaf open 前
  验证 exact membership/type 以及 recorded ancestor/descendant topology，再逐组件固定
  leaf descriptor，并机械验证其 size/hash/executable 与 before entry 完全一致。
- excluded、unsafe 或不属于 before SourceState 的 path 在 leaf open 前拒绝。
  dispatcher 只能消费 authority 绑定的 capability 与 one-shot descriptor；读取上限
  为 expected size 加一，并再次验证 identity、size 和 hash。成功或失败都会关闭
  descriptor；路径在 prepare 后被替换也不能把读取重定向到新目标。
- prepared handle 同时拥有 leaf descriptor 与 capture session，lease 跨 Gateway 的真实
  dispatch 保持。prepare、pre-dispatch discard/cancellation、dispatch failure 和
  after-scan failure 都释放两项资源；descriptor 尚未 dispatch 就请求 after-capture
  会关闭资源并以 `PREPARED_READ_NOT_DISPATCHED` fail closed。
- capture_after 总是通过同一 provider 重新生成 catalog 与 snapshot；Gateway 对任何
  非空 diff 扣留正常结果。

## 明确未实现与阻断

- D28-D30 是 pending questions，当前只有 D28 active；它们分别覆盖 current package 发布物/exact pin、
  foundation closure 和 grant-to-receipt/budget durable linearization；它们 required before
  S3，当前 decision gate 因而 blocked。recommendation 不是 resolution，必须由用户依次
  决定。
- D23 已决定 package 必须由 Server 拥有，但 Server-owned current package 发布物、
  package manifest/version/digest、consumer exact pin 与双向 conformance 尚未实现；
  D28-D29 关闭前不得由 Worker 自造替代 package/schema。
- source-selection-policy、source-tree-manifest、change-set、tool-dispatch-intent、
  observation 等 schema 的权威 validator/codec。
- production materializer/composition 对 Git >=2.45 部署身份、exact-revision catalog、
  canonical checkout/control roots、all-writers cooperating window 与跨进程行为的接线和
  强制证明。内部 acquisition primitive 已接受 caller deadline/cancellation，但还没有
  production composition 从权威 current invocation 派生它，也没有把它贯穿
  copy/clone/Git/scanner/capture/运行中的 writer body。
- current-only Task/Attempt/session/lease authority store。
- D7 elapsed-consumption budget ledger。
- crash-safe dispatch journal、two-phase unbound CAS publish/bind 和 automatic Observation。
- ExecutionState、R1、Agent session runtime、Completion/Verifier/Gate、S4-S8 与 D22 gate。
- D27 default ratchet 只证明本切片没有增加未登记 legacy surface；当前仓库仍有
  inventoried legacy_v1，`legacy_absent=false`。当前 strict gate 不能作为最终完成
  证据：verifier 必须保留并 exact-load frozen baseline 才能枚举 semantic surfaces，
  但同一文件又是显式 high-signal surface
  `worker.004-frozen-contract-baseline`。当前 verifier 会保留完整 surface 观测，
  并以 `status=indeterminate`、`strict_catalog_self_reference`、exit 2
  fail closed；删除或修改 baseline 也因 exact frozen input 无效而 exit 2。
  cutover 前必须以显式 decision/ADR 修正 historical evidence 与 live forbidden
  surfaces 的分离；这项诊断不定义修正方案，不能刷新 baseline 或宣称当前
  `--require-absent` 已可达。

现有 AgentKernelDatabase 初始化 legacy_v1 Task schema，现有 observations 表也外键到
该 Task；它们不得被本 tracer 当成 current production journal。上述阻断关闭前，
不得加入 production feature flag、shadow route、fallback 或 legacy adapter。

## 验证

从 pullwise-worker 运行：

    python -m unittest \
      tests.test_agent_kernel_source_state \
      tests.test_agent_kernel_gitlinks \
      tests.test_agent_kernel_gateway \
      tests.test_agent_kernel_checkout_lifecycle \
      tests.test_agent_kernel_checkout_session_lifecycle \
      tests.test_agent_kernel_checkout_writer_lifecycle \
      tests.test_agent_kernel_checkout_writer \
      tests.test_agent_kernel_checkout_lock \
      tests.test_agent_kernel_checkout_window \
      tests.test_agent_kernel_r0_capture \
      tests.test_agent_kernel_r0_read \
      tests.test_agent_kernel_r0_gateway

当前工作树本地 Windows 完整 S3a 证据：Ran 114 tests，OK（35 个
POSIX/host-capability 用例按平台跳过）。Ubuntu 22.04 WSL 的 checkout/R0 POSIX
子集：Ran 65 tests，OK（2 个 host-capability 用例跳过），实际覆盖 flock、
symlink-alias lock domain、mutex/flock contention、deadline/cancellation 与 cleanup
faults。该 WSL 的 Git 低于 2.45，完整 gitlink 套件按设计 fail closed 为
SOURCE_GIT_VERSION_UNSUPPORTED；exact-SHA Ubuntu CI 仍必须用 Git >=2.45 覆盖 dirfd、
FIFO race、case-sensitive component collision、real Git catalog 和全仓测试。最终
发布证据必须绑定包含本文修订的 exact SHA，不能沿用过渡提交的结果。

Decision gate：

    python scripts/agent_first_decision_register.py check \
      --repo-root . --require-slice S3

当前结果为 blocked：register 本身 valid，26 resolved、3 pending、D2 inactive，active
question 为 D28；D28-D30 均 required before S3。只有用户依次作出 resolution 并更新
generated register/document/tests 后才能恢复 S3 ready。

D27 ratchet 可单独复核：

    python scripts/verify_agent_first_legacy_absence.py --workspace-root ..

默认结果应为 `ratchet_clean=true` 且 `legacy_absent=false`；它证明没有意外扩张，
不是 clean-break 完成证据。当前 `--require-absent` 会在完成 live observation 后，
因上述 frozen-baseline self-surface 返回 `strict_catalog_self_reference`、
`status=indeterminate` 并 exit 2；删除或修改 baseline 同样 exit 2。修正后的最终门
必须先由显式 decision/ADR 定义；现有 production-catalog regression tests 只锁定
fail-closed 诊断和 default ratchet，不锁定尚未决策的最终 gate 语义。

## 文件尺寸证据

- agent_kernel_source_state.py：385 行，内部 identity/diff 与全局 path topology。
- agent_kernel_source_scan.py：377 行，Ubuntu dirfd scanner。
- agent_kernel_source_scan_windows.py：196 行，开发平台 fallback。
- agent_kernel_gitlinks.py：356 行，repo-root/version/topology verified catalog。
- agent_kernel_checkout_lifecycle.py：140 行，required process-local acquisition bounds。
- agent_kernel_checkout_writer.py：73 行，独立 pre-materialization writer coordinator。
- agent_kernel_checkout_lock.py：346 行，POSIX bounded cooperating-writer lock。
- agent_kernel_checkout_window.py：319 行，atomic before/dispatch/after capture lease。
- agent_kernel_gateway.py：400 行，固定顺序 orchestration。
- agent_kernel_r0_capture.py：120 行，descriptor/capture lease ownership。
- agent_kernel_r0_read.py：361 行，provider-bound held-descriptor R0 read。
- tests/agent_kernel_capture_fakes.py：88 行，跨平台 capture provider test fixture。
- test_agent_kernel_source_state.py：381 行。
- test_agent_kernel_gitlinks.py：394 行。
- test_agent_kernel_checkout_lifecycle.py：132 行。
- test_agent_kernel_checkout_session_lifecycle.py：60 行。
- test_agent_kernel_checkout_writer_lifecycle.py：106 行。
- test_agent_kernel_checkout_writer.py：358 行。
- test_agent_kernel_checkout_lock.py：323 行。
- test_agent_kernel_checkout_window.py：384 行。
- test_agent_kernel_gateway.py：400 行。
- test_agent_kernel_r0_capture.py：222 行。
- test_agent_kernel_r0_read.py：381 行。
- test_agent_kernel_r0_gateway.py：274 行。
- scripts/verify_agent_first_legacy_absence.py：261 行，D27 default/strict verdict。
- test_agent_first_legacy_absence.py：296 行，一般 ratchet/strict 语义。
- test_agent_first_legacy_absence_hardening.py：343 行，production catalog fail-closed。
- tests/legacy_absence_test_support.py：198 行，隔离 workspace fixture。

AGENTS.md 与 MVP implementation design 都是已有 oversized 文档；它们承载 durable
boundary/status，不计作本切片新增实现模块的尺寸豁免。
