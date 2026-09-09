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
  - obd display-trace (нужен Trace ID от start, не от deploy)
  - SQL __all_zone / __all_server / SHOW DATABASES (ocs)
  - identity obshell на всех observer
  - DAG take-over на TAKE OVER MASTER (X-OCS-Header, не голый curl)
  - SQL SHOW TABLES FROM ocs
  - хвост obshell.log сначала на master, затем на выборке follower
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
IDENTITIES_FILE=""
DAG_FILE=""
MASTER_IPS=()

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
    echo "--- bootstrap / SIZE_OVERFLOW / obshell (без цикла __all_server) ---"
    if grep -R -E -i 'SIZE_OVERFLOW|alter system bootstrap|obshell bootstrap|obshell take over|Cluster init failed' \
      "${log_files[@]}" 2>/dev/null | grep -v '__all_server execute failed' | tail -n 40; then
      BOOTSTRAP_HINTS=1
    else
      echo "Ключевых строк bootstrap/obshell не найдено (нужен display-trace)."
    fi
    if [[ -z "${TRACE_ID}" ]]; then
      # Trace ID из вывода OBD часто принадлежит `cluster deploy`, а не start.
      TRACE_ID="$(grep -h -E 'alter system bootstrap|obshell bootstrap' "${HOME}/.obd/log/obd" 2>/dev/null \
        | tail -1 | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || true)"
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

obshell_identity_from_json() {
  python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print("PARSE_ERROR")
    raise SystemExit(0)
data=d.get("data") or {}
print("%s\t%s" % (data.get("identity") or "?", data.get("ip") or "?"))'
}

dump_obshell_dag() {
  python3 -c 'import json,sys
STATES={0:"PENDING",1:"READY",2:"RUNNING",3:"FAILED",4:"SUCCEED"}
def st(v):
    if isinstance(v,int):
        return STATES.get(v, str(v))
    return str(v or "?")
try:
    d=json.load(sys.stdin)
except Exception as e:
    print("DAG parse error:", e)
    print("DAG_STATE=PARSE_ERROR")
    raise SystemExit(0)
data=d.get("data")
if not data:
    err=d.get("error") or {}
    print("нет data:", err.get("message") or d)
    print("DAG_STATE=MISSING")
    raise SystemExit(0)
state=st(data.get("state"))
print("DAG_STATE=%s" % state)
print("name=%s  state=%s  stage=%s/%s  operator=%s" % (
    data.get("name"), state, data.get("stage"), data.get("max_stage"), data.get("operator")))
print("id=%s  start=%s  end=%s" % (data.get("id"), data.get("start_time"), data.get("end_time")))
for node in data.get("nodes") or []:
    print("  node %s  state=%s  stage=%s" % (node.get("name"), st(node.get("state")), node.get("stage")))
    for sub in node.get("sub_nodes") or node.get("subNodes") or []:
        print("    sub %s  state=%s" % (sub.get("name"), st(sub.get("state"))))
'
}

observer_index_for_ip() {
  local want="$1" i ip_var
  [[ -n "${want}" && -n "${OBSERVER_COUNT:-}" ]] || return 1
  for (( i=1; i<=OBSERVER_COUNT; i++ )); do
    ip_var="OBSERVER_${i}_IP"
    if [[ "${!ip_var:-}" == "${want}" ]]; then
      printf '%s' "${i}"
      return 0
    fi
  done
  return 1
}

dump_remote_observer() {
  local idx="$1" ip="$2" host
  host="$(inventory_host OBSERVER "${idx}" 2>/dev/null || echo "${ip}")"
  echo "----- observer-${idx} ${host} ${ip} -----"
  if [[ -n "${ip}" ]]; then
    check_obshell_http "${ip}" || true
  fi
  if [[ -z "${host}" ]]; then
    echo "нет SSH-хоста для observer-${idx}"
    return 0
  fi
  if run_remote "${host}" "bash -s" <<REMOTE; then
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
  grep -E -i 'take.?over|create take over dag|Unknown database|not OCS|bootstrap|error|timeout|failed|SUCCEED|FAILED|CLUSTER AGENT' "\$f" 2>/dev/null | tail -n 25
done
REMOTE
    :
  else
    echo "SSH ${host} недоступен"
  fi
}

check_obshell_http() {
  local ip="$1"
  local url out ident
  for url in \
    "http://${ip}:${OBSHELL_PORT}/api/v1/info" \
    "https://${ip}:${OBSHELL_PORT}/api/v1/info"; do
    if out="$(curl -skf --max-time 5 "${url}" 2>/dev/null)"; then
      ident="$(printf '%s' "${out}" | obshell_identity_from_json)"
      printf '%s\t%s\n' "${ip}" "${ident}"
      OBSHELL_HINTS=1
      return 0
    fi
  done
  echo "${ip}	UNREACHABLE"
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
      echo "--- distinct zone ---"
      run_sql "${OBSERVER_1_IP}" "select distinct zone from oceanbase.__all_zone" || true
      echo "--- databases (ocs нужна obshell take-over) ---"
      run_sql "${OBSERVER_1_IP}" "show databases" || true
      echo "--- ocs tables ---"
      run_sql "${OBSERVER_1_IP}" "select table_name from information_schema.tables where table_schema='ocs' order by table_name" || true
      echo "--- ocs agents / dags (имена таблиц зависят от версии obshell) ---"
      run_sql "${OBSERVER_1_IP}" "select * from ocs.all_agent" || true
      run_sql "${OBSERVER_1_IP}" "select id, name, state, operator, start_time, end_time from ocs.task_dag order by id desc limit 15" || true
      run_sql "${OBSERVER_1_IP}" "select id, name, state, operator, start_time, end_time from ocs.dag_instance order by id desc limit 15" || true
      echo "--- __all_server status ---"
      run_sql "${OBSERVER_1_IP}" "select status, count(*) from oceanbase.__all_server group by status" || true
      echo "--- __all_server (ip, zone, status) ---"
      run_sql "${OBSERVER_1_IP}" "select svr_ip, zone, status from oceanbase.__all_server" || true
    else
      echo "SQL к ${OBSERVER_1_IP}:${MYSQL_PORT} не прошёл."
      echo "Если спиннер уже показал «oceanbase bootstrap ok» / «Connect to observer … ok», но SQL мёртв —"
      echo "bootstrap, скорее всего, не завершился (см. observer.log: SIZE_OVERFLOW / execute_bootstrap)."
      BOOTSTRAP_HINTS=1
    fi
  fi

  section "obshell identity (все observer, HTTP :${OBSHELL_PORT})"
  IDENTITIES_FILE="$(mktemp)"
  if [[ -z "${OBSERVER_COUNT:-}" || "${OBSERVER_COUNT}" -lt 1 ]]; then
    echo "Нет OBSERVER_COUNT в inventory — пропуск опроса identity."
  else
    echo "ip	identity	reported_ip"
    i=""
    ip=""
    for (( i=1; i<=OBSERVER_COUNT; i++ )); do
      ip_var="OBSERVER_${i}_IP"
      ip="${!ip_var:-}"
      [[ -n "${ip}" ]] || continue
      check_obshell_http "${ip}" | tee -a "${IDENTITIES_FILE}" || true
    done
    echo "--- сводка identity ---"
    if [[ -s "${IDENTITIES_FILE}" ]]; then
      cut -f2 "${IDENTITIES_FILE}" | sort | uniq -c | sort -nr
    fi
  fi

  section "obshell DAG на TAKE OVER MASTER"
  MASTER_IPS=()
  DAG_FILE="$(mktemp)"
  if [[ -s "${IDENTITIES_FILE}" ]]; then
    while IFS=$'\t' read -r ip ident _; do
      if [[ "${ident}" == "TAKE OVER MASTER" ]]; then
        MASTER_IPS+=("${ip}")
      fi
    done < "${IDENTITIES_FILE}"
  fi
  if [[ ${#MASTER_IPS[@]} -eq 0 ]]; then
    echo "TAKE OVER MASTER не найден — OBD не зайдёт в wait_dag_succeed, будет крутить опрос."
  else
    echo "OBD при identity TAKE OVER MASTER вызывает wait_dag_succeed без таймаута."
    echo "Master: ${MASTER_IPS[*]}"
    echo "DAG API требует X-OCS-Header (голый curl → 400 Request.Header.NotFound)."
    for ip in "${MASTER_IPS[@]}"; do
      echo "----- DAG ${ip}:${OBSHELL_PORT} -----"
      dag_summary=""
      dag_summary="$(
        python3 "${LIB_DIR}/lib/obshell_ocs.py" dag \
          --host "${ip}" --port "${OBSHELL_PORT}" \
          --password "${ROOT_PASSWORD}" --password "" \
          2>&1
      )" || true
      if [[ -n "${dag_summary}" ]]; then
        printf '%s\n' "${dag_summary}"
        printf '%s\n' "${dag_summary}" >> "${DAG_FILE}"
      fi
      if ! grep -qE '^DAG_STATE=(RUNNING|PENDING|READY|FAILED|SUCCEED|SUCCESS)' <<<"${dag_summary}"; then
        echo "Если http 400 Request.Header.NotFound — заголовок не принят; смотрите SSH-лог master."
        echo "  python3 scripts/lib/obshell_ocs.py dag --host ${ip} --port ${OBSHELL_PORT} --password '<ocp.root_password>' --password ''"
        if ! grep -q '^DAG_STATE=' <<<"${dag_summary}"; then
          echo "DAG_STATE=NEED_HEADER" | tee -a "${DAG_FILE}" >/dev/null
        fi
      fi
    done
  fi

  section "obshell / observer.log (master, затем выборка follower)"
  if [[ -z "${OBSERVER_COUNT:-}" || "${OBSERVER_COUNT}" -lt 1 ]]; then
    echo "Нет OBSERVER_COUNT в inventory — пропуск SSH."
  else
    ssh_idxs=()
    seen_idx="|"
    idx=""
    ip=""
    i=""
    ip_var=""
    if [[ ${#MASTER_IPS[@]} -gt 0 ]]; then
      for ip in "${MASTER_IPS[@]}"; do
        idx="$(observer_index_for_ip "${ip}" || true)"
        if [[ -n "${idx}" && "${seen_idx}" != *"|${idx}|"* ]]; then
          ssh_idxs+=("${idx}")
          seen_idx="${seen_idx}${idx}|"
        fi
      done
    fi
    for (( i=1; i<=OBSERVER_COUNT; i++ )); do
      [[ ${#ssh_idxs[@]} -ge "${SAMPLE_OBSERVERS}" ]] && break
      [[ "${seen_idx}" == *"|${i}|"* ]] && continue
      ssh_idxs+=("${i}")
      seen_idx="${seen_idx}${i}|"
    done
    echo "SSH: master сначала, затем follower (индексы ${ssh_idxs[*]:-} из ${OBSERVER_COUNT})"
    for idx in "${ssh_idxs[@]}"; do
      ip_var="OBSERVER_${idx}_IP"
      ip="${!ip_var:-}"
      dump_remote_observer "${idx}" "${ip}"
    done
  fi
else
  echo
  echo "--local: SQL и SSH пропущены."
fi

section "Вывод"
cleanup_tmp() {
  rm -f "${IDENTITIES_FILE:-}" "${DAG_FILE:-}"
}

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
  cleanup_tmp
  exit 2
fi

FOLLOWER_ONLY=0
HAS_MASTER=0
CLUSTER_AGENT_ALL=0
masters_txt=""
if [[ -n "${IDENTITIES_FILE:-}" && -s "${IDENTITIES_FILE}" ]]; then
  if grep -q $'TAKE OVER MASTER' "${IDENTITIES_FILE}"; then
    HAS_MASTER=1
    masters_txt="$(awk -F'\t' '$2=="TAKE OVER MASTER"{print $1}' "${IDENTITIES_FILE}" | paste -sd' ' -)"
  fi
  if grep -q $'TAKE OVER FOLLOWER' "${IDENTITIES_FILE}" \
    && ! grep -qE $'TAKE OVER MASTER|CLUSTER AGENT' "${IDENTITIES_FILE}"; then
    FOLLOWER_ONLY=1
  fi
  if grep -q $'CLUSTER AGENT' "${IDENTITIES_FILE}" \
    && ! grep -qE $'TAKE OVER MASTER|TAKE OVER FOLLOWER|UNREACHABLE' "${IDENTITIES_FILE}"; then
    CLUSTER_AGENT_ALL=1
  fi
fi

DAG_STATE_LINE=""
if [[ -n "${DAG_FILE:-}" && -s "${DAG_FILE}" ]]; then
  DAG_STATE_LINE="$(grep -E '^DAG_STATE=' "${DAG_FILE}" | tail -1 || true)"
fi

if [[ "${HAS_MASTER}" -eq 1 ]]; then
  cat <<EOF
Причина: OceanBase bootstrap УЖЕ прошёл (observer ACTIVE, zone ≤ 7), БД ocs
скорее всего уже есть. Найден TAKE OVER MASTER (${masters_txt:-см. сводку выше}).
Плагин OBD obshell_bootstrap после этого вызывает wait_dag_succeed на DAG
агента — БЕЗ таймаута. Спиннер так и остаётся на «obshell bootstrap -».

${DAG_STATE_LINE:-DAG_STATE неизвестен (API недоступен или DAG ещё не создан).}

DESTROY НЕ НУЖЕН — SQL-кластер живой. Не убивайте obshell на всех 30 узлах:
take-over master уже выбран, массовый restart собьёт DAG.

Что делать:
  1. Не destroy. Ctrl+C висящего obd cluster start можно, observer не трогать.
  2. Голый curl к DAG API даёт 400 Request.Header.NotFound — нужен X-OCS-Header.
     python3 scripts/lib/obshell_ocs.py dag --host <MASTER_IP> --port ${OBSHELL_PORT} \\
       --password '<ocp.root_password>' --password ''
     Либо SQL: SHOW TABLES FROM ocs; (агенты/DAG, если таблицы уже есть).
  3. SSH на master, хвост:
       ${HOME_PATH}/log_obshell/obshell.log
     Искать: create take over dag / SUCCEED / FAILED / identity CLUSTER AGENT
  4. Если DAG SUCCEED (или identity стал CLUSTER AGENT):
       obd cluster start ${CLUSTER_NAME} -c obproxy-ce,obagent,ocp-server-ce
     (oceanbase-ce уже running)
  5. Если DAG RUNNING/PENDING — подождать; не рестартовать остальные obshell.
  6. Если DAG FAILED или часами RUNNING без прогресса — перезапустить ТОЛЬКО
     obshell master-узла (не все 30), затем снова /api/v1/info и DAG.

FOLLOWER на остальных узлах при живом MASTER — норма. Их lock/unlock без
«create take over dag» в раннем логе не значит, что master тоже застрял.
EOF
  cleanup_tmp
  exit 4
fi

if [[ "${CLUSTER_AGENT_ALL}" -eq 1 ]]; then
  cat <<EOF
Take-over obshell, похоже, завершён: все опрошенные агенты — CLUSTER AGENT.
Если спиннер всё ещё на «obshell bootstrap -», это уже не опрос identity.
Проверьте, жив ли процесс obd cluster start; дальше:
  obd cluster start ${CLUSTER_NAME} -c obproxy-ce,obagent,ocp-server-ce
DESTROY не нужен.
EOF
  cleanup_tmp
  exit 0
fi

if [[ "${FOLLOWER_ONLY}" -eq 1 ]]; then
  cat <<EOF
Причина: OceanBase bootstrap УЖЕ прошёл (observer ACTIVE, zone ≤ 7), а obshell
застрял в take-over. Плагин OBD ждёт identity TAKE OVER MASTER или CLUSTER AGENT
на всех узлах; TAKE OVER FOLLOWER он игнорирует и крутит опрос (на 30 узлах это
десятки минут, затем timeout, либо hang на wait_dag_succeed).

В obshell.log типично: Unknown database 'ocs' / The current database is not OCS,
lock+unlock take-over без «create take over dag». Все агенты — FOLLOWER, master нет.

DESTROY НЕ НУЖЕН — SQL-кластер живой.

Что делать:
  1. Ctrl+C висящий obd cluster start (observer не трогать)
  2. На всех observer остановить только obshell:
       kill \$(cat ${HOME_PATH}/run/obshell.pid)
  3. На observer-1 (один узел) запустить obshell с паролем root@sys:
       export OB_ROOT_PASSWORD='<ocp.root_password>'
       cd ${HOME_PATH} && ./bin/obshell admin start --ip <OBSERVER_1_IP> --port ${OBSHELL_PORT}
     Дождаться identity CLUSTER AGENT или TAKE OVER MASTER:
       curl -sf http://<OBSERVER_1_IP>:${OBSHELL_PORT}/api/v1/info
  4. Запустить obshell на остальных узлах тем же admin start
  5. obd cluster start ${CLUSTER_NAME} -c obproxy-ce,obagent,ocp-server-ce
     (oceanbase-ce уже running; start доберёт остальные компоненты)

Проверьте: SHOW DATABASES — должна появиться ocs после успешного take-over master.
EOF
  cleanup_tmp
  exit 3
fi

cat <<EOF
Если unique zone ≤ 7 и SQL к sys работает — observer-кластер жив, destroy не нужен.
Зависание «obshell bootstrap -» — ожидание take-over (опрос /api/v1/info до 200×3с
плюс wait_dag_succeed без таймаута). На 30 узлах один круг опроса ~ десятки секунд.

Сводка identity выше: нужен хотя бы один TAKE OVER MASTER или все CLUSTER AGENT.
FOLLOWER без MASTER — агенты не выберут лидера сами, см. остановку/старт obshell
с одного узла.

Если SQL к sys не работает после «Connect to observer ok» — bootstrap не прошёл,
смотрите observer.log и display-trace. Кластер без успешного bootstrap чинится
только destroy -f и повторным deploy.
EOF
cleanup_tmp
exit 0
