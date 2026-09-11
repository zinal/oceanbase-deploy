#!/usr/bin/env bash
# Маршрутизация ODP: равномерное распределение сессий по observer.
# Явно: ./scripts/deploy.sh obproxy-route [show|apply|diagnose]
# deploy/all: apply --skip-if-none --skip-if-ok

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
    show|apply|diagnose) ACTION="${arg}" ;;
    *) PY_ARGS+=("${arg}") ;;
  esac
done

require_file "${CONFIG_FILE}"
if [[ "${SKIP_IF_NONE}" == "true" && ! -f "${GENERATED_DIR}/inventory.env" ]]; then
  info "Нет generated/inventory.env — пропуск маршрутизации ODP"
  exit 0
fi
load_inventory

if [[ "${OBPROXY_COUNT:-0}" -lt 1 ]]; then
  if [[ "${SKIP_IF_NONE}" == "true" ]]; then
    info "OBPROXY_COUNT=0 — пропуск ALTER PROXYCONFIG"
    exit 0
  fi
  die "В inventory нет obproxy — ALTER PROXYCONFIG некуда применять"
fi

python3 "${LIB_DIR}/lib/obproxy_route.py" "${ACTION}" \
  --config "${CONFIG_FILE}" \
  --inventory "${GENERATED_DIR}/inventory.env" \
  "${PY_ARGS[@]+"${PY_ARGS[@]}"}"
