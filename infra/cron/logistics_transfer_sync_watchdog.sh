#!/usr/bin/env bash
set -euo pipefail
SUCCESS_FILE="${LOGISTICS_TRANSFER_SYNC_SUCCESS_FILE:-/var/lib/pricing-service/logistics_transfer_sync.last_success}"
if [[ ! -f "${SUCCESS_FILE}" ]]; then
  echo "[$(date -Iseconds)] WARNING transfer sync: no successful apply"
  exit 1
fi
age_seconds="$(( $(date +%s) - $(stat -c %Y "${SUCCESS_FILE}") ))"
if [[ "${age_seconds}" -gt 180 ]]; then
  echo "[$(date -Iseconds)] WARNING transfer sync stale age_seconds=${age_seconds}"
  exit 1
fi
