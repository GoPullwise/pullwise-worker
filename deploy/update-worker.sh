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
if [ -z "${PULLWISE_WORKER_BIN_PATH:-}" ]; then
  echo "PULLWISE_WORKER_BIN_PATH is required in $PULLWISE_WORKER_ENV_FILE." >&2
  exit 2
fi
exec "$PULLWISE_WORKER_BIN_PATH" update
