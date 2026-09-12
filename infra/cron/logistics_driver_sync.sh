#!/usr/bin/env bash
set -euo pipefail
REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
cd "$REPO_DIR"
source "$REPO_DIR/infra/cron/load_env.sh"
load_env_file_preserve_json "${ENV_FILE:-$REPO_DIR/.env}"
[[ "${LOGISTICS_DRIVER_BITRIX_SYNC_ENABLED:-false}" == "true" ]] || exit 0
exec 9>"${LOGISTICS_DRIVER_SYNC_LOCK_FILE:-/var/lock/logistics_driver_sync.lock}"
flock -n 9 || exit 0
if timeout 50 "$REPO_DIR/.venv/bin/python" -m tasks.sync_logistics_drivers_from_bitrix --apply; then
  exit 0
else
  status=$?
  echo "[$(date -Iseconds)] logistics_driver_sync failed status=$status; previous snapshot preserved"
  exit "$status"
fi
