# Agent-First Worker Slice 0 当前实现证据

状态：只读事实基线，不是 Agent Kernel 设计决策
机器源：`contracts/agent-first/worker-slice-0-baseline.json`

本文件记录当前 Worker 模块、30 项 phase/progress registry 和手写文件规模 ratchet。它不把当前模块命名为未来 Task Owner、Gate、SourceState 或其他尚待确认的 Agent Kernel 组件，也不授权生产实现。

在 Worker 仓库执行：

```text
python scripts/agent_first_slice0_baseline.py check --repo-root .
```

文件规模 ratchet 以 Git commit `904165f3bed784faaa209ca80e33214c7b07f909` 为固定 genesis；检查需要可达的完整历史，并读取每个 genesis 路径的全部已提交 blob revision。baseline 只能向下更新；任何已提交的 source-only 降低也会降低 floor，降到 400 行及以下会永久退休该路径。

<!-- BEGIN GENERATED WORKER SLICE 0 BASELINE -->
> Generated from `worker-current-implementation-2026-07-17` with `physical-lf/v1`. Do not edit this block by hand.

Captured Worker HEAD `c707003960b853ec87fd4dfc7323b0d3d1d63528` is informational only. This is current-implementation evidence; it does not assign future Agent Kernel ownership or authorize production implementation.

### Current implementation map

| Current scope | Paths | Current responsibilities | Ownership/call boundary | Candidate extraction seam |
|---|---|---|---|---|
| `cli-composition` | `pullwise_worker/main.py`, `pullwise_worker/__main__.py` | Dispatch CLI commands and compose WorkerConfig, PullwiseClient, and exactly one legacy or shadow-observing ReviewWorker instance. | Lifecycle and readiness commands are imported lazily; the run path owns only composition. | Keep this as the narrow composition root while legacy command implementations remain in their current modules. |
| `configuration-http-control-plane` | `pullwise_worker/_main_part_01_bootstrap.py` | Parse instance configuration, isolate provider paths, collect host metrics, build strict-v1 registration and heartbeat HTTP requests, and normalize control-plane errors. | Owns transport/configuration mechanics; review semantics remain in review_worker_v1.py. | Separate host/provider configuration and metrics from the Pullwise HTTP client and strict-v1 payload builders. |
| `readiness-doctor` | `pullwise_worker/_main_part_07_readiness_doctor.py` | Evaluate provider, plan, runtime, path, disk, and Codex readiness and expose doctor and device-login flows. | Provides operator-facing doctor and device-login readiness results; the live daemon independently derives provider_ready from Codex SDK health and quota before WorkerState.can_lease(). | Separate plan/provider validation, Codex readiness probing, and operator-facing doctor rendering. |
| `host-lifecycle` | `pullwise_worker/_main_part_08_lifecycle_cleanup.py` | Run service actions and log streaming, supervise lifecycle commands, update the Worker and Codex CLI, and perform contained instance cleanup. | Provides host service, log, update, uninstall, and cleanup operations; review job state and phase dispatch remain in review_worker_v1.py. | Split log streaming, watcher command state, staged update/install, destructive instance cleanup, and idle cache pruning. |
| `active-slot-daemon` | `pullwise_worker/review_worker_v1.py` | Maintain the one active job slot, register/heartbeat/lease loop, cancellation supervisor, current job lifecycle, and phase dispatch. | The Server owns the global queue and outer lease; this module owns one in-process active slot and no local queue. | Extract active-marker recovery, control-loop transport, cancellation supervision, and phase orchestration behind narrow state interfaces. |
| `codex-runtime-isolation` | `pullwise_worker/codex_sdk_runtime.py`, `pullwise_worker/review_worker_v1.py` | Contain worker-scoped Codex runtime resources, event scopes, token usage, bounded SDK calls, approval denial, and model-turn workspace publication. | The SDK owns App Server lifecycle; Worker code owns containment, fencing, bounded waits, and declared output publication. | Move remaining SDK client/quota and model-turn publication responsibilities out of review_worker_v1.py into runtime-focused modules. |
| `fixed-review-pipeline` | `pullwise_worker/review_worker_v1.py`, `pullwise_worker/agentic_execution.py` | Use the 30-entry PIPELINE_PHASES registry to order progress and dispatch semantic and mechanical work; prepare phase-specific prompts, fan out domain reviewers, and derive bounded intent execution capabilities. | prepare_workspace executes before its no-op registry branch; terminal submission executes after submit_result_envelope is marked complete; active-slot cleanup runs in run_job's finally path. Codex produces semantic turn outputs, while Worker code validates and publishes declared outputs and enforces ordering, limits, and artifact contracts. | Extract phase registry/dispatch, prompt construction, fanout, bundle planning, and intent execution into cohesive one-way modules. |
| `source-evidence-quality-artifacts` | `pullwise_worker/review_worker_v1.py` | Freeze repository inventory, protect disposable validation source, validate evidence/report bindings, run legacy QA, and materialize/upload versioned artifacts. | Current inventory and QA are legacy full-scan facts; this map does not assign future SourceState, Gate, or Agent Kernel ownership. | Separate source inventory/integrity, intent workspace validation, QA/report validation, and artifact manifest/upload responsibilities. |
| `durable-terminal-publication` | `pullwise_worker/review_worker_v1.py` | Persist active-run identity, recover unfinished publication state, journal immutable terminal result payloads, replay retryable submissions, and record cancellation supersession. | This is the sole current terminal-result outbox path; it recovers publication transactions, not mid-pipeline semantic execution. | Extract active-slot persistence, terminal outbox validation/replay, and cancellation supersession into a durable publication module. |
| `debug-audit` | `pullwise_worker/review_worker_v1.py`, `pullwise_worker/debug_bundle_audit.py` | Build the current Worker debug bundle and independently audit bundle structure, counters, finding/evidence bindings, and redaction boundaries. | Debug evidence excludes source-bearing bundles and is distinct from audit download and future cross-end Assembly semantics. | Separate bundle input loading, rule groups, issue aggregation, and CLI/report rendering; keep builder and independent auditor distinct. |
| `current-run-eta` | `pullwise_worker/current_run_eta.py`, `pullwise_worker/review_worker_v1.py` | Estimate whole-scan remaining time from the current run dependency graph and publish sanitized progress snapshots. | Uses current-run monotonic observations only and does not own phase semantics or cross-run forecasting. | Keep the estimator independent; move only orchestration-specific work-unit wiring behind an adapter if the phase registry is extracted. |

### Current PIPELINE_PHASES registry

| Order | Phase | Progress ceiling |
|---:|---|---:|
| 1 | `prepare_workspace` | 3 |
| 2 | `start_codex_app_server` | 7 |
| 3 | `initialize_codex_connection` | 10 |
| 4 | `check_codex_auth` | 12 |
| 5 | `bootstrap_helper_scripts` | 17 |
| 6 | `inventory_repository` | 24 |
| 7 | `token_budget` | 27 |
| 8 | `repo_map` | 33 |
| 9 | `risk_routing` | 39 |
| 10 | `bundle_planning` | 43 |
| 11 | `bundle_packing` | 47 |
| 12 | `reviewer_fanout` | 70 |
| 13 | `reviewer_json_validation` | 73 |
| 14 | `location_validation` | 76 |
| 15 | `clustering_and_voting` | 81 |
| 16 | `intent_test_validation` | 82 |
| 17 | `intent_mining` | 84 |
| 18 | `intent_test_planning` | 86 |
| 19 | `validation_workspace_prepare` | 88 |
| 20 | `intent_test_writing` | 90 |
| 21 | `intent_test_running` | 92 |
| 22 | `intent_test_failure_analysis` | 94 |
| 23 | `validator_disproof` | 96 |
| 24 | `final_report_json` | 97 |
| 25 | `render_markdown_report` | 98 |
| 26 | `qa_gate` | 99 |
| 27 | `hash_artifacts` | 99 |
| 28 | `upload_artifacts` | 100 |
| 29 | `submit_result_envelope` | 100 |
| 30 | `cleanup_active_job` | 100 |

### Handwritten file-size ratchet

The inventory covers every Git-tracked regular file above 400 physical lines that matches the fixed handwritten code/test/maintenance suffix, name, or extensionless-executable catalog. `oversized_legacy` is the >600 grandfathered baseline; `review_trigger_existing` is the existing 401-600 review-trigger range. Any count drift or unregistered trigger file fails verification.

| Path | Kind | Classification | Physical lines | Current responsibilities | Candidate extraction seam |
|---|---|---|---:|---|---|
| `pullwise_worker/review_worker_v1.py` | `production` | `oversized_legacy` | 18531 | Strict-v1 daemon, active slot, Codex runtime bridge, fixed review pipeline, integrity/QA, artifacts, terminal outbox, debug, and checkout utilities. | Control loop and active slot; runtime/quota; phase registry and executors; intent validation; evidence/QA; artifacts/result/debug; checkout and storage utilities. |
| `tests/test_review_worker_v1.py` | `test` | `oversized_legacy` | 12958 | Legacy broad contract and behavior coverage for review_worker_v1.py. | Move cases by contract or failure mode into focused active-slot, pipeline, intent, artifact, terminal-publication, and checkout suites. |
| `pullwise_worker/_main_part_08_lifecycle_cleanup.py` | `production` | `oversized_legacy` | 1898 | Service/log commands, lifecycle watcher, staged updates, uninstall, contained cleanup, and idle cache/log pruning. | Log streaming; watcher state; update/install; uninstall containment; idle workspace/cache/log cleanup. |
| `tests/test_agentic_execution.py` | `test` | `oversized_legacy` | 1724 | Agentic execution, intent source materialization, workspace integrity, preflight, repair, and evidence contract coverage. | Split capability discovery, source materialization, integrity, preflight, execution, repair, and result-contract cases. |
| `pullwise_worker/debug_bundle_audit.py` | `production` | `oversized_legacy` | 1566 | Load debug bundle evidence, execute cross-artifact audit rules, aggregate issues, and render machine/Markdown reports. | Bundle loading; lifecycle/progress checks; finding/evidence checks; intent checks; report rendering and CLI. |
| `pullwise_worker/_main_part_01_bootstrap.py` | `production` | `oversized_legacy` | 1485 | Host/provider setup, safe file helpers, metrics, Worker configuration, strict-v1 payload builders, HTTP client, and uninstall entry compatibility. | Host dependencies/provider environment; safe IO/metrics; configuration; protocol payloads; HTTP transport/errors. |
| `tests/test_lifecycle_readiness_regressions.py` | `test` | `oversized_legacy` | 939 | Terminal-result outbox replay, cancellation supersession and conflict handling, durable active markers and upload manifests, pre-claim recovery and readiness, post-receipt cleanup ordering, idle workspace and mirror cleanup, and uninstall containment regressions. | Split terminal outbox, supersession, conflict, and durability cases from active-marker recovery/readiness and workspace, mirror, and uninstall cleanup cases. |
| `tests/test_codex_sdk_runtime_regressions.py` | `test` | `oversized_legacy` | 819 | Codex SDK timeout, cancellation, event scope, runtime health, shutdown, and archive regressions. | Split bounded RPC/runtime health, event scopes, turn lifecycle, archive, and shutdown failure suites. |
| `tests/test_lifecycle_deadline_cancellation_regressions.py` | `test` | `oversized_legacy` | 799 | Absolute deadline, cancellation supervision, child process termination, and terminal publication race regressions. | Split deadline propagation, cancellation state/events, subprocess termination, and terminal race cases. |
| `tests/test_debug_bundle_audit.py` | `test` | `oversized_legacy` | 784 | Debug bundle directory and ZIP audit coverage across identity, artifacts, progress, findings, validation and intent evidence coherence, size limits, and insufficient-evidence rejection. | Split bundle loading and size-limit cases from progress/artifact, finding/validation, intent-evidence, and insufficient-evidence cases. |
| `tests/test_intent_execution_resource_regressions.py` | `test` | `oversized_legacy` | 743 | Bundle resource limits and intent subprocess, repair, executable preflight, and approved-snapshot binding regressions. | The existing test classes are direct seams for separate bundle-limit, subprocess, repair, executable, and binding files. |
| `tests/test_codex_usage_ledger.py` | `test` | `review_trigger_existing` | 553 | Thread-cumulative Codex token attribution and run/phase usage ledger regressions. | Keep cohesive unless phase aggregation and concurrent-thread attribution need independent suites. |
| `pullwise_worker/_main_part_07_readiness_doctor.py` | `production` | `review_trigger_existing` | 548 | Provider/plan/host readiness evaluation plus doctor and Codex login presentation. | Plan/provider validation, readiness probes, and operator presentation are the natural seams if growth is required. |
| `tests/test_reviewer_fanout_concurrency.py` | `test` | `review_trigger_existing` | 538 | Reviewer plan-cap rejection, low-memory concurrency reduction, ETA critical-path and live-scheduling behavior, fatal sibling cancellation, thread-archive failure, and one transient-capacity retry with concurrency reduction. | Separate scheduling and ETA behavior from fatal cancellation/archive and transient-capacity retry behavior if the suite grows. |
| `tests/test_agentic_bundle_planning.py` | `test` | `review_trigger_existing` | 532 | Semantic grouping validation, exact coverage, bounded rendering/splitting, assignments, and plan resource limits. | Separate grouping contract, renderer/splitting, assignment derivation, and resource-limit suites if growth is required. |
| `tests/test_worker_main.py` | `test` | `review_trigger_existing` | 504 | CLI dispatch and host lifecycle/readiness/update/uninstall command contracts. | Split CLI parser/dispatch from lifecycle, update, uninstall, and doctor command tests if growth is required. |
| `tests/test_current_run_eta.py` | `test` | `review_trigger_existing` | 469 | Dependency scheduling, resource concurrency, retry rewiring, confidence, deadline, and sanitized ETA snapshot tests. | Keep estimator tests cohesive; split scheduling from snapshot/confidence behavior only if growth is required. |
| `pullwise_worker/codex_sdk_runtime.py` | `production` | `review_trigger_existing` | 463 | Symlink-safe SDK log IO, event/turn usage scoping, runtime resource health/close, bounded calls, and identifier validation. | Keep the runtime resource aggregate cohesive; safe IO and token/event accounting are the first seams if new duties are added. |

### Generated file exceptions

| Path | Physical lines | Digest | Provenance | Reason | Considered split seam | Owner | Removal condition |
|---|---:|---|---|---|---|---|---|
| `pullwise_worker/_generated_agent_task_contract.py` | 7120 | `aa5dda6c4646875bbe74078624a22c08957a7f0098606584464b94a24fd301de` | `pullwise-server@43ca421c862772a2e000d617ef0c2f1b83759590:pullwise_server/agent_first_contract_bundle_python.py` | D28 requires immutable Server-generated Python wrapper and Worker cannot hand-edit it. | Server generator owns partitioning and Worker must preserve exact bytes. | Pullwise Server current-contract package owner | Remove when Worker no longer checks in this wrapper or it falls to <=400 physical lines. |
<!-- END GENERATED WORKER SLICE 0 BASELINE -->
