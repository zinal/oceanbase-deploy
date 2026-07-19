#!/usr/bin/env bash
# Удаление инфраструктуры Yandex Cloud и (опционально) кластера OBD.
# ВНИМАНИЕ: obd cluster destroy удаляет данные!

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

DESTROY_OBD="${1:-false}"

if [[ "${DESTROY_OBD}" == "--destroy-obd" ]]; then
  if [[ -f "${GENERATED_DIR}/inventory.env" ]]; then
    load_inventory
    if obd_cluster_registered "${DEPLOY_NAME}"; then
      warn "Уничтожение кластера OBD ${DEPLOY_NAME} (данные будут удалены)..."
      obd cluster destroy "${DEPLOY_NAME}" -f || true
    fi
  fi
fi

bash "${LIB_DIR}/01-provision-vms.sh" delete
info "Инфраструктура Yandex Cloud удалена"
