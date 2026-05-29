#!/usr/bin/env bash
set -euo pipefail

SERVER_URL=""
WORKER_ID=""
WORKER_TOKEN=""
MAX_CONCURRENT_JOBS="8"
PROVIDER="codex"
WORKER_PACKAGE="${PULLWISE_WORKER_PACKAGE:-pullwise-worker}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER_URL="${2:-}"; shift 2 ;;
    --worker-id) WORKER_ID="${2:-}"; shift 2 ;;
    --worker-token) WORKER_TOKEN="${2:-}"; shift 2 ;;
    --max-concurrent-jobs) MAX_CONCURRENT_JOBS="${2:-8}"; shift 2 ;;
    --provider) PROVIDER="${2:-codex}"; shift 2 ;;
    --package) WORKER_PACKAGE="${2:-pullwise-worker}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$SERVER_URL" ] || [ -z "$WORKER_ID" ] || [ -z "$WORKER_TOKEN" ]; then
  echo "missing --server, --worker-id, or --worker-token" >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "run as root" >&2
  exit 1
fi

command -v python3 >/dev/null
command -v git >/dev/null
command -v node >/dev/null
command -v codex >/dev/null || npm install -g @openai/codex

useradd --system --home /var/lib/pullwise-worker --shell /usr/sbin/nologin pullwise-worker 2>/dev/null || true
install -d -m 0750 -o pullwise-worker -g pullwise-worker /etc/pullwise-worker /var/lib/pullwise-worker/checkouts /var/log/pullwise-worker
python3 -m pip install --upgrade "$WORKER_PACKAGE"

cat > /etc/pullwise-worker/worker.env <<EOF
PULLWISE_SERVER_URL=$SERVER_URL
PULLWISE_WORKER_ID=$WORKER_ID
PULLWISE_WORKER_TOKEN=$WORKER_TOKEN
PULLWISE_PROVIDER=$PROVIDER
PULLWISE_MAX_CONCURRENT_JOBS=$MAX_CONCURRENT_JOBS
PULLWISE_CHECKOUT_ROOT=/var/lib/pullwise-worker/checkouts
PULLWISE_LOG_DIR=/var/log/pullwise-worker
PULLWISE_WORKER_PACKAGE=$WORKER_PACKAGE
EOF
chown root:pullwise-worker /etc/pullwise-worker/worker.env
chmod 0640 /etc/pullwise-worker/worker.env

cat > /usr/local/bin/pullwise-worker <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
. /etc/pullwise-worker/worker.env
set +a
exec python3 -m pullwise_worker.main "$@"
EOF
chmod 0755 /usr/local/bin/pullwise-worker

cp "$(dirname "$0")/pullwise-worker.service" /etc/systemd/system/pullwise-worker.service
cp "$(dirname "$0")/logrotate.conf" /etc/logrotate.d/pullwise-worker
systemctl daemon-reload
systemctl enable pullwise-worker
systemctl restart pullwise-worker
pullwise-worker doctor || true
echo "Run Codex login for the service user if needed: sudo -u pullwise-worker codex login"
