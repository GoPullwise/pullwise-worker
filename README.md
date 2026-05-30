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
4. runs `codex exec`
5. uploads progress and final result
6. removes the checkout directory

Required environment:

- `PULLWISE_SERVER_URL`
- `PULLWISE_WORKER_TOKEN`
- `PULLWISE_WORKER_ID` optional, defaults to `{hostname}-{pid}`
- `PULLWISE_PROVIDER` optional, defaults to `codex`
- `PULLWISE_MAX_CONCURRENT_JOBS` optional, defaults to `1`
- `PULLWISE_WORKER_POLL_SECONDS` optional, defaults to `5`
- `PULLWISE_CHECKOUT_ROOT` optional, defaults to the temp directory
- `PULLWISE_WORKER_WORK_DIR` optional
- `PULLWISE_LOG_DIR` optional, defaults to the temp directory
- `PULLWISE_CODEX_COMMAND` optional, defaults to `codex`
- `PULLWISE_CODEX_TIMEOUT_SECONDS` optional, defaults to `1800`
- `PULLWISE_CODEX_DOCTOR_TIMEOUT_SECONDS` optional, defaults to `60`
- `PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS` optional, defaults to `0`
- `PULLWISE_MAX_CHECKOUT_BYTES` optional, defaults to `21474836480` (20 GiB)

## Deploy

Admin worker creation returns a one-time token plus an install command:

```bash
curl -fsSL https://pullwise.example.com/install-worker.sh | bash -s -- --server https://pullwise.example.com --worker-id wk_x --worker-token pww_x
```

The installer creates a `pullwise-worker` system user, writes `/etc/pullwise-worker/worker.env` with mode `0640`, installs the worker package, installs a systemd unit, enables logrotate, starts the worker, and runs `pullwise-worker doctor`.

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

Codex must be authenticated for the service user before scans can run:

```bash
sudo -u pullwise-worker codex login
```
