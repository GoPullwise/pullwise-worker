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
PROVIDER_CHAIN=""
WORKER_PACKAGE=""
DEFAULT_WORKER_VERSION="0.2.7"
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
    --provider-chain) PROVIDER_CHAIN="${2:-codex}"; shift 2 ;;
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
if [ -z "$PROVIDER_CHAIN" ]; then
  PROVIDER_CHAIN="${PULLWISE_PROVIDER_CHAIN:-$PROVIDER}"
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

write_env_value() {
  local key="$1"
  local value="$2"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    echo "environment value for $key must be single-line" >&2
    exit 2
  fi
  printf '%s=%s\n' "$key" "$value" >> "$ENV_FILE"
}

: > "$ENV_FILE"
write_env_value PULLWISE_SERVER_URL "$SERVER_URL"
write_env_value PULLWISE_WORKER_ID "$WORKER_ID"
write_env_value PULLWISE_WORKER_TOKEN "$WORKER_TOKEN"
write_env_value PULLWISE_PROVIDER "$PROVIDER"
write_env_value PULLWISE_PROVIDER_CHAIN "$PROVIDER_CHAIN"
write_env_value PULLWISE_MAX_CONCURRENT_JOBS "$MAX_CONCURRENT_JOBS"
write_env_value PULLWISE_CHECKOUT_ROOT "$CHECKOUT_ROOT"
write_env_value PULLWISE_LOG_DIR "$LOG_DIR"
write_env_value PULLWISE_WORKER_PACKAGE "$WORKER_PACKAGE"
write_env_value PULLWISE_CODEX_PACKAGE "$CODEX_PACKAGE"
write_env_value PULLWISE_CODEX_MODEL "${PULLWISE_CODEX_MODEL:-gpt-5.5}"
write_env_value PULLWISE_CODEX_REASONING_EFFORT "${PULLWISE_CODEX_REASONING_EFFORT:-medium}"
write_env_value PULLWISE_OPENCODE_COMMAND "${PULLWISE_OPENCODE_COMMAND:-opencode}"
write_env_value PULLWISE_OPENCODE_MODEL "${PULLWISE_OPENCODE_MODEL:-opencode/big-pickle}"
write_env_value PULLWISE_OPENCODE_VARIANT "${PULLWISE_OPENCODE_VARIANT:-medium}"
write_env_value PULLWISE_PYTHON_BIN "$PYTHON_BIN"
write_env_value PULLWISE_SERVICE_PATH "$SERVICE_PATH"
write_env_value PULLWISE_WORKER_POLL_JITTER_SECONDS "2"
write_env_value PULLWISE_WORKER_MAX_BACKOFF_SECONDS "60"
write_env_value PULLWISE_WORKER_CLEANUP_INTERVAL_SECONDS "3600"
write_env_value PULLWISE_RETAIN_FAILED_CHECKOUT_SECONDS "0"
write_env_value PULLWISE_MAX_CHECKOUT_BYTES "21474836480"
write_env_value PULLWISE_LOG_RETENTION_SECONDS "1209600"
write_env_value PULLWISE_MAX_LOG_BYTES "1073741824"
write_env_value PULLWISE_SCAN_SUMMARY_LOG_MAX_BYTES "10485760"
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

cat > "$BIN_PATH" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
load_worker_env() {
  local env_file="$1"
  local key value
  [ -f "$env_file" ] || return 0
  while IFS="=" read -r key value || [ -n "$key" ]; do
    [[ -z "$key" || "$key" == \#* ]] && continue
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    export "$key=$value"
  done < "$env_file"
}
load_worker_env /etc/pullwise-worker/worker.env
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
Restart=on-failure
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
echo "If Codex is not logged in, run: sudo -u $SERVICE_USER env HOME=$DATA_DIR PATH=$SERVICE_PATH codex login --device-auth"
