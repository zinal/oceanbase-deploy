#!/usr/bin/env bash
# Асинхронные операции Yandex Cloud (по образцу ydb-snippets/admin/vms/supp/vms.sh).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"

YC_OP_LOG="${GENERATED_DIR}/yc-op.log"
YC_RATE_LIMIT_SLEEP="${YC_RATE_LIMIT_SLEEP:-10}"
YC_ASYNC_MAX_RETRIES="${YC_ASYNC_MAX_RETRIES:-60}"
YC_WAIT_TIMEOUT="${YC_WAIT_TIMEOUT:-3600}"
YC_WAIT_POLL="${YC_WAIT_POLL:-5}"

# Кеш folder-id (yaml_get на каждый yc-вызов тормозит provision)
YC_FOLDER_ID=""
YC_FOLDER_ARGS=()

yc_folder_cache_init() {
  YC_FOLDER_ID="$(yaml_get yandex_cloud.folder_id)"
  YC_FOLDER_ARGS=()
  if [[ -n "${YC_FOLDER_ID}" && "${YC_FOLDER_ID}" != "null" ]]; then
    YC_FOLDER_ARGS=(--folder-id "${YC_FOLDER_ID}")
  fi
}

yc_folder_args() {
  yc_folder_cache_init
  printf '%s\n' "${YC_FOLDER_ARGS[@]}"
}

yc_op_has_rate_limit() {
  grep -q "The limit on maximum number of active operations has exceeded" "${1}" 2>/dev/null
}

yc_op_has_error() {
  grep -q "ERROR:" "${1}" 2>/dev/null
}

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

# Одним запросом: множество имён instance -> существующие
yc_list_existing_instances() {
  local -n _out=$1
  shift
  local -a names=("$@")
  yc_folder_cache_init

  if ((${#names[@]} == 0)); then
    _out=()
    return 0
  fi

  mapfile -t _out < <(yc compute instance list "${YC_FOLDER_ARGS[@]}" --format json 2>/dev/null | python3 -c "
import json, sys
want = set(sys.argv[1:])
existing = [i['name'] for i in json.load(sys.stdin) if i.get('name') in want]
print('\n'.join(existing))
" "${names[@]}")
}

yc_list_existing_disks() {
  local -n _out=$1
  shift
  local -a names=("$@")
  yc_folder_cache_init

  if ((${#names[@]} == 0)); then
    _out=()
    return 0
  fi

  mapfile -t _out < <(yc compute disk list "${YC_FOLDER_ARGS[@]}" --format json 2>/dev/null | python3 -c "
import json, sys
want = set(sys.argv[1:])
skip = {'DELETING'}
existing = [
    d['name'] for d in json.load(sys.stdin)
    if d.get('name') in want and d.get('status') not in skip
]
print('\n'.join(existing))
" "${names[@]}")
}

instance_exists() {
  local name="$1"
  local -a found=()
  yc_list_existing_instances found "${name}"
  ((${#found[@]} > 0))
}

# Точная проверка через disk get (list может давать рассинхрон после удаления).
disk_lookup() {
  local name="$1"
  yc_folder_cache_init
  yc compute disk get "${YC_FOLDER_ARGS[@]}" --name "${name}" --format json 2>/dev/null \
    | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(1)
status = d.get('status', '')
if status in ('DELETING',):
    sys.exit(1)
disk_id = d.get('id', '')
if not disk_id:
    sys.exit(1)
print(f\"{disk_id}\t{status}\")
"
}

disk_exists() {
  local name="$1"
  disk_lookup "${name}" >/dev/null 2>&1
}

disk_exists_info() {
  local name="$1"
  disk_lookup "${name}" 2>/dev/null || true
}

# Ожидание READY только для указанных дисков (не всего каталога по префиксу)
wait_for_disks_ready() {
  local -a disk_names=("$@")
  local elapsed=0

  if ((${#disk_names[@]} == 0)); then
    return 0
  fi

  yc_folder_cache_init
  info "Ожидание READY для ${#disk_names[@]} диск(ов)..."

  while (( elapsed < YC_WAIT_TIMEOUT )); do
    local pending
    pending="$(yc compute disk list "${YC_FOLDER_ARGS[@]}" --format json 2>/dev/null | python3 -c "
import json, sys
want = set(sys.argv[1:])
pending = sum(
    1 for d in json.load(sys.stdin)
    if d.get('name') in want and d.get('status') != 'READY'
)
print(pending)
" "${disk_names[@]}")"

    if [[ "${pending}" == "0" ]]; then
      info "Все диски READY"
      return 0
    fi
    info "Дисков в процессе: ${pending} (ожидание ${elapsed}/${YC_WAIT_TIMEOUT}с)..."
    sleep "${YC_WAIT_POLL}"
    elapsed=$((elapsed + YC_WAIT_POLL))
  done
  die "Таймаут ожидания дисков (${YC_WAIT_TIMEOUT}с). Проверьте: yc compute disk list"
}

wait_for_instances_ready() {
  local deploy_name="$1"
  local -a expect_names=("${@:2}")
  local elapsed=0

  yc_folder_cache_init
  info "Ожидание RUNNING/STOPPED для deployment=${deploy_name}..."

  while (( elapsed < YC_WAIT_TIMEOUT )); do
    local pending
    pending="$(yc compute instance list "${YC_FOLDER_ARGS[@]}" --format json 2>/dev/null | python3 -c "
import json, sys
deploy, *expect = sys.argv[1:]
expect_set = set(expect) if expect else None
ready = {'RUNNING', 'STOPPED'}
pending = 0
for i in json.load(sys.stdin):
    if i.get('labels', {}).get('deployment') != deploy:
        continue
    if expect_set is not None and i.get('name') not in expect_set:
        continue
    if i.get('status') not in ready:
        pending += 1
print(pending)
" "${deploy_name}" "${expect_names[@]}")"

    if [[ "${pending}" == "0" ]]; then
      info "Все ВМ готовы (RUNNING/STOPPED)"
      return 0
    fi
    info "ВМ в процессе: ${pending} (ожидание ${elapsed}/${YC_WAIT_TIMEOUT}с)..."
    sleep "${YC_WAIT_POLL}"
    elapsed=$((elapsed + YC_WAIT_POLL))
  done
  die "Таймаут ожидания ВМ (${YC_WAIT_TIMEOUT}с). Проверьте: yc compute instance list"
}

wait_for_instances_ssh() {
  local -a ips=("$@")
  local -a pids=()
  local ip pid failed=0

  for ip in "${ips[@]}"; do
    [[ -n "${ip}" ]] || continue
    wait_for_ssh "${ip}" &
    pids+=($!)
  done

  for pid in "${pids[@]}"; do
    wait "${pid}" || failed=1
  done

  if (( failed != 0 )); then
    die "SSH недоступен на одном или нескольких хостах"
  fi
  info "SSH доступен на всех ${#pids[@]} хост(ах)"
}
