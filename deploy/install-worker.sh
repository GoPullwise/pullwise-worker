#!/usr/bin/env bash
set -euo pipefail

SERVICE_USER="pullwise-worker"
SERVICE_PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
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
MAX_CONCURRENT_JOBS="1"
PROVIDER="codex"
WORKER_PACKAGE=""
DEFAULT_WORKER_VERSION="0.1.4"
DEFAULT_WORKER_PACKAGE="https://github.com/GoPullwise/pullwise-worker/releases/download/v${DEFAULT_WORKER_VERSION}/pullwise_worker-${DEFAULT_WORKER_VERSION}-py3-none-any.whl"
CODEX_PACKAGE="${PULLWISE_CODEX_PACKAGE:-@openai/codex@0.135.0}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER_URL="${2:-}"; shift 2 ;;
    --worker-id) WORKER_ID="${2:-}"; shift 2 ;;
    --worker-token-file) WORKER_TOKEN="$(cat "${2:-}")"; shift 2 ;;
    --worker-name) WORKER_NAME="${2:-}"; shift 2 ;;
    --max-concurrent-jobs) MAX_CONCURRENT_JOBS="${2:-1}"; shift 2 ;;
    --provider) PROVIDER="${2:-codex}"; shift 2 ;;
    --package) WORKER_PACKAGE="${2:-}"; shift 2 ;;
    --codex-package) CODEX_PACKAGE="${2:-@openai/codex@0.135.0}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$WORKER_TOKEN" ] && [ -n "${PULLWISE_WORKER_TOKEN:-}" ]; then
  WORKER_TOKEN="$PULLWISE_WORKER_TOKEN"
fi

if [ -z "$SERVER_URL" ] || [ -z "$WORKER_ID" ] || [ -z "$WORKER_TOKEN" ]; then
  echo "missing --server, --worker-id, or worker token env/file" >&2
  exit 2
fi
if [ -z "$WORKER_PACKAGE" ]; then
  WORKER_PACKAGE="${PULLWISE_WORKER_PACKAGE:-}"
fi
if [ -z "$WORKER_PACKAGE" ]; then
  WORKER_PACKAGE="$DEFAULT_WORKER_PACKAGE"
fi

case "$(uname -s)" in Linux) ;; *) echo "Pullwise worker installer requires Linux" >&2; exit 1 ;; esac
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) echo "Unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;; esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root so the installer can create service users and systemd units." >&2
  exit 1
fi

need_cmd() { command -v "$1" >/dev/null 2>&1 || { echo "missing required command: $1" >&2; exit 1; }; }
run_as_service_user() {
  if command -v runuser >/dev/null 2>&1; then
    runuser -u "$SERVICE_USER" -- env PATH="$SERVICE_PATH" "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo -u "$SERVICE_USER" env PATH="$SERVICE_PATH" "$@"
  else
    echo "missing runuser or sudo; cannot validate worker service user runtime" >&2
    return 127
  fi
}
need_cmd python3
need_cmd git
python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit("Pullwise worker requires Python 3.9 or newer.")
PY
PYTHON_BIN="$(python3 -c 'import sys; print(sys.executable)')"
if ! command -v node >/dev/null 2>&1; then
  echo "node is required for Codex CLI; install Node.js 20+ then rerun." >&2
  exit 1
fi
NODE_MAJOR="$(node -e 'process.stdout.write(String(process.versions.node.split(".")[0]))')"
if [ "${NODE_MAJOR:-0}" -lt 20 ]; then
  echo "Node.js 20+ is required for Codex CLI. Found $(node --version)." >&2
  exit 1
fi
if ! command -v codex >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    npm install -g "$CODEX_PACKAGE"
  else
    echo "npm is required to install Codex CLI. Install codex manually and rerun." >&2
    exit 1
  fi
fi

id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONFIG_DIR" "$DATA_DIR" "$CHECKOUT_ROOT" "$LOG_DIR"

SERVICE_NODE_MAJOR="$(run_as_service_user node -e 'process.stdout.write(String(process.versions.node.split(".")[0]))' 2>/dev/null || true)"
SERVICE_NODE_VERSION="$(run_as_service_user node --version 2>/dev/null || true)"
if [ "${SERVICE_NODE_MAJOR:-0}" -lt 20 ]; then
  echo "Node.js 20+ must be available to $SERVICE_USER. Found ${SERVICE_NODE_VERSION:-not found}." >&2
  exit 1
fi

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
PULLWISE_CODEX_PACKAGE=$CODEX_PACKAGE
PULLWISE_PYTHON_BIN=$PYTHON_BIN
PULLWISE_SERVICE_PATH=$SERVICE_PATH
PULLWISE_WORKER_POLL_JITTER_SECONDS=2
PULLWISE_WORKER_MAX_BACKOFF_SECONDS=60
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
export PATH="${PULLWISE_SERVICE_PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}"
PYTHON_BIN="${PULLWISE_PYTHON_BIN:-python3}"
exec "$PYTHON_BIN" -m pullwise_worker.main "$@"
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
Environment=PATH=$SERVICE_PATH
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
run_as_service_user "$BIN_PATH" doctor || true

echo "Pullwise worker installed as $WORKER_NAME ($WORKER_ID)."
echo "If Codex is not logged in, run: sudo -u $SERVICE_USER codex login"
