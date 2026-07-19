#!/usr/bin/env bash
# Подготовка серверов: монтирование data/log дисков, sysctl, пользователь OceanBase.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory

DEPLOY_USER="$(yaml_get oceanbase.deploy_user)"
DATA_DIR="$(yaml_get oceanbase.data_dir)"
REDO_DIR="$(yaml_get oceanbase.redo_dir)"

OBS_JSON="$(python3 "${LIB_DIR}/lib/vm_profiles.py" resolve observer --config "${CONFIG_FILE}" --format json)"
MON_JSON="$(python3 "${LIB_DIR}/lib/vm_profiles.py" resolve monitoring --config "${CONFIG_FILE}" --format json)"

read -r OBS_DATA_MOUNT OBS_LOG_ENABLED OBS_LOG_MOUNT < <(
  python3 -c "import json,sys; o=json.loads(sys.argv[1]); print(o['data_disk'].get('mount_point','/data')); print(str(o['log_disk'].get('enabled',False)).lower()); print(o['log_disk'].get('mount_point','/data/log1'))" "${OBS_JSON}"
)

MON_DATA_MOUNT="$(python3 -c "import json,sys; m=json.loads(sys.argv[1]); print(m['data_disk'].get('mount_point','/data') if m['data_disk'].get('enabled') else '')" "${MON_JSON}")"

MONITORING_VM_ENABLED="$(yaml_get vm_profiles.monitoring.enabled)"
PROMETHEUS_COMPONENT="$(yaml_get oceanbase.components.prometheus)"
NODE_EXPORTER_ENABLED="$(yaml_get monitoring.node_exporter.enabled)"
NODE_EXPORTER_PORT="$(yaml_get monitoring.node_exporter.port)"
NODE_EXPORTER_VERSION="$(yaml_get monitoring.node_exporter.version)"
INSTALL_NODE_EXPORTER=false

[[ -z "${NODE_EXPORTER_ENABLED}" || "${NODE_EXPORTER_ENABLED}" == "null" ]] && NODE_EXPORTER_ENABLED=true
[[ -z "${NODE_EXPORTER_PORT}" || "${NODE_EXPORTER_PORT}" == "null" ]] && NODE_EXPORTER_PORT=9100
[[ -z "${NODE_EXPORTER_VERSION}" || "${NODE_EXPORTER_VERSION}" == "null" ]] && NODE_EXPORTER_VERSION=1.8.2

if [[ "${NODE_EXPORTER_ENABLED}" == "true" ]]; then
  if [[ "${MONITORING_VM_ENABLED}" == "true" || "${PROMETHEUS_COMPONENT}" == "true" ]]; then
    INSTALL_NODE_EXPORTER=true
  fi
fi

TARGET_HOSTS=("$@")

prepare_host() {
  local host="$1" role="$2"
  local data_mount="${OBS_DATA_MOUNT}" log_enabled="${OBS_LOG_ENABLED}" log_mount="${OBS_LOG_MOUNT}"
  local need_data="true" need_log="${log_enabled}"

  if [[ "${role}" == "obproxy" || "${role}" == "configserver" ]]; then
    need_data="false"
    need_log="false"
  elif [[ "${role}" == "monitor" ]]; then
    need_log="false"
    data_mount="${MON_DATA_MOUNT}"
    [[ -n "${data_mount}" ]] || need_data="false"
  fi

  info "Подготовка ${host} (${role})..."

  if ! run_remote "${host}" "sudo bash -s" <<REMOTE
set -euo pipefail

command -v mkfs.ext4 >/dev/null 2>&1 || {
  apt-get update -qq
  apt-get install -y -qq e2fsprogs
}

DEPLOY_USER="${DEPLOY_USER}"
DATA_DIR="${DATA_DIR}"
REDO_DIR="${REDO_DIR}"
NEED_DATA="${need_data}"
NEED_LOG="${need_log}"
DATA_MOUNT="${data_mount}"
LOG_MOUNT="${log_mount}"

mount_device() {
  local device="\$1" mount_point="\$2"
  [[ -b "\${device}" ]] || return 1
  if ! blkid "\${device}" >/dev/null 2>&1; then
    mkfs.ext4 -F "\${device}" >/dev/null 2>&1
  fi
  mkdir -p "\${mount_point}"
  if ! grep -q "\${mount_point}" /etc/fstab; then
    uuid=\$(blkid -s UUID -o value "\${device}")
    echo "UUID=\${uuid} \${mount_point} ext4 defaults,noatime,nodiratime,nodelalloc 0 2" >> /etc/fstab
  fi
  mount -a >/dev/null 2>&1 || mount "\${mount_point}" >/dev/null 2>&1 || true
}

if [[ "\${NEED_DATA}" == "true" ]]; then
  for d in /dev/disk/by-id/virtio-data /dev/vdb /dev/sdb; do
    if mount_device "\${d}" "\${DATA_MOUNT}"; then break; fi
  done
fi
if [[ "\${NEED_LOG}" == "true" ]]; then
  for d in /dev/disk/by-id/virtio-log /dev/vdc /dev/sdc; do
    if mount_device "\${d}" "\${LOG_MOUNT}"; then break; fi
  done
fi

if [[ "${role}" == "observer" || "${role}" == "monitor" ]]; then
  mkdir -p "\${DATA_DIR}" "\${REDO_DIR}" 2>/dev/null || mkdir -p "\${DATA_MOUNT}"
  chown -R "\${DEPLOY_USER}:\${DEPLOY_USER}" "\${DATA_MOUNT}" 2>/dev/null || true
  [[ "\${NEED_LOG}" == "true" ]] && chown -R "\${DEPLOY_USER}:\${DEPLOY_USER}" "\${LOG_MOUNT}" 2>/dev/null || true
fi

id -u "\${DEPLOY_USER}" >/dev/null 2>&1 || useradd -m -s /bin/bash "\${DEPLOY_USER}"
usermod -aG sudo "\${DEPLOY_USER}" 2>/dev/null || usermod -aG wheel "\${DEPLOY_USER}" 2>/dev/null || true

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
sysctl -p /etc/sysctl.d/99-oceanbase.conf >/dev/null 2>&1 || sysctl --system >/dev/null 2>&1

cat >/etc/security/limits.d/oceanbase.conf <<LIMITS
${DEPLOY_USER} soft nofile 655350
${DEPLOY_USER} hard nofile 655350
${DEPLOY_USER} soft nproc 655350
${DEPLOY_USER} hard nproc 655350
${DEPLOY_USER} soft stack unlimited
${DEPLOY_USER} hard stack unlimited
${DEPLOY_USER} soft core unlimited
${DEPLOY_USER} hard core unlimited
LIMITS

swapoff -a 2>/dev/null || true
sed -i.bak '/ swap / s/^/#/' /etc/fstab 2>/dev/null || true

if [[ "${INSTALL_NODE_EXPORTER}" == "true" ]]; then
  NODE_EXPORTER_PORT="${NODE_EXPORTER_PORT}"
  NODE_EXPORTER_VERSION="${NODE_EXPORTER_VERSION}"
  if ! systemctl is-active --quiet node_exporter 2>/dev/null; then
    apt-get update -qq
    apt-get install -y -qq wget ca-certificates
    ARCH="$(uname -m)"
    case "${ARCH}" in
      x86_64) NE_ARCH=amd64 ;;
      aarch64) NE_ARCH=arm64 ;;
      *) echo "node_exporter: неподдерживаемая архитектура ${ARCH}" >&2; exit 1 ;;
    esac
    id -u node_exporter >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter
    TMPDIR="$(mktemp -d)"
    wget -q "https://github.com/prometheus/node_exporter/releases/download/v\${NODE_EXPORTER_VERSION}/node_exporter-\${NODE_EXPORTER_VERSION}.linux-\${NE_ARCH}.tar.gz" -O "\${TMPDIR}/node_exporter.tgz"
    tar xzf "\${TMPDIR}/node_exporter.tgz" -C "\${TMPDIR}"
    install -o node_exporter -g node_exporter -m 0755 "\${TMPDIR}/node_exporter-\${NODE_EXPORTER_VERSION}.linux-\${NE_ARCH}/node_exporter" /usr/local/bin/node_exporter
    rm -rf "\${TMPDIR}"
    cat >/etc/systemd/system/node_exporter.service <<NEUNIT
[Unit]
Description=Prometheus Node Exporter
After=network-online.target
Wants=network-online.target

[Service]
User=node_exporter
Group=node_exporter
Type=simple
ExecStart=/usr/local/bin/node_exporter \\
  --web.listen-address=0.0.0.0:\${NODE_EXPORTER_PORT} \\
  --collector.systemd \\
  --collector.processes \\
  --collector.filesystem.mount-points-exclude='^/(dev|proc|sys|var/lib/docker/.+)($|/)'
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
NEUNIT
    systemctl daemon-reload
    systemctl enable --now node_exporter >/dev/null 2>&1
  fi
fi
REMOTE
  then
    die "Ошибка подготовки ${host} (${role})"
  fi

  info "Готово: ${host} (${role})"
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
  if [[ "${CONFIGSERVER_DEDICATED:-false}" == "true" && "${CONFIGSERVER_COUNT:-0}" -gt 0 ]]; then
    prepare_host "${CONFIGSERVER_1_IP}" "configserver"
  fi
  if [[ "${MONITOR_COUNT:-0}" > 0 ]]; then
    for i in $(seq 1 "${MONITOR_COUNT}"); do
      var="MONITOR_${i}_IP"
      prepare_host "${!var}" "monitor"
    done
  fi
fi

if [[ "${INSTALL_NODE_EXPORTER}" == "true" ]]; then
  info "node_exporter установлен на всех узлах (port ${NODE_EXPORTER_PORT})"
fi
info "Подготовка всех серверов завершена"
