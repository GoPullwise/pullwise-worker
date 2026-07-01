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
  `service_home`, for example:
  - `$service_home/.local/bin/codex`
  - `$service_home/.codex/bin/codex`
- Provider subprocesses must run with instance-scoped environment values:
  - `HOME=$service_home`
  - `USERPROFILE=$service_home`
  - `CODEX_HOME=$service_home/.codex`
  - `XDG_CONFIG_HOME=$service_home/.config`
  - `XDG_CACHE_HOME=$service_home/.cache`
  - `XDG_DATA_HOME=$service_home/.local/share`
  - `PATH` with this worker's `$service_home/.local/bin`,
    `$service_home/.codex/bin` before the base service path
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

Review pipeline rules:

- Full repository scan only; this is not a diff or PR review.
- Do not install third-party dependencies or call external scan/lint/review
  tools such as Semgrep, SonarQube, CodeQL, reviewdog, or MegaLinter.
- Helper scripts must use Python 3 standard library only, write only under
  `.codex-review/runs/**`, and perform mechanical tasks only.
- Codex performs semantic judgment. Helper scripts must not decide whether a
  finding is real, severe, exploitable, or worth fixing.
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
- A phase may emit `phase_completed` only after its required output files or
  directories exist and local validation passes. Schema-bound phase outputs must
  be parseable JSON objects with the expected `schema_version`; hash-artifact
  completion requires a valid list-shaped `artifact-manifest.json`.
- Intent-driven tests are allowed only for selected P0/P1 high-value candidate
  findings. Generated tests must live in the disposable validation workspace or
  `.codex-review/generated-tests/**`, must not install dependencies or use
  network, and failing generated tests must be classified before they influence
  confidence.
- Required completed artifacts are `report.md`, `report.agent.json`,
  `coverage.json`, `token-budget.json`, `qa.json`, `artifact-manifest.json`,
  `codex-events.jsonl`, `worker.log.jsonl`, and `progress.log.jsonl`.
- Artifact manifest/upload entries must include stable v1 metadata:
  `artifact_id`, supported `kind`, `name`, `media_type`, `schema_id`,
  `schema_version = v1`, `encoding = utf-8`, `compression = none`, `required`,
  `sha256`, and `size_bytes`.
- Post run progress through `POST /v1/review-runs/{run_id}/events`, upload
  artifacts through `POST /v1/review-runs/{run_id}/artifacts`, and upload
  artifacts before submitting the terminal result envelope through
  `POST /v1/review-runs/{run_id}/result`.
- Every posted progress event must include `protocol_version`, `run_id`,
  `worker_id`, positive monotonic `sequence`, `timestamp`, supported
  `event_type`, `phase`, `severity`, `message`, and `progress` with
  `overall_percent`, `current_phase_percent`, and `status`.
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
- Failed and cancelled jobs must still submit valid terminal envelopes when
  possible, including required `qa`, `worker_log`, and either `error_report` or
  partial `report.agent` artifacts.

Plan policy:

- Subscription plan policy still controls the model, timeout, repository limits,
  and core reasoning effort.
- Core semantic phases use the plan reasoning effort. Non-core phases use the
  same model with medium reasoning effort.

## Job Slot And Upload Discipline

Each worker instance has exactly one job execution slot. It does not maintain a
local job queue and must claim a new server-side job only after the current job
has finished. The only job slot must not be occupied by avoidable retry sleep or
cleanup IO.

- Final result upload should attempt the immediate request once. If submission
  fails, write `result-envelope.json` and `pending-submit.json` under the run's
  artifact directory, keep `active_job` in `finishing`, continue heartbeat with
  the active job id, and do not claim another job until the pending submit is
  resolved by recovery code or operator action.
- Result upload payloads should use gzip compression for large JSON. Keep server
  gzip JSON support and worker compression thresholds aligned.
- Do not add unbounded `time.sleep()` retry loops to `run_job()` or other code
  that holds the only job execution slot.
- Cleanup should run only when the worker is idle or on a low-priority
  background path. Do not run checkout/log cleanup before heartbeat/claim in the
  hot loop.

## Checkout And Cache Discipline

Repository checkout performance depends on the worker mirror cache.

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

Terminal result envelopes must include the stable v1 summary shape:
`overall_risk`, `result_status`, `finding_counts`, `coverage`, and
`top_findings`. Do not submit top-findings-only summaries.

Do not require derived topology artifacts for worker output. New review logic,
protocols, reports, tests, and documentation must depend on the stable envelope
and versioned artifacts, not on retired report data structures.

Legacy worker lifecycle endpoints for operator commands, logs, and registry state
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
- Prefer `model_profile.default_model`, `model_profile.*_effort`, and
  `review_request.policy` over compatibility `agentConfig` fields when driving
  Codex. `agentConfig` is server-derived backing data for admin/doctor
  consistency, not worker-local policy.
- Reject jobs whose `review_request.policy` allows source modification,
  dependency install, network access, or non-standard-library helper scripts.
- Repository size limits used by worker preflight come from the job's
  `repositoryLimits`, not from local plan assumptions.

## Readiness Semantics

Provider readiness is plan-aware and provider-specific.

- `provider_ready` means at least one provider required by the loaded plan
  configs is ready.
- `codex_ready` is the Codex login/readiness state required for accepting jobs.
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
