#!/usr/bin/env bash
# Общие функции для скриптов развёртывания OceanBase в Yandex Cloud.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
GENERATED_DIR="${REPO_ROOT}/generated"
CONFIG_FILE="${REPO_ROOT}/config/deploy.yaml"

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
info() { log "INFO: $*"; }
warn() { log "WARN: $*" >&2; }
die()  { log "ERROR: $*" >&2; exit 1; }

require_cmd() {
  local cmd="$1"
  command -v "$cmd" >/dev/null 2>&1 || die "Команда '$cmd' не найдена. Установите её и повторите."
}

require_file() {
  local path="$1"
  [[ -f "$path" ]] || die "Файл не найден: $path"
}

expand_path() {
  local p="${1/#\~/$HOME}"
  printf '%s' "$p"
}

ssh_public_key_path() {
  expand_path "$(yaml_get yandex_cloud.ssh_public_key_file)"
}

ssh_private_key_path() {
  expand_path "$(yaml_get ssh.private_key_file)"
}

ssh_connect_user() {
  yaml_get yandex_cloud.ssh_user
}

ssh_connect_port() {
  local port
  port="$(yaml_get ssh.port)"
  printf '%s' "${port:-22}"
}

validate_ssh_key_pair() {
  local pub priv pub_fp priv_fp user
  pub="$(ssh_public_key_path)"
  priv="$(ssh_private_key_path)"
  user="$(ssh_connect_user)"

  require_file "$pub"
  require_file "$priv"

  pub_fp="$(ssh-keygen -lf "${pub}" 2>/dev/null | awk '{print $2}')"
  priv_fp="$(ssh-keygen -lf "${priv}" 2>/dev/null | awk '{print $2}')"

  if [[ -n "${pub_fp}" && -n "${priv_fp}" && "${pub_fp}" != "${priv_fp}" ]]; then
    die "Несовпадение SSH-ключей: cloud-init использует ${pub} (${pub_fp}), подключение — ${priv} (${priv_fp}). Укажите пару pub/priv от одного ключа."
  fi

  info "SSH для provision: ${user}@<host>:$(ssh_connect_port), ключ ${priv}${pub_fp:+ (${pub_fp})}"
  info "Cloud-init authorized_keys: ${pub}"
}

load_inventory() {
  local inv="${GENERATED_DIR}/inventory.env"
  require_file "$inv"
  # shellcheck disable=SC1090
  source "$inv"
}

# Внутренний DNS Yandex Cloud: <hostname>.<region>.internal (ru-central1-a -> ru-central1)
yc_region_from_zone() {
  local zone="$1"
  if [[ -z "${zone}" ]]; then
    return 0
  fi
  printf '%s' "${zone%-*}"
}

yc_internal_fqdn() {
  local hostname="$1"
  local zone="$2"
  local region
  region="$(yc_region_from_zone "${zone}")"
  if [[ -n "${region}" ]]; then
    printf '%s' "${hostname}.${region}.internal"
  else
    printf '%s' "${hostname}"
  fi
}

# Имя хоста из инвентаря (предпочтительно) или IP (fallback).
inventory_host() {
  local prefix="$1" idx="$2"
  local name_var="${prefix}_${idx}_NAME"
  local ip_var="${prefix}_${idx}_IP"
  local zone host

  zone="$(yaml_get yandex_cloud.zone)"
  host="${!name_var:-}"
  if [[ -n "${host}" ]]; then
    yc_internal_fqdn "${host}" "${zone}"
    return 0
  fi
  host="${!ip_var:-}"
  if [[ -n "${host}" ]]; then
    printf '%s' "${host}"
    return 0
  fi
  die "Не задан хост для ${prefix}_${idx} (ожидается ${name_var} или ${ip_var})"
}

# OBD check4ocp без -V берёт 3.1.1 и требует user.username=admin (OS/SSH).
# Для OCP ≥ 4.2.0 эта проверка не нужна; SSH остаётся oceanbase.deploy_user.
resolve_ocp_check_version() {
  local cluster="${1:-}" cfg_ver api_payload=""
  local -a yaml_args=()
  cfg_ver="$(yaml_get ocp.version 2>/dev/null || true)"
  [[ "${cfg_ver}" == "null" ]] && cfg_ver=""
  if [[ -f "${GENERATED_DIR}/obd-cluster.yaml" ]]; then
    yaml_args+=(--yaml "${GENERATED_DIR}/obd-cluster.yaml")
  fi
  if [[ -n "${cluster}" ]]; then
    local f
    for f in \
      "${HOME}/.obd/cluster/${cluster}/config.yaml" \
      "${HOME}/.obd/cluster/${cluster}/inner_config.yaml" \
      "${HOME}/.obd/cluster/${cluster}/config.yml" \
      "${HOME}/.obd/cluster/${cluster}/inner_config.yml"; do
      if [[ -f "${f}" ]]; then
        yaml_args+=(--yaml "${f}")
      fi
    done
  fi
  if [[ -n "${OCP_URL:-}" && -n "${OCP_USER:-}" && -n "${OCP_PASSWORD:-}" ]] && command -v curl >/dev/null 2>&1; then
    api_payload="$(curl -fsS --max-time 8 -u "${OCP_USER}:${OCP_PASSWORD}" "${OCP_URL%/}/api/v2/info" 2>/dev/null || true)"
  fi
  python3 "${SCRIPT_DIR}/ocp_check_version.py" \
    --config-version "${cfg_ver}" \
    --api-payload "${api_payload}" \
    "${yaml_args[@]}"
}

# OBD хранит метаданные развёртывания в ~/.obd/cluster/<deploy_name>.
# `obd cluster list` иногда не показывает кластер (формат вывода, ANSI), хотя deploy уже выполнен.
obd_cluster_registered() {
  local name="$1"

  [[ -n "${name}" ]] || return 1

  if [[ -d "${HOME}/.obd/cluster/${name}" ]]; then
    return 0
  fi

  command -v obd >/dev/null 2>&1 || return 1

  if obd cluster display "${name}" >/dev/null 2>&1; then
    return 0
  fi

  obd cluster list 2>/dev/null \
    | sed -E 's/\x1b\[[0-9;]*[[:alpha:]?]m//g' \
    | grep -qE "(^|[[:space:]])${name}([[:space:]]|$)"
}

ensure_generated_dir() {
  mkdir -p "${GENERATED_DIR}"
}

yaml_get() {
  # Чтение простых ключей из YAML через Python (PyYAML не обязателен — используем ruamel или yaml)
  local key="$1"
  python3 - "${CONFIG_FILE}" "$key" <<'PY'
import sys

path, dotted = sys.argv[1], sys.argv[2]

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML не установлен. Выполните: pip install pyyaml\n")
    sys.exit(1)

with open(path, encoding="utf-8") as f:
    data = yaml.safe_load(f)

node = data
for part in dotted.split("."):
    if part == "":
        continue
    if not isinstance(node, dict) or part not in node:
        print("")
        sys.exit(0)
    node = node[part]

if node is None:
    print("")
elif isinstance(node, bool):
    print("true" if node else "false")
elif isinstance(node, (int, float)):
    print(node)
else:
    print(node)
PY
}

yaml_get_list() {
  # Список YAML → слова через пробел (для передачи в remote env).
  local key="$1"
  python3 - "${CONFIG_FILE}" "$key" <<'PY'
import sys

path, dotted = sys.argv[1], sys.argv[2]
try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML не установлен. Выполните: pip install pyyaml\n")
    sys.exit(1)

with open(path, encoding="utf-8") as f:
    data = yaml.safe_load(f) or {}

node = data
for part in dotted.split("."):
    if part == "":
        continue
    if not isinstance(node, dict) or part not in node:
        print("")
        sys.exit(0)
    node = node[part]

if node is None:
    print("")
elif isinstance(node, list):
    print(" ".join(str(x).strip() for x in node if x is not None and str(x).strip()))
else:
    print(str(node).strip())
PY
}

ssh_opts() {
  local key
  key="$(ssh_private_key_path)"
  printf '%s' "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -i ${key}"
}

run_remote() {
  local host="$1"; shift
  local user key port
  user="$(ssh_connect_user)"
  key="$(ssh_private_key_path)"
  port="$(ssh_connect_port)"
  ssh -T -p "${port}" -i "${key}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o LogLevel=ERROR \
    "${user}@${host}" "$@"
}

verify_observer_storage() {
  local host="$1"
  local deploy_user data_dir redo_dir
  deploy_user="$(yaml_get oceanbase.deploy_user)"
  [[ -z "${deploy_user}" || "${deploy_user}" == "null" ]] && deploy_user="$(ssh_connect_user)"
  data_dir="$(yaml_get oceanbase.data_dir)"
  redo_dir="$(yaml_get oceanbase.redo_dir)"

  run_remote "${host}" "bash -s" <<REMOTE
set -euo pipefail
DEPLOY_USER="${deploy_user}"
DATA_DIR="${data_dir}"
REDO_DIR="${redo_dir}"
for dir in "\${DATA_DIR}" "\${REDO_DIR}"; do
  [[ -n "\${dir}" ]] || continue
  if ! sudo -u "\${DEPLOY_USER}" test -w "\${dir}"; then
    echo "ERROR: \${DEPLOY_USER} не может писать в \${dir}" >&2
    exit 1
  fi
done
REMOTE
}

verify_all_observer_storage() {
  local i host
  for (( i=1; i<=OBSERVER_COUNT; i++ )); do
    host="$(inventory_host OBSERVER "${i}")"
    info "Проверка data/log путей на ${host}..."
    if ! verify_observer_storage "${host}"; then
      die "На ${host} не подготовлены каталоги data/redo. Выполните: ./scripts/deploy.sh prepare"
    fi
  done
}

wait_for_ssh() {
  local host="$1"
  local user key port
  local poll="${SSH_WAIT_POLL:-10}"
  local timeout="${SSH_WAIT_TIMEOUT:-900}"
  local elapsed=0
  local err=""

  user="$(ssh_connect_user)"
  key="$(ssh_private_key_path)"
  port="$(ssh_connect_port)"

  info "Ожидание SSH: ${user}@${host}:${port}, ключ ${key} (после RUNNING cloud-init обычно 1–3 мин)..."

  while (( elapsed < timeout )); do
    if ssh -p "${port}" -i "${key}" \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=5 -o ConnectionAttempts=1 -o BatchMode=yes \
      "${user}@${host}" "echo ok" >/dev/null 2>&1; then
      info "SSH доступен: ${user}@${host} (через ${elapsed}с)"
      return 0
    fi

    if (( elapsed == 0 || elapsed % 30 == 0 )); then
      err="$(ssh -p "${port}" -i "${key}" \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ConnectTimeout=5 -o ConnectionAttempts=1 -o BatchMode=yes \
        "${user}@${host}" "echo ok" 2>&1 | tail -1 || true)"
      if [[ -n "${err}" ]]; then
        info "SSH ${host}: ${elapsed}/${timeout}с — ${err}"
      else
        info "SSH ${host}: ${elapsed}/${timeout}с..."
      fi
    fi

    sleep "${poll}"
    elapsed=$((elapsed + poll))
  done

  err="$(ssh -p "${port}" -i "${key}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=5 -o ConnectionAttempts=1 -o BatchMode=yes \
    "${user}@${host}" "echo ok" 2>&1 | tail -1 || true)"
  die "SSH недоступен на ${user}@${host}:${port} (ключ ${key}) после ${timeout}с. Проверьте: yandex_cloud.ssh_user + ssh.private_key_file совпадают с рабочим ssh (например demo@host), security group tcp/${port}, пара ssh_public_key_file/ssh.private_key_file.${err:+ Последняя ошибка: ${err}}"
}

obd_yaml_first_ip() {
  local yaml_path="$1"
  python3 - "${yaml_path}" <<'PY'
import sys
import yaml

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    data = yaml.safe_load(fh)
if not isinstance(data, dict) or not data:
    raise SystemExit(f"{path}: expected an OBD component mapping")
component = next(iter(data.values()))
if not isinstance(component, dict):
    raise SystemExit(f"{path}: missing component body")
servers = component.get("servers") or []
if not servers:
    raise SystemExit(f"{path}: no servers")
entry = servers[0]
ip = entry if isinstance(entry, str) else (entry or {}).get("ip")
if not ip:
    raise SystemExit(f"{path}: server has no ip")
print(ip)
PY
}

observer_deploy_user() {
  local user
  user="$(yaml_get oceanbase.deploy_user)"
  if [[ -z "${user}" || "${user}" == "null" ]]; then
    user="$(ssh_connect_user)"
  fi
  printf '%s' "${user}"
}

observer_home_path() {
  local path
  path="$(yaml_get oceanbase.home_path)"
  if [[ -n "${path}" && "${path}" != "null" ]]; then
    printf '%s' "${path}"
    return
  fi
  printf '/home/%s/observer' "$(observer_deploy_user)"
}

observer_is_seed_ip() {
  local ip="$1"
  [[ -n "${ip}" ]] || return 1
  [[ "${ip}" == "${OBSERVER_1_IP:-}" || "${ip}" == "${OBSERVER_2_IP:-}" || "${ip}" == "${OBSERVER_3_IP:-}" ]]
}

# Nodes from a failed all-at-once start keep local clog/config. OBD scale_out
# then either skips start (pid still alive) or ADD SERVER times out / 4179.
reset_observer_for_scale_out() {
  local host="$1"
  local home data redo
  [[ -n "${host}" ]] || die "reset_observer_for_scale_out: empty host"
  if observer_is_seed_ip "${host}"; then
    die "Отказ очищать seed observer ${host}"
  fi
  home="$(observer_home_path)"
  data="$(yaml_get oceanbase.data_dir)"
  redo="$(yaml_get oceanbase.redo_dir)"
  [[ -n "${data}" && "${data}" != "null" ]] || data="/ob-data/1"
  [[ -n "${redo}" && "${redo}" != "null" ]] || redo="/ob-log/1"
  info "Очистка leftover observer на ${host} перед scale_out..."
  run_remote "${host}" "bash -s" <<REMOTE
set -euo pipefail
HOME_PATH="${home}"
DATA_DIR="${data}"
REDO_DIR="${redo}"
pkill -TERM -u "\$(whoami)" -f "\${HOME_PATH}/bin/observer" 2>/dev/null || true
pkill -TERM -u "\$(whoami)" -f "\${HOME_PATH}/bin/obshell" 2>/dev/null || true
sleep 2
pkill -KILL -u "\$(whoami)" -f "\${HOME_PATH}/bin/observer" 2>/dev/null || true
pkill -KILL -u "\$(whoami)" -f "\${HOME_PATH}/bin/obshell" 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10; do
  pgrep -u "\$(whoami)" -f "\${HOME_PATH}/bin/observer" >/dev/null 2>&1 || break
  sleep 1
done
rm -rf "\${HOME_PATH}"
if [[ -d "\${DATA_DIR}" ]]; then
  find "\${DATA_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi
if [[ -d "\${REDO_DIR}" ]]; then
  find "\${REDO_DIR}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
fi
mkdir -p "\${DATA_DIR}" "\${REDO_DIR}"
REMOTE
}

# OBD Cursor не ставит ob_query_timeout: ALTER SYSTEM ADD SERVER падает за 10s (ERROR 4012).
OB_ADD_SERVER_TIMEOUT_US="${OB_ADD_SERVER_TIMEOUT_US:-3600000000}"

sql_client_bin() {
  if command -v mysql >/dev/null 2>&1; then
    printf '%s' mysql
  elif command -v obclient >/dev/null 2>&1; then
    printf '%s' obclient
  else
    printf ''
  fi
}

observer_sys_user() {
  printf '%s' "${OB_SYS_USER:-root@sys}"
}

observer_mysql_on_host() {
  local host="$1" sql="$2" mode="${3:--Nse}"
  local client port pass user
  client="$(sql_client_bin)"
  [[ -n "${client}" ]] || return 1
  port="$(yaml_get oceanbase.ports.mysql)"
  [[ -n "${port}" && "${port}" != "null" ]] || port=2881
  pass="$(yaml_get ocp.root_password)"
  [[ "${pass}" == "null" ]] && pass=""
  user="$(observer_sys_user)"
  if [[ -n "${pass}" ]]; then
    MYSQL_PWD="${pass}" "${client}" -h"${host}" -P"${port}" --user="${user}" --connect-timeout=8 "${mode}" "${sql}"
  else
    "${client}" -h"${host}" -P"${port}" --user="${user}" --connect-timeout=8 "${mode}" "${sql}"
  fi
}

observer_sys_sql() {
  local sql="$1"
  local ip
  for ip in "${OBSERVER_1_IP:-}" "${OBSERVER_2_IP:-}" "${OBSERVER_3_IP:-}"; do
    [[ -n "${ip}" ]] || continue
    if observer_mysql_on_host "${ip}" "${sql}" -Nse 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

raise_sys_query_timeout() {
  info "sys.ob_query_timeout=${OB_ADD_SERVER_TIMEOUT_US} мкс (иначе OBD ADD SERVER = 10s / ERROR 4012)"
  observer_sys_sql "SET GLOBAL ob_query_timeout = ${OB_ADD_SERVER_TIMEOUT_US}" \
    || warn "SET GLOBAL ob_query_timeout не прошёл — будет SQL-повтор ADD SERVER"
}

observer_cluster_status() {
  local ip="$1"
  observer_sys_sql "SELECT STATUS FROM oceanbase.DBA_OB_SERVERS WHERE SVR_IP='${ip}' LIMIT 1"
}

observer_active_ips() {
  observer_sys_sql "SELECT SVR_IP FROM oceanbase.DBA_OB_SERVERS WHERE UPPER(STATUS)='ACTIVE'"
}

seed_observers_active() {
  local ip status
  for ip in "${OBSERVER_1_IP:-}" "${OBSERVER_2_IP:-}" "${OBSERVER_3_IP:-}"; do
    [[ -n "${ip}" ]] || return 1
    status="$(observer_cluster_status "${ip}" 2>/dev/null || true)"
    grep -qi ACTIVE <<<"${status}" || return 1
  done
  return 0
}

missing_observer_ips() {
  local yaml="$1"
  local active ip
  [[ -f "${yaml}" ]] || return 0
  active="$(observer_active_ips 2>/dev/null || true)"
  while read -r ip; do
    [[ -n "${ip}" ]] || continue
    if ! grep -Fxq "${ip}" <<<"${active}"; then
      printf '%s\n' "${ip}"
    fi
  done < <(python3 "${SCRIPT_DIR}/ob_deploy_plan.py" ips --input "${yaml}")
}

scale_out_joined_ips_args() {
  local joined
  if joined="$(observer_active_ips)"; then
    if [[ -n "${joined}" ]]; then
      info "ACTIVE в DBA_OB_SERVERS: $(printf '%s' "${joined}" | tr '\n' ' ')"
      JOINED_IPS_ARGS=(--joined-ips "${joined}")
      return 0
    fi
  fi
  warn "DBA_OB_SERVERS недоступен или пуст — план scale-out только по метаданным OBD"
  JOINED_IPS_ARGS=()
  return 1
}

wait_observer_active() {
  local ip="$1"
  local timeout="${2:-300}"
  local elapsed=0 status
  while (( elapsed < timeout )); do
    status="$(observer_cluster_status "${ip}" 2>/dev/null || true)"
    if grep -qi ACTIVE <<<"${status}"; then
      info "${ip} в DBA_OB_SERVERS: ACTIVE"
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  warn "${ip} не стал ACTIVE за ${timeout}с (статус: ${status:-нет строки})"
  return 1
}

add_observer_server_sql() {
  local ip="$1" rpc="$2" zone="$3"
  local seed out sql
  sql="SET SESSION ob_query_timeout = ${OB_ADD_SERVER_TIMEOUT_US}; ALTER SYSTEM ADD SERVER '${ip}:${rpc}' ZONE ${zone}"
  info "ALTER SYSTEM ADD SERVER '${ip}:${rpc}' ZONE ${zone} (ob_query_timeout=${OB_ADD_SERVER_TIMEOUT_US})"
  for seed in "${OBSERVER_1_IP:-}" "${OBSERVER_2_IP:-}" "${OBSERVER_3_IP:-}"; do
    [[ -n "${seed}" ]] || continue
    out=""
    if out="$(observer_mysql_on_host "${seed}" "${sql}" -e 2>&1)"; then
      return 0
    fi
    if grep -qiE 'already exist|duplicate' <<<"${out}"; then
      return 0
    fi
    if grep -q '4179' <<<"${out}"; then
      warn "ERROR 4179: ${ip}:${rpc} не empty. Нужен wipe home/data/redo на этом узле (не на seed)."
      return 2
    fi
    warn "ADD SERVER через ${seed}: ${out}"
  done
  return 1
}

wait_host_mysql() {
  local ip="$1"
  local elapsed=0
  while (( elapsed < 90 )); do
    if observer_mysql_on_host "${ip}" "select 1" -Nse >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  return 1
}

obd_scale_out_yaml() {
  local deploy="$1" yaml="$2"
  if command -v stdbuf >/dev/null 2>&1; then
    stdbuf -oL -eL obd cluster scale_out "${deploy}" -c "${yaml}"
  else
    obd cluster scale_out "${deploy}" -c "${yaml}"
  fi
}

join_empty_observer() {
  local deploy="$1" ip="$2" rpc="$3" zone="$4" mysql_port="$5"
  # После wipe узел уже в конфиге OBD — start, сразу ADD SERVER с длинным timeout.
  # Нельзя оставлять observer работать «вхолостую»: clog → ERROR 4179.
  if ! obd_start_component "${deploy}" "oceanbase-ce" "${ip}"; then
    warn "obd cluster start -s ${ip} не удался (узла может не быть в OBD config)"
    return 1
  fi
  wait_host_mysql "${ip}" || warn "${ip}:${mysql_port} ещё не отвечает — ADD SERVER всё равно"
  add_observer_server_sql "${ip}" "${rpc}" "${zone}" || return 1
  wait_observer_active "${ip}"
}

obd_yaml_observer_spec() {
  python3 "${SCRIPT_DIR}/ob_deploy_plan.py" spec --input "$1"
}

# OBD scale_out: start observer, затем ADD SERVER с дефолтным ob_query_timeout=10s.
# При 6+ узлах SQL часто не укладывается — OBD-5000. Живой observer пишет clog → 4179.
scale_out_observer() {
  local deploy="$1" yaml="$2"
  local ip rpc mysql_port zone status
  [[ -f "${yaml}" ]] || die "нет YAML scale-out: ${yaml}"
  read -r ip rpc mysql_port zone < <(obd_yaml_observer_spec "${yaml}")
  [[ -n "${ip}" && -n "${rpc}" && -n "${zone}" ]] || die "не разобрать observer spec из ${yaml}"
  raise_sys_query_timeout || true
  reset_observer_for_scale_out "${ip}"
  info "Очистка ${ip} и добавление observer из ${yaml}"
  if obd_scale_out_yaml "${deploy}" "${yaml}"; then
    if wait_observer_active "${ip}" 90; then
      return 0
    fi
    warn "obd cluster scale_out ${ip} вернул 0, но в DBA_OB_SERVERS нет ACTIVE — leftover в метаданных OBD"
  else
    status="$(observer_cluster_status "${ip}" 2>/dev/null || true)"
    if grep -qi ACTIVE <<<"${status}"; then
      info "${ip} уже ACTIVE — OBD не дождался ответа SQL"
      return 0
    fi
    warn "obd cluster scale_out ${ip} не прошёл. Не делайте ADD SERVER по уже запущенному узлу — будет ERROR 4179 (non-empty)."
  fi
  info "Повтор: wipe ${ip}, start, ADD SERVER с ob_query_timeout=3600s"
  reset_observer_for_scale_out "${ip}"
  if join_empty_observer "${deploy}" "${ip}" "${rpc}" "${zone}" "${mysql_port}"; then
    return 0
  fi
  reset_observer_for_scale_out "${ip}"
  if obd_scale_out_yaml "${deploy}" "${yaml}"; then
    wait_observer_active "${ip}" 90 && return 0
  fi
  status="$(observer_cluster_status "${ip}" 2>/dev/null || true)"
  if grep -qi ACTIVE <<<"${status}"; then
    return 0
  fi
  die "не удалось ADD SERVER ${ip}:${rpc} zone ${zone}. ERROR 4179 → wipe только ${ip} (не seed). Timeout 10s → SET GLOBAL ob_query_timeout."
}

obagent_home_path() {
  printf '/home/%s/obagent' "$(observer_deploy_user)"
}

obd_yaml_component_ips() {
  local yaml_path="$1" component="$2"
  python3 - "${yaml_path}" "${component}" <<'PY'
import sys
import yaml

path, component = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as fh:
    data = yaml.safe_load(fh)
if not isinstance(data, dict):
    raise SystemExit(0)
block = data.get(component)
if not isinstance(block, dict):
    raise SystemExit(0)
for entry in block.get("servers") or []:
    ip = entry if isinstance(entry, str) else (entry or {}).get("ip")
    if ip:
        print(ip)
PY
}

# OBD scale_out does not run obagent init, so run/ is missing and
# ob_agentctl start fails with fetch_admin_lock_failed.
ensure_obagent_work_home() {
  local host="$1"
  local home="${2:-}"
  [[ -n "${home}" ]] || home="$(obagent_home_path)"
  info "Каталоги obagent на ${host}: ${home}/{run,bin,lib,conf,log}"
  run_remote "${host}" "mkdir -p '${home}/run' '${home}/bin' '${home}/lib' '${home}/conf' '${home}/log'"
}

start_obagent_node() {
  local deploy="$1" ip="$2"
  ensure_obagent_work_home "${ip}"
  obd_start_component "${deploy}" "obagent" "${ip}"
}

start_registered_obagents() {
  local deploy="$1" registered_yaml="$2"
  local ip home
  local -a ips=()
  [[ -f "${registered_yaml}" ]] || return 0
  home="$(obagent_home_path)"
  mapfile -t ips < <(obd_yaml_component_ips "${registered_yaml}" "obagent")
  ((${#ips[@]})) || return 0
  for ip in "${ips[@]}"; do
    [[ -n "${ip}" ]] || continue
    ensure_obagent_work_home "${ip}" "${home}"
  done
  obd_start_component "${deploy}" "obagent"
}

# Официально: obd cluster start <deploy> -c <component> [-s <ip>]
obd_start_component() {
  local deploy="$1" component="$2" ip="${3:-}"
  if [[ -n "${ip}" ]]; then
    info "obd cluster start ${deploy} -c ${component} -s ${ip}"
    if command -v stdbuf >/dev/null 2>&1; then
      stdbuf -oL -eL obd cluster start "${deploy}" -c "${component}" -s "${ip}"
    else
      obd cluster start "${deploy}" -c "${component}" -s "${ip}"
    fi
  else
    info "obd cluster start ${deploy} -c ${component}"
    if command -v stdbuf >/dev/null 2>&1; then
      stdbuf -oL -eL obd cluster start "${deploy}" -c "${component}"
    else
      obd cluster start "${deploy}" -c "${component}"
    fi
  fi
}
