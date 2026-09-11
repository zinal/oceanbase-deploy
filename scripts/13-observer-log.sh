#!/usr/bin/env bash
# Детальность логов observer: syslog_level и соседние ALTER SYSTEM.
# Явно: ./scripts/deploy.sh observer-log [show|apply]
# deploy: apply --skip-if-none --skip-if-ok

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

SKIP_IF_NONE=false
ACTION="apply"
PY_ARGS=()
for arg in "$@"; do
  case "${arg}" in
    --skip-if-none) SKIP_IF_NONE=true ;;
    show|apply|self-test) ACTION="${arg}" ;;
    *) PY_ARGS+=("${arg}") ;;
  esac
done

require_file "${CONFIG_FILE}"
if [[ "${SKIP_IF_NONE}" == "true" && ! -f "${GENERATED_DIR}/inventory.env" ]]; then
  info "Нет generated/inventory.env — пропуск логов observer"
  exit 0
fi
load_inventory

if [[ "${OBSERVER_COUNT:-0}" -lt 1 ]]; then
  if [[ "${SKIP_IF_NONE}" == "true" ]]; then
    info "OBSERVER_COUNT=0 — пропуск ALTER SYSTEM syslog_level"
    exit 0
  fi
  die "В inventory нет observer — ALTER SYSTEM некуда применять"
fi

python3 "${LIB_DIR}/lib/observer_log.py" "${ACTION}" \
  --config "${CONFIG_FILE}" \
  --inventory "${GENERATED_DIR}/inventory.env" \
  "${PY_ARGS[@]+"${PY_ARGS[@]}"}"
