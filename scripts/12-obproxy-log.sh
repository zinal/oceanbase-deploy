#!/usr/bin/env bash
# Детальность логов ODP: syslog_level и соседние PROXYCONFIG.
# Явно: ./scripts/deploy.sh obproxy-log [show|apply]

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory

ACTION="${1:-apply}"
shift || true

case "${ACTION}" in
  show|apply|self-test)
    ;;
  *)
    die "Использование: $0 {show|apply} [--mode info|warn|debug] [--skip-if-ok]"
    ;;
esac

python3 "${LIB_DIR}/lib/obproxy_log.py" "${ACTION}" \
  --config "${CONFIG_FILE}" \
  --inventory "${GENERATED_DIR}/inventory.env" \
  "$@"
