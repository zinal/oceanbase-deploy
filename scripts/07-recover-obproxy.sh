#!/usr/bin/env bash
# Восстановление после полной потери одного хоста obproxy.
# Официально: сначала добавить новый ODP, затем убрать старый (add-then-delete).

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPTS_DIR}/lib/common.sh"
# shellcheck source=recover-common.sh
source "${SCRIPTS_DIR}/lib/recover-common.sh"

usage() {
  cat <<'USAGE'
Использование: ./scripts/07-recover-obproxy.sh <index> [опции]

<index> — номер obproxy в inventory (OBPROXY_1_* = 1).

Опции:
  --yes              не спрашивать подтверждение
  --dry-run          показать план, ничего не менять
  --skip-provision   ВМ уже создана; только OBD scale_out
  --keep-old-vm      не удалять старую ВМ в YC

Документация: docs/node-recovery.md
USAGE
}

INDEX=""
RECOVER_YES=false
DRY_RUN=false
SKIP_PROVISION=false
KEEP_OLD_VM=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) RECOVER_YES=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --skip-provision) SKIP_PROVISION=true; shift ;;
    --keep-old-vm) KEEP_OLD_VM=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ -z "${INDEX}" && "$1" =~ ^[0-9]+$ ]]; then
        INDEX="$1"
        shift
      else
        die "Неизвестный аргумент: $1"
      fi
      ;;
  esac
done

[[ -n "${INDEX}" ]] || { usage >&2; die "Укажите индекс obproxy"; }
export RECOVER_YES

require_file "${CONFIG_FILE}"
load_inventory
yc_folder_cache_init
ensure_generated_dir

[[ "${OBPROXY_COUNT:-0}" -gt 0 ]] || die "В inventory нет obproxy"
[[ "${INDEX}" -ge 1 && "${INDEX}" -le "${OBPROXY_COUNT}" ]] \
  || die "index=${INDEX} вне диапазона 1..${OBPROXY_COUNT}"

OLD_IP_VAR="OBPROXY_${INDEX}_IP"
OLD_NAME_VAR="OBPROXY_${INDEX}_NAME"
OLD_IP="${!OLD_IP_VAR:-}"
OLD_NAME="${!OLD_NAME_VAR:-}"
[[ -n "${OLD_IP}" && -n "${OLD_NAME}" ]] || die "В inventory нет OBPROXY_${INDEX}_{NAME,IP}"

ob_sys() {
  python3 "${OB_SYS}" --config "${CONFIG_FILE}" --inventory "${GENERATED_DIR}/inventory.env" "$@"
}

info "Потерянный obproxy: ${OLD_NAME} (${OLD_IP})"
if [[ "${OBPROXY_COUNT}" -eq 1 ]]; then
  warn "Это единственный obproxy: клиенты без прямого доступа к observer:2881 останутся без входа, пока замена не встанет."
fi

if [[ "${DRY_RUN}" == "true" ]]; then
  cat <<EOF
План восстановления obproxy ${INDEX}:
  1. Удалить ВМ ${OLD_NAME} в Yandex Cloud
  2. Создать замену с тем же именем и подготовить ОС
  3. obd cluster scale_out ${DEPLOY_NAME} — только новый obproxy-ce
  4. Вычистить ${OLD_IP} из ~/.obd/cluster/${DEPLOY_NAME}/
  5. Обновить inventory и generated/obd-cluster.yaml
  6. Если есть HAProxy — убрать старый IP из backend (docs/haproxy-obproxy-tcp-lb.md)
EOF
  exit 0
fi

command -v obd >/dev/null 2>&1 || die "OBD не установлен"
obd_cluster_registered "${DEPLOY_NAME}" \
  || die "Кластер OBD ${DEPLOY_NAME} не зарегистрирован"

confirm_or_die "Восстановить ${OLD_NAME} (${OLD_IP})? Старая ВМ будет удалена."

NEW_IP="${OLD_IP}"
if [[ "${SKIP_PROVISION}" == "true" ]]; then
  NEW_IP="$(get_instance_ip "${OLD_NAME}")"
  [[ -n "${NEW_IP}" ]] || die "ВМ ${OLD_NAME} не найдена; уберите --skip-provision"
else
  if [[ "${KEEP_OLD_VM}" == "true" ]]; then
    if instance_exists "${OLD_NAME}"; then
      die "${OLD_NAME} ещё существует. Уберите --keep-old-vm или удалите ВМ."
    fi
  fi
  recreate_role_vm "${OLD_NAME}" "obproxy" NEW_IP
  zone="$(yaml_get yandex_cloud.zone)"
  new_host="$(yc_internal_fqdn "${OLD_NAME}" "${zone}")"
  bash "${SCRIPTS_DIR}/02-prepare-servers.sh" --role obproxy "${new_host}"
fi

SCALE_OUT="${GENERATED_DIR}/recover-obproxy-${INDEX}.yaml"
ob_sys write-scale-out --role obproxy --index "${INDEX}" --ip "${NEW_IP}" --output "${SCALE_OUT}"

info "OBD scale_out obproxy-ce (${NEW_IP})..."
obd cluster scale_out "${DEPLOY_NAME}" -c "${SCALE_OUT}"

info "Синхронизация метаданных OBD..."
ob_sys clean-obd --ip "${OLD_IP}" --deploy-name "${DEPLOY_NAME}" || true
ob_sys update-inventory --prefix OBPROXY --index "${INDEX}" --ip "${NEW_IP}" --name "${OLD_NAME}"
python3 "${SCRIPTS_DIR}/03-generate-obd-config.py" --output "${GENERATED_DIR}/obd-cluster.yaml"

info "Статус кластера:"
obd cluster display "${DEPLOY_NAME}" || true

cat <<EOF

Восстановление obproxy-${INDEX} завершено.
  Старый IP: ${OLD_IP}
  Новый IP:  ${NEW_IP}

Если перед obproxy стоит HAProxy — обновите backend (старый IP больше не существует).
См. docs/haproxy-obproxy-tcp-lb.md и config/haproxy-obproxy-tcp-lb.cfg.example.

EOF
