# Agent-First Worker Slice 2 运行与完成证据

日期：2026-07-18

## 当前结论

Slice 2 已实现 Task/Attempt/Supervisor 的 typed shadow foundation：完整状态矩阵、
SQLite version CAS、幂等事件、native/owner epoch、actor fencing、单槽 legacy 投影、
取消/发布竞态和 v1→v2 crash-safe migration 均有自动测试。它仍不接管 legacy v1
terminal publisher，不创建第二个本地队列，也不运行 Agent Kernel Task。

D1 已由用户选择 `pullwise_full_scan`，resolution digest 为
`ab117e7c86472b7ce57bf2433978df0efe1299353ad747b7eabbff723fec469a`；
D2 失活，`--require-slice S2` 通过。D3 已由用户选择
`mvp_r0_r1_reject_r2`，resolution digest 为
`0126d5ee3329c0f954e88e08979e8f0883086b3846315e2904cd7d323b97b07a`；当前
active decision 是 D4。进入 S3 前仍须解决 D4、D11、D15、D16、D17，本文不把
推荐项或当前实现当成这些决策。

## 已实现范围

### Typed reducer

- Task lifecycle 固定为
  `QUEUED|ACTIVE|WAITING_INPUT|WAITING_APPROVAL|FINALIZING|TERMINAL`，事件集合
  与第 10.1 节一致。Cartesian 测试遍历全部 state/event pair；未列出的 pair 拒绝
  `STATE_TRANSITION_INVALID`，terminal Task 拒绝 `TASK_ALREADY_TERMINAL`。
- `task.accepted` 是唯一无 prior Task 的 transition；`attempt.claimed` 原子产生
  `native_epoch+1`、新 current Attempt 和 `LEASED` 状态。
- FINALIZING 中同一 idempotency retry 是 no-op；新的权威 terminalization fact
  可以在当前 version append，只有 outcome selection 改变才加 version。
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
- `attempts.state_version` 单独 CAS；Task current Attempt/native epoch 必须匹配，不能
  修改历史 Attempt，也不能从 terminal Task 继续推进 Attempt。
- owner incarnation 在同一事务中递增 `owner_epoch`、绑定 exact session、更新
  Attempt owner pointer 并记录事件。actor fence 同时绑定 task/deletion version、
  lease/transport epoch、current Attempt/native epoch、owner ID/epoch/session；任一旧值
  分别以 stable fence code fail closed。
- 并发 claim 只有一个 current Attempt；并发 publish 只有一个 Task version CAS 和
  一个 `result_publications` row。cancel CAS 先赢时 stale success 为零；publish 先赢
  时 late cancel 得到 `TASK_ALREADY_TERMINAL`。
- `outer_lease.fenced` 只写 `transport_abandoned` terminal kind 和 FENCED Attempt，
  不插入 Worker TaskResult publication。缺失的 abandonment contract 仍遵守 Slice 1
  `SPEC_GAP-TRANSPORT-CONTRACTS`，没有猜造 wire bytes。

### SQLite migration 2

Migration 1 的 statement 和 digest 保持不变；migration 2 原子增加：

- `task_events.event_digest` 和 version lookup index；
- `tasks.terminalization_reason` 的精确 enum check；
- Attempt 的 `transport_binding/state_version/predecessor_checkpoint_generation/
  owner_session_id/lease_acquired_at/budget_reservation_id` 与 lookup index。

测试从真实 v1 schema 原地升级，并在 v2 commit 前注入 crash：失败后 user version、
history 和 columns 仍为 v1；干净重启只应用一次 v2。

## Legacy 单槽接入与回滚

生产 composition root 通过 `build_review_worker` 选择 exactly one Worker：

- 默认或 `PULLWISE_AGENT_KERNEL_SHADOW_ENABLED=false` 直接构造原
  `ReviewWorkerV1`；这是回滚路径。
- flag 为 true 时构造 `AgentKernelShadowReviewWorker`。它只在既有
  `persist_active_run_marker/clear_active_run_marker` 后读取 legacy marker、terminal
  outbox 和 success receipt，形成内存中的 typed one-slot projection。
- projection 永远报告 `maintains_local_queue=false`、`local_queue_depth=0`；active 时
  available slot 为零。不同 run 未先清槽就出现时 fail closed。
- marker/outbox identity 必须 exact 匹配 job/run/lease/attempt。marker 消失但 outbox
  尚在时保持 FINALIZING 并暴露 shadow error；exact-bound success receipt 已赢时，
  stale outbox 不再阻止投影回到 IDLE。
- shadow 异常可通过 `agent_kernel_shadow_error` 观察，但不会让 legacy authority
  停机；bridge 没有网络写入、TaskResult publisher 或 Task runner。

## D5 与后续 Slice 边界

S2 的一个 reducer transition 当前对应一个 provisional control transaction；这是
实现第 10.1 节状态 CAS 所需的局部单位，不把 D5 的“每 control transaction”选项
冻结为后续规范。S4 composite mutation 开始前必须解决 D5，再决定 checkpoint、
ledger、owner 等多个 pointer 的最终 version 单位。

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

- Python 3.10/3.12 Agent Kernel S1+S2 聚焦测试：76/76 通过。
- Python 3.12 Worker 全量：728 tests 通过，4 个既有条件性 skip。
- output contracts 4/4；Slice 0 baseline `compatible`；cross-repo legacy baseline
  `compatible`，14 个固定 Server/Web/Worker runner 全部通过。
- decision register 为 `valid_pending`，S2 无 blocker；D3 已解决，active decision D4。
- 隔离 wheel 安装成功；从源码树外完成 13 schema/3 fixture inventory、CAS round-trip
  和 Task `QUEUED→ACTIVE` transition。
- GitHub Actions CI
  [#808](https://github.com/GoPullwise/pullwise-worker/actions/runs/29639124148)
  对 S2 实现提交 `f6a4fc61d949b1b5e4e10f92d1a346451fb0c647` 通过。

## 文件规模与模块化报告

以下为本 Slice 新增或修改文件的物理行数（完成证据写入前）：

| 文件 | 行数 | 职责 |
|---|---:|---|
| `pullwise_worker/agent_kernel_state.py` | 386 | 纯 Task/Attempt reducer |
| `pullwise_worker/agent_kernel_task_store.py` | 374 | Task/version/owner 事务 |
| `pullwise_worker/agent_kernel_task_validation.py` | 81 | reducer adapter 与 contract 校验 |
| `pullwise_worker/agent_kernel_attempt_store.py` | 147 | Attempt 持久化与边应用 |
| `pullwise_worker/agent_kernel_event_log.py` | 61 | append-only idempotency journal |
| `pullwise_worker/agent_kernel_fencing.py` | 55 | actor/lease/epoch fence |
| `pullwise_worker/agent_kernel_task_records.py` | 156 | typed records 与 row codec |
| `pullwise_worker/agent_kernel_supervisor.py` | 148 | one-slot legacy projection |
| `pullwise_worker/agent_kernel_review_worker.py` | 116 | rollbackable composition seam |
| `pullwise_worker/agent_kernel_migrations.py` | 318 | atomic migration registry |
| `pullwise_worker/main.py` | 124 | 单 Worker composition root |
| `scripts/check_agent_kernel_wheel.py` | 170 | installed S1+S2 smoke |
| `tests/test_agent_kernel_state_reducer.py` | 239 | Cartesian reducer contract |
| `tests/test_agent_kernel_task_store.py` | 323 | persistence/idempotency/publication |
| `tests/test_agent_kernel_owner_fencing.py` | 147 | owner replacement 与 exact-session fence |
| `tests/test_agent_kernel_task_races.py` | 193 | claim/cancel/publish schedules |
| `tests/test_agent_kernel_supervisor.py` | 198 | slot/outbox/runtime projection |
| `tests/test_agent_kernel_slice2_migration.py` | 102 | v1 upgrade/crash recovery |
| `tests/test_agent_kernel_storage.py` | 358 | migration count regression |
| `tests/test_ci_cross_repo_contract.py` | 41 | CI installed-wheel requirement |
| `contracts/agent-first/worker-slice-0-baseline.json` | 290 | composition anchor evidence |
| `docs/agent-first-worker-current-code-map.md` | 96 | generated code-map view |
| `docs/agent-first-worker-slice-1-runbook.md` | 175 | D1/S2 gate 状态同步 |
| `docs/agent-first-worker-slice-2-runbook.md` | 169 | 本完成证据 |
| `AGENTS.md` | 979 | durable S2 rules（非代码阈值） |

全部新增手写生产、测试和维护脚本不超过 400 行；没有 401–600 行说明项或超过
600 行例外。S2 未修改 18,531 行的 `review_worker_v1.py`，因此 oversized legacy
baseline、职责和 extraction seam 均未增长。只修改 124 行 composition root 并同步
Slice 0 的 anchor；11 个 oversized legacy file 的 ratchet 未放宽。
