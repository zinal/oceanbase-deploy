#!/usr/bin/env bash
# Горизонтальное масштабирование: добавление observer-узлов (OBD scale_out).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"
# shellcheck source=yc-instance.sh
source "${LIB_DIR}/lib/yc-instance.sh"

require_file "${CONFIG_FILE}"
load_inventory
yc_folder_cache_init

ADD_COUNT="${1:-1}"
if ! [[ "${ADD_COUNT}" =~ ^[0-9]+$ ]] || [[ "${ADD_COUNT}" -lt 1 ]]; then
  die "Использование: $0 <количество_новых_observer>"
fi

command -v obd >/dev/null 2>&1 || die "OBD не установлен"

current="${OBSERVER_COUNT}"
deploy_name="${DEPLOY_NAME}"
declare -a new_names=()
declare -a new_ips=()
declare -a disks_to_create=()
declare -a disks_to_wait=()
declare -a existing_disks=()
declare -a existing_names=()

for (( n=1; n<=ADD_COUNT; n++ )); do
  idx=$((current + n))
  name="${deploy_name}-observer-${idx}"
  new_names+=("${name}")
  collect_disk_names_for_vm "${name}" "observer" disks_to_create
done

info "=== Создание дисков для ${ADD_COUNT} observer-узлов ==="
yc_list_existing_disks existing_disks "${disks_to_create[@]}"
for name in "${new_names[@]}"; do
  create_instance_disks_async "${name}" "observer" "${existing_disks[@]}"
done
for d in "${disks_to_create[@]}"; do
  if ! printf '%s\n' "${existing_disks[@]:-}" | grep -qx "${d}"; then
    disks_to_wait+=("${d}")
  fi
done
if ((${#disks_to_wait[@]} > 0)); then
  wait_for_disks_ready "${disks_to_wait[@]}"
fi

info "=== Создание ВМ ==="
yc_list_existing_instances existing_names "${new_names[@]}"
for name in "${new_names[@]}"; do
  if printf '%s\n' "${existing_names[@]:-}" | grep -qx "${name}"; then
    warn "ВМ ${name} уже существует, пропуск"
  else
    create_instance_async "${name}" "observer"
  fi
done
yc_assert_last_op_ok "создание observer-ВМ"
wait_for_instances_ready "${deploy_name}" "${new_names[@]}"

for name in "${new_names[@]}"; do
  ip="$(get_instance_ip "${name}")"
  [[ -n "${ip}" ]] || die "Не удалось получить IP для ${name}"
  idx="${name##*-}"
  echo "OBSERVER_${idx}_NAME=${name}" >> "${GENERATED_DIR}/inventory.env"
  echo "OBSERVER_${idx}_IP=${ip}" >> "${GENERATED_DIR}/inventory.env"
  new_ips+=("${ip}")
done

new_total=$((current + ADD_COUNT))
sed -i "s/^OBSERVER_COUNT=.*/OBSERVER_COUNT=${new_total}/" "${GENERATED_DIR}/inventory.env"

wait_for_instances_ssh "${new_ips[@]}"
bash "${LIB_DIR}/02-prepare-servers.sh" "${new_ips[@]}"

python3 "${LIB_DIR}/03-generate-obd-config.py" --output "${GENERATED_DIR}/scale-out.yaml"

info "Масштабирование через OBD scale_out..."
obd cluster scale_out "${deploy_name}" -c "${GENERATED_DIR}/scale-out.yaml"
obd cluster display "${deploy_name}"

info "Добавлено ${ADD_COUNT} observer-узлов. Всего: ${new_total}"
