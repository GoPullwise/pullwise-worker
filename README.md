# pullwise-worker

Pull-based Pullwise scan worker.

## Run

```powershell
$env:PULLWISE_SERVER_URL = "http://localhost:8080"
$env:PULLWISE_WORKER_TOKEN = "<server worker token>"
python -m pullwise_worker.main
```

The worker loop:

1. sends `POST /worker/heartbeat`
2. claims queued jobs up to `min(free_slots, PULLWISE_WORKER_MAX_CLAIM_JOBS)` with `POST /worker/jobs/claim`
3. clones the repository using the short-lived clone token returned by the server
4. runs the configured review provider chain
5. uploads progress and final result
6. removes the checkout directory

Required environment:

- `PULLWISE_SERVER_URL`
- `PULLWISE_WORKER_TOKEN`
- `PULLWISE_WORKER_ID` optional, defaults to `{hostname}-{pid}`
- `PULLWISE_PROVIDER` optional, defaults to `codex`
- `PULLWISE_PROVIDER_CHAIN` optional, defaults to `codex,opencode` for Codex-first fallback
- `PULLWISE_MAX_CONCURRENT_JOBS` optional, defaults to `1`
- `PULLWISE_WORKER_MAX_CLAIM_JOBS` optional, defaults to `2`
- `PULLWISE_WORKER_POLL_SECONDS` optional, defaults to `5`
- `PULLWISE_WORKER_POLL_JITTER_SECONDS` optional, defaults to `2`
- `PULLWISE_WORKER_MAX_BACKOFF_SECONDS` optional, defaults to `60`
- `PULLWISE_CHECKOUT_ROOT` optional, defaults to the temp directory
- `PULLWISE_WORKER_WORK_DIR` optional
- `PULLWISE_LOG_DIR` optional, defaults to the temp directory
- `PULLWISE_CODEX_COMMAND` optional, defaults to `codex`
- `PULLWISE_CODEX_MODEL` optional, defaults to `gpt-5.5`
- `PULLWISE_CODEX_REASONING_EFFORT` optional, defaults to `medium`
- `PULLWISE_OPENCODE_COMMAND` optional, defaults to `opencode`
- `PULLWISE_OPENCODE_MODEL` optional, defaults to `opencode/big-pickle`
- `PULLWISE_OPENCODE_VARIANT` optional, defaults to `medium`; use this for OpenCode provider-specific reasoning effort
- `PULLWISE_CODEX_TIMEOUT_SECONDS` optional, defaults to `1800`
- `PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS` optional, defaults to `60`
- `PULLWISE_CODEX_AUTH_FAILURE_COOLDOWN_SECONDS` optional, defaults to `3600`; set to `0` to disable
- `PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS` optional, defaults to `3600`
- `PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS` optional, defaults to `0`
- `PULLWISE_MAX_CHECKOUT_BYTES` optional, defaults to `21474836480` (20 GiB)
- `PULLWISE_REVIEW_CALIBRATION_MODE` optional, defaults to `shadow`; use `audit_only` or `enforce` only after server-side shadow evaluation passes
- `PULLWISE_REVIEW_CALIBRATION_MODEL` optional, defaults to `relative_factor`; set to `logit_beta` to emit `truth_probability`
- `PULLWISE_REVIEW_CALIBRATION_HALF_LIFE_DAYS` optional, defaults to `45`
- `PULLWISE_REVIEW_CALIBRATION_MIN_EFFECTIVE_SAMPLES` optional, defaults to `20`
- `PULLWISE_REVIEW_CALIBRATION_ENABLE_BUCKETS` optional, defaults to `false`
- `PULLWISE_REVIEW_CALIBRATION_ENABLE_HIERARCHY` optional, defaults to `false`
- `PULLWISE_REVIEW_CALIBRATION_ENABLE_DRIFT` optional, defaults to `false`
- `PULLWISE_REVIEW_CALIBRATION_SAMPLE_AUDIT_RATE` optional, defaults to `0.02`; sampled candidates are kept in audit artifacts for manual/internal review without becoming truth labels
- `PULLWISE_REVIEW_CALIBRATION_BORDERLINE_SAMPLE_WINDOW` optional, defaults to `0.03`; candidates near report/audit thresholds are marked for manual/internal review to reduce selection bias
- `PULLWISE_LOG_RETENTION_SECONDS` optional, defaults to `1209600` (14 days)
- `PULLWISE_MAX_LOG_BYTES` optional, defaults to `1073741824` (1 GiB)
- `PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES` optional, defaults to `10485760` (10 MiB)

Worker cleanup runs at startup and then periodically. It removes expired failed checkouts, prunes checkout disk usage by oldest inactive job directory, deletes old verifier logs, caps total log bytes, and truncates `scan-summary.log` to its configured maximum.

Provider model defaults are intentionally conservative. Codex passes `gpt-5.5` and `model_reasoning_effort=medium` by default so the worker does not inherit an unsupported Codex CLI default model. The default provider chain is Codex first, then OpenCode fallback; OpenCode defaults to `opencode/big-pickle` with variant `medium`.
When the server includes per-job `agentConfig`, the worker applies that review's provider chain plus per-provider model and reasoning settings without changing the process-wide defaults. Executable command paths such as `PULLWISE_CODEX_COMMAND` and `PULLWISE_OPENCODE_COMMAND` remain local worker configuration and are not overridden by job policy. Repository file/byte limits are also read from the claimed job payload. Those runtime policies are server database config delivered over HTTP; the worker never reads the server database and does not use local env vars for migrated server policy.

Codex `exec` calls are serialized inside the worker because Codex keeps local login state under the service user's home directory. If Codex reports an authentication or refresh-token failure, the worker cools down further Codex launches for `PULLWISE_CODEX_AUTH_FAILURE_COOLDOWN_SECONDS` and then uses the next configured provider, if any.

Production Codex-first fallback example:

```bash
PULLWISE_PROVIDER_CHAIN=codex,opencode
PULLWISE_CODEX_MODEL=gpt-5.5
PULLWISE_CODEX_REASONING_EFFORT=medium
PULLWISE_OPENCODE_MODEL=opencode/big-pickle
PULLWISE_OPENCODE_VARIANT=medium
```

## Deploy

The worker supports Python 3.9 or newer.

Admin worker creation returns a one-time token plus an install command:

```bash
read -rsp 'Pullwise worker token: ' PULLWISE_WORKER_TOKEN; echo
export PULLWISE_WORKER_TOKEN
curl -fsSL https://pullwise.example.com/install-worker.sh | bash -s -- --server https://pullwise.example.com --worker-id wk_x
```

The installer is served by Pullwise Server at `/install-worker.sh`. It creates a worker-specific system user, writes a locked-down worker env file, installs the selected worker package, installs Codex/OpenCode CLIs when needed, installs a systemd unit and logrotate config, starts the worker, and runs `pullwise-worker doctor`. The worker package intentionally does not ship a second install script; server is the single installer source of truth.

## Release

To publish a worker package:

1. In GitHub, open Actions -> Release -> Run workflow.
2. Enter the version, for example `0.1.0`.

The workflow updates `pyproject.toml`, `pullwise_worker/__init__.py`, and `deploy/worker.env.template`, commits the version bump, creates `v<version>`, builds the wheel and source archive, and uploads both to the GitHub Release. Pullwise server install commands can then use that version directly.

Useful lifecycle commands:

```bash
pullwise-worker doctor
pullwise-worker start
pullwise-worker status
pullwise-worker stop
pullwise-worker restart
pullwise-worker update
pullwise-worker cleanup
pullwise-worker uninstall
```

`pullwise-worker stop` is a local host operation and normally needs root or
systemd authorization. Admin-queued stop commands are handled by the running
worker exiting cleanly; the installed unit uses `Restart=on-failure` so the
service stays stopped.
`pullwise-worker uninstall` first unregisters the worker from the server when
`PULLWISE_WORKER_TOKEN` is configured, then removes the local service. A stopped
worker stays in the registry; an uninstalled worker is removed from admin lists.

Codex must be authenticated for the service user before Codex scans can run:

```bash
sudo -u pullwise-worker env HOME=/var/lib/pullwise-worker PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/var/lib/pullwise-worker/.local/bin:/var/lib/pullwise-worker/.codex/bin:/var/lib/pullwise-worker/.opencode/bin codex login --device-auth
```

When `PULLWISE_PROVIDER_CHAIN` includes `opencode`, authenticate OpenCode for the same service user before relying on fallback:

```bash
sudo -u pullwise-worker env HOME=/var/lib/pullwise-worker PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/var/lib/pullwise-worker/.local/bin:/var/lib/pullwise-worker/.codex/bin:/var/lib/pullwise-worker/.opencode/bin opencode auth login --provider opencode
```

OpenCode provider examples:

```bash
PULLWISE_OPENCODE_MODEL=deepseek/deepseek-v4-pro
sudo -u pullwise-worker env HOME=/var/lib/pullwise-worker PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/var/lib/pullwise-worker/.local/bin:/var/lib/pullwise-worker/.codex/bin:/var/lib/pullwise-worker/.opencode/bin opencode auth login --provider deepseek

PULLWISE_OPENCODE_MODEL=minimax/MiniMax-M3
sudo -u pullwise-worker env HOME=/var/lib/pullwise-worker PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/var/lib/pullwise-worker/.local/bin:/var/lib/pullwise-worker/.codex/bin:/var/lib/pullwise-worker/.opencode/bin opencode auth login --provider minimax

# Generic: choose interactively, or set PULLWISE_OPENCODE_MODEL=<provider>/<model>
# and run opencode auth login --provider <provider>.
sudo -u pullwise-worker env HOME=/var/lib/pullwise-worker PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/var/lib/pullwise-worker/.local/bin:/var/lib/pullwise-worker/.codex/bin:/var/lib/pullwise-worker/.opencode/bin opencode auth login
```
