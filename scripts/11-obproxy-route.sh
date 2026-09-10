#!/usr/bin/env bash
# Маршрутизация ODP: равномерное распределение сессий по observer.
# Явно: ./scripts/deploy.sh obproxy-route [show|apply|diagnose]

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory

ACTION="${1:-apply}"
shift || true

case "${ACTION}" in
  show|apply|diagnose)
    ;;
  *)
    die "Использование: $0 {show|apply|diagnose} [--mode even|oltp]"
    ;;
esac

python3 "${LIB_DIR}/lib/obproxy_route.py" "${ACTION}" \
  --config "${CONFIG_FILE}" \
  --inventory "${GENERATED_DIR}/inventory.env" \
  "$@"
