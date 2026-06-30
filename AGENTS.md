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

## Codex Execution Concurrency

GraphVerified Codex work runs through one instance-scoped `codex app-server`
per worker. The `codex` binary is still used to launch the app-server and for
login flows, but the review pipeline must not start CLI review commands.

- Express Codex concurrency as app-server threads/turns inside the worker's
  single server process. App-server turns may run concurrently when bounded by
  config such as `finders.turn_parallel`; this is not process-level CLI
  concurrency.
- The current implementation supports bounded concurrent app-server turns by
  sharing one `CodexAppServerClient` per worker provider environment. Concurrent
  callers get separate app-server threads/turn ids while the client serializes
  only JSON writes to the app-server stdio pipe.
- Keep the default conservative unless the server plan explicitly enables more:
  `graphVerified.finderTurnParallel` maps to `finders.turn_parallel`, defaults
  to `1`, and is capped at `6`. `graphVerified.finderMaxParallel` controls how
  many finder subagents are batched into one turn, also capped at `6`.
- Increasing `finders.turn_parallel` is the supported way to parallelize
  GraphVerified model work for one job under app-server mode. It does not
  permit multiple local jobs, a local job queue, or multiple Codex CLI
  executions for one worker identity.
- Do not start multiple app-server processes for one worker identity.
- Do not add process-level Codex parallelism to a worker identity. Add more
  worker instances when more throughput is needed.

- Do not start multiple app-server processes that share the same `CODEX_HOME`,
  `CODEX_SQLITE_HOME`, `HOME`, or credential store. The desktop app can keep
  the default `~/.codex/sqlite` state locked, so worker app-server runs must use
  worker-owned SQLite state.
- Treat auth and token refresh as shared state for all concurrent turns in the
  worker's single app-server. If Codex reports markers such as `401
  Unauthorized`, `Failed to refresh token`, or `refresh token was already used`,
  the worker must degrade readiness/cool down rather than launching extra CLI or
  app-server processes to work around the failure. Reduce
  `finderTurnParallel` before raising it again if token refresh failures appear
  under load.
- Prefer stdio or an instance-scoped Unix socket for app-server transport. If a
  TCP/WebSocket listener is ever used, bind it to a worker-specific local port
  and require app-server WebSocket auth; do not expose unauthenticated
  non-loopback listeners.
- Multiple workers on the same host may each run their own app-server only when
  each worker has distinct `service_home`, `CODEX_HOME`, `CODEX_SQLITE_HOME`,
  config, cache, and auth state.
- Recycle a long-lived app-server only between active turns. The worker defaults
  to recycling after roughly 40 minutes or 48 completed turns to avoid stale
  control-plane hangs.
- Treat app-server failures as backend failures, not as permission to launch
  parallel CLI review processes.

Codex configuration and generated review context follow the same boundary:

- Different worker instances on the same server must not share Codex config,
  Codex auth, provider cache, or provider binaries. Each worker's Codex config
  must live under that worker's `CODEX_HOME`, normally
  `$service_home/.codex/config.toml`.
- Different checkouts handled by the same worker may share that worker
  instance's Codex configuration. This is intentional because they run under the
  same worker identity and provider account.
- Repository files, generated review context, `.codereview/config.json`, and
  review runs remain checkout-scoped. Do not put checkout context under
  `service_home` or another checkout.
- Reproduction workers may inherit the parent worker's Codex configuration
  (`HOME`, `USERPROFILE`, `CODEX_HOME`, provider `PATH`, and non-cache XDG
  config/data dirs), but their repository copy, logs, temp dirs, npm/pip caches,
  and pycache prefix must remain candidate-worker scoped.

## Job Slot And Upload Discipline

Each worker instance has exactly one job execution slot. It does not maintain a
local job queue and must claim a new server-side job only after the current job
has finished. The only job slot must not be occupied by avoidable retry sleep or
cleanup IO.

- Final result upload should attempt the immediate request once. Retryable
  network/5xx failures should be written to the pending result upload spool and
  retried by the background upload worker.
- Pending result uploads must remain in heartbeat `active_job_ids` so the server
  renews the claimed job lease while the final payload is being retried. They do
  not count as active Codex/job execution capacity.
- Result upload payloads should use gzip compression for large JSON. Keep server
  gzip JSON support and worker compression thresholds aligned.
- Do not add `time.sleep()`-based retry loops to `run_job()` or other code that
  holds the only job execution slot. Put backoff in a background path.
- Pending result upload files live under `.pullwise-result-uploads`; keep this
  directory protected from checkout cleanup.
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

## Worker Scan Reproduction Sandbox

Worker scan agents should maximize reproducible evidence without expanding host
scope.

- Job scan flow should be two-stage: first enumerate as many plausible issue
  candidates as the repository context supports, then reproduce or verify each
  candidate one by one. Only candidates that survive reproduction/verification
  should become formal reported findings.
- Each scan job must run provider agents inside that job's isolated checkout or
  work directory.
- Codex review and semantic fallback agent executions may use
  `--sandbox workspace-write`, but their subprocess `cwd` must stay scoped to
  the job checkout. Do not point provider agent execution at `service_home`,
  global temp roots, or any directory shared by multiple jobs.
- Agents may create temporary reproduction files, focused tests, or scripts only
  inside the job checkout. Scan completion should clean the job checkout unless
  the worker intentionally retains a failed checkout according to configured
  retention policy.
- A formally reportable finding must carry reproducible runtime evidence:
  an actual command, an explicit exit code, substantive output or a redacted
  structured log path, and a repository-relative file/line tied to the issue.
- Agent/provider findings that lack runtime reproduction evidence must be kept
  audit-only or filtered before reporting. Do not treat speculative model claims,
  source-only observations, or natural-language reproduction notes as trusted
  findings.

## Graph-Verified Review Implementation Notes

The graph-verified review implementation lives under `codereview/` and follows
the v3 full-repository design in `../codex-native-full-repo-graph-review.md`:
use Git only for file discovery/status metadata, Python standard library, and
the worker's Codex app-server interface. Do not add third-party graph,
static-analysis, parser, or database dependencies for this pipeline.

- All code that starts Codex for graph mapping, finder, verifier, repro, judge,
  context, or fallback work must call `codereview.codex_runner.run_codex_turn`.
  That function uses the worker's instance-scoped app-server. It is fine for
  orchestrator stages to use thread pools for task scheduling; concurrent calls
  under one worker identity must still stay within the single app-server
  boundary.
- Batch model-driven finder work by module/context before starting Codex work.
  A single app-server turn may fan out to at most six subagents. Do not
  split one module into repeated context reads when the jobs can share one
  context pack, and do not force unrelated modules into the same batch. Multiple
  finder batch turns may run concurrently through the same app-server, bounded
  by `finders.turn_parallel`. This is the app-server replacement for the old
  serialized `codex exec` CLI path; do not reintroduce CLI commands to get
  parallelism.
- `.codereview/runs/<run_id>/` is checkout-scoped run state. Keep graph JSONL,
  review units, candidate artifacts, worker task files, reports, and debug data
  there. Do not move repository context or review run data under `service_home`.
- Every run is a full-repository scan of the current checkout snapshot,
  including configured tracked, modified, and untracked files. Git metadata is
  for file discovery, ignore rules, status metadata, and source-state checks.
- The immutable review input is `workers/coordinator/snapshot/repo/`. Agent
  stages should read source from that snapshot, not from the mutable source
  checkout. Source-state hash checks must fail closed when analyzable files
  change during a run.
- Evidence graph JSONL artifacts are the source of truth: `nodes.jsonl`,
  `edges.jsonl`, and `unresolved.jsonl`. Do not invent graph edges to make a
  review path look complete. If a relationship cannot be proven from source
  evidence or unique resolution, keep it as unresolved and let later stages
  request repair or reject the candidate.
- Review planning is unit-based. The planner must cover entrypoint_flow,
  component, state, trust_boundary, config_build, test_integrity,
  cross_boundary, global_invariant, and orphan production-symbol units.
  Coverage reports are required even when there are no confirmed findings.
- Finder candidates must have a review unit id, graph path, concrete file/line
  evidence, trigger condition, expected behavior source, and a local
  reproduction idea. Candidates without those fields stay internal and must not
  become final findings.
- Reproduction workers write only inside their worker directory. `repo/` is the
  private copy of the immutable full-repository snapshot. Do not create base or
  alternate checkout directories for reproduction.
- User-facing reports are confirmed-only. A runtime-command finding must pass reproduction and
  judge gates with a real command, exit code, log evidence, and filesystem-boundary
  checks before it is shown. A static-proof finding may be shown only when it is
  backed by inspected repository files, covers every primary evidence file,
  has concrete verification steps, leaves runtime command fields empty, and is
  clearly labeled as `static-proof` and `model-self-certified`; a narrative
  description alone is not evidence.
- When adding or changing graph/review schemas or prompts, update
  `codereview/templates.py` so `python -m codereview init` and fresh checkouts
  receive the same assets.

## Server-Controlled Agent Policy

The worker can advertise local provider capability, but review policy comes from
server-provided subscription plan agent configs and per-job `agentConfig`.

- Treat `PULLWISE_PROVIDER_CHAIN` as local installed capability/order, not as the
  source of plan policy.
- `doctor` must load free/pro/max agent configs from the server. If they cannot
  be loaded or validated, do not silently fall back to the local provider chain.
- A claimed job must include `agentConfig` and `repositoryLimits`; reject jobs
  that omit them instead of using local defaults.
- When a job uses Codex in `agentConfig.provider`, require Codex model
  and reasoning effort.
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
