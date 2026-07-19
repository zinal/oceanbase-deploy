#!/usr/bin/env bash
# Создание и удаление ВМ в Yandex Cloud с профилями по ролям.
# Формат образа и параметры создания — по образцу ydb-snippets/admin/vms.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"

VM_PROFILES="${LIB_DIR}/vm_profiles.py"

resolve_vm_params() {
  local role="$1"
  python3 "${VM_PROFILES}" resolve "${role}" --config "${CONFIG_FILE}"
}

create_instance() {
  local name="$1" role="$2"
  local platform cores memory_gb image_spec core_fraction
  local boot_type boot_size
  local data_enabled data_type data_size data_mount
  local log_enabled log_type log_size log_mount

  mapfile -t params < <(resolve_vm_params "$role")
  platform="${params[0]}"
  cores="${params[1]}"
  memory_gb="${params[2]}"
  image_spec="${params[3]}"
  core_fraction="${params[4]}"
  boot_type="${params[5]}"
  boot_size="${params[6]}"
  data_enabled="${params[7]}"
  data_type="${params[8]}"
  data_size="${params[9]}"
  data_mount="${params[10]}"
  log_enabled="${params[11]}"
  log_type="${params[12]}"
  log_size="${params[13]}"
  log_mount="${params[14]}"

  local zone subnet ssh_user ssh_key_file deploy_name net_accel
  zone="$(yaml_get yandex_cloud.zone)"
  subnet="$(yaml_get yandex_cloud.subnet_name)"
  ssh_user="$(yaml_get yandex_cloud.ssh_user)"
  ssh_key_file="$(expand_path "$(yaml_get yandex_cloud.ssh_public_key_file)")"
  deploy_name="$(yaml_get deployment.name)"
  net_accel="$(yaml_get yandex_cloud.network_acceleration)"

  require_file "$ssh_key_file"

  local cloud_init="${GENERATED_DIR}/cloud-init-${name}.yaml"
  python3 - "$cloud_init" "$ssh_user" "$ssh_key_file" "$role" \
    "$data_enabled" "$data_mount" "$log_enabled" "$log_mount" <<'PY'
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

  info "Создание ВМ ${name} (${role}): ${cores} vCPU, ${memory_gb} GB, image=${image_spec}"

  # ydb-snippets: --create-boot-disk image-folder-id=standard-images,image-family=...,name=...,type=...,size=...,auto-delete=true
  local boot_disk_spec="${image_spec},name=${name}-boot,type=${boot_type},size=${boot_size}G,auto-delete=true"

  # SSH-ключ задаётся в cloud-init (user-data); --ssh-key конфликтует с user-data в YC CLI
  local create_args=(
    yc compute instance create
    --name "${name}"
    --hostname "${name}"
    --zone "${zone}"
    --platform "${platform}"
    --cores "${cores}"
    --memory "${memory_gb}"
    --core-fraction "${core_fraction}"
    --create-boot-disk "${boot_disk_spec}"
    --network-interface "subnet-name=${subnet},nat-ip-version=ipv4"
    --metadata-from-file "user-data=${cloud_init}"
    --labels "deployment=${deploy_name},role=${role},managed-by=oceanbase-deploy"
    --format json
  )

  if [[ -n "${net_accel}" && "${net_accel}" != "null" ]]; then
    create_args+=(--network-settings "type=${net_accel}")
  fi

  if [[ "${data_enabled}" == "true" ]]; then
    create_args+=(--create-disk "name=${name}-data,auto-delete=true,size=${data_size}G,type=${data_type},device-name=data")
  fi

  if [[ "${log_enabled}" == "true" ]]; then
    create_args+=(--create-disk "name=${name}-log,auto-delete=true,size=${log_size}G,type=${log_type},device-name=log")
  fi

  local folder_id
  folder_id="$(yaml_get yandex_cloud.folder_id)"
  if [[ -n "${folder_id}" ]]; then
    create_args+=(--folder-id "${folder_id}")
  fi

  "${create_args[@]}"
}

get_instance_ip() {
  local name="$1"
  local folder_id
  folder_id="$(yaml_get yandex_cloud.folder_id)"
  local args=(yc compute instance get --name "$name" --format json)
  [[ -n "${folder_id}" ]] && args+=(--folder-id "${folder_id}")
  "${args[@]}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(next((a.get('address') for ni in d.get('network_interfaces',[]) for a in [ni.get('primary_v4_address',{}).get('one_to_one_nat',{}).get('address') or ni.get('primary_v4_address',{}).get('address')] if a), ''))"
}

delete_instance() {
  local name="$1"
  local folder_id
  folder_id="$(yaml_get yandex_cloud.folder_id)"
  local args=(yc compute instance delete --name "$name" --async)
  [[ -n "${folder_id}" ]] && args+=(--folder-id "${folder_id}")
  info "Удаление ВМ ${name}..."
  "${args[@]}" || warn "Не удалось удалить ${name}"
}
