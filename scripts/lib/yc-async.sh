#!/usr/bin/env bash
# Асинхронные операции Yandex Cloud (по образцу ydb-snippets/admin/vms/supp/vms.sh).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"

YC_OP_LOG="${GENERATED_DIR}/yc-op.log"
YC_RATE_LIMIT_SLEEP="${YC_RATE_LIMIT_SLEEP:-10}"
YC_ASYNC_MAX_RETRIES="${YC_ASYNC_MAX_RETRIES:-60}"

yc_folder_args() {
  local folder_id
  folder_id="$(yaml_get yandex_cloud.folder_id)"
  if [[ -n "${folder_id}" && "${folder_id}" != "null" ]]; then
    # Отдельные строки — иначе mapfile склеивает в один аргумент
    printf '%s\n' "--folder-id" "${folder_id}"
  fi
}

yc_op_has_rate_limit() {
  grep -q "The limit on maximum number of active operations has exceeded" "${1}" 2>/dev/null
}

yc_op_has_error() {
  grep -q "ERROR:" "${1}" 2>/dev/null
}

# Запуск yc-команды с --async и повтором при rate limit (ydb-snippets checkLimit).
yc_async_retry() {
  local description="$1"
  shift
  local attempt=0

  ensure_generated_dir
  while (( attempt < YC_ASYNC_MAX_RETRIES )); do
    if "$@" --async >"${YC_OP_LOG}" 2>&1; then
      if yc_op_has_rate_limit "${YC_OP_LOG}"; then
        warn "Rate limit при ${description}, повтор через ${YC_RATE_LIMIT_SLEEP}с..."
        sleep "${YC_RATE_LIMIT_SLEEP}"
        ((attempt++))
        continue
      fi
      return 0
    fi
    if yc_op_has_rate_limit "${YC_OP_LOG}"; then
      warn "Rate limit при ${description}, повтор через ${YC_RATE_LIMIT_SLEEP}с..."
      sleep "${YC_RATE_LIMIT_SLEEP}"
      ((attempt++))
      continue
    fi
    cat "${YC_OP_LOG}" >&2
    die "Ошибка при ${description}"
  done
  die "Превышен лимит повторов при ${description}"
}

yc_assert_last_op_ok() {
  local phase="$1"
  if [[ -f "${YC_OP_LOG}" ]] && yc_op_has_error "${YC_OP_LOG}"; then
    cat "${YC_OP_LOG}" >&2
    die "Ошибка на этапе: ${phase}"
  fi
}

# Ожидание READY для дисков с именем ${prefix}*
wait_for_disks_ready() {
  local prefix="$1"
  local folder_args
  mapfile -t folder_args < <(yc_folder_args)

  info "Ожидание готовности дисков (${prefix}*)..."
  while true; do
    local pending
    pending="$(yc compute disk list "${folder_args[@]}" --format json 2>/dev/null | python3 -c "
import json, sys
prefix = sys.argv[1]
pending = sum(
    1 for d in json.load(sys.stdin)
    if d.get('name', '').startswith(prefix) and d.get('status') != 'READY'
)
print(pending)
" "${prefix}")"
    if [[ "${pending}" == "0" ]]; then
      info "Все диски READY"
      return 0
    fi
    info "Дисков в процессе создания: ${pending}..."
    sleep 5
  done
}

# Ожидание RUNNING/STOPPED для ВМ с меткой deployment
wait_for_instances_ready() {
  local deploy_name="$1"
  local folder_args
  mapfile -t folder_args < <(yc_folder_args)

  info "Ожидание готовности ВМ (deployment=${deploy_name})..."
  while true; do
    local pending
    pending="$(yc compute instance list "${folder_args[@]}" --format json 2>/dev/null | python3 -c "
import json, sys
deploy = sys.argv[1]
ready = {'RUNNING', 'STOPPED'}
pending = sum(
    1 for i in json.load(sys.stdin)
    if i.get('labels', {}).get('deployment') == deploy and i.get('status') not in ready
)
print(pending)
" "${deploy_name}")"
    if [[ "${pending}" == "0" ]]; then
      info "Все ВМ в состоянии RUNNING/STOPPED"
      return 0
    fi
    info "ВМ в процессе создания: ${pending}..."
    sleep 5
  done
}

# Проверка SSH-доступа ко всем IP из списка (ydb-snippets validate network access)
wait_for_instances_ssh() {
  local -a ips=("$@")
  local ip
  for ip in "${ips[@]}"; do
    [[ -n "${ip}" ]] || continue
    wait_for_ssh "${ip}"
  done
}

instance_exists() {
  local name="$1"
  local folder_args
  mapfile -t folder_args < <(yc_folder_args)
  yc compute instance get "${folder_args[@]}" --name "${name}" >/dev/null 2>&1
}

disk_exists() {
  local name="$1"
  local folder_args
  mapfile -t folder_args < <(yc_folder_args)
  yc compute disk get "${folder_args[@]}" --name "${name}" >/dev/null 2>&1
}
