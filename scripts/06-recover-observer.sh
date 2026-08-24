#!/usr/bin/env bash
# Восстановление observer: временный отказ (--temporary) или полная потеря хоста (--replace).
# Временный: start ВМ → obd/manual start процесса → START SERVER.
# Замена: STOP SERVER → add в ту же zone → MIGRATE UNIT → DELETE SERVER.

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPTS_DIR}/lib/common.sh"
# shellcheck source=recover-common.sh
source "${SCRIPTS_DIR}/lib/recover-common.sh"

usage() {
  cat <<'USAGE'
Использование: ./scripts/06-recover-observer.sh <index> [--temporary|--replace] [опции]

<index> — номер observer в inventory (1 = zone1 / OBSERVER_1_*).

Режим (если не указан — авто: ВМ есть → --temporary, нет → --replace):
  --temporary        ВМ и диски целы: запустить машину и процессы, START SERVER
  --replace          Полная гибель хоста: пересоздать ВМ и заменить узел в кластере

Опции:
  --yes              не спрашивать подтверждение
  --dry-run          показать план, ничего не менять
  --skip-provision   только для --replace: ВМ уже создана
  --skip-sql         не выполнять SQL
  --keep-old-vm      только для --replace: не удалять старую ВМ

Документация: docs/node-recovery.md
USAGE
}

INDEX=""
MODE=""
RECOVER_YES=false
DRY_RUN=false
SKIP_PROVISION=false
SKIP_SQL=false
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
    --skip-sql) SKIP_SQL=true; shift ;;
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

[[ -n "${INDEX}" ]] || { usage >&2; die "Укажите индекс observer"; }
export RECOVER_YES

require_file "${CONFIG_FILE}"
load_inventory
yc_folder_cache_init
ensure_generated_dir

[[ "${INDEX}" -ge 1 && "${INDEX}" -le "${OBSERVER_COUNT}" ]] \
  || die "index=${INDEX} вне диапазона 1..${OBSERVER_COUNT}"

OLD_IP_VAR="OBSERVER_${INDEX}_IP"
OLD_NAME_VAR="OBSERVER_${INDEX}_NAME"
OLD_IP="${!OLD_IP_VAR:-}"
OLD_NAME="${!OLD_NAME_VAR:-}"
[[ -n "${OLD_IP}" && -n "${OLD_NAME}" ]] || die "В inventory нет OBSERVER_${INDEX}_{NAME,IP}"

ZONE="zone${INDEX}"
RPC_PORT="$(yaml_get oceanbase.ports.rpc)"
[[ -z "${RPC_PORT}" || "${RPC_PORT}" == "null" ]] && RPC_PORT=2882
DEPLOY_NAME_CLUSTER="${DEPLOY_NAME}"
OBAGENT_ON="$(yaml_get oceanbase.components.obagent)"
[[ -z "${OBAGENT_ON}" || "${OBAGENT_ON}" == "null" ]] && OBAGENT_ON=true
CS_DEDICATED="${CONFIGSERVER_DEDICATED:-false}"

ob_sys() {
  python3 "${OB_SYS}" --config "${CONFIG_FILE}" --inventory "${GENERATED_DIR}/inventory.env" "$@"
}

run_cluster_sql() {
  local sql="$1"
  if [[ "${2:-}" == "--ignore-error" ]]; then
    ob_sys sql --skip-observer "${INDEX}" --ignore-error "${sql}"
  else
    ob_sys sql --skip-observer "${INDEX}" "${sql}"
  fi
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
План временного восстановления observer ${INDEX} (${OLD_NAME}, ${OLD_IP}, ${ZONE}):
  1. yc compute instance start, если ВМ STOPPED (диски не трогаем)
  2. Дождаться RUNNING и SSH
  3. obd cluster start ${DEPLOY_NAME_CLUSTER} -c oceanbase-ce -s ${OLD_IP}
     запасной путь: cd home_path && ./bin/observer
  4. при необходимости obagent / ob-configserver (observer-1)
  5. ALTER SYSTEM START SERVER '${OLD_IP}:${RPC_PORT}'
  6. Проверить DBA_OB_SERVERS: STATUS=ACTIVE, STOP_TIME IS NULL, START_SERVICE_TIME IS NOT NULL
  Окно простоя должно быть меньше server_permanent_offline_time (по умолчанию 3600s),
  иначе узел уже снят с Paxos — нужен --replace.
EOF
}

print_replace_plan() {
  cat <<EOF
План замены observer ${INDEX} (${OLD_NAME}, ${OLD_IP}, ${ZONE}):
  1. Проверить majority через DBA_OB_SERVERS (оставшиеся ACTIVE > N/2)
  2. ALTER SYSTEM STOP SERVER '${OLD_IP}:${RPC_PORT}'
  3. Удалить ВМ ${OLD_NAME} и её диски в Yandex Cloud
  4. Создать замену с тем же именем, подготовить ОС (sysctl, data/log)
  5. obd cluster scale_out ${DEPLOY_NAME_CLUSTER} — только новый узел в ${ZONE}
  6. ALTER SYSTEM MIGRATE UNIT ... DESTINATION='<new_ip>:${RPC_PORT}'
  7. ALTER SYSTEM DELETE SERVER '${OLD_IP}:${RPC_PORT}' ZONE '${ZONE}'
  8. Вычистить ${OLD_IP} из ~/.obd/cluster/${DEPLOY_NAME_CLUSTER}/
  9. Обновить inventory и generated/obd-cluster.yaml
EOF
}

recover_observer_temporary() {
  local home_path deploy_user live_ip host zone
  home_path="$(yaml_get oceanbase.home_path)"
  deploy_user="$(yaml_get oceanbase.deploy_user)"
  [[ -z "${home_path}" || "${home_path}" == "null" ]] && home_path="/home/${deploy_user:-obadmin}/observer"

  confirm_or_die "Временно восстановить ${OLD_NAME} (${OLD_IP})? ВМ и диски сохраняются."

  ensure_instance_running "${OLD_NAME}"
  live_ip="$(get_instance_ip "${OLD_NAME}")"
  [[ -n "${live_ip}" ]] || die "Нет IP у ${OLD_NAME}"
  if [[ "${live_ip}" != "${OLD_IP}" ]]; then
    warn "IP сменился ${OLD_IP} → ${live_ip}; обновляю inventory (OBD поле ip — только IPv4)"
    ob_sys update-inventory --prefix OBSERVER --index "${INDEX}" --ip "${live_ip}" --name "${OLD_NAME}"
    python3 "${SCRIPTS_DIR}/03-generate-obd-config.py" --output "${GENERATED_DIR}/obd-cluster.yaml" || true
    OLD_IP="${live_ip}"
  fi

  zone="$(yaml_get yandex_cloud.zone)"
  host="$(yc_internal_fqdn "${OLD_NAME}" "${zone}")"

  if ! obd_start_component "${DEPLOY_NAME_CLUSTER}" "oceanbase-ce" "${OLD_IP}"; then
    warn "OBD start не удался — ручной старт ./bin/observer из home_path"
    start_binary_from_home "${host}" "${home_path}" "observer" \
      || die "Не удалось запустить observer на ${host}"
  fi

  if [[ "${OBAGENT_ON}" == "true" ]]; then
    obd_start_component "${DEPLOY_NAME_CLUSTER}" "obagent" "${OLD_IP}" \
      || warn "obagent не стартовал — метрики узла могут отсутствовать"
  fi
  if [[ "${INDEX}" -eq 1 && "${CS_DEDICATED}" != "true" ]]; then
    obd_start_component "${DEPLOY_NAME_CLUSTER}" "ob-configserver" "${OLD_IP}" \
      || warn "ob-configserver на observer-1 не стартовал"
  fi

  if [[ "${SKIP_SQL}" == "true" ]]; then
    info "SQL пропущен. Проверьте START SERVER вручную."
    return 0
  fi

  info "START SERVER ${OLD_IP}:${RPC_PORT}..."
  if ! run_cluster_sql "ALTER SYSTEM START SERVER '${OLD_IP}:${RPC_PORT}';" --ignore-error; then
    ob_sys sql --host "${OLD_IP}" --user root@sys --ignore-error \
      "ALTER SYSTEM START SERVER '${OLD_IP}:${RPC_PORT}';" \
      || warn "START SERVER не принят — процесс мог поднять сервис сам"
  fi

  local deadline status_row recovered=false
  deadline=$((SECONDS + 600))
  while (( SECONDS < deadline )); do
    status_row="$(run_cluster_sql "SELECT STATUS, STOP_TIME, START_SERVICE_TIME FROM oceanbase.DBA_OB_SERVERS WHERE SVR_IP='${OLD_IP}';" || true)"
    if [[ -z "${status_row}" ]]; then
      status_row="$(ob_sys sql --host "${OLD_IP}" --user root@sys --ignore-error \
        "SELECT STATUS, STOP_TIME, START_SERVICE_TIME FROM oceanbase.DBA_OB_SERVERS WHERE SVR_IP='${OLD_IP}';" || true)"
    fi
    info "DBA_OB_SERVERS ${OLD_IP}: ${status_row:-<нет строки>}"
    if echo "${status_row}" | awk '{print toupper($1)}' | grep -qx ACTIVE; then
      info "Узел ${OLD_IP} ACTIVE"
      recovered=true
      break
    fi
    sleep 10
  done
  if [[ "${recovered}" != "true" ]]; then
    warn "Узел ${OLD_IP} не стал ACTIVE за 600с. Если простой дольше server_permanent_offline_time — нужен --replace."
  fi

  info "Статус кластера:"
  obd cluster display "${DEPLOY_NAME_CLUSTER}" || true
  cat <<EOF

Временное восстановление observer-${INDEX} завершено.
  ВМ:  ${OLD_NAME}
  IP:  ${OLD_IP}
  Zone: ${ZONE}

Если узел не вернулся в ACTIVE — возможно истёк server_permanent_offline_time.
Тогда это уже не временный отказ: ./scripts/06-recover-observer.sh ${INDEX} --replace

EOF
}

info "Observer ${INDEX}: ${OLD_NAME} (${OLD_IP}), zone=${ZONE}"
if [[ "${INDEX}" -eq 1 && "${CS_DEDICATED}" != "true" ]]; then
  warn "observer-1 обычно несёт ob-configserver."
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
obd_cluster_registered "${DEPLOY_NAME_CLUSTER}" \
  || die "Кластер OBD ${DEPLOY_NAME_CLUSTER} не зарегистрирован"

if [[ "${MODE}" == "temporary" ]]; then
  recover_observer_temporary
  exit 0
fi

if instance_exists "${OLD_NAME}"; then
  warn "ВМ ${OLD_NAME} ещё существует. Для остановки процессов/ВМ без потери дисков используйте --temporary."
fi

if [[ "${SKIP_SQL}" != "true" ]]; then
  if ! ob_sys pick-sql --skip-observer "${INDEX}" >/dev/null; then
    die "Нет SQL-доступа к оставшимся узлам. Нужен живой observer или obproxy."
  fi
  info "Проверка majority (DBA_OB_SERVERS)..."
  active_rows="$(run_cluster_sql "SELECT SVR_IP, STATUS FROM oceanbase.DBA_OB_SERVERS;" || true)"
  if [[ -z "${active_rows}" ]]; then
    warn "Не удалось прочитать DBA_OB_SERVERS. Продолжение только с --yes."
    confirm_or_die "Продолжить без проверки majority?"
  else
    printf '%s\n' "${active_rows}"
    remaining_active="$(printf '%s\n' "${active_rows}" | awk -v old="${OLD_IP}" 'toupper($2)=="ACTIVE" && $1!=old {c++} END{print c+0}')"
    if ! python3 -c "import sys; sys.exit(0 if ${remaining_active} > ${OBSERVER_COUNT}/2 else 1)"; then
      die "Оставшихся ACTIVE=${remaining_active} из ${OBSERVER_COUNT} — нет majority. Официально: backup/restore, не замена узла."
    fi
    info "Majority есть: ACTIVE без потерянного узла = ${remaining_active}"
  fi
fi

confirm_or_die "Восстановить ${OLD_NAME} (${OLD_IP})? Старая ВМ будет удалена, данные на ней нечитаемы."

if [[ "${SKIP_SQL}" != "true" ]]; then
  info "STOP SERVER ${OLD_IP}:${RPC_PORT} (официальная изоляция)..."
  run_cluster_sql "ALTER SYSTEM STOP SERVER '${OLD_IP}:${RPC_PORT}';" --ignore-error \
    || warn "STOP SERVER не применился (узел уже мёртв) — это ожидаемо"
fi

NEW_IP="${OLD_IP}"
if [[ "${SKIP_PROVISION}" == "true" ]]; then
  NEW_IP="$(get_instance_ip "${OLD_NAME}")"
  [[ -n "${NEW_IP}" ]] || die "ВМ ${OLD_NAME} не найдена; уберите --skip-provision"
  info "Используем существующую ВМ ${OLD_NAME} -> ${NEW_IP}"
else
  if [[ "${KEEP_OLD_VM}" == "true" ]]; then
    info "Пропуск удаления старой ВМ (--keep-old-vm)"
    if instance_exists "${OLD_NAME}"; then
      die "${OLD_NAME} ещё существует. Уберите --keep-old-vm или удалите ВМ."
    fi
  fi
  recreate_role_vm "${OLD_NAME}" "observer" NEW_IP
  info "Новая ВМ ${OLD_NAME} -> ${NEW_IP}"
  zone="$(yaml_get yandex_cloud.zone)"
  new_host="$(yc_internal_fqdn "${OLD_NAME}" "${zone}")"
  bash "${SCRIPTS_DIR}/02-prepare-servers.sh" --role observer "${new_host}"
fi

if [[ "${NEW_IP}" == "${OLD_IP}" && "${SKIP_PROVISION}" != "true" ]]; then
  warn "Новый IP совпал со старым — проверяем, что это действительно новая ВМ"
fi

SCALE_OUT_OB="${GENERATED_DIR}/recover-observer-${INDEX}.yaml"
SCALE_OUT_AGENT="${GENERATED_DIR}/recover-obagent-${INDEX}.yaml"
obagent_flag=()
if [[ "${OBAGENT_ON}" == "true" ]]; then
  obagent_flag=(--obagent)
fi
ob_sys write-scale-out --role observer --index "${INDEX}" --ip "${NEW_IP}" \
  --output "${SCALE_OUT_OB}" "${obagent_flag[@]}"

# OBD официально: в файле scale_out только новый узел, без правки global исходного кластера.
# obagent лучше добавлять отдельным scale_out, если оба компонента в одном YAML не принимаются.
python3 - "${SCALE_OUT_OB}" "${SCALE_OUT_AGENT}" <<'PY'
import sys, yaml
from pathlib import Path
src = Path(sys.argv[1])
dst = Path(sys.argv[2])
data = yaml.safe_load(src.read_text())
agent = data.pop("obagent", None)
src.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
if agent:
    dst.write_text(yaml.safe_dump({"obagent": agent}, sort_keys=False, allow_unicode=True))
else:
    dst.write_text("")
PY

info "OBD scale_out oceanbase-ce (${NEW_IP}, ${ZONE})..."
obd cluster scale_out "${DEPLOY_NAME_CLUSTER}" -c "${SCALE_OUT_OB}"

if [[ -s "${SCALE_OUT_AGENT}" ]]; then
  info "OBD scale_out obagent (${NEW_IP})..."
  obd cluster scale_out "${DEPLOY_NAME_CLUSTER}" -c "${SCALE_OUT_AGENT}" \
    || warn "obagent scale_out не удался — поставьте агент вручную на ${NEW_IP}"
fi

if [[ "${SKIP_SQL}" != "true" ]]; then
  info "Ожидание ACTIVE для ${NEW_IP}..."
  deadline=$((SECONDS + 900))
  while (( SECONDS < deadline )); do
    status="$(run_cluster_sql "SELECT STATUS FROM oceanbase.DBA_OB_SERVERS WHERE SVR_IP='${NEW_IP}';" || true)"
    if echo "${status}" | grep -qi ACTIVE; then
      info "${NEW_IP} в статусе ACTIVE"
      break
    fi
    sleep 10
  done

  info "MIGRATE UNIT со старого IP на ${NEW_IP}:${RPC_PORT}..."
  units="$(run_cluster_sql "SELECT UNIT_ID FROM oceanbase.DBA_OB_UNITS WHERE SVR_IP='${OLD_IP}';" || true)"
  if [[ -z "${units}" ]]; then
    info "Unit на ${OLD_IP} нет (возможно, уже сработал permanent offline)"
  else
    while read -r unit_id; do
      [[ -n "${unit_id}" ]] || continue
      info "MIGRATE UNIT ${unit_id} -> ${NEW_IP}:${RPC_PORT}"
      run_cluster_sql "ALTER SYSTEM MIGRATE UNIT = ${unit_id} DESTINATION = '${NEW_IP}:${RPC_PORT}';" \
        || warn "Миграция unit ${unit_id} не принята — повторите вручную"
    done <<< "${units}"
  fi

  info "DELETE SERVER ${OLD_IP}:${RPC_PORT} ZONE ${ZONE}..."
  run_cluster_sql "ALTER SYSTEM DELETE SERVER '${OLD_IP}:${RPC_PORT}' ZONE '${ZONE}';" \
    || warn "DELETE SERVER не принят — если STATUS=DELETING, см. CANCEL DELETE SERVER в docs/node-recovery.md"

  deadline=$((SECONDS + 1800))
  while (( SECONDS < deadline )); do
    left="$(run_cluster_sql "SELECT SVR_IP, STATUS FROM oceanbase.DBA_OB_SERVERS WHERE SVR_IP='${OLD_IP}';" || true)"
    if [[ -z "${left}" ]]; then
      info "Старый сервер ${OLD_IP} удалён из DBA_OB_SERVERS"
      break
    fi
    info "Ожидание DELETE SERVER: ${left}"
    sleep 15
  done
fi

info "Синхронизация метаданных OBD (официальный KB: OBD не умеет scale_in)..."
ob_sys clean-obd --ip "${OLD_IP}" --deploy-name "${DEPLOY_NAME_CLUSTER}" || true
ob_sys update-inventory --prefix OBSERVER --index "${INDEX}" --ip "${NEW_IP}" --name "${OLD_NAME}"
python3 "${SCRIPTS_DIR}/03-generate-obd-config.py" --output "${GENERATED_DIR}/obd-cluster.yaml"

info "Статус кластера:"
obd cluster display "${DEPLOY_NAME_CLUSTER}" || true

cat <<EOF

Восстановление observer-${INDEX} завершено.
  Старый IP: ${OLD_IP}
  Новый IP:  ${NEW_IP}
  Zone:      ${ZONE}

Проверьте:
  SELECT SVR_IP, ZONE, STATUS FROM oceanbase.DBA_OB_SERVERS;
  SELECT UNIT_ID, ZONE, SVR_IP, STATUS FROM oceanbase.DBA_OB_UNITS;

EOF
