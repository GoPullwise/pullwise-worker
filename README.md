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
2. claims queued jobs up to `free_slots` with `POST /worker/jobs/claim`
3. clones the repository using the short-lived clone token returned by the server
4. runs the configured review provider chain
5. uploads progress and final result
6. removes the checkout directory

Required environment:

- `PULLWISE_SERVER_URL`
- `PULLWISE_WORKER_TOKEN`
- `PULLWISE_WORKER_ID` optional, defaults to `{hostname}-{pid}`
- `PULLWISE_PROVIDER` optional, defaults to `codex`
- `PULLWISE_PROVIDER_CHAIN` optional, defaults to `codex`; use `codex,opencode` for Codex-first fallback
- `PULLWISE_MAX_CONCURRENT_JOBS` optional, defaults to `1`
- `PULLWISE_WORKER_POLL_SECONDS` optional, defaults to `5`
- `PULLWISE_WORKER_POLL_JITTER_SECONDS` optional, defaults to `2`
- `PULLWISE_WORKER_MAX_BACKOFF_SECONDS` optional, defaults to `60`
- `PULLWISE_CHECKOUT_ROOT` optional, defaults to the temp directory
- `PULLWISE_WORKER_WORK_DIR` optional
- `PULLWISE_LOG_DIR` optional, defaults to the temp directory
- `PULLWISE_CODEX_COMMAND` optional, defaults to `codex`
- `PULLWISE_CODEX_MODEL` optional, defaults to empty; when empty, the Codex CLI chooses its default model
- `PULLWISE_CODEX_REASONING_EFFORT` optional, defaults to `xhigh`
- `PULLWISE_OPENCODE_COMMAND` optional, defaults to `opencode`
- `PULLWISE_OPENCODE_MODEL` optional, defaults to empty; when empty, OpenCode uses its own model loading order
- `PULLWISE_OPENCODE_VARIANT` optional, defaults to empty; use this for OpenCode provider-specific reasoning effort
- `PULLWISE_CODEX_TIMEOUT_SECONDS` optional, defaults to `1800`
- `PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS` optional, defaults to `60`
- `PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS` optional, defaults to `0`
- `PULLWISE_MAX_CHECKOUT_BYTES` optional, defaults to `21474836480` (20 GiB)

Provider model defaults are intentionally conservative. Codex keeps the old worker behavior by default: no explicit model is passed, and `model_reasoning_effort` is set to `xhigh`. OpenCode is not used unless it appears in `PULLWISE_PROVIDER_CHAIN`; when enabled, the worker passes `--model` and `--variant` only if their environment variables are set.

Production Codex-first fallback example:

```bash
PULLWISE_PROVIDER_CHAIN=codex,opencode
PULLWISE_CODEX_MODEL=gpt-5.5
PULLWISE_CODEX_REASONING_EFFORT=xhigh
PULLWISE_OPENCODE_MODEL=openai/gpt-5
PULLWISE_OPENCODE_VARIANT=xhigh
```

## Deploy

The worker supports Python 3.9 or newer.

Admin worker creation returns a one-time token plus an install command:

```bash
read -rsp 'Pullwise worker token: ' PULLWISE_WORKER_TOKEN; echo
export PULLWISE_WORKER_TOKEN
curl -fsSL https://pullwise.example.com/install-worker.sh | bash -s -- --server https://pullwise.example.com --worker-id wk_x
```

The installer creates a `pullwise-worker` system user, writes `/etc/pullwise-worker/worker.env` with mode `0640`, installs the worker package from `PULLWISE_WORKER_PACKAGE` or, by default, from the `v0.1.8` GitHub Release wheel, installs the pinned Codex CLI package from `PULLWISE_CODEX_PACKAGE` when `codex` is missing (default `@openai/codex@0.135.0`), installs a systemd unit, enables logrotate, starts the worker, and runs `pullwise-worker doctor`.

## Release

To publish a worker package:

1. In GitHub, open Actions -> Release -> Run workflow.
2. Enter the version, for example `0.1.0`.

The workflow updates `pyproject.toml` and `pullwise_worker/__init__.py`, commits the version bump, creates `v<version>`, builds the wheel and source archive, and uploads both to the GitHub Release. Pullwise server install commands can then use that version directly.

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

Codex must be authenticated for the service user before Codex scans can run:

```bash
sudo -u pullwise-worker codex login
```

When `PULLWISE_PROVIDER_CHAIN` includes `opencode`, install and authenticate OpenCode for the same service user before relying on fallback.
