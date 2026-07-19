#!/usr/bin/env bash
# Подготовка серверов: монтирование дисков, sysctl, пользователь OceanBase.
# Основано на рекомендациях oceanbase-skills (prepare-servers, configure-sysctl-conf).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"

require_file "${CONFIG_FILE}"
load_inventory

DEPLOY_USER="$(yaml_get oceanbase.deploy_user)"
DATA_MOUNT="$(yaml_get vm.data_disk.mount_point)"
LOG_DISK_ENABLED="$(yaml_get vm.log_disk.enabled)"
LOG_MOUNT="$(yaml_get vm.log_disk.mount_point)"
DATA_DIR="$(yaml_get oceanbase.data_dir)"
REDO_DIR="$(yaml_get oceanbase.redo_dir)"

TARGET_HOSTS=("$@")

prepare_host() {
  local host="$1" role="$2"
  info "Подготовка ${host} (${role})..."

  run_remote "${host}" "sudo bash -s" <<REMOTE
set -euo pipefail

DEPLOY_USER="${DEPLOY_USER}"
DATA_MOUNT="${DATA_MOUNT}"
LOG_DISK_ENABLED="${LOG_DISK_ENABLED}"
LOG_MOUNT="${LOG_MOUNT}"
DATA_DIR="${DATA_DIR}"
REDO_DIR="${REDO_DIR}"

# --- Монтирование дополнительного диска ---
mount_data_disk() {
  local mount_point="\$1"
  local device=""
  for d in /dev/vdb /dev/sdb /dev/nvme1n1; do
    if [[ -b "\$d" ]]; then device="\$d"; break; fi
  done
  [[ -n "\$device" ]] || { echo "Дополнительный диск не найден, пропуск"; return 0; }

  if ! blkid "\$device" >/dev/null 2>&1; then
    mkfs.ext4 -F "\$device"
  fi
  mkdir -p "\$mount_point"
  if ! grep -q "\$mount_point" /etc/fstab; then
    uuid=\$(blkid -s UUID -o value "\$device")
    echo "UUID=\${uuid} \${mount_point} ext4 defaults,noatime,nodiratime,nodelalloc 0 2" >> /etc/fstab
  fi
  mount -a || mount "\$mount_point" || true
}

if [[ "${role}" != "obproxy" ]]; then
  mount_data_disk "\${DATA_MOUNT}"
  if [[ "\${LOG_DISK_ENABLED}" == "true" ]]; then
    mount_data_disk "\${LOG_MOUNT}"
  fi
  mkdir -p "\${DATA_DIR}" "\${REDO_DIR}"
  chown -R "\${DEPLOY_USER}:\${DEPLOY_USER}" "\${DATA_MOUNT}" 2>/dev/null || true
fi

id -u "\${DEPLOY_USER}" >/dev/null 2>&1 || useradd -m -s /bin/bash "\${DEPLOY_USER}"
usermod -aG sudo "\${DEPLOY_USER}" 2>/dev/null || usermod -aG wheel "\${DEPLOY_USER}" 2>/dev/null || true

# --- sysctl (oceanbase-skills) ---
cat >/etc/sysctl.d/99-oceanbase.conf <<'SYSCTL'
fs.aio-max-nr = 1048576
net.core.somaxconn = 2048
net.core.netdev_max_backlog = 10000
net.core.rmem_default = 16777216
net.core.wmem_default = 16777216
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.ip_forward = 0
net.ipv4.conf.default.rp_filter = 1
net.ipv4.conf.default.accept_source_route = 0
net.ipv4.tcp_syncookies = 1
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.tcp_max_syn_backlog = 16384
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_slow_start_after_idle = 0
vm.swappiness = 0
vm.min_free_kbytes = 2097152
vm.overcommit_memory = 0
fs.file-max = 6573688
fs.pipe-user-pages-soft = 0
vm.max_map_count = 655360
SYSCTL
sysctl -p /etc/sysctl.d/99-oceanbase.conf || sysctl --system

# --- limits ---
cat >/etc/security/limits.d/oceanbase.conf <<LIMITS
${DEPLOY_USER} soft nofile 655350
${DEPLOY_USER} hard nofile 655350
${DEPLOY_USER} soft nproc 655350
${DEPLOY_USER} hard nproc 655350
${DEPLOY_USER} soft core unlimited
${DEPLOY_USER} hard core unlimited
LIMITS

# --- disable swap (рекомендация для production) ---
swapoff -a 2>/dev/null || true
sed -i.bak '/ swap / s/^/#/' /etc/fstab 2>/dev/null || true

echo "Подготовка завершена на \$(hostname)"
REMOTE
}

prepare_all_observers() {
  if ((${#TARGET_HOSTS[@]} > 0)); then
    for host in "${TARGET_HOSTS[@]}"; do
      prepare_host "${host}" "observer"
    done
    return
  fi
  for i in $(seq 1 "${OBSERVER_COUNT}"); do
    var="OBSERVER_${i}_IP"
    prepare_host "${!var}" "observer"
  done
}

prepare_all_observers

if ((${#TARGET_HOSTS[@]} == 0)); then
  if [[ "${OBPROXY_COUNT:-0}" -gt 0 ]]; then
    for i in $(seq 1 "${OBPROXY_COUNT}"); do
      var="OBPROXY_${i}_IP"
      prepare_host "${!var}" "obproxy"
    done
  fi

  if [[ "${MONITOR_COUNT:-0}" -gt 0 ]]; then
    for i in $(seq 1 "${MONITOR_COUNT}"); do
      var="MONITOR_${i}_IP"
      prepare_host "${!var}" "monitor"
    done
  fi
fi

info "Подготовка всех серверов завершена"
