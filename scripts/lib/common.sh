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
  key="$(expand_path "$(yaml_get ssh.private_key_file)")"
  printf '%s' "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=15 -i ${key}"
}

run_remote() {
  local host="$1"; shift
  local user key port
  user="$(yaml_get yandex_cloud.ssh_user)"
  key="$(expand_path "$(yaml_get ssh.private_key_file)")"
  port="$(yaml_get ssh.port)"
  ssh -p "${port}" -i "${key}" \
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

  user="$(yaml_get yandex_cloud.ssh_user)"
  key="$(expand_path "$(yaml_get ssh.private_key_file)")"
  port="$(yaml_get ssh.port)"

  info "Ожидание SSH: ${user}@${host}:${port} (после RUNNING cloud-init обычно 1–3 мин)..."

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
  die "SSH недоступен на ${user}@${host}:${port} после ${timeout}с. Проверьте security group (tcp/${port}), пару ssh.private_key_file / ssh_public_key_file и маршрут до приватной подсети.${err:+ Последняя ошибка: ${err}}"
}
