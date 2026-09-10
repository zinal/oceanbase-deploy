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

# All-in-One кладёт obd в ~/.oceanbase-all-in-one; без source его нет в PATH.
source_obd_env() {
  if command -v obd >/dev/null 2>&1; then
    return 0
  fi
  local envf="${HOME}/.oceanbase-all-in-one/bin/env.sh"
  if [[ -f "${envf}" ]]; then
    # shellcheck disable=SC1090
    source "${envf}"
  fi
  command -v obd >/dev/null 2>&1
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

# Remote bash -s + apt-retry.sh: повтор apt при dpkg lock (unattended-upgrades на свежей Ubuntu).
run_remote_with_apt() {
  local host="$1"; shift
  local helper="${SCRIPT_DIR}/apt-retry.sh"
  require_file "${helper}"
  {
    cat "${helper}"
    printf '\n'
    cat
  } | run_remote "${host}" "$@"
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

observer_status_normalize() {
  tr -d '[:space:]' <<<"${1:-}"
}

observer_status_is_active() {
  local s
  s="$(observer_status_normalize "${1:-}")"
  [[ "${s^^}" == ACTIVE ]]
}

observer_status_is_present() {
  [[ -n "$(observer_status_normalize "${1:-}")" ]]
}

# Nodes from a failed all-at-once start keep local clog/config. OBD scale_out
# then either skips start (pid still alive) or ADD SERVER times out / 4179.
# Never wipe seed IPs or members already ACTIVE in DBA_OB_SERVERS.
reset_observer_for_scale_out() {
  local host="$1"
  local home data redo member_status
  [[ -n "${host}" ]] || die "reset_observer_for_scale_out: empty host"
  if observer_is_seed_ip "${host}"; then
    die "Отказ очищать seed observer ${host}"
  fi
  member_status="$(observer_cluster_status "${host}" 2>/dev/null || true)"
  if observer_status_is_active "${member_status}"; then
    die "Отказ очищать ${host}: уже ACTIVE в DBA_OB_SERVERS (не leftover)"
  fi
  if observer_status_is_present "${member_status}"; then
    die "Отказ очищать ${host}: уже в DBA_OB_SERVERS (STATUS=${member_status}). Это не leftover — ./scripts/06-recover-observer.sh"
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

# Рабочие учётки root@sys после первого успешного SELECT 1 (пароль может быть пустым).
OB_SYS_SQL_READY=0
OB_SYS_SQL_USER=""
OB_SYS_SQL_PASS=""
OB_SYS_SQL_CLIENT=""
OB_SYS_SQL_LAST_ERR=""
OB_SYS_SQL_EMPTY_WARNED=0

sql_client_bin() {
  local c bundled=() old_nullglob
  for c in obclient mysql; do
    if command -v "${c}" >/dev/null 2>&1; then
      printf '%s' "${c}"
      return 0
    fi
  done
  old_nullglob="$(shopt -p nullglob)"
  shopt -s nullglob
  bundled=( "${HOME}/.obd/repository/obclient/"*/obclient/bin/obclient )
  eval "${old_nullglob}"
  if ((${#bundled[@]} > 0)) && [[ -x "${bundled[-1]}" ]]; then
    printf '%s' "${bundled[-1]}"
    return 0
  fi
  printf ''
}

observer_sys_user() {
  printf '%s' "${OB_SYS_USER:-root}"
}

observer_sys_users() {
  if [[ -n "${OB_SYS_USER:-}" ]]; then
    printf '%s\n' "${OB_SYS_USER}"
    return 0
  fi
  # Прямой observer:2881 — sys по умолчанию; root@sys ломает MariaDB (--user=root@host).
  printf '%s\n' "root"
  printf '%s\n' "root@sys"
}

# Кандидаты пароля root@sys: config, затем пустой (bootstrap / OBD yaml без root_password).
observer_root_password_candidates() {
  local p
  if [[ -n "${OB_ROOT_PASSWORD:-}" ]]; then
    printf '%s\n' "${OB_ROOT_PASSWORD}"
  fi
  p="$(yaml_get ocp.root_password 2>/dev/null || true)"
  if [[ -n "${p}" && "${p}" != "null" && "${p}" != "${OB_ROOT_PASSWORD:-}" ]]; then
    printf '%s\n' "${p}"
  fi
  printf '%s\n' ""
}

observer_mysql_port() {
  local port
  port="$(yaml_get oceanbase.ports.mysql)"
  [[ -n "${port}" && "${port}" != "null" ]] || port=2881
  printf '%s' "${port}"
}

observer_mysql_exec() {
  local host="$1" sql="$2" mode="$3" user="$4" pass="$5" client="$6" port="$7"
  if [[ -n "${pass}" ]]; then
    MYSQL_PWD="${pass}" "${client}" -h"${host}" -P"${port}" -u"${user}" --connect-timeout=8 "${mode}" "${sql}"
  else
    env -u MYSQL_PWD "${client}" -h"${host}" -P"${port}" -u"${user}" --connect-timeout=8 "${mode}" "${sql}"
  fi
}

observer_mysql_on_host() {
  local host="$1" sql="$2" mode="${3:--Nse}"
  local client port user pass err configured
  client="$(sql_client_bin)"
  if [[ -z "${client}" ]]; then
    OB_SYS_SQL_LAST_ERR="нет mysql/obclient в PATH (и нет ~/.obd/repository/obclient)"
    return 1
  fi
  port="$(observer_mysql_port)"

  if [[ "${OB_SYS_SQL_READY}" -eq 1 ]]; then
    observer_mysql_exec "${host}" "${sql}" "${mode}" \
      "${OB_SYS_SQL_USER}" "${OB_SYS_SQL_PASS}" "${OB_SYS_SQL_CLIENT}" "${port}"
    return
  fi

  while IFS= read -r user; do
    [[ -n "${user}" ]] || continue
    while IFS= read -r pass; do
      err="$(observer_mysql_exec "${host}" "SELECT 1" "-Nse" "${user}" "${pass}" "${client}" "${port}" 2>&1 >/dev/null)" && {
        OB_SYS_SQL_READY=1
        OB_SYS_SQL_USER="${user}"
        OB_SYS_SQL_PASS="${pass}"
        OB_SYS_SQL_CLIENT="${client}"
        if [[ -z "${pass}" && "${OB_SYS_SQL_EMPTY_WARNED}" -eq 0 ]]; then
          configured="$(yaml_get ocp.root_password 2>/dev/null || true)"
          if [[ -n "${configured}" && "${configured}" != "null" ]]; then
            warn "root@sys принимает пустой пароль, хотя в config задан ocp.root_password. Так бывает, если OBD yaml без root_password (OCP VM выключен). Пароль не меняем — иначе OBD scale_out отвалится. SQL идёт с пустым."
            OB_SYS_SQL_EMPTY_WARNED=1
          fi
        fi
        observer_mysql_exec "${host}" "${sql}" "${mode}" "${user}" "${pass}" "${client}" "${port}"
        return
      }
      OB_SYS_SQL_LAST_ERR="${err:-отказ ${client} -h${host} -P${port} -u${user}}"
    done < <(observer_root_password_candidates)
  done < <(observer_sys_users)
  return 1
}

observer_sys_sql() {
  local sql="$1"
  local ip
  for ip in "${OBSERVER_1_IP:-}" "${OBSERVER_2_IP:-}" "${OBSERVER_3_IP:-}"; do
    [[ -n "${ip}" ]] || continue
    if observer_mysql_on_host "${ip}" "${sql}" -Nse; then
      return 0
    fi
  done
  return 1
}

wait_seed_sys_sql() {
  local timeout="${1:-90}" elapsed=0
  while (( elapsed < timeout )); do
    if observer_sys_sql "SELECT 1" >/dev/null; then
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
  done
  return 1
}

observer_sys_sql_fail_hint() {
  local port client
  port="$(observer_mysql_port)"
  client="$(sql_client_bin)"
  if [[ -z "${client}" ]]; then
    printf '%s' "нет mysql/obclient в PATH"
    return
  fi
  printf '%s' "${OB_SYS_SQL_LAST_ERR:-${client} не подключился к ${OBSERVER_1_IP:-?}:${port} (пустой пароль или ocp.root_password; пользователь root / root@sys)}"
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
    observer_status_is_active "${status}" || return 1
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
    if observer_status_is_active "${status}"; then
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

# OBD scale_out: start observer, затем ADD SERVER (сессия часто 10s — поднимаем timeout).
# YAML пакета: до 3 узлов (по одному на zone). Wipe только ещё не ACTIVE IP;
# уже ACTIVE из того же YAML выкидываются (повторный deploy посреди раунда).
# Неуспевших добираем по одному (wipe + start + ADD SERVER), не повторяя весь пакет.
scale_out_observer() {
  local deploy="$1" yaml="$2"
  local ip rpc mysql_port zone status failed=0 work_yaml
  local -a ips=() rpcs=() mysqls=() zones=()
  local -a pending_ips=() pending_rpcs=() pending_mysqls=() pending_zones=()
  local -a active_ips=()
  [[ -f "${yaml}" ]] || die "нет YAML scale-out: ${yaml}"
  while read -r ip rpc mysql_port zone; do
    [[ -n "${ip}" ]] || continue
    ips+=("${ip}")
    rpcs+=("${rpc}")
    mysqls+=("${mysql_port}")
    zones+=("${zone}")
  done < <(obd_yaml_observer_spec "${yaml}")
  [[ "${#ips[@]}" -ge 1 ]] || die "не разобрать observer spec из ${yaml}"
  if ! observer_sys_sql "SELECT 1" >/dev/null; then
    die "Нет SQL к seed observer — не очищаем узлы из ${yaml} (риск стереть уже вступивший). $(observer_sys_sql_fail_hint). Почините root@sys и повторите deploy."
  fi
  raise_sys_query_timeout || true
  local idx
  for idx in "${!ips[@]}"; do
    ip="${ips[idx]}"
    status="$(observer_cluster_status "${ip}" 2>/dev/null || true)"
    if observer_status_is_active "${status}"; then
      active_ips+=("${ip}")
      continue
    fi
    if observer_status_is_present "${status}"; then
      die "${ip} в DBA_OB_SERVERS со статусом ${status} (не leftover). Не wipe. ./scripts/06-recover-observer.sh"
    fi
    pending_ips+=("${ip}")
    pending_rpcs+=("${rpcs[idx]}")
    pending_mysqls+=("${mysqls[idx]}")
    pending_zones+=("${zones[idx]}")
  done
  if [[ "${#pending_ips[@]}" -eq 0 ]]; then
    info "Все observer из ${yaml} уже ACTIVE (${ips[*]}) — пропуск wipe и scale_out"
    return 0
  fi
  work_yaml="${yaml}"
  if [[ "${#active_ips[@]}" -gt 0 ]]; then
    work_yaml="${yaml%.yaml}-pending.yaml"
    info "Уже ACTIVE, не трогаем: ${active_ips[*]}. YAML сужаем до ${pending_ips[*]}"
    python3 "${SCRIPT_DIR}/ob_deploy_plan.py" filter-scaleout \
      --input "${yaml}" \
      --output "${work_yaml}" \
      --ips "${pending_ips[*]}"
  fi
  info "Очистка ${#pending_ips[@]} leftover observer и scale_out из ${work_yaml}: ${pending_ips[*]}"
  for ip in "${pending_ips[@]}"; do
    reset_observer_for_scale_out "${ip}"
  done
  if ! obd_scale_out_yaml "${deploy}" "${work_yaml}"; then
    warn "obd cluster scale_out ${work_yaml} не прошёл целиком — доберём не-ACTIVE по одному"
  fi
  for idx in "${!pending_ips[@]}"; do
    ip="${pending_ips[idx]}"
    rpc="${pending_rpcs[idx]}"
    mysql_port="${pending_mysqls[idx]}"
    zone="${pending_zones[idx]}"
    if wait_observer_active "${ip}" 90; then
      continue
    fi
    status="$(observer_cluster_status "${ip}" 2>/dev/null || true)"
    if observer_status_is_active "${status}"; then
      info "${ip} уже ACTIVE — OBD не дождался ответа SQL"
      continue
    fi
    warn "${ip} не ACTIVE после пакетного scale_out. Wipe + ADD SERVER (не seed, не ACTIVE)."
    reset_observer_for_scale_out "${ip}"
    if join_empty_observer "${deploy}" "${ip}" "${rpc}" "${zone}" "${mysql_port}"; then
      continue
    fi
    status="$(observer_cluster_status "${ip}" 2>/dev/null || true)"
    if observer_status_is_active "${status}"; then
      continue
    fi
    warn "не удалось ADD SERVER ${ip}:${rpc} zone ${zone}"
    failed=1
  done
  if [[ "${failed}" -ne 0 ]]; then
    die "пакет ${yaml}: не все observer ACTIVE (${pending_ips[*]}). ERROR 4179 → wipe только этот IP. Повторите deploy."
  fi
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

# After a 3-node obagent scale_out, `obd cluster start -s <ip>` still starts
# every not-running agent (or a different server: 10.130.0.8 → server8/.18).
# Create run/ on all packet IPs first, then start the component without -s.
start_obagent_nodes() {
  local deploy="$1"
  shift || true
  local ip home
  local -a ips=()
  home="$(obagent_home_path)"
  for ip in "$@"; do
    [[ -n "${ip}" ]] || continue
    ips+=("${ip}")
  done
  ((${#ips[@]})) || return 0
  for ip in "${ips[@]}"; do
    ensure_obagent_work_home "${ip}" "${home}"
  done
  info "Запуск obagent (${#ips[@]} узлов: ${ips[*]}). OBD scale_out их не стартует; start -s по IP ненадёжен"
  obd_start_component "${deploy}" "obagent"
}

start_obagent_node() {
  start_obagent_nodes "$1" "$2"
}

start_obagent_yaml() {
  local deploy="$1" yaml="$2"
  local -a ips=()
  [[ -f "${yaml}" ]] || return 0
  mapfile -t ips < <(obd_yaml_component_ips "${yaml}" "obagent")
  start_obagent_nodes "${deploy}" "${ips[@]}"
}

start_registered_obagents() {
  local deploy="$1" registered_yaml="$2"
  [[ -f "${registered_yaml}" ]] || return 0
  start_obagent_yaml "${deploy}" "${registered_yaml}"
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
