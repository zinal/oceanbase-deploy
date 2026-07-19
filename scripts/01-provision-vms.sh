#!/usr/bin/env bash
# Создание виртуальных машин в Yandex Cloud (асинхронно, ydb-snippets pattern).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"
# shellcheck source=yc-instance.sh
source "${LIB_DIR}/lib/yc-instance.sh"

ACTION="${1:-create}"

require_file "${CONFIG_FILE}"
ensure_generated_dir
yc_folder_cache_init

deploy_name="$(yaml_get deployment.name)"
observer_count="$(yaml_get vm_profiles.observer.count)"
obproxy_count="$(yaml_get vm_profiles.obproxy.count)"
monitoring_enabled="$(yaml_get vm_profiles.monitoring.enabled)"
configserver_dedicated="$(yaml_get vm_profiles.configserver.dedicated)"

inventory="${GENERATED_DIR}/inventory.env"

declare -a VM_QUEUE=()

queue_vms() {
  local role="$1" count="$2" prefix="$3"
  local i name
  for (( i=1; i<=count; i++ )); do
    name="${deploy_name}-${prefix}-${i}"
    VM_QUEUE+=("${role}:${prefix}:${i}:${name}")
  done
}

write_inventory() {
  local key="$1" value="$2"
  echo "${key}=${value}" >> "${inventory}"
}

build_inventory_from_queue() {
  local entry role prefix idx name ip
  : > "${inventory}"
  for entry in "${VM_QUEUE[@]}"; do
    IFS=: read -r role prefix idx name <<< "${entry}"
    ip="$(get_instance_ip "${name}")"
    [[ -n "${ip}" ]] || die "Не удалось получить IP для ${name}"
    write_inventory "${prefix^^}_${idx}_NAME" "${name}"
    write_inventory "${prefix^^}_${idx}_IP" "${ip}"
    info "${name} -> ${ip}"
  done

  write_inventory "OBSERVER_COUNT" "${observer_count}"
  write_inventory "OBPROXY_COUNT" "${obproxy_count:-0}"
  write_inventory "CONFIGSERVER_COUNT" "$([[ "${configserver_dedicated}" == "true" ]] && yaml_get vm_profiles.configserver.count || echo 0)"
  write_inventory "MONITOR_COUNT" "$([[ "${monitoring_enabled}" == "true" ]] && yaml_get vm_profiles.monitoring.count || echo 0)"
  write_inventory "DEPLOY_NAME" "${deploy_name}"
  write_inventory "SSH_USER" "$(yaml_get yandex_cloud.ssh_user)"
  write_inventory "CONFIGSERVER_DEDICATED" "${configserver_dedicated}"
  info "Инвентарь сохранён: ${inventory}"
}

provision_async() {
  local entry role prefix idx name
  local -a new_vms=()
  local -a all_names=()
  local -a existing_names=()
  local -a disks_to_create=()
  local disks_needed=false

  for entry in "${VM_QUEUE[@]}"; do
    IFS=: read -r _ _ _ name <<< "${entry}"
    all_names+=("${name}")
  done

  info "Проверка существующих ВМ (${#all_names[@]})..."
  yc_list_existing_instances existing_names "${all_names[@]}"

  for entry in "${VM_QUEUE[@]}"; do
    IFS=: read -r role prefix idx name <<< "${entry}"
    if printf '%s\n' "${existing_names[@]:-}" | grep -qx "${name}"; then
      warn "ВМ ${name} уже существует, пропуск создания"
    else
      new_vms+=("${entry}")
      if needs_secondary_disks "${role}"; then
        disks_needed=true
        collect_disk_names_for_vm "${name}" "${role}" disks_to_create
      fi
    fi
  done

  if ((${#new_vms[@]} == 0)); then
    info "Новых ВМ для создания нет"
    build_inventory_from_queue
    return 0
  fi

  info "Будет создано ВМ: ${#new_vms[@]}"

  if [[ "${disks_needed}" == "true" ]]; then
    info "=== Фаза 1: создание дисков ==="
    if [[ -n "${YC_FOLDER_ID}" && "${YC_FOLDER_ID}" != "null" ]]; then
      info "Каталог Yandex Cloud: ${YC_FOLDER_ID}"
    else
      info "Каталог Yandex Cloud: из профиля yc (folder_id не задан в config)"
    fi
    local -a disks_created=()

    for entry in "${new_vms[@]}"; do
      IFS=: read -r role prefix idx name <<< "${entry}"
      create_instance_disks_async "${name}" "${role}" disks_created
    done

    if ((${#disks_created[@]} > 0)); then
      wait_for_disks_ready "${disks_created[@]}"
    fi
    yc_assert_last_op_ok "создание дисков"
  fi

  info "=== Фаза 2: создание ВМ ==="
  local -a new_vm_names=()
  for entry in "${new_vms[@]}"; do
    IFS=: read -r role prefix idx name <<< "${entry}"
    new_vm_names+=("${name}")
    create_instance_async "${name}" "${role}"
  done
  yc_assert_last_op_ok "создание ВМ"

  info "=== Фаза 3: ожидание готовности ВМ ==="
  wait_for_instances_ready "${deploy_name}" "${new_vm_names[@]}"

  build_inventory_from_queue

  info "=== Фаза 4: проверка SSH ==="
  local -a ips=()
  # shellcheck disable=SC1090
  source "${inventory}"
  for entry in "${VM_QUEUE[@]}"; do
    IFS=: read -r _ prefix idx _ <<< "${entry}"
    var="${prefix^^}_${idx}_IP"
    ips+=("${!var}")
  done
  wait_for_instances_ssh "${ips[@]}"
  info "Provision завершён успешно"
}

case "${ACTION}" in
  create)
    : > "${inventory}"
    info "Планирование observer-ВМ: ${observer_count}"
    queue_vms "observer" "${observer_count}" "observer"

    if [[ "${obproxy_count}" -gt 0 ]]; then
      info "Планирование obproxy-ВМ: ${obproxy_count}"
      queue_vms "obproxy" "${obproxy_count}" "obproxy"
    fi

    if [[ "${configserver_dedicated}" == "true" ]]; then
      cs_count="$(yaml_get vm_profiles.configserver.count)"
      info "Планирование configserver-ВМ: ${cs_count}"
      queue_vms "configserver" "${cs_count}" "configserver"
    fi

    if [[ "${monitoring_enabled}" == "true" ]]; then
      mon_count="$(yaml_get vm_profiles.monitoring.count)"
      info "Планирование monitoring-ВМ: ${mon_count}"
      queue_vms "monitoring" "${mon_count}" "monitor"
    fi

    provision_async
    ;;
  delete)
    if [[ -f "${inventory}" ]]; then
      # shellcheck disable=SC1090
      source "${inventory}"
      for var in $(compgen -v | grep -E '_NAME$'); do
        name="${!var}"
        delete_instance "${name}"
      done
      delete_deployment_disks "${deploy_name}"
      rm -f "${inventory}"
    else
      warn "Инвентарь не найден, удаление по метке deployment=${deploy_name}"
      yc_folder_cache_init
      mapfile -t names < <(yc compute instance list "${YC_FOLDER_ARGS[@]}" --format json | python3 -c "
import json,sys
name='${deploy_name}'
for i in json.load(sys.stdin):
  if i.get('labels',{}).get('deployment')==name:
    print(i['name'])
")
      for n in "${names[@]}"; do
        delete_instance "$n"
      done
      delete_deployment_disks "${deploy_name}"
    fi
    info "Удаление запущено (async)"
    ;;
  *)
    die "Использование: $0 [create|delete]"
    ;;
esac
