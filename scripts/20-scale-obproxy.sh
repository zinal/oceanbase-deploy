#!/usr/bin/env bash
# Добавить/пересобрать состав obproxy на живом кластере под vm_profiles.obproxy.
# ВМ не удаляет: досоздаёт недостающие {deploy}-obproxy-N с текущими параметрами yaml,
# делает OBD scale_out, вычищает исчезнувшие IP из метаданных OBD,
# переписывает inventory и HAProxy на всех runner.

set -euo pipefail

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${SCRIPTS_DIR}/lib/common.sh"
# shellcheck source=lib/yc-instance.sh
source "${SCRIPTS_DIR}/lib/yc-instance.sh"

OB_SYS="${SCRIPTS_DIR}/lib/ob-sys.py"
SCALE_PY="${SCRIPTS_DIR}/lib/obproxy_scale.py"

usage() {
  cat <<'USAGE'
Использование: ./scripts/deploy.sh scale-obproxy [опции]
         или: ./scripts/20-scale-obproxy.sh [опции]

Доводит число obproxy до vm_profiles.obproxy.count и применяет
текущие cores/memory/boot_disk из yaml к НОВЫМ ВМ (уже существующие не трогает).

Официальная замена ODP — add-then-delete:
  1. Поднимите count и параметры в config/deploy.yaml
  2. Запустите эту команду (новые ВМ + OBD + HAProxy на всех runner)
  3. Старые ВМ удалите сами, когда трафик уйдёт на новые
  4. Повторите команду: недостающие имена 1..N создадутся уже с новыми
     параметрами, старые IP уйдут из OBD, HAProxy обновится снова

Опции:
  --yes              не спрашивать подтверждение
  --dry-run          показать план, ничего не менять
  --skip-provision   ВМ уже созданы, только OBD / inventory / HAProxy
  --skip-haproxy     не трогать runner HAProxy
  --skip-obd         не вызывать scale_out / clean-obd (только облако + inventory + HAProxy)

Документация: docs/haproxy-obproxy-tcp-lb.md, README «Масштабирование»
USAGE
}

RECOVER_YES=false
DRY_RUN=false
SKIP_PROVISION=false
SKIP_HAPROXY=false
SKIP_OBD=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes) RECOVER_YES=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --skip-provision) SKIP_PROVISION=true; shift ;;
    --skip-haproxy) SKIP_HAPROXY=true; shift ;;
    --skip-obd) SKIP_OBD=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "Неизвестный аргумент: $1" ;;
  esac
done

require_file "${CONFIG_FILE}"
require_file "${GENERATED_DIR}/inventory.env"
load_inventory
yc_folder_cache_init
ensure_generated_dir
source_obd_env || true

ob_sys() {
  python3 "${OB_SYS}" --config "${CONFIG_FILE}" --inventory "${GENERATED_DIR}/inventory.env" "$@"
}

confirm_or_die() {
  local prompt="$1"
  if [[ "${RECOVER_YES}" == "true" ]]; then
    return 0
  fi
  read -r -p "${prompt} [y/N] " answer
  [[ "${answer}" == "y" || "${answer}" == "Y" ]] || die "Отменено"
}

DESIRED_COUNT="$(yaml_get vm_profiles.obproxy.count)"
[[ -n "${DESIRED_COUNT}" && "${DESIRED_COUNT}" != "null" ]] \
  || die "В config/deploy.yaml нет vm_profiles.obproxy.count"
if ! [[ "${DESIRED_COUNT}" =~ ^[0-9]+$ ]] || [[ "${DESIRED_COUNT}" -lt 1 ]]; then
  die "vm_profiles.obproxy.count должен быть целым >= 1 (сейчас: ${DESIRED_COUNT})"
fi

PROXY_CORES="$(yaml_get vm_profiles.obproxy.cores)"
PROXY_MEM="$(yaml_get vm_profiles.obproxy.memory_gb)"
[[ -n "${PROXY_CORES}" && "${PROXY_CORES}" != "null" ]] || PROXY_CORES="(vm_defaults)"
[[ -n "${PROXY_MEM}" && "${PROXY_MEM}" != "null" ]] || PROXY_MEM="(vm_defaults)"

EXISTING_FILE="${GENERATED_DIR}/scale-obproxy-existing.txt"
OBD_IPS_FILE="${GENERATED_DIR}/scale-obproxy-obd-ips.txt"
FORCE_SCALE_FILE="${GENERATED_DIR}/scale-obproxy-force-scale.txt"
DISPLAY_FILE="${GENERATED_DIR}/scale-obproxy-display.txt"
PLAN_JSON="${GENERATED_DIR}/scale-obproxy-plan.json"
PLAN_DIR="${GENERATED_DIR}/scale-obproxy"
: > "${FORCE_SCALE_FILE}"
: > "${DISPLAY_FILE}"

collect_existing() {
  local -a check_names=()
  local -a found_names=()
  local i name var ip
  : > "${EXISTING_FILE}"
  for (( i=1; i<=DESIRED_COUNT; i++ )); do
    check_names+=("${DEPLOY_NAME}-obproxy-${i}")
  done
  for (( i=1; i<=${OBPROXY_COUNT:-0}; i++ )); do
    var="OBPROXY_${i}_NAME"
    name="${!var:-}"
    [[ -n "${name}" ]] && check_names+=("${name}")
  done
  if ((${#check_names[@]} == 0)); then
    return 0
  fi
  yc_list_existing_instances found_names "${check_names[@]}"
  for name in "${found_names[@]:-}"; do
    [[ -n "${name}" ]] || continue
    ip="$(get_instance_ip "${name}")"
    echo "${name}=${ip}" >> "${EXISTING_FILE}"
  done
}

collect_obd_ips() {
  local -a display_arg=()
  : > "${OBD_IPS_FILE}"
  : > "${DISPLAY_FILE}"
  if [[ "${SKIP_OBD}" == "true" ]]; then
    return 0
  fi
  if command -v obd >/dev/null 2>&1; then
    obd cluster display "${DEPLOY_NAME}" >"${DISPLAY_FILE}" 2>/dev/null || true
  fi
  if [[ -s "${DISPLAY_FILE}" ]]; then
    display_arg=(--display-file "${DISPLAY_FILE}")
  fi
  ob_sys list-component-ips --component obproxy-ce --deploy-name "${DEPLOY_NAME}" \
    "${display_arg[@]}" > "${OBD_IPS_FILE}" || true
}

write_plan() {
  python3 "${SCALE_PY}" plan \
    --deploy-name "${DEPLOY_NAME}" \
    --desired-count "${DESIRED_COUNT}" \
    --inventory "${GENERATED_DIR}/inventory.env" \
    --existing-file "${EXISTING_FILE}" \
    --obd-ips-file "${OBD_IPS_FILE}" \
    --force-scale-file "${FORCE_SCALE_FILE}" \
    --output "${PLAN_JSON}" \
    --text
}

plan_rows() {
  local key="$1"
  python3 - "${PLAN_JSON}" "${key}" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as fh:
    plan = json.load(fh)
val = plan[sys.argv[2]]
if isinstance(val, list):
    if val and isinstance(val[0], dict):
        for item in val:
            print(f"{item.get('index', '')}\t{item.get('name', '')}\t{item.get('ip', '')}")
    else:
        for item in val:
            print(item)
elif val not in (None, ""):
    print(val)
PY
}

obproxy_home_path() {
  printf '/home/%s/obproxy' "$(observer_deploy_user)"
}

# Короткий SSH: OBD scale_out ходит по всем уже прописанным obproxy.
# Мёртвый адрес даёт OBD-1013 (connect failed: timed out).
ssh_probe() {
  local host="$1"
  local user key port
  user="$(ssh_connect_user)"
  key="$(ssh_private_key_path)"
  port="$(ssh_connect_port)"
  ssh -T -p "${port}" -i "${key}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=5 -o ConnectionAttempts=1 -o BatchMode=yes \
    -o LogLevel=ERROR \
    "${user}@${host}" "echo ok" >/dev/null 2>&1
}

# running | installed | empty | unreachable
obproxy_host_state() {
  local host="$1"
  local user key port home state
  if ! ssh_probe "${host}"; then
    printf '%s\n' "unreachable"
    return 0
  fi
  user="$(ssh_connect_user)"
  key="$(ssh_private_key_path)"
  port="$(ssh_connect_port)"
  home="$(obproxy_home_path)"
  state="$(ssh -T -p "${port}" -i "${key}" \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o ConnectTimeout=8 -o ConnectionAttempts=1 -o BatchMode=yes \
    -o LogLevel=ERROR \
    "${user}@${host}" "bash -s" <<REMOTE
set -euo pipefail
HOME_PATH="${home}"
if pgrep -f '[/]bin/obproxy' >/dev/null 2>&1; then
  echo running
  exit 0
fi
if [[ -x "\${HOME_PATH}/bin/obproxy" ]]; then
  echo installed
  exit 0
fi
echo empty
REMOTE
)" || state="unreachable"
  printf '%s\n' "${state:-unreachable}"
}

# OBD scale_out требует, чтобы уже прописанные obproxy-ce были running.
# Пересозданная ВМ с тем же IP: SSH есть, бинаря нет → «is not running».
# $1=true — можно делать obd start (после подтверждения, не в dry-run).
probe_obd_obproxy_peers() {
  local allow_start="${1:-false}"
  local ip state
  : > "${FORCE_SCALE_FILE}"
  if [[ "${SKIP_OBD}" == "true" ]]; then
    return 0
  fi
  while IFS= read -r ip; do
    [[ -n "${ip}" ]] || continue
    state="$(obproxy_host_state "${ip}")"
    case "${state}" in
      running)
        info "OBD peer ${ip}: obproxy running"
        ;;
      installed)
        if [[ "${allow_start}" == "true" ]]; then
          info "OBD peer ${ip}: бинарь есть, процесс не запущен — пробую obd start"
          if obd_start_component "${DEPLOY_NAME}" "obproxy-ce" "${ip}" \
            && [[ "$(obproxy_host_state "${ip}")" == "running" ]]; then
            info "OBD peer ${ip}: процесс поднят"
          else
            warn "OBD peer ${ip}: start не помог — сниму из OBD и поставлю scale_out"
            echo "${ip}" >> "${FORCE_SCALE_FILE}"
          fi
        else
          info "OBD peer ${ip}: бинарь есть, процесс не запущен (после подтверждения — start)"
        fi
        ;;
      empty)
        info "OBD peer ${ip}: ВМ пустая (нет $(obproxy_home_path)/bin) — сниму из OBD и поставлю scale_out"
        echo "${ip}" >> "${FORCE_SCALE_FILE}"
        ;;
      *)
        info "OBD peer ${ip}: ${state} — сниму из OBD до scale_out"
        echo "${ip}" >> "${FORCE_SCALE_FILE}"
        ;;
    esac
  done < "${OBD_IPS_FILE}"
}

final_ips() {
  plan_rows final | awk -F'\t' '$3 != "" { print $3 }'
}

clean_stale_obproxy_obd() {
  local old_ip
  while IFS= read -r old_ip; do
    [[ -n "${old_ip}" ]] || continue
    info "Вычищаю старый IP ${old_ip} из метаданных OBD до scale_out (иначе OBD-1013)..."
    ob_sys clean-obd --ip "${old_ip}" --deploy-name "${DEPLOY_NAME}" || true
  done < <(plan_rows clean_obd_ips)

  while IFS= read -r old_ip; do
    [[ -n "${old_ip}" ]] || continue
    info "Вычищаю пустой/мёртвый OBD peer ${old_ip}..."
    ob_sys clean-obd --ip "${old_ip}" --deploy-name "${DEPLOY_NAME}" || true
  done < "${FORCE_SCALE_FILE}"
}

# Пока в YAML/display остаются пустые peer — scale_out других узлов падает.
# Повторяем list+probe+clean: 10.130.0.5 мог быть только в inner_config / display.
purge_empty_obd_peers() {
  local allow_start="${1:-false}"
  local round
  if [[ "${SKIP_OBD}" == "true" ]]; then
    return 0
  fi
  for round in 1 2 3 4 5; do
    collect_obd_ips
    probe_obd_obproxy_peers "${allow_start}"
    if [[ ! -s "${FORCE_SCALE_FILE}" ]]; then
      return 0
    fi
    info "Раунд ${round}: в OBD ещё пустые peer ($(tr '\n' ' ' < "${FORCE_SCALE_FILE}")) — чищу и перечитываю"
    clean_stale_obproxy_obd
  done
  collect_obd_ips
  probe_obd_obproxy_peers false
  if [[ -s "${FORCE_SCALE_FILE}" ]]; then
    warn "После 5 раундов OBD всё ещё знает пустые peer: $(tr '\n' ' ' < "${FORCE_SCALE_FILE}")"
  fi
}

refresh_runner_haproxy() {
  if [[ "${SKIP_HAPROXY}" == "true" ]]; then
    info "HAProxy пропущен (--skip-haproxy)"
    return 0
  fi
  load_inventory
  if [[ "${RUNNER_COUNT:-0}" -lt 1 ]]; then
    info "Нет runner-ВМ в inventory — HAProxy некуда ставить"
    return 0
  fi
  info "Обновление HAProxy на всех runner (backend — новые имена obproxy)..."
  bash "${SCRIPTS_DIR}/10-runner-haproxy.sh" --skip-if-none
}

apply_proxy_runtime() {
  if ! bash "${SCRIPTS_DIR}/12-obproxy-log.sh" apply --skip-if-ok; then
    warn "не удалось выставить логи ODP — ./scripts/deploy.sh obproxy-log apply"
  fi
  if ! bash "${SCRIPTS_DIR}/17-obproxy-mem.sh" apply --skip-if-ok; then
    warn "не удалось выставить proxy_mem_limited — ./scripts/deploy.sh obproxy-mem apply"
  fi
  if ! bash "${SCRIPTS_DIR}/11-obproxy-route.sh" apply --skip-if-ok; then
    warn "не удалось выставить маршрутизацию ODP — ./scripts/deploy.sh obproxy-route apply"
  fi
}

info "Целевой состав obproxy: ${DESIRED_COUNT} шт., ${PROXY_CORES} vCPU / ${PROXY_MEM} GB (из vm_profiles.obproxy)"
info "Сейчас в inventory: ${OBPROXY_COUNT:-0}"

collect_existing
collect_obd_ips
probe_obd_obproxy_peers false
write_plan

if [[ "${DRY_RUN}" == "true" ]]; then
  info "dry-run: облако, OBD и HAProxy не меняю"
  exit 0
fi

confirm_or_die "Продолжить scale-obproxy до ${DESIRED_COUNT} ВМ? Существующие ВМ не удаляются."

if [[ "${SKIP_OBD}" != "true" ]]; then
  command -v obd >/dev/null 2>&1 || die "OBD не установлен"
  obd_cluster_registered "${DEPLOY_NAME}" \
    || die "Кластер OBD ${DEPLOY_NAME} не зарегистрирован"
fi

declare -a new_names=()
declare -a new_hosts=()
while IFS=$'\t' read -r idx name _ip; do
  [[ -n "${name}" ]] || continue
  new_names+=("${name}")
done < <(plan_rows create)

if [[ "${SKIP_PROVISION}" == "true" ]]; then
  if ((${#new_names[@]} > 0)); then
    die "В YC нет ВМ (${new_names[*]}), а задан --skip-provision"
  fi
  info "provision пропущен — используем уже существующие ВМ"
elif ((${#new_names[@]} == 0)); then
  info "Новых ВМ создавать не нужно"
else
  info "=== Создание ${#new_names[@]} obproxy-ВМ (${new_names[*]}) ==="
  declare -a disks_created=()
  for name in "${new_names[@]}"; do
    create_instance_disks_async "${name}" "obproxy" disks_created
  done
  if ((${#disks_created[@]} > 0)); then
    wait_for_disks_ready "${disks_created[@]}"
  fi

  declare -a existing_names=()
  yc_list_existing_instances existing_names "${new_names[@]}"
  for name in "${new_names[@]}"; do
    if printf '%s\n' "${existing_names[@]:-}" | grep -qx "${name}"; then
      warn "ВМ ${name} уже существует, пропуск создания"
    else
      create_instance_async "${name}" "obproxy"
    fi
  done
  yc_assert_last_op_ok "создание obproxy-ВМ"
  wait_for_instances_ready "${DEPLOY_NAME}" "${new_names[@]}"

  zone="$(yaml_get yandex_cloud.zone)"
  for name in "${new_names[@]}"; do
    new_hosts+=("$(yc_internal_fqdn "${name}" "${zone}")")
  done
  wait_for_instances_ssh "${new_hosts[@]}"
  bash "${SCRIPTS_DIR}/02-prepare-servers.sh" --role obproxy "${new_hosts[@]}"
fi

# После create IP известны — пересчитать план и снять все пустые OBD peer
# (не только те, что нашлись в первом config.yaml — 10.130.0.5 мог быть
# только в inner_config / obd cluster display).
load_inventory
collect_existing
if [[ "${SKIP_OBD}" != "true" ]]; then
  purge_empty_obd_peers true
fi
write_plan >/dev/null

missing_ip="$(plan_rows missing_ip | tr '\n' ' ')"
if [[ -n "${missing_ip// /}" ]]; then
  die "Нет IP у ВМ: ${missing_ip}. Проверьте yc compute instance get"
fi

mkdir -p "${PLAN_DIR}"
if [[ "${SKIP_OBD}" != "true" ]]; then
  while IFS=$'\t' read -r idx name ip; do
    [[ -n "${ip}" ]] || continue
    scale_out="${PLAN_DIR}/obproxy-${idx}.yaml"
    ob_sys write-scale-out --role obproxy --index "${idx}" --ip "${ip}" --output "${scale_out}"
    info "OBD scale_out obproxy-ce ${name} (${ip})..."
    if ! obd cluster scale_out "${DEPLOY_NAME}" -c "${scale_out}"; then
      die "OBD scale_out ${name} (${ip}) не удался. OBD-1013 — мёртвый IP в ~/.obd/cluster/${DEPLOY_NAME}/; «obproxy-ce is not running» — уже прописанный peer без процесса (пересозданная ВМ). Повторите ./scripts/deploy.sh scale-obproxy --yes --skip-provision (скрипт снимет пустые peer и поставит их заново)."
    fi
  done < <(plan_rows scale_out)
fi

replace_args=()
while IFS=$'\t' read -r idx name ip; do
  [[ -n "${idx}" && -n "${name}" && -n "${ip}" ]] || continue
  replace_args+=(--entry "${idx},${name},${ip}")
done < <(plan_rows final)
[[ ${#replace_args[@]} -gt 0 ]] || die "Пустой итоговый список obproxy"
ob_sys replace-inventory --prefix OBPROXY "${replace_args[@]}"
python3 "${SCRIPTS_DIR}/03-generate-obd-config.py" --output "${GENERATED_DIR}/obd-cluster.yaml"

info "Статус кластера:"
if [[ "${SKIP_OBD}" != "true" ]]; then
  obd cluster display "${DEPLOY_NAME}" || true
fi

apply_proxy_runtime
refresh_runner_haproxy

cat <<EOF

scale-obproxy завершён.
  Цель:     ${DESIRED_COUNT} × ${PROXY_CORES} vCPU / ${PROXY_MEM} GB
  Inventory: ${GENERATED_DIR}/inventory.env (OBPROXY_COUNT=${DESIRED_COUNT})
  HAProxy:   generated/haproxy-runner.cfg и все runner-ВМ

Если старые ВМ ещё работают — удалите их сами, затем повторите
./scripts/deploy.sh scale-obproxy
чтобы недостающие имена 1..N создались с новыми параметрами, а HAProxy
снова переписался без старых backend.

EOF
