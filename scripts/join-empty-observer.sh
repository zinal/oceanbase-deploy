#!/usr/bin/env bash
# ERROR 4179 / leftover observer: wipe one IP, start empty, ADD SERVER.
# Не для узлов, которые уже есть в DBA_OB_SERVERS (там 06-recover-observer.sh).

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPTS_DIR}/lib/common.sh"

usage() {
  cat <<'USAGE'
Использование: ./scripts/join-empty-observer.sh <index|ip> [--yaml path] [--yes]

ERROR 4179 (add non-empty server) при пустом DBA_OB_SERVERS: observer на этом IP
уже записал clog после неудачного ADD SERVER (обычно timeout 10s), но в кластер
не вошёл. Повторный ADD SERVER без wipe не сработает.

Скрипт очищает только этот узел (home/data/redo), поднимает empty observer и
сразу делает ADD SERVER с ob_query_timeout=3600s.

  <index|ip>   номер observer в inventory (6) или IP (10.130.0.37)
  --yaml path  готовый одноузловой OBD YAML; иначе из generated/obd-cluster.yaml
  --yes        не спрашивать подтверждение

Не трогает seed observer-1..3 и уже ACTIVE узлы.
Не используйте 06-recover-observer.sh --temporary: START SERVER здесь бесполезен.

Пример (server6 / 10.130.0.37 zone3):
  ./scripts/join-empty-observer.sh 6 --yes
  ./scripts/deploy.sh join-observer 10.130.0.37 --yes
USAGE
}

TARGET=""
JOIN_YAML=""
JOIN_YES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yaml)
      [[ $# -ge 2 ]] || die "--yaml требует путь"
      JOIN_YAML="$2"
      shift 2
      ;;
    --yes) JOIN_YES=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *)
      if [[ -z "${TARGET}" ]]; then
        TARGET="$1"
        shift
      else
        die "Неизвестный аргумент: $1"
      fi
      ;;
  esac
done

[[ -n "${TARGET}" ]] || { usage >&2; die "Укажите индекс observer или IP"; }

require_file "${CONFIG_FILE}"
load_inventory
ensure_generated_dir

command -v obd >/dev/null 2>&1 || die "OBD не установлен"
obd_cluster_registered "${DEPLOY_NAME}" \
  || die "Кластер OBD ${DEPLOY_NAME} не зарегистрирован"

resolve_observer_target() {
  local arg="$1"
  local i ip_var
  if [[ "${arg}" =~ ^[0-9]+$ ]]; then
    [[ "${arg}" -ge 1 && "${arg}" -le "${OBSERVER_COUNT}" ]] \
      || die "index=${arg} вне диапазона 1..${OBSERVER_COUNT}"
    ip_var="OBSERVER_${arg}_IP"
    JOIN_INDEX="${arg}"
    JOIN_IP="${!ip_var:-}"
    [[ -n "${JOIN_IP}" ]] || die "В inventory нет ${ip_var}"
    return
  fi
  JOIN_IP="${arg}"
  JOIN_INDEX=""
  for (( i=1; i<=OBSERVER_COUNT; i++ )); do
    ip_var="OBSERVER_${i}_IP"
    if [[ "${!ip_var:-}" == "${JOIN_IP}" ]]; then
      JOIN_INDEX="${i}"
      break
    fi
  done
  [[ -n "${JOIN_INDEX}" ]] || die "IP ${JOIN_IP} нет в inventory (OBSERVER_*_IP)"
}

write_join_yaml() {
  local ip="$1" dest="$2"
  local full="${GENERATED_DIR}/obd-cluster.yaml"
  if [[ ! -f "${full}" ]]; then
    python3 "${SCRIPTS_DIR}/03-generate-obd-config.py" --output "${full}"
  fi
  require_file "${full}"
  python3 "${SCRIPTS_DIR}/lib/ob_deploy_plan.py" one-node \
    --input "${full}" --ip "${ip}" --output "${dest}"
}

JOIN_INDEX=""
JOIN_IP=""
resolve_observer_target "${TARGET}"

if ! observer_sys_sql "SELECT 1" >/dev/null; then
  die "Нет SQL к seed observer (root@sys). Проверьте ocp.root_password и OBSERVER_1_IP."
fi

if observer_is_seed_ip "${JOIN_IP}"; then
  die "Отказ трогать seed observer ${JOIN_IP} (observer-1..3)"
fi

status="$(observer_cluster_status "${JOIN_IP}" 2>/dev/null || true)"
if observer_status_is_active "${status}"; then
  info "${JOIN_IP} уже ACTIVE в DBA_OB_SERVERS — ничего делать не нужно"
  exit 0
fi
if [[ -n "${status}" ]]; then
  die "${JOIN_IP} уже в DBA_OB_SERVERS (STATUS=${status}). Это не ERROR 4179. Используйте: ./scripts/06-recover-observer.sh ${JOIN_INDEX} --temporary"
fi

info "DBA_OB_SERVERS: нет строки для ${JOIN_IP} — leftover observer (ERROR 4179), не член кластера"
info "Будет очищен только ${JOIN_IP} (index=${JOIN_INDEX}). Seed и ACTIVE узлы не трогаем."

if [[ "${JOIN_YES}" != "true" ]]; then
  read -r -p "Wipe leftover observer на ${JOIN_IP} и ADD SERVER? [y/N] " answer
  [[ "${answer}" == "y" || "${answer}" == "Y" ]] || die "Отменено"
fi

YAML_PATH="${JOIN_YAML}"
if [[ -n "${YAML_PATH}" ]]; then
  require_file "${YAML_PATH}"
else
  YAML_PATH="${GENERATED_DIR}/join-empty-${JOIN_INDEX}-${JOIN_IP}.yaml"
  write_join_yaml "${JOIN_IP}" "${YAML_PATH}"
fi

scale_out_observer "${DEPLOY_NAME}" "${YAML_PATH}"

info "Проверка DBA_OB_SERVERS:"
observer_sys_sql "SELECT SVR_IP, SVR_PORT, ZONE, STATUS FROM oceanbase.DBA_OB_SERVERS WHERE SVR_IP='${JOIN_IP}'" \
  || true
cat <<EOF

${JOIN_IP} должен быть ACTIVE. Дальше:
  ./scripts/deploy.sh deploy
(продолжит staged scale-out с ещё не зарегистрированных узлов и obagent).

EOF
