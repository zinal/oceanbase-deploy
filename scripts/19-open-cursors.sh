#!/usr/bin/env bash
# Лимит PS-хендлов на сессии: tenant-параметр open_cursors.
# Явно: ./scripts/deploy.sh open-cursors [show|apply]
# tenant: apply --skip-if-none --skip-if-ok

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
  info "Нет generated/inventory.env — пропуск open_cursors"
  exit 0
fi
load_inventory

obs_count="${OBSERVER_COUNT:-0}"
proxy_count="${OBPROXY_COUNT:-0}"
if [[ "${obs_count}" -lt 1 && "${proxy_count}" -lt 1 ]]; then
  if [[ "${SKIP_IF_NONE}" == "true" ]]; then
    info "Нет observer/obproxy — пропуск ALTER SYSTEM open_cursors"
    exit 0
  fi
  die "В inventory нет SQL-endpoint — ALTER SYSTEM open_cursors некуда применять"
fi

python3 "${LIB_DIR}/lib/open_cursors.py" "${ACTION}" \
  --config "${CONFIG_FILE}" \
  --inventory "${GENERATED_DIR}/inventory.env" \
  "${PY_ARGS[@]+"${PY_ARGS[@]}"}"
