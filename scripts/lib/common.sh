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
  local -a ssh_extra=()
  user="$(ssh_connect_user)"
  key="$(ssh_private_key_path)"
  port="$(ssh_connect_port)"
  if [[ -t 1 ]]; then
    ssh_extra=(-tt)
  fi
  ssh "${ssh_extra[@]}" -p "${port}" -i "${key}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "${user}@${host}" "$@"
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
