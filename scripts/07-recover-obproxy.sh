#!/usr/bin/env bash
# Восстановление obproxy: временный отказ (--temporary) или полная потеря хоста (--replace).
# Временный: start ВМ → obd/manual start процесса.
# Замена: add-then-delete (официальная замена ODP).

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPTS_DIR}/lib/common.sh"
# shellcheck source=recover-common.sh
source "${SCRIPTS_DIR}/lib/recover-common.sh"

usage() {
  cat <<'USAGE'
Использование: ./scripts/07-recover-obproxy.sh <index> [--temporary|--replace] [опции]

<index> — номер obproxy в inventory (OBPROXY_1_* = 1).

Режим (если не указан — авто: ВМ есть → --temporary, нет → --replace):
  --temporary        ВМ цела: запустить машину и процесс obproxy
  --replace          Полная гибель хоста: пересоздать ВМ и scale_out

Опции:
  --yes              не спрашивать подтверждение
  --dry-run          показать план, ничего не менять
  --skip-provision   только для --replace: ВМ уже создана
  --keep-old-vm      только для --replace: не удалять старую ВМ

Документация: docs/node-recovery.md
USAGE
}

INDEX=""
MODE=""
RECOVER_YES=false
DRY_RUN=false
SKIP_PROVISION=false
KEEP_OLD_VM=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --temporary)
      [[ -z "${MODE}" || "${MODE}" == "temporary" ]] \
        || die "Укажите только один режим: --temporary или --replace"
      MODE=temporary
      shift
      ;;
    --replace)
      [[ -z "${MODE}" || "${MODE}" == "replace" ]] \
        || die "Укажите только один режим: --temporary или --replace"
      MODE=replace
      shift
      ;;
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

resolve_mode() {
  if [[ -n "${MODE}" ]]; then
    return 0
  fi
  if instance_exists "${OLD_NAME}"; then
    MODE=temporary
    info "Режим не указан, ВМ ${OLD_NAME} существует → --temporary"
  else
    MODE=replace
    info "Режим не указан, ВМ ${OLD_NAME} нет → --replace"
  fi
}

print_temporary_plan() {
  cat <<EOF
План временного восстановления obproxy ${INDEX} (${OLD_NAME}, ${OLD_IP}):
  1. yc compute instance start, если ВМ STOPPED
  2. Дождаться RUNNING и SSH
  3. obd cluster start ${DEPLOY_NAME} -c obproxy-ce -s ${OLD_IP}
     запасной путь: cd home_path && ./bin/obproxy
  HAProxy не меняем — IP тот же.
EOF
}

print_replace_plan() {
  cat <<EOF
План замены obproxy ${INDEX} (${OLD_NAME}, ${OLD_IP}):
  1. Удалить ВМ ${OLD_NAME} в Yandex Cloud
  2. Создать замену с тем же именем и подготовить ОС
  3. obd cluster scale_out ${DEPLOY_NAME} — только новый obproxy-ce
  4. Вычистить ${OLD_IP} из ~/.obd/cluster/${DEPLOY_NAME}/
  5. Обновить inventory и generated/obd-cluster.yaml
  6. Если есть HAProxy — убрать старый IP из backend (docs/haproxy-obproxy-tcp-lb.md)
EOF
}

recover_obproxy_temporary() {
  local home_path deploy_user live_ip host zone
  deploy_user="$(yaml_get oceanbase.deploy_user)"
  [[ -z "${deploy_user}" || "${deploy_user}" == "null" ]] && deploy_user="$(yaml_get yandex_cloud.ssh_user)"
  home_path="/home/${deploy_user:-obadmin}/obproxy"

  confirm_or_die "Временно восстановить ${OLD_NAME} (${OLD_IP})? ВМ сохраняется."
  ensure_instance_running "${OLD_NAME}"
  live_ip="$(get_instance_ip "${OLD_NAME}")"
  [[ -n "${live_ip}" ]] || die "Нет IP у ${OLD_NAME}"
  if [[ "${live_ip}" != "${OLD_IP}" ]]; then
    warn "IP сменился ${OLD_IP} → ${live_ip}; обновляю inventory"
    ob_sys update-inventory --prefix OBPROXY --index "${INDEX}" --ip "${live_ip}" --name "${OLD_NAME}"
    python3 "${SCRIPTS_DIR}/03-generate-obd-config.py" --output "${GENERATED_DIR}/obd-cluster.yaml" || true
    OLD_IP="${live_ip}"
  fi

  zone="$(yaml_get yandex_cloud.zone)"
  host="$(yc_internal_fqdn "${OLD_NAME}" "${zone}")"
  if ! obd_start_component "${DEPLOY_NAME}" "obproxy-ce" "${OLD_IP}"; then
    warn "OBD start не удался — ручной старт ./bin/obproxy"
    start_binary_from_home "${host}" "${home_path}" "obproxy" \
      || die "Не удалось запустить obproxy на ${host}"
  fi

  info "Статус кластера:"
  obd cluster display "${DEPLOY_NAME}" || true
  cat <<EOF

Временное восстановление obproxy-${INDEX} завершено.
  ВМ: ${OLD_NAME}
  IP: ${OLD_IP}

EOF
}

info "Obproxy ${INDEX}: ${OLD_NAME} (${OLD_IP})"
if [[ "${OBPROXY_COUNT}" -eq 1 ]]; then
  warn "Это единственный obproxy: пока процесс/ВМ не поднимутся, вход на 2883 недоступен."
fi

if [[ "${DRY_RUN}" == "true" ]]; then
  if [[ -z "${MODE}" ]]; then
    echo "Режим не указан. Для точного плана задайте --temporary или --replace."
    echo
    print_temporary_plan
    echo
    print_replace_plan
  elif [[ "${MODE}" == "temporary" ]]; then
    print_temporary_plan
  else
    print_replace_plan
  fi
  exit 0
fi

resolve_mode
info "Режим: ${MODE}"

command -v obd >/dev/null 2>&1 || die "OBD не установлен"
obd_cluster_registered "${DEPLOY_NAME}" \
  || die "Кластер OBD ${DEPLOY_NAME} не зарегистрирован"

if [[ "${MODE}" == "temporary" ]]; then
  recover_obproxy_temporary
  exit 0
fi

if instance_exists "${OLD_NAME}"; then
  warn "ВМ ${OLD_NAME} ещё существует. Для остановки без потери диска используйте --temporary."
fi

confirm_or_die "Заменить ${OLD_NAME} (${OLD_IP})? Старая ВМ будет удалена."

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
