# pullwise-worker

Pull-based Pullwise scan worker.

## Run

```bash
export PULLWISE_SERVER_URL="http://localhost:8080"
export PULLWISE_WORKER_TOKEN="<server worker token>"
python3 -m pullwise_worker.main
```

The worker loop:

1. registers its v1 capability/isolation metadata with `POST /v1/workers/register`
2. sends `POST /v1/workers/{worker_id}/heartbeat`
3. claims one queued job with `POST /v1/workers/{worker_id}/lease` when no job is active locally
4. clones the repository using the short-lived clone token returned by the server
5. runs Codex through the worker-owned App Server with server-selected model policy
6. performs the v1.2 full-repository pipeline, including P0/P1 intent-test validation when selected
7. posts run progress to `POST /v1/review-runs/{run_id}/events`, uploads artifacts to `POST /v1/review-runs/{run_id}/artifacts`, and submits the v1 result envelope to `POST /v1/review-runs/{run_id}/result`
8. clears the active job only after terminal result handling completes

Required environment:

- `PULLWISE_SERVER_URL`
- `PULLWISE_WORKER_TOKEN`
- `PULLWISE_WORKER_ID` optional, defaults to `{hostname}-{pid}`
- `PULLWISE_PROVIDER` optional, defaults to `codex`
- `PULLWISE_PROVIDER_CHAIN` optional local install capability list; review policy comes from server lease `model_profile` and `review_request.policy`
- `PULLWISE_WORKER_POLL_SECONDS` optional, defaults to `5`
- `PULLWISE_WORKER_POLL_JITTER_SECONDS` optional, defaults to `2`
- `PULLWISE_WORKER_MAX_BACKOFF_SECONDS` optional, defaults to `60`
- `PULLWISE_CHECKOUT_ROOT` optional, defaults to the temp directory
- `PULLWISE_WORKER_WORK_DIR` optional
- `PULLWISE_LOG_DIR` optional, defaults to the temp directory
- `PULLWISE_SERVICE_NAME`, `PULLWISE_SERVICE_USER`, `PULLWISE_SERVICE_HOME`, `PULLWISE_SERVICE_PATH`, `PULLWISE_WORKER_ENV_FILE`, `PULLWISE_WORKER_ENV_BACKUP_FILE`, `PULLWISE_WORKER_BIN_PATH`, and `PULLWISE_LOGROTATE_FILE` optional for local/manual runs; server-generated installs set them per worker instance
- `PULLWISE_LIFECYCLE_WATCHER_ENABLED`, `PULLWISE_WATCHER_SERVICE_NAME`, `PULLWISE_WATCHER_SERVICE_FILE`, `PULLWISE_WATCHER_POLL_SECONDS`, `PULLWISE_REMOTE_UNINSTALL_FINALIZER`, and `PULLWISE_UNINSTALL_MARKER_FILE` optional watcher/finalizer settings used by installed workers
- `PULLWISE_WORKER_PACKAGE` optional package URL for controlled upgrades
- `PULLWISE_CODEX_COMMAND` optional explicit local Codex executable override; unset uses the `openai-codex` SDK pinned runtime
- `PULLWISE_CODEX_HOME` optional, defaults to `<worker-root>/codex-home`
- `PULLWISE_CODEX_SQLITE_HOME` optional, defaults to `<worker-root>/codex-sqlite`
- `PULLWISE_CODEX_RELEASE` optional standalone Codex CLI release; only used when explicitly opting into a local CLI override
- `PULLWISE_CODEX_INSTALLER_URL` optional Codex standalone installer URL; only used with an explicit local CLI override
- `PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS` optional, defaults to `60`
- `PULLWISE_ACTIVE_READINESS_CHECK_SECONDS` optional, defaults to `60`; used while the worker can claim jobs
- `PULLWISE_DEGRADED_READINESS_CHECK_SECONDS` optional, defaults to `600`; used while readiness is degraded and the worker is waiting for auth/quota/operator recovery
- `PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS` optional, defaults to `3600`
- `PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS` optional, defaults to `0`
- `PULLWISE_MAX_CHECKOUT_BYTES` optional, defaults to `21474836480` (20 GiB)
- `PULLWISE_LOG_RETENTION_SECONDS` optional, defaults to `1209600` (14 days)
- `PULLWISE_MAX_LOG_BYTES` optional, defaults to `1073741824` (1 GiB)
- `PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES` optional, defaults to `10485760` (10 MiB)

Each worker processes exactly one job at a time. Queuing is maintained on the server; after a job finishes, the worker returns to the server to claim the next job. Worker cleanup runs at startup and then periodically. It removes expired failed checkouts, prunes checkout disk usage by oldest inactive job directory, deletes old run logs, caps total log bytes, and truncates `scan-summary.log` to its configured maximum.

Review model, reasoning effort, repository file/byte limits, intent-test limits, and worker review deadlines come from the claimed job payload. The payload must include canonical v1 `model_profile`, `review_request.policy`, `review_request.budget`, and `repositoryLimits`; `agentConfig` may be present as server metadata, but the worker must not use it to fill missing policy fields. The default Codex runtime comes from the `openai-codex` SDK pinned dependency, so no Codex executable path is required. If `PULLWISE_CODEX_COMMAND` is set as an explicit local override, it remains local worker configuration, is not overridden by job policy, must be an absolute path inside the worker instance home, and global `codex` commands are rejected before launch. Those runtime policies are server database config delivered over HTTP; the worker never reads the server database and does not use local env vars for server-owned review policy.

Codex review work runs through one instance-scoped OpenAI Codex Python SDK client and one App Server per worker identity. A worker has one active job slot, never prefetches jobs, and drives root semantic phases sequentially on one coordinator thread. The reviewer fanout phase may run the server-owned bounded concurrency of one or two independent reviewer threads inside that same App Server; it does not launch another Codex process or share an auth store across workers. Review transport uses the `review-worker-protocol/v1` register, lease, heartbeat, run event, artifact, and terminal result routes under `/v1/workers...` and `/v1/review-runs/{run_id}/...`; lifecycle command/log endpoints are separate operator plumbing, not the core review pipeline. Review output is the v1 result envelope plus `.codex-review/runs/<run_id>/` artifacts such as `report.md`, `report.agent.json`, `coverage.json`, `token-budget.json`, `qa.json`, `artifact-manifest.json`, `codex-events.jsonl`, `worker.log.jsonl`, and `progress.log.jsonl`.

The v1.2 pipeline is a full repository scan, not a diff or PR review. It inventories the repo, estimates token budget, maps repo structure, routes files into P0/P1/P2/P3/SKIP coverage, asks Codex to group eligible paths into a small set of semantically coherent same-tier groups, validates exact path coverage, and then mechanically bounds and packs those groups as line-numbered bundles. It runs each logical reviewer assignment as its own Codex turn with bounded fanout concurrency and a run-level output-byte cap, validates reviewer JSON and locations, clusters/votes, runs intent-driven test validation only for selected high-value P0/P1 candidates, validates findings, renders reports, QA-checks outputs, uploads artifacts, and submits the terminal envelope. Writable Codex turns run in worker-owned staging directories outside the source repository. Only declared, regular, per-file and aggregate-bounded outputs are published as exact mirrors through a recoverable all-or-nothing transaction, and each stage is removed after the turn. Generated intent tests are temporary evidence only: source is published under the run-local `intent/generated-tests/**` tree and then materialized into the disposable validation workspace; tests do not install dependencies or use network, and failures must be classified before the validator can use them.

Intent-test execution is capability-driven rather than selected from a framework template matrix: Codex proposes commands and fallback strategies, while the Worker performs structured preflight, bounded repair/rerun, bubblewrap execution, immutable-source checks, and attempt-history preservation. See [Agentic Intent-Test Execution](docs/agentic-intent-test-execution.md).

Core semantic phases use the server subscription plan reasoning effort; mechanical and non-core phases use the same model with medium reasoning effort. Do not add alternate review pipelines, per-task CLI review flows, local job queues, or worker-side prefetch.

If terminal result submission fails, the worker writes `result-envelope.json` plus `result-submit-failed.json` or `result-submit-blocked.json`, keeps the active job in `finishing`, continues heartbeat with the active job id, and does not create a saved submission queue. Retrying the scan requires the user to start a new scan.

Production local capability example:

```bash
PULLWISE_PROVIDER_CHAIN=codex
# PULLWISE_CODEX_COMMAND is normally unset; openai-codex supplies the pinned runtime.
```

## Deploy

The worker supports Python 3.10 or newer.

Admin worker creation returns a one-time token plus an install command:

```bash
read -rsp 'Pullwise worker token: ' PULLWISE_WORKER_TOKEN; echo
export PULLWISE_WORKER_TOKEN
install_script="$(mktemp)"
trap 'rm -f "$install_script"' EXIT
curl -fsSL https://pullwise.example.com/install-worker.sh -o "$install_script"
printf '%s  %s\n' '<sha256 from admin install command>' "$install_script" | sha256sum -c -
bash "$install_script" --server https://pullwise.example.com --worker-id wk_x
```

The installer is served by Pullwise Server at `/install-worker.sh`. It creates a worker-specific system user, writes a locked-down worker env file, installs the selected worker package, installs a systemd unit and logrotate config, starts the worker, and runs `pullwise-worker doctor`. The worker package dependency on `openai-codex` supplies the pinned Codex runtime by default; the standalone Codex CLI installer runs only when an explicit local override such as `PULLWISE_CODEX_COMMAND` or `PULLWISE_CODEX_RELEASE` is provided. The worker package intentionally does not ship a second install script; server is the single installer source of truth.

## Release

To publish a worker package:

1. In GitHub, open Actions -> Release -> Run workflow.
2. Enter the version, for example `0.1.0`.

The workflow updates `pyproject.toml`, `pullwise_worker/__init__.py`, and `deploy/worker.env.template`, commits the version bump, creates `v<version>`, builds the wheel and source archive, and uploads both to the GitHub Release. Pullwise server install commands can then use that version directly.

Useful lifecycle commands:

```bash
pullwise-worker doctor
pullwise-worker logs
pullwise-worker start
pullwise-worker status
pullwise-worker stop
pullwise-worker restart
pullwise-worker update
pullwise-worker cleanup
pullwise-worker uninstall
pullwise-worker watch
pullwise-worker finalize-uninstall
```

`pullwise-worker watch` and `pullwise-worker finalize-uninstall` are normally
run by the installed watcher/systemd units, not typed during ordinary operation.
`pullwise-worker stop` is a local host operation and normally needs root or
systemd authorization. Admin-queued stop commands are handled by the running
worker exiting cleanly; the installed unit uses `Restart=on-failure` so the
service stays stopped.
Admin-queued Delete instance commands are handled by the worker host's
instance-scoped watcher after active jobs finish. Current installs create one
watcher service per worker instance; the watcher polls lifecycle commands,
stops the paired worker service, writes an uninstall marker, reports command
status, and removes the service unit, watcher unit, wrapper binary, logrotate
file, `/etc` configuration directory, instance home, and instance log directory.
Units installed before watcher rollout may rely on the running worker for
cleanup, which is less reliable when the worker is already stopped or degraded.
`pullwise-worker uninstall` first unregisters the worker from the server when
`PULLWISE_WORKER_TOKEN` is configured, then removes the local service. A stopped
worker stays in the registry; an uninstalled worker is removed from admin lists.

Codex must be authenticated for the service user before Codex scans can run. The worker uses the OpenAI Codex Python SDK and exposes a device-code login helper:

```bash
sudo -u pullwise-worker-wk_x env HOME=/var/lib/pullwise-worker/wk_x/workers/wk_x USERPROFILE=/var/lib/pullwise-worker/wk_x/workers/wk_x CODEX_HOME=/var/lib/pullwise-worker/wk_x/workers/wk_x/codex-home CODEX_SQLITE_HOME=/var/lib/pullwise-worker/wk_x/workers/wk_x/codex-sqlite XDG_CONFIG_HOME=/var/lib/pullwise-worker/wk_x/workers/wk_x/.config XDG_CACHE_HOME=/var/lib/pullwise-worker/wk_x/workers/wk_x/.cache XDG_DATA_HOME=/var/lib/pullwise-worker/wk_x/workers/wk_x/.local/share PATH=/var/lib/pullwise-worker/wk_x/workers/wk_x/.local/bin:/var/lib/pullwise-worker/wk_x/workers/wk_x/.codex/bin:/var/lib/pullwise-worker/wk_x/workers/wk_x/codex-home/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin sh -lc 'cd "$HOME" && exec pullwise-worker codex-login'
```


