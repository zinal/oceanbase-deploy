#!/usr/bin/env bash
# Потолок памяти ODP: proxy_mem_limited.
# Явно: ./scripts/deploy.sh obproxy-mem [show|apply]
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
    show|apply|self-test) ACTION="${arg}" ;;
    *) PY_ARGS+=("${arg}") ;;
  esac
done

require_file "${CONFIG_FILE}"
if [[ "${SKIP_IF_NONE}" == "true" && ! -f "${GENERATED_DIR}/inventory.env" ]]; then
  info "Нет generated/inventory.env — пропуск памяти ODP"
  exit 0
fi
load_inventory

if [[ "${OBPROXY_COUNT:-0}" -lt 1 ]]; then
  if [[ "${SKIP_IF_NONE}" == "true" ]]; then
    info "OBPROXY_COUNT=0 — пропуск ALTER PROXYCONFIG proxy_mem_limited"
    exit 0
  fi
  die "В inventory нет obproxy — ALTER PROXYCONFIG некуда применять"
fi

python3 "${LIB_DIR}/lib/obproxy_mem.py" "${ACTION}" \
  --config "${CONFIG_FILE}" \
  --inventory "${GENERATED_DIR}/inventory.env" \
  "${PY_ARGS[@]+"${PY_ARGS[@]}"}"
