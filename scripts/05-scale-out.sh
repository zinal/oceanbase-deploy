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
declare -a new_hosts=()
declare -a disks_created=()
declare -a existing_names=()

for (( n=1; n<=ADD_COUNT; n++ )); do
  idx=$((current + n))
  name="${deploy_name}-observer-${idx}"
  new_names+=("${name}")
done

info "=== Создание дисков для ${ADD_COUNT} observer-узлов ==="
for name in "${new_names[@]}"; do
  create_instance_disks_async "${name}" "observer" disks_created
done
if ((${#disks_created[@]} > 0)); then
  wait_for_disks_ready "${disks_created[@]}"
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
  new_hosts+=("$(yc_internal_fqdn "${name}" "$(yaml_get yandex_cloud.zone)")")
done

new_total=$((current + ADD_COUNT))
sed -i "s/^OBSERVER_COUNT=.*/OBSERVER_COUNT=${new_total}/" "${GENERATED_DIR}/inventory.env"

wait_for_instances_ssh "${new_hosts[@]}"
bash "${LIB_DIR}/02-prepare-servers.sh" "${new_hosts[@]}"

FULL_OBD_CONFIG="${GENERATED_DIR}/obd-cluster.yaml"
PLAN_DIR="${GENERATED_DIR}/manual-scale-out"
PLAN_MANIFEST="${PLAN_DIR}/manifest.txt"
python3 "${LIB_DIR}/03-generate-obd-config.py" --output "${FULL_OBD_CONFIG}"

registered_config=""
for candidate in \
  "${HOME}/.obd/cluster/${deploy_name}/config.yaml" \
  "${HOME}/.obd/cluster/${deploy_name}/config.yml" \
  "${HOME}/.obd/cluster/${deploy_name}/inner_config.yaml" \
  "${HOME}/.obd/cluster/${deploy_name}/inner_config.yml"; do
  if [[ -f "${candidate}" ]]; then
    registered_config="${candidate}"
    break
  fi
done
[[ -n "${registered_config}" ]] || die "Кластер ${deploy_name} не зарегистрирован в OBD"

JOINED_IPS_ARGS=()
scale_out_joined_ips_args || true
python3 "${LIB_DIR}/lib/ob_deploy_plan.py" scale-out \
  --input "${FULL_OBD_CONFIG}" \
  --registered-config "${registered_config}" \
  --output-dir "${PLAN_DIR}" \
  --manifest "${PLAN_MANIFEST}" \
  "${JOINED_IPS_ARGS[@]}"

info "Запуск уже зарегистрированных obagent, если они ещё не работают..."
start_registered_obagents "${deploy_name}" "${registered_config}"

while IFS='|' read -r batch_label observer_yaml obagent_yaml; do
  [[ -n "${batch_label}" ]] || continue
  info "Масштабирование через OBD, пакет ${batch_label}..."
  if [[ "${observer_yaml}" != "-" ]]; then
    scale_out_observer "${deploy_name}" "${observer_yaml}"
  fi
  if [[ "${obagent_yaml}" != "-" ]]; then
    obd cluster scale_out "${deploy_name}" -c "${obagent_yaml}"
    start_obagent_yaml "${deploy_name}" "${obagent_yaml}"
  fi
done < "${PLAN_MANIFEST}"

obd cluster display "${deploy_name}"

info "Добавлено ${ADD_COUNT} observer-узлов. Всего: ${new_total}"
