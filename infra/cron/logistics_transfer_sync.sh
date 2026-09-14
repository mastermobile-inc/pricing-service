#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/MM/pricing-service-task43-current}"
cd "${REPO_DIR}"
if [[ -f "${REPO_DIR}/.env" ]]; then
  source "${REPO_DIR}/infra/cron/load_env.sh"
  load_env_file_preserve_json "${REPO_DIR}/.env"
fi
LOG_FILE="${LOGISTICS_TRANSFER_SYNC_LOG_FILE:-/var/log/pricing/logistics_transfer_sync.log}"
LOCK_FILE="${LOGISTICS_TRANSFER_SYNC_LOCK_FILE:-/tmp/logistics_transfer_sync.lock}"
SUCCESS_FILE="${LOGISTICS_TRANSFER_SYNC_SUCCESS_FILE:-/var/lib/pricing-service/logistics_transfer_sync.last_success}"
APPLY="${LOGISTICS_TRANSFER_SYNC_APPLY:-false}"
mkdir -p "$(dirname "${LOG_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[$(date -Iseconds)] transfer sync skipped: previous run active" >> "${LOG_FILE}"
  exit 0
fi
cmd=("${REPO_DIR}/.venv/bin/python" -m tasks.sync_logistics_transfers_from_onec
  --date-from "$(date -u -d '14 days ago' +%F)" --limit 500)
if [[ "${APPLY}" == "true" ]]; then cmd+=(--apply); fi
echo "[$(date -Iseconds)] transfer sync starting apply=${APPLY}" >> "${LOG_FILE}"
if timeout --kill-after=5 49 "${cmd[@]}" >> "${LOG_FILE}" 2>&1; then
  if [[ "${APPLY}" == "true" ]]; then
    mkdir -p "$(dirname "${SUCCESS_FILE}")"
    touch "${SUCCESS_FILE}"
  fi
else
  result=$?
  echo "[$(date -Iseconds)] transfer sync failed status=${result}" >> "${LOG_FILE}"
  exit "${result}"
fi
