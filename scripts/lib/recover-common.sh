#!/usr/bin/env bash
# Общие шаги восстановления хоста: удаление мёртвой ВМ и создание замены с тем же именем.

set -euo pipefail

RECOVER_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${RECOVER_LIB}/common.sh"
# shellcheck source=yc-instance.sh
source "${RECOVER_LIB}/yc-instance.sh"

OB_SYS="${RECOVER_LIB}/ob-sys.py"

# Третий аргумент — имя переменной, куда записать новый IP (не stdout: туда идут логи).
recreate_role_vm() {
  local name="$1" role="$2"
  local -n _recreate_ip=$3
  local deploy_name
  deploy_name="$(yaml_get deployment.name)"

  local -a disk_names=()
  collect_disk_names_for_vm "${name}" "${role}" disk_names
  disk_names+=("${name}-boot")

  if instance_exists "${name}"; then
    info "Удаление потерянной ВМ ${name}..."
    delete_instance "${name}"
    wait_until_instance_absent "${name}"
  else
    info "ВМ ${name} уже отсутствует в Yandex Cloud"
  fi

  local disk_name
  for disk_name in "${disk_names[@]}"; do
    delete_instance_disk "${disk_name}"
  done
  wait_until_disks_absent "${disk_names[@]}"

  local -a disks_created=()
  if needs_secondary_disks "${role}"; then
    info "Создание дисков для ${name} (${role})..."
    create_instance_disks_async "${name}" "${role}" disks_created
    if ((${#disks_created[@]} > 0)); then
      wait_for_disks_ready "${disks_created[@]}"
    fi
  fi

  info "Создание замены ${name} (${role})..."
  create_instance_async "${name}" "${role}"
  yc_assert_last_op_ok "создание замены ${name}"
  wait_for_instances_ready "${deploy_name}" "${name}"

  local ip
  ip="$(get_instance_ip "${name}")"
  [[ -n "${ip}" ]] || die "Не удалось получить IP замены ${name}"
  wait_for_instances_ssh "${ip}"
  _recreate_ip="${ip}"
}

confirm_or_die() {
  local prompt="$1"
  if [[ "${RECOVER_YES:-false}" == "true" ]]; then
    return 0
  fi
  read -r -p "${prompt} [y/N] " answer
  [[ "${answer}" == "y" || "${answer}" == "Y" ]] || die "Отменено"
}
