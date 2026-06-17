# Pullwise Worker Agent Notes

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

Do not change a single worker instance to run multiple Codex review jobs in
parallel. The process-wide Codex execution lock is intentional: concurrent
Codex CLI executions under the same worker identity can refresh auth at the
same time and invalidate the token/session state. If job latency or timeout
behavior needs improvement, keep Codex execution serial within each worker and
address queueing, timeout reporting, scheduling, or multi-worker capacity
instead.

Codex and CodeGraph configuration follows the same boundary:

- Different worker instances on the same server must not share Codex config,
  Codex auth, CodeGraph MCP config, provider cache, or provider binaries. Each
  worker's MCP config must live under that worker's `CODEX_HOME`, normally
  `$service_home/.codex/config.toml`.
- Different checkouts handled by the same worker may share that worker
  instance's Codex and CodeGraph MCP configuration. This is intentional because
  they run under the same worker identity and provider account.
- Repository files and CodeGraph project indexes remain checkout-scoped. A
  checkout's CodeGraph database belongs under that checkout's `.codegraph/`
  directory, not under `service_home` and not under another checkout.
- Reproduction workers may inherit the parent worker's Codex/MCP configuration
  (`HOME`, `USERPROFILE`, `CODEX_HOME`, provider `PATH`, and non-cache XDG
  config/data dirs), but their repository copy, logs, temp dirs, npm/pip caches,
  and pycache prefix must remain candidate-worker scoped.
- Do not use `CODEGRAPH_DIR` to point multiple jobs or workers at a shared graph
  directory. Let CodeGraph use each checkout's `.codegraph/` directory.

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
