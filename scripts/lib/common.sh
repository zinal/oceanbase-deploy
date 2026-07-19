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
