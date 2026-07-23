#!/usr/bin/env bash
# Подготовка только OCP-ВМ: диски, sysctl, Java, clockdiff.
# Хосты готовятся параллельно; подробные логи — в generated/prepare-logs/.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory
ensure_generated_dir

PREPARE_LOG_DIR="${GENERATED_DIR}/prepare-logs"
mkdir -p "${PREPARE_LOG_DIR}"

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

declare -a PREPARE_PIDS=()
declare -a PREPARE_LABELS=()
declare -a PREPARE_LOGS=()

prepare_log_path() {
  local host="$1"
  local safe
  safe="$(printf '%s' "${host}" | tr './:' '___')"
  printf '%s/ocp-%s.log' "${PREPARE_LOG_DIR}" "${safe}"
}

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

start_prepare_job() {
  local host="$1"
  local logfile
  logfile="$(prepare_log_path "${host}")"
  : > "${logfile}"
  info "Старт: ${host} (ocp) → ${logfile}"
  (
    prepare_ocp_vm "${host}"
  ) >>"${logfile}" 2>&1 &
  PREPARE_PIDS+=($!)
  PREPARE_LABELS+=("${host} (ocp)")
  PREPARE_LOGS+=("${logfile}")
}

wait_prepare_jobs() {
  local failed=0 i status
  local -a failed_logs=()

  for i in "${!PREPARE_PIDS[@]}"; do
    status=0
    wait "${PREPARE_PIDS[$i]}" || status=$?
    if (( status == 0 )); then
      info "Готово: ${PREPARE_LABELS[$i]}"
    else
      warn "Ошибка: ${PREPARE_LABELS[$i]} (код ${status}) — см. ${PREPARE_LOGS[$i]}"
      failed_logs+=("${PREPARE_LOGS[$i]}")
      failed=1
    fi
  done

  if (( failed != 0 )); then
    for logfile in "${failed_logs[@]}"; do
      warn "----- tail ${logfile} -----"
      tail -n 40 "${logfile}" >&2 || true
    done
    die "Подготовка OCP завершилась с ошибками на ${#failed_logs[@]} хост(ах). Логи: ${PREPARE_LOG_DIR}"
  fi
}

info "Фаза prepare (OCP): параллельная подготовка (логи: ${PREPARE_LOG_DIR})"

for i in $(seq 1 "${OCP_COUNT}"); do
  start_prepare_job "$(inventory_host OCP "${i}")"
done

info "Ожидание завершения ${#PREPARE_PIDS[@]} параллельн(ых) задач(и)..."
wait_prepare_jobs

info "Подготовка OCP-ВМ завершена"
