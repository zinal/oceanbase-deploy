#!/usr/bin/env bash
# Создание и удаление ВМ в Yandex Cloud.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"

resolve_vm_params() {
  # resolve_vm_params <role> -> exports PLATFORM CORES MEMORY_GB IMAGE FAMILY BOOT_*
  local role="$1"
  python3 - "${CONFIG_FILE}" "${role}" <<'PY'
import sys
import yaml

cfg_path, role = sys.argv[1], sys.argv[2]

with open(cfg_path, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

vm = cfg.get("vm", {})
role_cfg = cfg.get("nodes", {}).get(role, {}) or {}

def pick(key, default=None):
    val = role_cfg.get(key)
    if val is None:
        val = vm.get(key, default)
    return val

boot = pick("boot_disk") or vm.get("boot_disk", {})
if isinstance(boot, dict) and role_cfg.get("boot_disk"):
    boot = {**boot, **{k: v for k, v in role_cfg["boot_disk"].items() if v is not None}}

data = pick("data_disk") or vm.get("data_disk", {})
if isinstance(data, dict) and role_cfg.get("data_disk"):
    data = {**data, **{k: v for k, v in role_cfg["data_disk"].items() if v is not None}}

print(pick("platform", "standard-v3"))
print(pick("cores", 4))
print(pick("memory_gb", 16))
print(cfg["vm"].get("image_family", "ubuntu-2204-lts"))
print(cfg["vm"].get("core_fraction", 100))
print(boot.get("type", "network-ssd"))
print(boot.get("size_gb", 50))
print(str(data.get("enabled", True)).lower())
print(data.get("type", "network-ssd"))
print(data.get("size_gb", 500))
print(data.get("mount_point", "/data"))
PY
}

create_instance() {
  local name="$1" role="$2"
  local platform cores memory_gb image_family core_fraction
  local boot_type boot_size data_enabled data_type data_size data_mount

  mapfile -t params < <(resolve_vm_params "$role")
  platform="${params[0]}"
  cores="${params[1]}"
  memory_gb="${params[2]}"
  image_family="${params[3]}"
  core_fraction="${params[4]}"
  boot_type="${params[5]}"
  boot_size="${params[6]}"
  data_enabled="${params[7]}"
  data_type="${params[8]}"
  data_size="${params[9]}"
  data_mount="${params[10]}"

  local zone network subnet ssh_user ssh_key_file deploy_name
  zone="$(yaml_get yandex_cloud.zone)"
  network="$(yaml_get yandex_cloud.network_name)"
  subnet="$(yaml_get yandex_cloud.subnet_name)"
  ssh_user="$(yaml_get yandex_cloud.ssh_user)"
  ssh_key_file="$(expand_path "$(yaml_get yandex_cloud.ssh_public_key_file)")"
  deploy_name="$(yaml_get deployment.name)"

  require_file "$ssh_key_file"

  local cloud_init="${GENERATED_DIR}/cloud-init-${name}.yaml"
  python3 - "$cloud_init" "$ssh_user" "$ssh_key_file" "$role" "$data_enabled" "$data_mount" <<'PY'
import sys, pathlib
out, user, key_file, role, data_enabled, mount = sys.argv[1:7]
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
      data_disk_enabled={data_enabled}
      data_mount={mount}
    permissions: '0644'
"""
pathlib.Path(out).write_text(content)
PY

  info "Создание ВМ ${name} (${role}): ${cores} vCPU, ${memory_gb} GB RAM, platform=${platform}"

  local create_args=(
    yc compute instance create
    --name "${name}"
    --hostname "${name}"
    --zone "${zone}"
    --platform "${platform}"
    --cores "${cores}"
    --memory "${memory_gb}GB"
    --core-fraction "${core_fraction}"
    --create-boot-disk "image-family=${image_family},size=${boot_size},type=${boot_type}"
    --network-interface "subnet-name=${subnet},nat-ip-version=ipv4"
    --metadata-from-file "user-data=${cloud_init}"
    --labels "deployment=${deploy_name},role=${role},managed-by=oceanbase-deploy"
    --format json
  )

  if [[ "${data_enabled}" == "true" ]]; then
    create_args+=(--create-disk "name=${name}-data,auto-delete=true,size=${data_size},type=${data_type}")
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
