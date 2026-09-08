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
# Квота Compute Cloud: 15 одновременных операций на каталог.
# Держим запас, 0 — не ждать слот (только retry после ошибки).
YC_MAX_INFLIGHT="${YC_MAX_INFLIGHT:-10}"

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

# Число незавершённых операций в каталоге. -1 — не удалось определить.
yc_count_active_ops() {
  command -v yc >/dev/null 2>&1 || { echo -1; return 0; }
  yc_folder_cache_init
  local out
  out="$(yc operation list "${YC_FOLDER_ARGS[@]}" --format json --limit 100 2>/dev/null | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print(-1)
    raise SystemExit
if isinstance(data, dict):
    ops = data.get('operations') or data.get('items') or []
elif isinstance(data, list):
    ops = data
else:
    print(-1)
    raise SystemExit
print(sum(1 for o in ops if isinstance(o, dict) and not o.get('done', True)))
" 2>/dev/null || true)"
  if [[ "${out}" =~ ^-?[0-9]+$ ]]; then
    printf '%s\n' "${out}"
  else
    echo -1
  fi
}

yc_wait_for_op_slot() {
  local max="${YC_MAX_INFLIGHT:-10}"
  if ! [[ "${max}" =~ ^[0-9]+$ ]] || (( max == 0 )); then
    return 0
  fi
  local elapsed=0 active
  while (( elapsed < YC_WAIT_TIMEOUT )); do
    active="$(yc_count_active_ops)"
    active="${active//$'\n'/}"
    if ! [[ "${active}" =~ ^-?[0-9]+$ ]] || (( active < 0 )); then
      return 0
    fi
    if (( active < max )); then
      return 0
    fi
    info "Активных операций YC: ${active} (лимит ${max}), ожидание слота..."
    sleep "${YC_WAIT_POLL}"
    elapsed=$((elapsed + YC_WAIT_POLL))
  done
  die "Таймаут ожидания слота операций YC (${YC_WAIT_TIMEOUT}с)"
}

yc_async_retry() {
  local description="$1"
  shift
  local attempt=0

  ensure_generated_dir
  while (( attempt < YC_ASYNC_MAX_RETRIES )); do
    yc_wait_for_op_slot
    if "$@" --async >"${YC_OP_LOG}" 2>&1; then
      if ! yc_op_has_rate_limit "${YC_OP_LOG}"; then
        return 0
      fi
    elif ! yc_op_has_rate_limit "${YC_OP_LOG}"; then
      cat "${YC_OP_LOG}" >&2
      die "Ошибка при ${description}"
    fi
    warn "Rate limit при ${description}, повтор через ${YC_RATE_LIMIT_SLEEP}с (попытка $((attempt + 1))/${YC_ASYNC_MAX_RETRIES})..."
    sleep "${YC_RATE_LIMIT_SLEEP}"
    # Не ((attempt++)): при attempt=0 и set -e это завершает скрипт (значение выражения 0).
    attempt=$((attempt + 1))
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
for i in json.load(sys.stdin):
    name = i.get('name')
    if name in want:
        print(name)
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
for d in json.load(sys.stdin):
    name = d.get('name')
    if name in want and d.get('status') not in skip:
        print(name)
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
ready = {'RUNNING', 'STOPPED'}
try:
    instances = json.load(sys.stdin)
except Exception:
    print(-1)
    raise SystemExit
by_name = {}
for i in instances:
    if i.get('labels', {}).get('deployment') != deploy:
        continue
    by_name[i.get('name')] = i.get('status')
if expect:
    pending = sum(1 for name in expect if by_name.get(name) not in ready)
else:
    pending = sum(1 for status in by_name.values() if status not in ready)
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

get_instance_status() {
  local name="$1"
  yc_folder_cache_init
  yc compute instance get "${YC_FOLDER_ARGS[@]}" --name "${name}" --format json 2>/dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('status',''))" \
    || true
}

wait_until_instance_status() {
  local name="$1" want="$2"
  local elapsed=0 status=""
  yc_folder_cache_init
  info "Ожидание статуса ${want} для ВМ ${name}..."
  while (( elapsed < YC_WAIT_TIMEOUT )); do
    status="$(get_instance_status "${name}")"
    if [[ "${status}" == "${want}" ]]; then
      info "ВМ ${name}: ${status}"
      return 0
    fi
    sleep "${YC_WAIT_POLL}"
    elapsed=$((elapsed + YC_WAIT_POLL))
  done
  die "ВМ ${name} не перешла в ${want} за ${YC_WAIT_TIMEOUT}с (сейчас: ${status:-unknown})"
}

wait_until_instance_absent() {
  local name="$1"
  local elapsed=0
  yc_folder_cache_init
  info "Ожидание удаления ВМ ${name}..."
  while (( elapsed < YC_WAIT_TIMEOUT )); do
    if ! instance_exists "${name}"; then
      info "ВМ ${name} удалена"
      return 0
    fi
    sleep "${YC_WAIT_POLL}"
    elapsed=$((elapsed + YC_WAIT_POLL))
  done
  die "ВМ ${name} всё ещё существует после ${YC_WAIT_TIMEOUT}с"
}

wait_until_disks_absent() {
  local -a disk_names=("$@")
  local elapsed=0
  if ((${#disk_names[@]} == 0)); then
    return 0
  fi
  yc_folder_cache_init
  info "Ожидание удаления дисков: ${disk_names[*]}"
  while (( elapsed < YC_WAIT_TIMEOUT )); do
    local pending=0
    local disk_name
    for disk_name in "${disk_names[@]}"; do
      if disk_exists "${disk_name}"; then
        pending=1
      fi
    done
    if (( pending == 0 )); then
      info "Указанные диски удалены"
      return 0
    fi
    sleep "${YC_WAIT_POLL}"
    elapsed=$((elapsed + YC_WAIT_POLL))
  done
  die "Диски не удалились за ${YC_WAIT_TIMEOUT}с: ${disk_names[*]}"
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
