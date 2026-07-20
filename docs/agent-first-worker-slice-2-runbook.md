# Agent-First Worker Slice 2 运行与完成证据

日期：2026-07-20

## 当前结论

Slice 2 已实现 Task/Attempt/Supervisor 的 typed shadow foundation：完整状态矩阵、
SQLite version CAS、幂等事件、native/owner epoch、actor fencing、单槽 legacy 投影、
取消/发布竞态和 v1→v3 crash-safe migration 均有自动测试。它仍不接管 legacy v1
terminal publisher，不创建第二个本地队列，也不运行 Agent Kernel Task。

D1 已由用户选择 `pullwise_full_scan`，resolution digest 为
`ab117e7c86472b7ce57bf2433978df0efe1299353ad747b7eabbff723fec469a`；
D2 失活，`--require-slice S2` 通过。D3 已由用户选择
`mvp_r0_r1_reject_r2`，resolution digest 为
`0126d5ee3329c0f954e88e08979e8f0883086b3846315e2904cd7d323b97b07a`；D4 已由用户
选择 `field_by_field_ownership`，resolution digest 为
`b009c68af93c965837e562d57cd20328e037b5fca0da30cc694125e0fee79654`。D5 已由用户
选择 `per_control_transaction`，resolution digest 为
`859647945022b9d62bca4c6cf16b290c48e4e9bdb2f10700a40553194748b74a`。D6 已由用户
选择 `single_claim_owner_transaction`，resolution digest 为
`e1ad16c135ae5f0880123becdd640bf685c0f201b44dd941830590b0b39174d8`；它冻结 S4 的
claim write set，但在 S4 gate 通过前不改写当前 S2 shadow seam。D7 已由用户选择
`persist_elapsed_consumption`，resolution digest 为
`5d7916e9389c0203185fb7e2e64be49df0ea52557d875f661f5d0180e093f5ea`；它冻结 S4 的
时钟持久化语义，但在 S4 gate 通过前不改写当前裸 `monotonic_ms` schema/SQLite 或
legacy/runtime path。D8 已由用户选择 `task_active_attempt_fenced`，resolution digest 为
`e895f73c3a0962937cbab61b4c8037f9ccba9daa6e6de89d5004005dd830b98a`；它冻结 lease-loss
的 Task/Attempt 分层语义，但在 S4 gate 通过前不改写当前 shadow/runtime。当前 register
有 18 个 pending、7 个 resolved，唯一 active decision 是 D9。S3 blockers 保持 D11、
D15、D16、D17；S4 blockers 为 D9–D17。本文不把推荐项或当前实现当成这些决策。

## 已实现范围

### Typed reducer

- Task lifecycle 固定为
  `QUEUED|ACTIVE|WAITING_INPUT|WAITING_APPROVAL|FINALIZING|TERMINAL`，事件集合
  与第 10.1 节一致。Cartesian 测试遍历全部 state/event pair；未列出的 pair 拒绝
  `STATE_TRANSITION_INVALID`，terminal Task 拒绝 `TASK_ALREADY_TERMINAL`。
- `task.accepted` 是唯一无 prior Task 的 transition；`attempt.claimed` 原子产生
  `native_epoch+1`、新 current Attempt 和 `LEASED` 状态。
- FINALIZING 中同一 idempotency retry 是 no-op；新的权威 terminalization fact
  可以在当前 version append，并作为新控制事务恰好增加一次 version；只有
  outcome selection 字段是否改变由 `terminal_outcome_changed` 决定。
- terminalization reason 只允许八个冻结值；全部 guard 默认 fail closed，测试必须
  显式给出 authority、lease、budget、deadline、effect/tool quiescence 等事实。
- Attempt reducer 固定完整合法边和 terminal set；Cartesian 测试遍历全部
  Attempt state/state pair，terminal Attempt 无出边。

### Task Store 与 fencing

- `TaskStore` 使用每操作独立 SQLite connection 和 `BEGIN IMMEDIATE`。一个事件事务
  同时执行 expected task version、Attempt action、Task update、可选 publication row
  和 append-only event；affected row 不为一即失败。
- event digest 绑定 typed payload。相同 idempotency key 只允许同 task/type/digest
  exact retry，并返回原 event version；任何重绑都拒绝 `IDEMPOTENCY_CONFLICT`。
  SQLite migration 3 同时在数据库层全局唯一化 event idempotency key。
- `task.accepted` 的幂等摘要与 identity collision 检查绑定原始 `scan_id`；
  owner-incarnation event digest 绑定 `occurred_at`，改变任一值都不是 exact retry。
- `attempts.state_version` 单独 CAS；Task current Attempt/native epoch 必须匹配，不能
  修改历史 Attempt，也不能从 terminal Task 继续推进 Attempt。
- owner incarnation 在同一事务中递增 `owner_epoch`、绑定 exact session、更新
  Attempt owner pointer 并记录事件。actor fence 同时绑定 task/deletion version、
  lease/transport epoch、current Attempt/native epoch、owner ID/epoch/session；任一旧值
  分别以 stable fence code fail closed。
- 并发 claim 只有一个 current Attempt；并发 publish 只有一个 Task version CAS 和
  一个 `result_publications` row。cancel CAS 先赢时 stale success 为零；publish 先赢
  时 late cancel 得到 `TASK_ALREADY_TERMINAL`。
- 当前 pre-D8 shadow reducer 的 `outer_lease.fenced` 会把 Task 写成
  `terminal_kind=transport_abandoned`，同时把 Attempt 写成 `FENCED`，且不插入 Worker
  TaskResult publication；这是历史 scaffold，不是 D8 production contract。D8 要求
  lease loss 本身只原子 fence 精确的旧 transport/native Attempt 及其 actor/owner/session，
  让执行或收尾中的 Task 保持 `ACTIVE`/`FINALIZING`，terminal/result 字段保持不变，并将
  transport abandonment 记为独立的非结果证据。只有满足显式恢复资格谓词的 successor
  才可携带新 fence/epoch 接管；资格细节及最终 terminal authority/precedence 留给
  D9/D10。缺失的 abandonment contract 仍遵守 Slice 1
  `SPEC_GAP-TRANSPORT-CONTRACTS`，没有猜造 wire bytes；S4 gate 清零前不得修改或提升
  当前 runtime、schema、migration 1–3。

### SQLite migration 2/3

Migration 1 的 statement 和 digest 保持不变；migration 2 原子增加：

- `task_events.event_digest` 和 version lookup index；
- `tasks.terminalization_reason` 的精确 enum check；
- Attempt 的 `transport_binding/state_version/predecessor_checkpoint_generation/
  owner_session_id/lease_acquired_at/budget_reservation_id` 与 lookup index。

Migration 3 在不改变 migration 1/2 statement 或 digest 的前提下增加
`UNIQUE task_events(idempotency_key)`。测试覆盖真实 v1→latest 升级、v2→v3
commit 前 crash、干净重启只应用一次，以及含跨 Task 重复 key 的 v2 数据
fail closed 并完整保留 v2 user version/history/index 状态。

Migration 1 已有的 `budget_entries.monotonic_ms` 没有生产 writer/reader，只是 shadow
scaffold。D7 禁止跨 process/boot 比较或恢复该裸值；migration 1–3 bytes/digest 与当前
runtime 保持不变。D7 的 elapsed-consumption ledger、immutable absolute deadline
恢复和 process-local monotonic origin 重建，等待 S4 gate、版本化 contract 与后续
migration 落地。

## Legacy 单槽接入与回滚

生产 composition root 通过 `build_review_worker` 选择 exactly one Worker：

- 默认或 `PULLWISE_AGENT_KERNEL_SHADOW_ENABLED=false` 直接构造原
  `ReviewWorkerV1`；这是回滚路径。
- flag 为 true 时构造 `AgentKernelShadowReviewWorker`。它只在既有
  `persist_active_run_marker/clear_active_run_marker` 后读取 legacy marker、terminal
  outbox 和 success receipt，形成内存中的 typed one-slot projection。
- projection 永远报告 `maintains_local_queue=false`、`local_queue_depth=0`；active 时
  available slot 为零。同一 ACTIVE 生命周期冻结完整 job/run/lease/attempt identity，
  任一字段漂移或不同 run 未先清槽都 fail closed。
- marker/outbox identity 必须 exact 匹配 job/run/lease/attempt。marker 消失但 outbox
  尚在时保持 FINALIZING 并暴露 shadow error；exact-bound success receipt 已赢时，
  stale outbox 不再阻止投影回到 IDLE。
- shadow 异常可通过 `agent_kernel_shadow_error` 观察，但不会让 legacy authority
  停机；bridge 没有网络写入、TaskResult publisher 或 Task runner。进程启动恢复
  persisted active job 后会立即重读 marker/outbox，避免 shadow 错留在 IDLE。

## D5–D8 决议与后续 Slice 边界

D5 已冻结为 `per_control_transaction`：每个新应用的 Task 控制事件事务令
`task_version` 恰好 `+1`，checkpoint、ledger、owner 等多个 pointer 即使在同一
事务一起变化也只共享一个新版本。精确幂等重试复用原版本，拒绝/回滚不增版；
新 FINALIZING terminalization fact 即使不改变已选 outcome 仍是一次控制事务并
增版。TaskStore 会拒绝 0 或大于 1 的版本跳跃。

S3 尚未开始。Policy Gateway、Source/ExecutionState、Observation、工具 dispatch 和
Agent 权限仍没有接线；不得把本 Slice 的 actor fence helper 宣称为已完成 Gateway。

## 验证命令

从 `pullwise-worker/` 运行：

```bash
python3 -m unittest \
  tests.test_agent_kernel_state_reducer \
  tests.test_agent_kernel_task_store \
  tests.test_agent_kernel_owner_fencing \
  tests.test_agent_kernel_task_races \
  tests.test_agent_kernel_supervisor \
  tests.test_agent_kernel_slice2_migration \
  tests.test_agent_kernel_storage \
  tests.test_agent_kernel_cas_concurrency \
  tests.test_agent_kernel_storage_boundaries \
  tests.test_agent_kernel_shadow_store \
  tests.test_agent_kernel_canonical \
  tests.test_agent_kernel_schema_registry \
  tests.test_agent_kernel_contract_semantics \
  tests.test_agent_kernel_legacy_mapping \
  tests.test_ci_cross_repo_contract
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_output_contracts.py
python3 scripts/agent_first_slice0_baseline.py check --repo-root .
python3 scripts/verify_agent_first_contract_baseline.py check --workspace-root ..
python3 scripts/agent_first_decision_register.py check --repo-root . --require-slice S2
python3 scripts/check_agent_kernel_wheel.py
```

## 最近验证结果

- Python 3.12 Agent Kernel S1+S2 聚焦测试：94/94 通过。
- Python 3.12 Worker 全量：746 tests 通过，5 个既有条件性 skip。
- output contracts 4/4；Slice 0 baseline `compatible`；cross-repo legacy baseline
  `compatible`，14 个固定 Server/Web/Worker runner 全部通过。
- decision register 为 `valid_pending`，18 个 pending、7 个 resolved，S2 无 blocker；
  D3-D8 已解决，active decision D9；S3 blockers 为 D11、D15、D16、D17，S4 blockers
  为 D9–D17。
- 隔离 wheel 安装成功；从源码树外完成 13 schema/3 fixture inventory、CAS round-trip
  和 Task `QUEUED→ACTIVE` transition。
- GitHub Actions
  [#826](https://github.com/GoPullwise/pullwise-worker/actions/runs/29715819480)
  对当前完整证据提交 `c346db08fdcacf65270b06809eb83e6dd35ab723` 通过。

## 文件规模与模块化报告

以下为本 Slice 新增或修改文件的物理行数（完成证据写入前）：

| 文件 | 行数 | 职责 |
|---|---:|---|
| `pullwise_worker/agent_kernel_state.py` | 377 | 纯 Task/Attempt reducer |
| `pullwise_worker/agent_kernel_task_store.py` | 366 | Task/version/owner 事务 |
| `pullwise_worker/agent_kernel_task_validation.py` | 81 | reducer adapter 与 contract 校验 |
| `pullwise_worker/agent_kernel_attempt_store.py` | 147 | Attempt 持久化与边应用 |
| `pullwise_worker/agent_kernel_event_log.py` | 61 | append-only idempotency journal |
| `pullwise_worker/agent_kernel_fencing.py` | 55 | actor/lease/epoch fence |
| `pullwise_worker/agent_kernel_task_records.py` | 156 | typed records 与 row codec |
| `pullwise_worker/agent_kernel_supervisor.py` | 151 | one-slot legacy projection |
| `pullwise_worker/agent_kernel_review_worker.py` | 121 | rollbackable composition seam |
| `pullwise_worker/agent_kernel_migrations.py` | 331 | atomic migration registry |
| `pullwise_worker/main.py` | 124 | 单 Worker composition root |
| `scripts/check_agent_kernel_wheel.py` | 170 | installed S1+S2 smoke |
| `tests/test_agent_kernel_state_reducer.py` | 272 | Cartesian reducer contract |
| `tests/test_agent_kernel_task_store.py` | 398 | persistence/idempotency/publication |
| `tests/test_agent_kernel_owner_fencing.py` | 147 | owner replacement 与 exact-session fence |
| `tests/test_agent_kernel_task_races.py` | 193 | claim/cancel/publish schedules |
| `tests/test_agent_kernel_supervisor.py` | 261 | slot/outbox/runtime projection |
| `tests/test_agent_kernel_slice2_migration.py` | 245 | v1/v2/v3 upgrade, dirty-data and crash recovery |
| `tests/test_agent_kernel_storage.py` | 391 | migration count regression |
| `tests/test_agent_kernel_cas_concurrency.py` | 71 | CAS publish convergence regression |
| `tests/test_ci_cross_repo_contract.py` | 76 | CI installed-wheel requirement |
| `tests/test_agent_first_decision_register.py` | 375 | D5-D8 resolution/digest gate |
| `tests/test_agent_first_decision_register_gate.py` | 392 | slice blocker and normative reference gate |
| `contracts/agent-first/spec-decision-register.json` | 419 | machine decision source |
| `contracts/agent-first/worker-slice-0-baseline.json` | 290 | composition anchor evidence |
| `docs/agent-first-worker-current-code-map.md` | 96 | generated code-map view |
| `docs/agent-first-worker-mvp-implementation-design.md` | 1792 | D5-D8 normative state semantics |
| `docs/agent-first-worker-spec-decision-register.md` | 559 | generated decision view |
| `docs/agent-first-worker-slice-1-runbook.md` | 226 | D1-D8 gate 状态同步 |
| `docs/agent-first-worker-slice-2-runbook.md` | 216 | 本完成证据 |
| `AGENTS.md` | 1067 | durable Agent-First rules（非代码阈值） |

全部新增手写生产、测试和维护脚本不超过 400 行；没有 401–600 行说明项或超过
600 行例外。S2 未修改 18,531 行的 `review_worker_v1.py`，因此 oversized legacy
baseline、职责和 extraction seam 均未增长。只修改 124 行 composition root 并同步
Slice 0 的 anchor；11 个 oversized legacy file 的 ratchet 未放宽。
