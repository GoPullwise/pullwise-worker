# Pullwise Worker Agent Notes

## Problem Solving Discipline

When resolving failures or regressions, do not default to adding diagnostic
patches or surface-level workarounds first. Identify the root cause from the
current code and available evidence, then fix that root cause. Add diagnostics
only when they directly support root-cause isolation or make a verified fix
safer to operate.

## Worker Host Platform

Pullwise worker installs target Ubuntu 22.04 hosts. Worker runtime, doctor,
update, restart, uninstall, and cleanup changes may assume Linux/systemd
behavior available on Ubuntu 22.04, including `useradd`, `chown`, `chmod`,
`sudo`/`runuser`, logrotate, and systemd unit management. Do not add macOS or
Windows worker installer behavior.

## Worker Provider Isolation

Each worker instance owns its own Codex runtime state. Do not let a
worker use global Codex binaries, global auth, root auth, or another
worker instance's config.

- Provider commands must resolve to absolute paths inside the current worker
  `worker_root`, for example:
  - `$worker_root/.local/bin/codex`
  - `$worker_root/.codex/bin/codex`
- Review execution and Codex quota refresh must enforce this at runtime before
  starting the app-server; do not fall back to `codex` from `PATH` or any global
  Codex binary.
- Provider subprocesses must run with instance-scoped environment values:
  - `HOME=$worker_root`
  - `USERPROFILE=$worker_root`
  - `CODEX_HOME=$worker_root/codex-home`
  - `XDG_CONFIG_HOME=$worker_root/.config`
  - `XDG_CACHE_HOME=$worker_root/.cache`
  - `XDG_DATA_HOME=$worker_root/.local/share`
  - `PATH` with this worker's `$worker_root/.local/bin`,
    `$worker_root/.codex/bin`, `$worker_root/codex-home/bin` before the base service path
- Do not inherit global provider credentials such as root `HOME`,
  root `CODEX_HOME`, or global API-key based readiness when checking or running
  provider work.
- `doctor`, provider readiness checks, provider review execution, and semantic
  fallback execution must all use the same instance-scoped provider environment.
- A fresh install followed by no manual action and then `doctor` must report the
  same provider readiness state as the installer reported. In particular,
  `doctor` must not become ready by seeing auth/config from root, a global CLI
  install, or another worker.

Multiple workers on the same server are supported only if each worker uses its
own `service_home` for Codex binaries, config, cache, and auth state.

## Codex Review Worker Architecture

`../codex_full_repo_review_worker_spec_v1_2_FULL_SELF_CONTAINED.md` is the
source of truth for worker review behavior. The current worker is the
`review-worker-protocol/v1` Codex full-repository review worker; do not
reintroduce alternate review pipelines, per-task CLI review flows, local job
queues, or worker-side prefetch compatibility.

Hard invariants:

- One worker instance may process at most one active job at a time.
- The worker must not maintain `pending_jobs`, `prefetched_jobs`, `next_job`, or
  any local job queue.
- The worker must call `POST /v1/workers/register` during startup with v1
  capability, isolation, platform, and one-slot/no-prefetch metadata before it
  enters the heartbeat/lease loop.
- The worker may call `POST /v1/workers/{worker_id}/lease` only when
  `active_job == null`, state is `idle`, and the local queue depth is zero.
  The request must include `review-worker-protocol/v1`, `active_jobs = 0`,
  `available_job_slots = 1`, `maintains_local_queue = false`,
  `local_queue_depth = 0`, and required capabilities for full repo scan, Codex
  App Server, isolated Codex home, progress events, cancellation, and intent
  test validation.
- A busy, cancelling, or finishing worker must heartbeat with zero available job
  slots through `POST /v1/workers/{worker_id}/heartbeat` and must not claim
  another job. The heartbeat payload must use the fixed v1 shape:
  `protocol_version`, `status`, `active_run_id`, `concurrency`,
  `codex_app_server`, and active-run `progress`; do not make legacy
  `running_jobs`/`active_job_ids` the worker-facing protocol. Idle heartbeats
  must report `active_jobs = 0` and `available_job_slots = 1`; active heartbeats
  must report `active_jobs = 1`, `available_job_slots = 0`, and a progress
  snapshot whose `run_id` matches `active_run_id`.
- The worker HTTP client must require the fixed v1 heartbeat shape directly.
  Do not translate legacy `running_jobs`, `active_job_ids`, or partial heartbeat
  inputs into v1 payloads.
- Each worker instance owns an isolated `WORKER_ROOT`, lock file, `CODEX_HOME`,
  `CODEX_SQLITE_HOME`, Codex auth/config/log/session/cache directories,
  workspace root, artifact root, and worker log.
- Multiple workers on one host must not share Codex config, auth, sqlite state,
  app-server process, sockets, workspaces, artifacts, service user runtime, or
  mutable lifecycle files.
- Worker runtime targets Linux/Ubuntu 22.04 only. Do not add Windows or macOS
  worker runtime behavior.

Codex execution rules:

- Use one instance-scoped Codex App Server per worker; prefer stdio transport or
  a worker-unique Unix socket.
- Do not use `codex exec` for review phases and do not launch one Codex CLI
  process per reviewer/subtask.
- Each run has one root Codex thread. Logical subagents are sequential Codex
  turns by default; the default active Codex turns per worker is one.
- Start the app-server with the worker-owned `CODEX_HOME` and
  `CODEX_SQLITE_HOME`, then initialize the JSON-RPC connection before turns.
- After root thread initialization, store the `thread_id` on the active job and
  include it as `codex_app_server.active_thread_id` in busy heartbeats until the
  job reaches a terminal state. Idle heartbeats must keep `active_thread_id`
  null.
- Capture Codex events to `codex-events.jsonl` and treat completion/error events
  as authoritative for terminal handling.
- Implement a fixed approval handler even when approval policy is `never`:
  allow writes only under `.codex-review/**` in the main repo or the disposable
  validation workspace, allow Python standard-library helper scripts under
  `.codex-review/tools/*.py`, allow read-only repository inspection commands,
  allow bounded project test commands only inside the disposable validation
  workspace, and deny source modifications in the main repo, installs,
  downloads, network access, branch changes, commit, push, and access to other
  worker directories.

## Adaptive Repository Scan Rules

`../auto-adjust-plan.md` defines the current adaptive scan upgrade. Keep these rules in force for worker changes in this area:

- Keep the full-repository pipeline fixed. Do not change `PIPELINE_PHASES`, create repo-type-specific pipelines, let adapters skip core phases, or let adapters decide terminal status, QA gates, upload/result envelopes, or final finding confidence.
- `repo-profile.json` is an optional, mechanical, best-effort side artifact produced from inventory/file-tree evidence only. It must not call Codex, depend on semantic artifacts, fail `inventory_repository`, enter `PHASE_JSON_OUTPUTS`, or enter `REQUIRED_COMPLETED_ARTIFACT_FILES`.
- Repo profile generation and helper scripts must use the Python standard library only and remain Python 3.10 compatible. Do not add `tomli`, `PyYAML`, dependency-audit parsing, package installs, or external scan services.
- If profile generation fails, keep `inventory.json`, log `repo_profile_skipped` to `worker.log.jsonl`, and continue the phase.
- Treat adapters as strategy providers only. They may provide signals, fallback risk rules, skip patterns, grouping hints, prompt emphasis, and intent-test preferences; they must not become a scheduler or artifact-contract owner.
- Risk tier priority is `hard skip/generated/binary > semantic explicit route > profile fallback > generic default`. Broad semantic routes must not promote generated, vendor, cache, minified, binary, or lock-file source into review bundles.
- Do not overwrite `risk-routing.json` with deterministic fallback routes. Put merged/explanatory downstream routing in optional run-local artifacts such as `effective-risk-routing.json` when implemented.
- Keep `bundle-plan.json` at `bundle-plan/v1` and `coverage.json` at `coverage/v1`. Conservative grouping may use path/name/entrypoint/test affinity only; do not build dependency graphs or call graphs for grouping.
- Keep reviewer ids limited to `security`, `correctness`, `test_gap`, and `correctness_lite` unless a future migration updates fanout, validation, clustering, reporting, QA, and backward compatibility together.
- Adaptive prompt context may be appended only when `repo-profile.json` is valid. It must not request extra required artifacts, change required outputs, or introduce reviewer ids.
- Intent command policy remains the first gate and must not be loosened. Runnable preflight may only skip unsafe/not-runnable commands with explicit reasons; it must not permit installs, `npx`, network calls, provider initialization, external scanners, or dependency setup.
- A generated intent-test failure is evidence only. It must not automatically classify a finding as `confirmed_bug` or increase confidence before the allowed failure-analysis classification step.
- Worker-side adaptation may tilt internal emphasis within server-owned job policy, but must not invent or raise repository limits, wall-time limits, token budgets, model policy, or reasoning effort.
Review pipeline rules:

- Full repository scan only; this is not a diff or PR review.
- Do not install third-party dependencies or call external scan/lint/review
  tools such as Semgrep, SonarQube, CodeQL, reviewdog, or MegaLinter.
- Helper scripts must use Python 3 standard library only, write only under
  `.codex-review/runs/**`, and perform mechanical tasks only.
- Codex performs semantic judgment. Helper scripts must not decide whether a
  finding is real, severe, exploitable, or worth fixing.
- Reviewer JSON validation must reject malformed reviewer outputs and use a
  Codex repair turn before retrying validation; never silently default missing
  schemas or `findings` arrays into verified reviewer artifacts. The phase
  error artifact is `json-errors.json`.
- Required phases follow the v1.2 spec: prepare workspace, start app server,
  initialize, auth check, bootstrap helper scripts, inventory, token budget,
  repo map, risk routing, bundle planning/packing, reviewer fanout, reviewer
  JSON validation, location validation, clustering/voting, intent-test
  validation, intent mining, intent-test planning, validation workspace
  preparation, intent-test writing, intent-test running, intent-test failure
  analysis, validator disproof, final report JSON, markdown render, QA gate,
  hash artifacts, upload artifacts, submit envelope, and cleanup active job.
- Codex semantic phases must use phase-specific prompts/templates that name the
  role, inputs, required output files, schema discipline, and safety rules for
  that phase. Do not send generic `Phase: <name>` prompts for repo mapping,
  risk routing, reviewer fanout, clustering, intent mining/planning/writing,
  test failure analysis, validator disproof, or final report generation.
- Semantic phases and semantic output repair require an initialized Codex
  App Server and root thread. Do not satisfy a semantic phase by writing local
  fallback artifacts when the app-server or thread is missing.
- A phase may emit `phase_completed` only after its required output files or
  directories exist and local validation passes. Schema-bound phase outputs must
  be parseable JSON objects with the expected `schema_version`; hash-artifact
  completion requires an `artifact-manifest/v1` object with an `items` list.
- Intent-driven tests are allowed only for selected P0/P1 high-value candidate
  findings. Generated tests must live in the disposable validation workspace or
  `.codex-review/generated-tests/**`, must not install dependencies or use
  network, and must execute with `cwd` inside the disposable validation repo.
  Local phase validation must enforce the v1.2 intent artifact contract:
  `intent-map.json` has `bundle_id` and `behavioral_contracts`, every planned,
  generated, raw, and analyzed test has a unique `test_id`, plan/source/result
  `linked_finding_ids` reference IDs present in `clusters.json`, generated test
  files exist, non-skipped raw runs include a command plus existing stdout and
  stderr log artifacts, and final classifications use only the allowed enum:
  `confirmed_bug`, `plausible_bug`, `test_oracle_wrong`, `test_harness_error`,
  `environment_error`, `flaky_or_nondeterministic`, `dependency_missing`,
  `unclear_requirement`, `passed_no_bug_reproduced`, and
  `skipped_not_runnable`.
  When `intent_test_validation.enabled` is false in the canonical job policy,
  the worker must skip the intent child phases without Codex turns or local test
  execution after writing the parent intent validation config artifact.
  The worker must record stdout/stderr under `intent/test-output/`, include
  those logs in artifact manifests with unique artifact ids, and report
  skipped/error/timeout cases as degraded intent-test evidence, not as direct
  job failure. Failing generated tests must be classified before they influence
  confidence. If Codex does not materialize `intent-test-results.json`, the
  fallback must preserve raw project test runs using only the
  `intent-test-result/v1` classification enum, with `confidence = 0.0` and no
  positive finding confidence impact.
- Intent artifact repair must handle existing malformed Codex outputs, not only
  missing files. Keep the strict validators strict, but normalize common model
  shape variants at the repair/fallback boundary before retrying validation; for
  example, `generated_tests` may arrive as a string path list that must become
  object entries with `test_id`, `path`, and `artifact_refs`, and analyzed
  results may arrive with `outcome`/`raw_status` instead of canonical
  `status`/`classification`/`confidence`/`evidence` fields. Skipped, blocked,
  timeout, or environment-limited intent tests are degraded evidence and must
  not fail the whole repository scan after they can be represented in
  `intent-test-result/v1`.
- QA must fail completed runs when intent-test validation is enabled and
  `intent-test-results.json` is missing unless an explicit skipped reason is
  recorded in the intent validation, planning, source, or raw run artifacts.
- Required completed artifacts are `report.md`, `report.agent.json`,
  `coverage.json`, `token-budget.json`, `qa.json`, `artifact-manifest.json`,
  `codex-events.jsonl`, `worker.log.jsonl`, and `progress.log.jsonl`.
- Optional v1 artifact catalog entries must be preserved when present,
  including `raw_reviewer_output`, `verified_reviewer_output`,
  `intent_test_output`, and the intent planning/source/result artifacts.
- Artifact manifest/upload entries must include stable v1 metadata:
  `artifact_id`, supported `kind`, `name`, `media_type`, `schema_id`,
  `schema_version = v1`, `encoding = utf-8`, `compression = none`, `required`,
  `storage`, `sha256`, and `size_bytes`.
- Intent source repair must run before strict validation is retried for
  `intent_test_writing`. Preserve strict validation, but normalize common
  Codex output variants such as generated test objects with `test_file`,
  `filename`, `created_files`, or materialized generated-test files but no
  canonical `path`.
- Required artifact upload failures must remain terminal for completed uploads.
  Optional artifact upload failures, especially large debug/log artifacts that
  hit server/proxy body limits, should be recorded as artifact-manifest warnings
  and must not prevent result envelope submission when required artifacts were
  uploaded.
- For completed runs, the terminal result envelope must reuse the exact
  artifact manifest that was uploaded before result submission. Do not refresh
  log/debug-bundle hashes while building the completed result envelope; final
  log uploads after accepted result submission are best-effort replacements and
  must not change the manifest used for terminal result validation.
- The worker persists `uploaded-artifact-manifest.json` as the upload-success
  snapshot. Result envelopes must merge this snapshot over the mutable
  `artifact-manifest.json` by `artifact_id`, because `artifact-manifest.json`
  can be rewritten by late logs, debug-bundle refresh, terminal snapshots, or
  local mutation after artifact upload.
- Artifact IDs in one manifest must be unique, and upload must reject
  duplicates before posting any artifact, because artifact upload idempotency is
  keyed by `run_id + artifact_id`.
- Artifact upload must reject manifest names that resolve outside the artifact
  directory before reading or posting any file.
- Artifact upload must reject any manifest-listed artifact whose file is missing
  before posting any artifact; optional manifest entries must not be silently
  skipped once listed.
- Artifact storage URLs must exactly reference the active artifact run directory:
  `/v1/review-runs/<run_id>/artifacts/<artifact_id>`.
- `artifact-manifest.json` must use `artifact-manifest/v1` and its `run_id`
  must match the active artifact directory/run before QA or upload can pass.
- Post run progress through `POST /v1/review-runs/{run_id}/events`, upload
  artifacts through `POST /v1/review-runs/{run_id}/artifacts`, and upload
  artifacts before submitting the terminal result envelope through
  `POST /v1/review-runs/{run_id}/result`.
- Every posted progress event must include `protocol_version`, `run_id`,
  `worker_id`, positive monotonic `sequence`, `timestamp`, supported
  `event_type`, `phase`, `severity`, `message`, and `progress` with
  `overall_percent`, `current_phase_percent`, `status`, and the worker-owned
  ordered `steps` snapshot for the full flow this worker is executing.
- Active heartbeat `progress` snapshots must include the v1 counter set
  (`source_like_files_*`, `bundles_*`, `reviewer_runs_*`,
  `intent_tests_*`, `validator_candidates_*`, and `artifacts_*`) plus
  `active_unit`, even when counters are zero.
- The worker is the source of truth for jobscan detail flow shape. Keep phase
  definitions, ordering, labels, and step counts on the worker side, report them
  through progress events and heartbeat snapshots, and do not rely on web or
  server code to recreate this worker's pipeline. Future workers may report a
  different flow; their reported steps must remain internally consistent with
  their own events.
- Long-running phases must post `progress_updated` events, not only
  `phase_started`/`phase_completed`. `reviewer_fanout` progress data must
  include `reviewer_runs_total` and `reviewer_runs_completed`;
  `intent_test_validation` progress data must include `intent_tests_total`,
  `intent_tests_written`, and `intent_tests_run`; `upload_artifacts` progress
  data must include `artifacts_total` and `artifacts_uploaded` and update after
  each successfully uploaded artifact.
- V1 `cancel_run` commands must mark the active job `cancelling`, keep
  available job slots at zero, emit exactly one `run_cancel_requested` event
  before the terminal `run_cancelled` event, interrupt the active Codex turn,
  and still submit a valid cancelled result envelope with partial artifacts
  when possible.
- Unrepaired QA gate failure must not be submitted as a completed run. It must
  emit `phase_failed` for `qa_gate`, then `qa_failed`, then
  `run_partial_completed`, upload terminal artifacts, and submit a
  `partial_completed` result envelope.
- Failed, cancelled, and partial-completed jobs must still submit valid
  terminal envelopes when possible, including required `qa`, `worker_log`, and
  either `error_report` or partial `report.agent` artifacts.

Plan policy:

- Subscription plan policy still controls the model, timeout, repository limits,
  and core reasoning effort.
- Core semantic phases use the plan reasoning effort. Non-core phases use the
  same model with medium reasoning effort.

## Job Slot And Upload Discipline

Each worker instance has exactly one job execution slot. It does not maintain a
local job queue and must claim a new server-side job only after the current job
has finished. The only job slot must not be occupied by avoidable job-level retry sleep or`r`ncleanup IO.

- Final result upload should attempt the immediate request once. If submission
  fails or local manifest/upload-snapshot validation blocks submission, write
  `result-envelope.json` plus `result-submit-failed.json` or
  `result-submit-blocked.json`, keep `active_job` in `finishing`, continue
  heartbeat with the active job id, and do not create, scan, migrate, or`r`n  resubmit saved result-submission queue files.
- Result upload payloads should use gzip compression for large JSON. Keep server
  gzip JSON support and worker compression thresholds aligned.
- Do not add unbounded job-level `time.sleep()` retry loops to `run_job()` or other code
  that holds the only job execution slot.
- Cleanup should run only when the worker is idle or on a low-priority
  background path. Do not run checkout/log cleanup before heartbeat/claim in the
  hot loop.

## Checkout And Cache Discipline

Repository checkout performance depends on the worker mirror cache.

- Server/worker responsibility is fixed: the server owns job/scan state,
  repository access validation, short-lived clone token issuance, and lease
  payload fields (`clone_url`, branch, commit, `clone_token`, and
  `repositoryLimits`); the worker owns materializing that repository inside its
  isolated workspace before inventory or review phases run.
- A claimed v1 job may provide an already materialized `checkout_dir` only for
  tests or trusted local integration paths. Production workers must be able to
  clone from the server-provided `clone_url` and short-lived `clone_token` when
  no `checkout_dir` is present.
- After copy or clone, the worker must verify that the repository workspace
  contains real repository files excluding `.git` and `.codex-review`. Empty
  checkouts must fail during `prepare_workspace`, not later during semantic
  phases such as `repo_map`.
- After clone/copy and before starting the Codex App Server, the worker must
  enforce the claimed job `repositoryLimits` against the materialized checkout.
  Repository limit failures must not wait until `inventory_repository`; they
  must submit `REPOSITORY_TOO_LARGE` with `preflight.repositoryStats`,
  `preflight.repositoryLimits`, `repositoryLimitExceeded = true`, and concrete
  `repositoryLimitReasons` so scan history, audit bundles, and quota handling
  have evidence immediately.
- Repository limit preflight stats must report the full eligible checkout
  totals, not the first threshold-crossing values. For example, a 1,028-file
  checkout with `maxFiles = 200` must report `fileCount = 1028`, not `201`;
  only set `scanStoppedEarly` when the stats are actually truncated.
- Keep repository mirrors under `.pullwise-repo-cache` and protect that runtime
  directory from ordinary checkout cleanup.
- Commit-specific jobs should use shallow fetch into the mirror plus a shared
  no-checkout worktree/checkout, not a full fresh clone per job.
- Do not include clone tokens in mirror path names, logs, or persistent config.
  Token-sensitive remote URLs may be used for fetch, but persisted cache
  identity and diagnostics must be redacted.

## Review Evidence Discipline

The v1 worker reports findings through `report.agent.json` and the stable result
envelope. Findings shown to users must be grounded in concrete repository files
and line locations, include a clear failure scenario or risk, and provide an
actionable recommendation. Weak or uncertain observations belong in appendix or
internal artifacts, not as confirmed findings.

Main `report.agent.json.findings` are a mechanically validated surface. Each main finding must be backed by `validated-findings.json.validated_findings` with status `confirmed`, `plausible`, or `validated`. Report repair must demote unbacked findings into `appendix_findings` with `demoted_from_main_findings = true`, recompute `summary.overall_risk` from retained main findings, and rebuild `next_agent_tasks` only from retained main findings. QA must fail non-empty main findings when `validated-findings.json` is missing, malformed, or lacks a matching accepted validation entry.

Terminal result envelopes must include the stable v1 summary shape:
`overall_risk`, `result_status`, `finding_counts`, `coverage`, and
`top_findings`. Do not submit top-findings-only summaries.

Do not require derived topology artifacts for worker output. New review logic,
protocols, reports, tests, and documentation must depend on the stable envelope
and versioned artifacts.

Completed-run artifacts must be real outputs produced by the run, not
placeholders synthesized during hashing or envelope construction. The QA gate
must validate `report.md`, `report.agent.json`, `coverage.json`,
`token-budget.json`, `qa.json`, source-file hashes from inventory, intent-test
classifications and generated-test artifact refs, and the final
`artifact-manifest.json` required kinds, sizes, and SHA-256 values before
upload/result submission.

Worker lifecycle endpoints for operator commands, logs, and registry state
are not the core review protocol. Do not route new review leasing, progress,
artifact, or result behavior through `/worker/...` compatibility paths.

## Server-Controlled Agent Policy

The worker can advertise local provider capability, but review policy comes from
server-provided subscription plan agent configs attached to the claimed job.

- Treat `PULLWISE_PROVIDER_CHAIN` as local installed capability/order, not as the
  source of plan policy.
- `doctor` must load free/pro/max agent configs from the server. If they cannot
  be loaded or validated, do not silently fall back to the local provider chain.
- A claimed v1 job must include canonical `model_profile`,
  `review_request.policy`, `review_request.budget`, and `repositoryLimits`;
  reject jobs that omit required server-owned policy instead of using local
  defaults.
- Drive Codex from `model_profile.default_model`, `model_profile.*_effort`,
  `review_request.policy`, and `review_request.budget`; do not fall back to
  `agentConfig.codex` or `agentConfig.reviewWorker` for model, effort, timeout,
  deadline, or intent-test validation-limit decisions.
- Reject jobs whose `review_request.policy` allows source modification,
  dependency install, network access, or non-standard-library helper scripts.
- Repository size limits used by worker preflight come from the job's
  `repositoryLimits`, not from local plan assumptions.

## Readiness Semantics

Provider readiness is plan-aware and provider-specific.

- `provider_ready` means at least one provider required by the loaded plan
  configs is ready.
- `codex_ready` is the Codex login/readiness state required for accepting jobs.
- A quota/readiness failure must set `provider_ready = false` locally so the
  worker does not call lease, while the idle v1 heartbeat still keeps the
  server-required idle concurrency shape (`active_jobs = 0`,
  `available_job_slots = 1`) and carries the failure through `codex_ready`,
  `codex_quota`, `doctor_status`, and `last_error`.
- Only check providers required by the loaded plan configs.
- Login/auth instructions printed by `doctor` must use the same instance-scoped
  command environment documented above.

## Instance-Scoped Files

Do not share mutable runtime files between worker instances.

- `service_home`, `checkout_root`, `log_dir`, provider home/config/cache dirs,
  and service-user commands must stay instance-scoped.
- Cleanup/update/doctor helpers must operate inside the configured worker roots.
- Avoid fixed global paths for checkouts, logs, provider state, or auth files
  unless they are only base directories containing per-worker subdirectories.

## Delete Instance Cleanup

Admin Delete instance must remove the worker-host resources owned by that worker
instance, not merely let the server hide the worker from admin lists. Cleanup
must cover the instance service unit, wrapper, logrotate entry, `/etc` config,
service user when safe, `service_home` under `/var/lib/pullwise-worker`, `log_dir`
under `/var/log/pullwise-worker`, and other instance-scoped runtime files.

Do not assume Pullwise Server is installed on the same host as the worker. The
worker host needs a local lifecycle manager, watcher, supervisor, or finalizer to
own destructive cleanup and status reporting. A worker process can participate
by acknowledging an uninstall command, but durable deletion should not rely only
on the process that is deleting itself; stopped or degraded workers still need a
host-local owner that can remove resources and report failure/success.

A single host may run multiple Pullwise worker instances. Each worker instance
must have its own watcher/supervisor and must not reuse another instance's
worker process, watcher process, systemd unit, service user, env file, config
directory, `service_home`, `log_dir`, runtime directory, uninstall marker, or
provider state. Instance-specific names and paths must be derived from the safe
worker id.

The watcher is the worker-host role that monitors and controls its paired worker
instance. Treat watcher reliability as a lifecycle boundary: the watcher service
must be enabled and started before the worker service, and its systemd unit must
be ordered before the paired worker unit. The watcher may stop and remove the
worker service and instance-owned resources during lifecycle cleanup.

Watcher ownership is strictly one-to-one with a worker instance. Different
worker instances on the same host must have different watcher ids, service
names, runtime directories, env/config paths, and lifecycle markers; they must
never share a watcher service.

Once a watcher service has successfully started, do not stop, disable, remove,
or uninstall it from any non-delete path, including update, restart, cleanup,
manual/local worker uninstall, and post-watcher-start install failures. Watcher
self-removal is allowed only for an admin-initiated Delete instance lifecycle
operation, and only after the watcher has first ensured the paired worker
instance service and instance resources have been successfully uninstalled.
## Debug Bundle Contract

A debug bundle is not the audit bundle and must never silently fall back to the audit bundle.

- A real debug bundle combines worker-side live evidence and server-side evidence for the same scan/job/run.
- Worker-side evidence should include run-local logs, Codex app-server events, progress logs, run-state, phase outputs, terminal QA/error reports, and the worker artifact manifest. It must not include repository source files, raw API keys, unredacted environment dumps, or unrelated worker-instance state.
- Server-side evidence should include only scoped records for the same scan/job/run: scan/job/attempt/run identifiers, phase/progress/error snapshots, review-run events, artifact metadata/storage references, quota state, and relevant timestamps. It must not include full database dumps, secrets, other users' data, or unrelated scans.
- The UI must disable or omit debug bundle actions when no real debug_bundle artifact/server debug bundle endpoint exists. Do not substitute /scans/{scanId}/audit-bundle.zip as a debug zip URL.
- Tests should protect this contract: missing debugBundleUrl must not produce an audit-bundle URL, and server/worker tests must verify failed runs still expose a real debug_bundle artifact or explicit absence.
