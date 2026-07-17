# Agent-First Worker Slice 1 运行与完成证据

日期：2026-07-18

## 当前结论

Slice 1 已实现可执行的 schema、canonical JSON、SQLite 和 CAS 影子存储基础，
且不接管 legacy v1 terminal path。已定义部分的 contract、property、并发、损坏与
crash-recovery 测试通过；但 Slice 1 暂不标记为规范完成，因为第 2/4/5 节点名的
两个 transport schema 没有字段级规范，不能安全猜测后注册。

当前模块没有由 daemon 或 terminal publisher 调用。关闭影子写入即可回到原有
legacy 行为；`agent-kernel/` 可保留为只读诊断数据，不要求数据库降级。

## 已实现范围

- Pullwise JCS Profile 1：严格 UTF-8、NFC、ASCII object key、safe integer、
  duplicate-key 拒绝、跨进程 golden digest 和 key permutation property test。
- Digest-bound schema registry：每个 schema 的 canonical SHA-256、闭合 `$ref`、
  受控 JSON Schema 子集、valid/invalid/unknown-field/enum/size golden cases。
- Contract semantics：policy digest/invariant、TaskRecord pointer/terminal coherence、
  TaskRequest source identity、Requirement、Charter、Interaction、Waiver 时间窗。
- legacy scan identity：domain-separated SHA-256、原始 `scan_id` 逐字节绑定和
  injectable collision fail-closed test。
- SQLite：每 Worker 独立目录、WAL/foreign key/FULL/busy timeout、digest-bound
  migration history、未知高版本拒绝，以及第 4.4 节最小表集合。
- CAS：流式限额、独立 hash/size 校验、no-clobber publish、file/directory fsync、
  SQLite 后置引用、artifact rebinding 拒绝、verified read 和 idle+TTL orphan GC。
- Shadow Store：schema validation -> canonical bytes -> CAS 的窄接口；读取时重新
  校验 digest、canonical bytes、声明 schema 和 semantic invariant。

已注册 schema：

`actor/v1`、`availability-ref/v1`、`budget-entry/v1`、`content-ref/v1`、
`effective-execution-policy/v1`、`interaction-request/v1`、
`interaction-response/v1`、`legacy-v1-task-mapping/v1`、
`requirement-entry/v1`、`task-charter/v1`、`task-record/v1`、
`task-request/v1`、`waiver-event/v1`。

## 规范缺口和边界

`SPEC_GAP-TRANSPORT-CONTRACTS`：设计只点名 `transport-receipt/v1` 和
`transport-abandonment-record/v1`，没有定义字段、identity、digest、幂等键或
golden fixture。全仓文本检索没有发现另一份定义。因此 registry 不包含这两个
schema；先补规范再实现，不能从调用点或 Agent 推断 wire shape。

`SPEC_GAP-WAIVER-KEYRING`：`effective-execution-policy/v1` 的字段表没有固定
keyring 的来源或结构。当前 Pullwise MVP profile 可通过空 issuer 集合拒绝所有
waiver；通用 signed-waiver acceptance 仍需后续明确 keyring 契约。

D1 仍是 decision register 的 active decision。S1 本身不受 `--require-slice`
阻断，但开始 S2 前必须取得显式 D1 resolution；不得以当前代码或推荐项代替用户
选择。

## 指标

影子接口提供线程安全 snapshot，名称固定为：

- `agent_kernel_shadow_contract_writes_total`
- `agent_kernel_shadow_contract_write_bytes_total`
- `agent_kernel_shadow_contract_reads_total`
- `agent_kernel_shadow_contract_read_bytes_total`
- `agent_kernel_shadow_contract_validation_failures_total`
- `agent_kernel_shadow_cas_conflicts_total`
- `agent_kernel_shadow_cas_corruption_total`

这些指标目前只属于未接入 daemon 的影子组件；后续接线不得把指标成功当成
terminal 成功，也不得让新 Gate 放宽 legacy QA。

## 故障处理

- `schema_migration_*`：停止影子初始化，保留数据库和对象；修正兼容性后重试。
  migration 使用单事务，注入的 commit 前崩溃会完整回滚，重启只应用一次。
- `CAS_CORRUPT`：停止消费该 ContentRef，记录 corruption metric，禁止静默覆盖、
  重 hash 或改写 ref。生产接线后应把实例置为需要 operator 处理。
- `CONTENT_REF_CONFLICT`：同一 `(task_id, artifact_id)` 已绑定不同 immutable
  metadata/bytes；拒绝新绑定，不能用新 artifact 内容覆盖旧 identity。
- `collect_orphans` 只可在 Worker idle 时运行，并使用大于零的运营 TTL。它只删除
  超龄、未引用的 digest object，以及严格匹配 `object-*.tmp` 的私有 regular
  staging file；返回的 staging identity 使用 `tmp:` 前缀。
- CAS 顺序保持：durable temp bytes -> durable digest path -> durable temp unlink ->
  SQLite object/binding transaction。任何注入点都不会产生“DB 有 ref、bytes 不在”的
  状态。

## 验证命令

从 `pullwise-worker/` 运行：

```bash
python3 -m unittest \
  tests.test_agent_kernel_canonical \
  tests.test_agent_kernel_schema_registry \
  tests.test_agent_kernel_contract_semantics \
  tests.test_agent_kernel_legacy_mapping \
  tests.test_agent_kernel_storage \
  tests.test_agent_kernel_shadow_store
python3 -m unittest discover -s tests -p 'test_*.py'
python3 scripts/check_output_contracts.py
python3 scripts/agent_first_slice0_baseline.py check --repo-root .
python3 scripts/verify_agent_first_contract_baseline.py check --workspace-root ..
python3 scripts/agent_first_decision_register.py check --repo-root .
python3 scripts/agent_first_decision_register.py check --repo-root . --require-slice S2
python3 -m pip wheel --no-deps --no-build-isolation --wheel-dir /tmp/pullwise-wheel .
```

最后一条只验证 wheel 可构建且包含 schema/fixture package data，不安装或发布它。
decision check 的 pending/blocked 是规范状态证据，不应伪装成测试故障。

## 文件规模与模块化报告

以下为本 Slice 新增或修改文件的物理行数。

| 文件 | 行数 | 职责 |
|---|---:|---|
| `pullwise_worker/agent_kernel_canonical.py` | 130 | canonical JSON |
| `pullwise_worker/agent_kernel_contract_semantics.py` | 203 | 跨字段语义不变量 |
| `pullwise_worker/agent_kernel_database.py` | 239 | SQLite 生命周期与 migration |
| `pullwise_worker/agent_kernel_identity.py` | 64 | legacy identity mapping |
| `pullwise_worker/agent_kernel_migrations.py` | 263 | 原子 migration registry |
| `pullwise_worker/agent_kernel_object_store.py` | 379 | CAS side effects |
| `pullwise_worker/agent_kernel_schema_registry.py` | 187 | digest-bound schema loading |
| `pullwise_worker/agent_kernel_schema_validation.py` | 331 | 受控 schema 子集 |
| `pullwise_worker/agent_kernel_shadow_store.py` | 126 | validation/CAS composition |
| `tests/test_agent_kernel_canonical.py` | 128 | canonical contract/property |
| `tests/test_agent_kernel_contract_semantics.py` | 187 | semantic invariants |
| `tests/test_agent_kernel_legacy_mapping.py` | 67 | identity/collision |
| `tests/test_agent_kernel_schema_registry.py` | 212 | schema/golden/fail-closed |
| `tests/test_agent_kernel_shadow_store.py` | 164 | shadow boundary/metrics |
| `tests/test_agent_kernel_storage.py` | 358 | migration/CAS/crash/concurrency |
| `contracts/agent-task/v1/actor.schema.json` | 56 | schema |
| `contracts/agent-task/v1/availability-ref.schema.json` | 24 | schema |
| `contracts/agent-task/v1/budget-entry.schema.json` | 53 | schema |
| `contracts/agent-task/v1/content-ref.schema.json` | 32 | schema |
| `contracts/agent-task/v1/effective-execution-policy.schema.json` | 93 | schema |
| `contracts/agent-task/v1/interaction-request.schema.json` | 35 | schema |
| `contracts/agent-task/v1/interaction-response.schema.json` | 24 | schema |
| `contracts/agent-task/v1/legacy-v1-task-mapping.schema.json` | 13 | schema |
| `contracts/agent-task/v1/requirement-entry.schema.json` | 63 | schema |
| `contracts/agent-task/v1/task-charter.schema.json` | 70 | schema |
| `contracts/agent-task/v1/task-record.schema.json` | 76 | schema |
| `contracts/agent-task/v1/task-request.schema.json` | 83 | schema |
| `contracts/agent-task/v1/waiver-event.schema.json` | 31 | schema |
| `contracts/agent-task/v1/schema-registry.json` | 70 | registry |
| `contracts/agent-task/v1/fixtures/canonical-json.json` | 15 | frozen fixture |
| `contracts/agent-task/v1/fixtures/schema-golden-control.json` | 110 | frozen fixture |
| `contracts/agent-task/v1/fixtures/schema-golden.json` | 261 | frozen fixture |
| `pyproject.toml` | 28 | wheel package data |
| `AGENTS.md` | 931 | 持久工程规则（非代码阈值） |
| `docs/agent-first-worker-slice-1-runbook.md` | 153 | 本证据 |

全部新增手写生产/测试文件不超过 400 行；没有 401–600 行说明项，没有超过 600 行
的新增文件，也没有生成/第三方/原子 registry 例外需要登记。S1 未触及任何超过
600 行的遗留生产或测试模块，因此 legacy baseline、职责和 extraction seam 无变化。
