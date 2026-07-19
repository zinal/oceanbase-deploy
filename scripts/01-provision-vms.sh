#!/usr/bin/env bash
# Создание виртуальных машин в Yandex Cloud по профилям vm_profiles.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"
# shellcheck source=yc-instance.sh
source "${LIB_DIR}/yc-instance.sh"

ACTION="${1:-create}"

require_file "${CONFIG_FILE}"
ensure_generated_dir

deploy_name="$(yaml_get deployment.name)"
observer_count="$(yaml_get vm_profiles.observer.count)"
obproxy_count="$(yaml_get vm_profiles.obproxy.count)"
monitoring_enabled="$(yaml_get vm_profiles.monitoring.enabled)"
configserver_dedicated="$(yaml_get vm_profiles.configserver.dedicated)"

inventory="${GENERATED_DIR}/inventory.env"
: > "${inventory}"

write_inventory() {
  local key="$1" value="$2"
  echo "${key}=${value}" >> "${inventory}"
}

provision_role() {
  local role="$1" count="$2" prefix="$3"
  local i name ip
  for (( i=1; i<=count; i++ )); do
    name="${deploy_name}-${prefix}-${i}"
    if [[ "${ACTION}" == "create" ]]; then
      if yc compute instance get --name "${name}" >/dev/null 2>&1; then
        warn "ВМ ${name} уже существует, пропуск создания"
      else
        create_instance "${name}" "${role}"
        sleep 5
      fi
    fi
    ip="$(get_instance_ip "${name}")"
    [[ -n "${ip}" ]] || die "Не удалось получить IP для ${name}"
    write_inventory "${prefix^^}_${i}_NAME" "${name}"
    write_inventory "${prefix^^}_${i}_IP" "${ip}"
    info "${name} -> ${ip}"
  done
  write_inventory "${prefix^^}_COUNT" "${count}"
}

case "${ACTION}" in
  create)
    info "Создание observer-ВМ: ${observer_count}"
    provision_role "observer" "${observer_count}" "observer"

    if [[ "${obproxy_count}" -gt 0 ]]; then
      info "Создание obproxy-ВМ: ${obproxy_count}"
      provision_role "obproxy" "${obproxy_count}" "obproxy"
    else
      write_inventory "OBPROXY_COUNT" "0"
    fi

    if [[ "${configserver_dedicated}" == "true" ]]; then
      cs_count="$(yaml_get vm_profiles.configserver.count)"
      info "Создание configserver-ВМ: ${cs_count}"
      provision_role "configserver" "${cs_count}" "configserver"
    else
      write_inventory "CONFIGSERVER_COUNT" "0"
    fi

    if [[ "${monitoring_enabled}" == "true" ]]; then
      mon_count="$(yaml_get vm_profiles.monitoring.count)"
      info "Создание monitoring-ВМ: ${mon_count}"
      provision_role "monitoring" "${mon_count}" "monitor"
    else
      write_inventory "MONITOR_COUNT" "0"
    fi

    write_inventory "DEPLOY_NAME" "${deploy_name}"
    write_inventory "SSH_USER" "$(yaml_get yandex_cloud.ssh_user)"
    write_inventory "CONFIGSERVER_DEDICATED" "${configserver_dedicated}"
    info "Инвентарь сохранён: ${inventory}"
    ;;
  delete)
    if [[ -f "${inventory}" ]]; then
      # shellcheck disable=SC1090
      source "${inventory}"
      for var in $(compgen -v | grep -E '_NAME$'); do
        delete_instance "${!var}"
      done
      rm -f "${inventory}"
    else
      warn "Инвентарь не найден, удаление по метке deployment=${deploy_name}"
      mapfile -t names < <(yc compute instance list --format json | python3 -c "
import json,sys
name='${deploy_name}'
for i in json.load(sys.stdin):
  if i.get('labels',{}).get('deployment')==name:
    print(i['name'])
")
      for n in "${names[@]}"; do delete_instance "$n"; done
    fi
    ;;
  *)
    die "Использование: $0 [create|delete]"
    ;;
esac
