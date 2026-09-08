#!/usr/bin/env bash
# Создание user tenant после deploy (oceanbase-skills/tenant-management).
# Явно вызывается: ./scripts/deploy.sh tenant

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory

OBD_CONFIG="${GENERATED_DIR}/obd-cluster.yaml"
CLUSTER_NAME="${DEPLOY_NAME}"

[[ -f "${OBD_CONFIG}" ]] || die "Сначала выполните: ./scripts/deploy.sh config"
command -v obd >/dev/null 2>&1 || die "OBD не установлен. Выполните: ./scripts/deploy.sh deploy"

if ! obd_cluster_registered "${CLUSTER_NAME}"; then
  die "Кластер ${CLUSTER_NAME} не развёрнут. Сначала: ./scripts/deploy.sh deploy"
fi

info "Проверка секции tenant в ${CONFIG_FILE}..."
python3 "${LIB_DIR}/lib/tenant-create.py" validate --config "${CONFIG_FILE}"

info "Создание тенанта (obd cluster tenant create + user/database)..."
python3 "${LIB_DIR}/lib/tenant-create.py" create \
  --config "${CONFIG_FILE}" \
  --inventory "${GENERATED_DIR}/inventory.env"
