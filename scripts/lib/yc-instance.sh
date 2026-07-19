#!/usr/bin/env bash
# Создание и удаление ВМ в Yandex Cloud с профилями по ролям.
# Асинхронный режим — ydb-snippets/admin/vms/supp/vms.sh.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"
# shellcheck source=yc-async.sh
source "${LIB_DIR}/yc-async.sh"

VM_PROFILES="${LIB_DIR}/vm_profiles.py"

resolve_vm_params() {
  local role="$1"
  python3 "${VM_PROFILES}" resolve "${role}" --config "${CONFIG_FILE}"
}

load_vm_params() {
  local role="$1"
  mapfile -t _VM_PARAMS < <(resolve_vm_params "${role}")
  VM_PLATFORM="${_VM_PARAMS[0]}"
  VM_CORES="${_VM_PARAMS[1]}"
  VM_MEMORY_GB="${_VM_PARAMS[2]}"
  VM_IMAGE_SPEC="${_VM_PARAMS[3]}"
  VM_CORE_FRACTION="${_VM_PARAMS[4]}"
  VM_BOOT_TYPE="${_VM_PARAMS[5]}"
  VM_BOOT_SIZE="${_VM_PARAMS[6]}"
  VM_DATA_ENABLED="${_VM_PARAMS[7]}"
  VM_DATA_TYPE="${_VM_PARAMS[8]}"
  VM_DATA_SIZE="${_VM_PARAMS[9]}"
  VM_DATA_MOUNT="${_VM_PARAMS[10]}"
  VM_LOG_ENABLED="${_VM_PARAMS[11]}"
  VM_LOG_TYPE="${_VM_PARAMS[12]}"
  VM_LOG_SIZE="${_VM_PARAMS[13]}"
  VM_LOG_MOUNT="${_VM_PARAMS[14]}"
}

write_cloud_init() {
  local cloud_init="$1" ssh_user="$2" ssh_key_file="$3" role="$4"
  local data_en="$5" data_mp="$6" log_en="$7" log_mp="$8"
  python3 - "$cloud_init" "$ssh_user" "$ssh_key_file" "$role" \
    "$data_en" "$data_mp" "$log_en" "$log_mp" <<'PY'
import sys, pathlib
out, user, key_file, role, data_en, data_mp, log_en, log_mp = sys.argv[1:9]
pub = pathlib.Path(key_file).read_text().strip()
content = f"""#cloud-config
users:
  - name: {user}
    groups: [sudo, adm]
    shell: /bin/bash
    sudo: ['ALL=(ALL) NOPASSWD:ALL']
    ssh_authorized_keys:
      - {pub}
package_update: true
packages:
  - python3
  - curl
  - wget
  - jq
  - lvm2
  - xfsprogs
  - e2fsprogs
write_files:
  - path: /etc/oceanbase-deploy-role-marker
    content: |
      role={role}
      data_disk_enabled={data_en}
      data_mount={data_mp}
      log_disk_enabled={log_en}
      log_mount={log_mp}
    permissions: '0644'
"""
pathlib.Path(out).write_text(content)
PY
}

# Создание secondary-дисков асинхронно (отдельно от ВМ, как в ydb-snippets).
create_instance_disks_async() {
  local name="$1" role="$2"
  load_vm_params "${role}"

  local zone deploy_name folder_args
  zone="$(yaml_get yandex_cloud.zone)"
  deploy_name="$(yaml_get deployment.name)"
  mapfile -t folder_args < <(yc_folder_args)

  local labels="deployment=${deploy_name},role=${role},managed-by=oceanbase-deploy"
  local created=false

  if [[ "${VM_DATA_ENABLED}" == "true" ]]; then
    local disk_name="${name}-data"
    if disk_exists "${disk_name}"; then
      info "Диск ${disk_name} уже существует, пропуск"
    else
      info "Создание диска ${disk_name} (${VM_DATA_TYPE}, ${VM_DATA_SIZE}G)..."
      yc_async_retry "создание диска ${disk_name}" \
        yc compute disk create --name "${disk_name}" --zone "${zone}" \
          "${folder_args[@]}" \
          --type "${VM_DATA_TYPE}" --size "${VM_DATA_SIZE}G" \
          --labels "${labels}"
      created=true
    fi
  fi

  if [[ "${VM_LOG_ENABLED}" == "true" ]]; then
    local disk_name="${name}-log"
    if disk_exists "${disk_name}"; then
      info "Диск ${disk_name} уже существует, пропуск"
    else
      info "Создание диска ${disk_name} (${VM_LOG_TYPE}, ${VM_LOG_SIZE}G)..."
      yc_async_retry "создание диска ${disk_name}" \
        yc compute disk create --name "${disk_name}" --zone "${zone}" \
          "${folder_args[@]}" \
          --type "${VM_LOG_TYPE}" --size "${VM_LOG_SIZE}G" \
          --labels "${labels}"
      created=true
    fi
  fi

  [[ "${created}" == "true" ]]
}

# Создание ВМ асинхронно с attach существующих дисков.
create_instance_async() {
  local name="$1" role="$2"
  load_vm_params "${role}"

  local zone subnet ssh_user ssh_key_file deploy_name net_accel nat_enabled
  local folder_args network_iface
  zone="$(yaml_get yandex_cloud.zone)"
  subnet="$(yaml_get yandex_cloud.subnet_name)"
  ssh_user="$(yaml_get yandex_cloud.ssh_user)"
  ssh_key_file="$(expand_path "$(yaml_get yandex_cloud.ssh_public_key_file)")"
  deploy_name="$(yaml_get deployment.name)"
  net_accel="$(yaml_get yandex_cloud.network_acceleration)"
  nat_enabled="$(yaml_get yandex_cloud.nat_enabled)"
  mapfile -t folder_args < <(yc_folder_args)

  if [[ "${nat_enabled}" == "true" ]]; then
    network_iface="subnet-name=${subnet},nat-ip-version=ipv4"
  else
    network_iface="subnet-name=${subnet}"
  fi

  require_file "$ssh_key_file"

  local cloud_init="${GENERATED_DIR}/cloud-init-${name}.yaml"
  write_cloud_init "${cloud_init}" "${ssh_user}" "${ssh_key_file}" "${role}" \
    "${VM_DATA_ENABLED}" "${VM_DATA_MOUNT}" "${VM_LOG_ENABLED}" "${VM_LOG_MOUNT}"

  info "Создание ВМ ${name} (${role}): ${VM_CORES} vCPU, ${VM_MEMORY_GB} GB, image=${VM_IMAGE_SPEC}"

  local boot_disk_spec="${VM_IMAGE_SPEC},name=${name}-boot,type=${VM_BOOT_TYPE},size=${VM_BOOT_SIZE}G,auto-delete=true"
  local -a create_args=(
    yc compute instance create
    --name "${name}"
    --hostname "${name}"
    --zone "${zone}"
    --platform "${VM_PLATFORM}"
    --cores "${VM_CORES}"
    --memory "${VM_MEMORY_GB}"
    --core-fraction "${VM_CORE_FRACTION}"
    --create-boot-disk "${boot_disk_spec}"
    --network-interface "${network_iface}"
    --metadata-from-file "user-data=${cloud_init}"
    --labels "deployment=${deploy_name},role=${role},managed-by=oceanbase-deploy"
  )

  if [[ -n "${net_accel}" && "${net_accel}" != "null" ]]; then
    create_args+=(--network-settings "type=${net_accel}")
  fi

  if [[ "${VM_DATA_ENABLED}" == "true" ]]; then
    create_args+=(--attach-disk "disk-name=${name}-data,auto-delete=true,device-name=data")
  fi
  if [[ "${VM_LOG_ENABLED}" == "true" ]]; then
    create_args+=(--attach-disk "disk-name=${name}-log,auto-delete=true,device-name=log")
  fi

  create_args+=("${folder_args[@]}")
  yc_async_retry "создание ВМ ${name}" "${create_args[@]}"
}

needs_secondary_disks() {
  local role="$1"
  load_vm_params "${role}"
  [[ "${VM_DATA_ENABLED}" == "true" || "${VM_LOG_ENABLED}" == "true" ]]
}

get_instance_ip() {
  local name="$1"
  local folder_args
  mapfile -t folder_args < <(yc_folder_args)
  yc compute instance get "${folder_args[@]}" --name "$name" --format json \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
for ni in d.get('network_interfaces', []):
    addr = ni.get('primary_v4_address', {})
    ip = addr.get('address') or (addr.get('one_to_one_nat') or {}).get('address')
    if ip:
        print(ip)
        break
"
}

delete_instance() {
  local name="$1"
  local folder_args
  mapfile -t folder_args < <(yc_folder_args)
  info "Удаление ВМ ${name}..."
  yc compute instance delete "${folder_args[@]}" --name "$name" --async \
    || warn "Не удалось удалить ${name}"
}

delete_instance_disk() {
  local name="$1"
  local folder_args
  mapfile -t folder_args < <(yc_folder_args)
  if disk_exists "${name}"; then
    yc compute disk delete "${folder_args[@]}" --name "$name" --async \
      || warn "Не удалось удалить диск ${name}"
  fi
}
