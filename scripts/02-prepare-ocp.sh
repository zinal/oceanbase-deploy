#!/usr/bin/env bash
# Подготовка только OCP-ВМ: диски, sysctl, Java, clockdiff.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory

if [[ "${OCP_COUNT:-0}" -lt 1 ]]; then
  die "OCP_COUNT=0 — сначала выполните provision"
fi

DEPLOY_USER="$(yaml_get oceanbase.deploy_user)"
SSH_USER="$(yaml_get yandex_cloud.ssh_user)"
[[ -z "${DEPLOY_USER}" || "${DEPLOY_USER}" == "null" ]] && DEPLOY_USER="${SSH_USER}"

OCP_JSON="$(python3 "${LIB_DIR}/lib/vm_profiles.py" resolve ocp --config "${CONFIG_FILE}" --format json)"
read -r OCP_DATA_MOUNT OCP_DATA_ENABLED < <(
  python3 -c "import json,sys; o=json.loads(sys.argv[1]); print(o['data_disk'].get('mount_point','/ocp-data')); print(str(o['data_disk'].get('enabled',False)).lower())" "${OCP_JSON}"
)

OCP_HOME="$(yaml_get ocp.home_path)"
OCP_SOFT_DIR="$(yaml_get ocp.soft_dir)"
OCP_LOG_DIR="$(yaml_get ocp.log_dir)"

prepare_ocp_vm() {
  local host="$1"
  local need_data="${OCP_DATA_ENABLED}"

  info "Подготовка OCP-ВМ ${host}..."

  if ! run_remote "${host}" "sudo env \
ROLE='ocp' \
DEPLOY_USER='${DEPLOY_USER}' \
DATA_DISK_ENABLED='${need_data}' \
DATA_MOUNT='${OCP_DATA_MOUNT}' \
LOG_DISK_ENABLED='false' \
LOG_MOUNT='' \
DATA_DIR='' \
REDO_DIR='' \
bash -s" < "${LIB_DIR}/lib/mount-role-disks.sh"
  then
    die "Ошибка монтирования дисков на ${host}"
  fi

  if ! run_remote "${host}" "sudo bash -s" <<REMOTE
set -euo pipefail
command -v mkfs.ext4 >/dev/null 2>&1 || {
  apt-get update -qq
  apt-get install -y -qq e2fsprogs
}
swapoff -a 2>/dev/null || true
sed -i.bak '/ swap / s/^/#/' /etc/fstab 2>/dev/null || true
REMOTE
  then
    die "Ошибка базовой подготовки ${host}"
  fi

  if ! run_remote "${host}" "sudo env \
DEPLOY_USER='${DEPLOY_USER}' \
OCP_HOME='${OCP_HOME}' \
OCP_SOFT_DIR='${OCP_SOFT_DIR}' \
OCP_LOG_DIR='${OCP_LOG_DIR}' \
bash -s" < "${LIB_DIR}/lib/prepare-ocp-host.sh"
  then
    die "Ошибка установки Java/clockdiff на ${host}"
  fi

  info "OCP-ВМ готова: ${host}"
}

for i in $(seq 1 "${OCP_COUNT}"); do
  prepare_ocp_vm "$(inventory_host OCP "${i}")"
done

info "Подготовка OCP-ВМ завершена"
