#!/usr/bin/env bash
set -euo pipefail
if [ -z "${PULLWISE_WORKER_ENV_FILE:-}" ]; then
  echo "PULLWISE_WORKER_ENV_FILE must point to this worker instance env file." >&2
  exit 2
fi
if [ ! -r "$PULLWISE_WORKER_ENV_FILE" ]; then
  echo "worker env file is not readable: $PULLWISE_WORKER_ENV_FILE" >&2
  exit 2
fi
set -a
# shellcheck source=/dev/null
. "$PULLWISE_WORKER_ENV_FILE"
set +a
install_ubuntu_2204_worker_deps() {
  if [ "${PULLWISE_WORKER_AUTO_INSTALL_DEPS:-1}" = "0" ]; then
    return 0
  fi
  if [ ! -r /etc/os-release ]; then
    return 0
  fi
  # shellcheck source=/dev/null
  . /etc/os-release
  if [ "${ID:-}" != "ubuntu" ] || [ "${VERSION_ID:-}" != "22.04" ]; then
    return 0
  fi
  packages=()
  command -v python3.10 >/dev/null 2>&1 || packages+=("python3.10" "python3.10-venv")
  python3.10 -m pip --version >/dev/null 2>&1 || packages+=("python3-pip")
  command -v systemctl >/dev/null 2>&1 || packages+=("systemd")
  command -v runuser >/dev/null 2>&1 || packages+=("util-linux")
  if [ "${#packages[@]}" -eq 0 ]; then
    return 0
  fi
  if [ "$(id -u)" -ne 0 ]; then
    echo "missing worker dependencies (${packages[*]}); rerun as root to install them on Ubuntu 22.04" >&2
    return 1
  fi
  if [ "${#packages[@]}" -gt 0 ]; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"
  fi
}
if [ -z "${PULLWISE_WORKER_BIN_PATH:-}" ]; then
  echo "PULLWISE_WORKER_BIN_PATH is required in $PULLWISE_WORKER_ENV_FILE." >&2
  exit 2
fi
install_ubuntu_2204_worker_deps
exec "$PULLWISE_WORKER_BIN_PATH" cleanup
