#!/usr/bin/env bash
# Диагностика зависания `obd cluster start` (oceanbase bootstrap / obshell bootstrap).
#
# Типичный лог:
#   oceanbase bootstrap ok
#   obshell start ok
#   obshell program health check ok
#   obshell bootstrap -
#
# «oceanbase bootstrap ok» — надпись спиннера OBD, не результат SQL.
# Плагин obshell_bootstrap затем до 10 мин опрашивает агенты и может бесконечно
# ждать DAG take-over (`wait_dag_succeed`).
#
# Использование:
#   ./scripts/deploy.sh diagnose
#   ./scripts/diagnose-obd-start.sh [Trace-ID]
#   ./scripts/diagnose-obd-start.sh --local          # без SSH на observer
#   ./scripts/diagnose-obd-start.sh --trace <id>

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

LOCAL_ONLY=false
TRACE_ID=""
SAMPLE_OBSERVERS=3

usage() {
  cat <<'EOF'
Использование: ./scripts/diagnose-obd-start.sh [--local] [--trace TRACE_ID] [TRACE_ID]

Собирает признаки зависания obd cluster start (в т.ч. «obshell bootstrap -»):
  - число zone в generated/obd-cluster.yaml и ~/.obd/cluster/<deploy>
  - ошибки bootstrap / OBD-5000 / take over в ~/.obd/log
  - obd display-trace
  - SQL __all_zone / __all_server (если кластер отвечает)
  - identity obshell и хвост observer.log на нескольких узлах
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --local)
      LOCAL_ONLY=true
      shift
      ;;
    --trace)
      TRACE_ID="${2:-}"
      [[ -n "${TRACE_ID}" ]] || die "--trace требует Trace ID"
      shift 2
      ;;
    -*)
      die "Неизвестный флаг: $1"
      ;;
    *)
      TRACE_ID="$1"
      shift
      ;;
  esac
done

section() {
  printf '\n======== %s ========\n' "$1"
}

require_file "${CONFIG_FILE}"
OBD_CONFIG="${GENERATED_DIR}/obd-cluster.yaml"
CLUSTER_NAME=""
if [[ -f "${GENERATED_DIR}/inventory.env" ]]; then
  load_inventory
  CLUSTER_NAME="${DEPLOY_NAME:-}"
fi
if [[ -z "${CLUSTER_NAME}" || "${CLUSTER_NAME}" == "null" ]]; then
  CLUSTER_NAME="$(yaml_get deployment.name)"
fi
[[ -n "${CLUSTER_NAME}" && "${CLUSTER_NAME}" != "null" ]] || CLUSTER_NAME="ob-yc-prod"

MYSQL_PORT="$(yaml_get oceanbase.ports.mysql)"
[[ -z "${MYSQL_PORT}" || "${MYSQL_PORT}" == "null" ]] && MYSQL_PORT=2881
OBSHELL_PORT="$(yaml_get oceanbase.ports.obshell)"
[[ -z "${OBSHELL_PORT}" || "${OBSHELL_PORT}" == "null" ]] && OBSHELL_PORT=2886
HOME_PATH="$(yaml_get oceanbase.home_path)"
[[ -z "${HOME_PATH}" || "${HOME_PATH}" == "null" ]] && HOME_PATH="/home/obadmin/observer"
ROOT_PASSWORD="$(yaml_get ocp.root_password)"
[[ "${ROOT_PASSWORD}" == "null" ]] && ROOT_PASSWORD=""

ZONE_PROBLEM=0
BOOTSTRAP_HINTS=0
OBSHELL_HINTS=0

dump_zones() {
  local path="$1"
  if [[ ! -f "${path}" ]]; then
    echo "(нет файла ${path})"
    return 0
  fi
  if python3 "${LIB_DIR}/lib/ob_zones.py" check-obd "${path}" --dump; then
    echo "OK: ${path} — не больше 7 уникальных zone"
  else
    ZONE_PROBLEM=1
  fi
}

section "Конфигурация"
echo "deploy: ${CLUSTER_NAME}"
echo "generated: ${OBD_CONFIG}"
echo "obd home: ${HOME}/.obd/cluster/${CLUSTER_NAME}"
dump_zones "${OBD_CONFIG}"
if [[ -d "${HOME}/.obd/cluster/${CLUSTER_NAME}" ]]; then
  shopt -s nullglob
  local_yamls=("${HOME}/.obd/cluster/${CLUSTER_NAME}"/*.yaml "${HOME}/.obd/cluster/${CLUSTER_NAME}"/*.yml)
  shopt -u nullglob
  if [[ ${#local_yamls[@]} -eq 0 ]]; then
    echo "(в ~/.obd/cluster/${CLUSTER_NAME} нет yaml)"
  else
    for yaml_path in "${local_yamls[@]}"; do
      dump_zones "${yaml_path}"
    done
  fi
else
  echo "Кластер не зарегистрирован в OBD (~/.obd/cluster/${CLUSTER_NAME} нет)."
  echo "obd cluster start использует зарегистрированный конфиг, не generated/obd-cluster.yaml."
fi

section "Логи OBD (~/.obd/log)"
OBD_LOG_DIR="${HOME}/.obd/log"
if [[ ! -d "${OBD_LOG_DIR}" ]]; then
  echo "Каталог ${OBD_LOG_DIR} не найден."
else
  mapfile -t log_files < <(find "${OBD_LOG_DIR}" -type f \( -name 'obd*' -o -name '*.log' \) 2>/dev/null | head -20)
  if [[ ${#log_files[@]} -eq 0 ]]; then
    echo "Файлов логов нет в ${OBD_LOG_DIR}"
  else
    echo "Файлы: ${log_files[*]}"
    if grep -R -E -i 'OBD-5000|SIZE_OVERFLOW|alter system bootstrap|take over|obshell bootstrap' "${log_files[@]}" 2>/dev/null \
      | tail -n 80; then
      BOOTSTRAP_HINTS=1
    else
      echo "Ключевых строк bootstrap/OBD-5000/take over не найдено (нужен display-trace)."
    fi
    if [[ -z "${TRACE_ID}" ]]; then
      TRACE_ID="$(grep -R -h -E 'Trace ID:[[:space:]]*[0-9a-f-]{8,}' "${log_files[@]}" 2>/dev/null \
        | tail -1 | grep -oE '[0-9a-f-]{8,}' | tail -1 || true)"
    fi
  fi
fi

section "obd display-trace"
if [[ -n "${TRACE_ID}" ]]; then
  echo "Trace ID: ${TRACE_ID}"
  if command -v obd >/dev/null 2>&1; then
    obd display-trace "${TRACE_ID}" 2>&1 | tail -n 120 || warn "obd display-trace ${TRACE_ID} не удался"
  else
    warn "obd не в PATH — выполните: obd display-trace ${TRACE_ID}"
  fi
else
  echo "Trace ID не задан. Возьмите его из вывода OBD и повторите:"
  echo "  obd display-trace <Trace-ID>"
  echo "  ./scripts/diagnose-obd-start.sh <Trace-ID>"
fi

sql_client() {
  if command -v mysql >/dev/null 2>&1; then
    echo mysql
  elif command -v obclient >/dev/null 2>&1; then
    echo obclient
  else
    echo ""
  fi
}

run_sql() {
  local host="$1" sql="$2"
  local client pass
  client="$(sql_client)"
  [[ -n "${client}" ]] || return 1
  for pass in "" "${ROOT_PASSWORD}"; do
    if [[ -z "${pass}" ]]; then
      if "${client}" -h"${host}" -P"${MYSQL_PORT}" -uroot -Nse "${sql}" 2>/dev/null; then
        return 0
      fi
    else
      if MYSQL_PWD="${pass}" "${client}" -h"${host}" -P"${MYSQL_PORT}" -uroot -Nse "${sql}" 2>/dev/null; then
        return 0
      fi
    fi
  done
  return 1
}

check_obshell_http() {
  local ip="$1"
  local url out
  for url in \
    "http://${ip}:${OBSHELL_PORT}/api/v1/info" \
    "https://${ip}:${OBSHELL_PORT}/api/v1/info"; do
    if out="$(curl -skf --max-time 5 "${url}" 2>/dev/null)"; then
      printf '%s -> %s\n' "${url}" "$(printf '%s' "${out}" | tr '\n' ' ' | head -c 400)"
      OBSHELL_HINTS=1
      return 0
    fi
  done
  echo "${ip}:${OBSHELL_PORT} — HTTP info недоступен с управляющей машины (curl -skf --max-time 5)"
  return 1
}

if [[ "${LOCAL_ONLY}" != "true" ]]; then
  section "SQL observer (sys)"
  if [[ -z "${OBSERVER_1_IP:-}" ]]; then
    echo "Нет inventory (OBSERVER_1_IP). Пропуск SQL. Сгенерируйте: ./scripts/deploy.sh provision"
  elif [[ -z "$(sql_client)" ]]; then
    echo "Нет mysql/obclient. Пропуск SQL. Вручную:"
    echo "  mysql -h${OBSERVER_1_IP} -P${MYSQL_PORT} -uroot"
    echo "  SELECT zone, COUNT(*) FROM oceanbase.__all_server GROUP BY zone;"
    echo "  SELECT zone FROM oceanbase.__all_zone;"
  else
    echo "Подключение: ${OBSERVER_1_IP}:${MYSQL_PORT} (пустой пароль, затем ocp.root_password)"
    if run_sql "${OBSERVER_1_IP}" "select zone, count(*) from oceanbase.__all_server group by zone"; then
      echo "--- __all_zone ---"
      run_sql "${OBSERVER_1_IP}" "select zone from oceanbase.__all_zone" || true
      echo "--- __all_server (ip, zone, status) ---"
      run_sql "${OBSERVER_1_IP}" "select svr_ip, zone, status from oceanbase.__all_server" || true
    else
      echo "SQL к ${OBSERVER_1_IP}:${MYSQL_PORT} не прошёл."
      echo "Если спиннер уже показал «oceanbase bootstrap ok» / «Connect to observer … ok», но SQL мёртв —"
      echo "bootstrap, скорее всего, не завершился (см. observer.log: SIZE_OVERFLOW / execute_bootstrap)."
      BOOTSTRAP_HINTS=1
    fi
  fi

  section "obshell / observer.log (выборка узлов)"
  if [[ -z "${OBSERVER_COUNT:-}" || "${OBSERVER_COUNT}" -lt 1 ]]; then
    echo "Нет OBSERVER_COUNT в inventory — пропуск SSH."
  else
    local_max="${OBSERVER_COUNT}"
    if [[ "${local_max}" -gt "${SAMPLE_OBSERVERS}" ]]; then
      local_max="${SAMPLE_OBSERVERS}"
    fi
    echo "Проверяю observer 1..${local_max} из ${OBSERVER_COUNT} (порт obshell ${OBSHELL_PORT})"
    i=""
    host=""
    ip=""
    for (( i=1; i<=local_max; i++ )); do
      ip_var="OBSERVER_${i}_IP"
      ip="${!ip_var:-}"
      host="$(inventory_host OBSERVER "${i}" 2>/dev/null || echo "${ip}")"
      echo "----- observer-${i} ${host} ${ip} -----"
      if [[ -n "${ip}" ]]; then
        check_obshell_http "${ip}" || true
      fi
      if [[ -n "${host}" ]] && run_remote "${host}" "bash -s" <<REMOTE; then
set +e
echo "obshell pid: \$(cat '${HOME_PATH}/run/obshell.pid' 2>/dev/null || echo none)"
echo "listener 2881/2882/2886:"
ss -lnt 2>/dev/null | grep -E ':2881|:2882|:2886' || netstat -lnt 2>/dev/null | grep -E ':2881|:2882|:2886' || true
if command -v chronyc >/dev/null 2>&1; then
  echo "chronyc tracking:"
  chronyc tracking 2>/dev/null | egrep 'System time|Leap status|Last offset' || true
fi
echo "observer.log (bootstrap / SIZE_OVERFLOW):"
for f in '${HOME_PATH}/log/observer.log' '${HOME_PATH}/log/observer.log.wf'; do
  [[ -f "\$f" ]] || continue
  grep -E 'SIZE_OVERFLOW|execute_bootstrap|alter system bootstrap|OB_SIZE_OVERFLOW' "\$f" 2>/dev/null | tail -n 8
done
echo "obshell.log (take over / error):"
for f in '${HOME_PATH}/log_obshell/obshell.log' '${HOME_PATH}/log/obshell.log'; do
  [[ -f "\$f" ]] || continue
  grep -E -i 'take.?over|bootstrap|error|timeout|failed' "\$f" 2>/dev/null | tail -n 12
done
REMOTE
        :
      else
        echo "SSH ${host} недоступен"
      fi
    done
  fi
else
  echo
  echo "--local: SQL и SSH пропущены."
fi

section "Вывод"
if [[ "${ZONE_PROBLEM}" -eq 1 ]]; then
  cat <<EOF
Причина: в конфиге OBD больше 7 уникальных OceanBase zone.
При 30 observer старый генератор ставил zone1..zone30. ALTER SYSTEM BOOTSTRAP
падает (OB_MAX_MEMBER_NUMBER=7), спиннер всё равно пишет «oceanbase bootstrap ok»,
OBD идёт дальше и зависает на ожидании серверов либо на «obshell bootstrap -».

Что делать:
  1. Ctrl+C, если start ещё висит
  2. ./scripts/deploy.sh config          # перегенерировать yaml (3 zone)
  3. obd cluster destroy ${CLUSTER_NAME} -f
  4. ./scripts/deploy.sh deploy

Правки generated/obd-cluster.yaml недостаточно для уже зарегистрированного кластера:
obd cluster start читает ~/.obd/cluster/${CLUSTER_NAME}/.
EOF
  exit 2
fi

cat <<EOF
Если unique zone ≤ 7, зависание «obshell bootstrap -» — это ожидание take-over
агентов obshell (плагин OBD: опрос /api/v1/info до 200×3с, затем wait_dag_succeed
без таймаута). На 30 узлах это может идти много минут.

Проверьте по сбору выше:
  - SQL __all_server: все ли observer ACTIVE, сколько zone в __all_zone
  - identity obshell: TAKE_OVER_MASTER / CLUSTER_AGENT vs SINGLE
  - obshell.log: застрявший DAG take-over
  - chrony: рассинхрон часов ломает агенты
  - с jump host curl http://<ip>:${OBSHELL_PORT}/api/v1/info (health check OBD
    часто идёт по SSH локально, а bootstrap — HTTP с управляющей машины)

Если SQL к sys не работает после «Connect to observer ok» — bootstrap не прошёл,
смотрите observer.log и display-trace, не obshell. Кластер без успешного
bootstrap чинится только destroy -f и повторным deploy.
EOF
exit 0
