#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="pullwise-worker"
CONFIG_DIR="/etc/pullwise-worker"
ENV_FILE="$CONFIG_DIR/worker.env"
BIN_PATH="/usr/local/bin/pullwise-worker"
DATA_DIR="/var/lib/pullwise-worker"
CHECKOUT_ROOT="$DATA_DIR/checkouts"
LOG_DIR="/var/log/pullwise-worker"
SERVER_URL=""
WORKER_ID=""
WORKER_TOKEN=""
WORKER_NAME="pullwise-worker"
MAX_CONCURRENT_JOBS="8"
PROVIDER="codex"
WORKER_PACKAGE="${PULLWISE_WORKER_PACKAGE:-pullwise-worker}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER_URL="${2:-}"; shift 2 ;;
    --worker-id) WORKER_ID="${2:-}"; shift 2 ;;
    --worker-token) WORKER_TOKEN="${2:-}"; shift 2 ;;
    --worker-name) WORKER_NAME="${2:-}"; shift 2 ;;
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

case "$(uname -s)" in Linux) ;; *) echo "Pullwise worker installer requires Linux" >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;; esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root so the installer can create service users and systemd units." >&2
  exit 1
fi

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 1; }; }
need_cmd python3
need_cmd git
if ! command -v node >/dev/null 2>&1; then
  echo "node is required for Codex CLI; install Node.js 20+ then rerun." >&2
  exit 1
fi
if ! command -v codex >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    npm install -g @openai/codex
  else
    echo "npm is required to install Codex CLI. Install codex manually and rerun." >&2
    exit 1
  fi
fi

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR" "$CHECKOUT_ROOT" "$LOG_DIR"

python3 -m pip install --upgrade "$WORKER_PACKAGE"

cat > "$ENV_FILE" <<EOF
PULLWISE_SERVER_URL=$SERVER_URL
PULLWISE_WORKER_ID=$WORKER_ID
PULLWISE_WORKER_TOKEN=$WORKER_TOKEN
PULLWISE_PROVIDER=$PROVIDER
PULLWISE_MAX_CONCURRENT_JOBS=$MAX_CONCURRENT_JOBS
PULLWISE_CHECKOUT_ROOT=$CHECKOUT_ROOT
PULLWISE_LOG_DIR=$LOG_DIR
PULLWISE_WORKER_PACKAGE=$WORKER_PACKAGE
EOF
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

cat > "$BIN_PATH" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
set -a
if [ -f /etc/pullwise-worker/worker.env ]; then
  . /etc/pullwise-worker/worker.env
fi
set +a
exec python3 -m pullwise_worker.main "$@"
EOF
chmod 0755 "$BIN_PATH"

cat > /etc/systemd/system/pullwise-worker.service <<EOF
[Unit]
Description=Pullwise Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$DATA_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$BIN_PATH run
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR $LOG_DIR

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/logrotate.d/pullwise-worker <<EOF
$LOG_DIR/*.log {
  daily
  rotate 14
  compress
  missingok
  notifempty
  create 0640 $SERVICE_USER $SERVICE_USER
}
EOF

systemctl daemon-reload
systemctl enable pullwise-worker >/dev/null
systemctl restart pullwise-worker
"$BIN_PATH" doctor || true

echo "Pullwise worker installed as $WORKER_NAME ($WORKER_ID)."
echo "If Codex is not logged in, run: sudo -u $SERVICE_USER codex login"
