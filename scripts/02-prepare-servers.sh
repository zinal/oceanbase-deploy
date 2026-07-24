#!/usr/bin/env bash
# Подготовка серверов: монтирование data/log дисков, sysctl, пользователь OceanBase.
# Хосты готовятся параллельно; подробные логи — в generated/prepare-logs/.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory
ensure_generated_dir

PREPARE_LOG_DIR="${GENERATED_DIR}/prepare-logs"
mkdir -p "${PREPARE_LOG_DIR}"

DEPLOY_USER="$(yaml_get oceanbase.deploy_user)"
SSH_USER="$(yaml_get yandex_cloud.ssh_user)"
[[ -z "${DEPLOY_USER}" || "${DEPLOY_USER}" == "null" ]] && DEPLOY_USER="${SSH_USER}"
if [[ -n "${SSH_USER}" && "${SSH_USER}" != "null" && "${DEPLOY_USER}" != "${SSH_USER}" ]]; then
  die "oceanbase.deploy_user (${DEPLOY_USER}) должен совпадать с yandex_cloud.ssh_user (${SSH_USER}) — OBD подключается по SSH как deploy_user"
fi
DATA_DIR="$(yaml_get oceanbase.data_dir)"
REDO_DIR="$(yaml_get oceanbase.redo_dir)"

OBS_JSON="$(python3 "${LIB_DIR}/lib/vm_profiles.py" resolve observer --config "${CONFIG_FILE}" --format json)"

read -r OBS_DATA_MOUNT OBS_LOG_ENABLED OBS_LOG_MOUNT < <(
  python3 -c "import json,sys; o=json.loads(sys.argv[1]); print(o['data_disk'].get('mount_point','/data')); print(str(o['log_disk'].get('enabled',False)).lower()); print(o['log_disk'].get('mount_point','/data/log1'))" "${OBS_JSON}"
)

MONITORING_VM_ENABLED="$(yaml_get vm_profiles.monitoring.enabled)"
OCP_VM_ENABLED="$(yaml_get vm_profiles.ocp.enabled)"

# monitoring/ocp профили опциональны — resolve только если включены
MON_DATA_MOUNT=""
if [[ "${MONITORING_VM_ENABLED}" == "true" ]]; then
  MON_JSON="$(python3 "${LIB_DIR}/lib/vm_profiles.py" resolve monitoring --config "${CONFIG_FILE}" --format json)"
  MON_DATA_MOUNT="$(python3 -c "import json,sys; m=json.loads(sys.argv[1]); print(m['data_disk'].get('mount_point','/data') if m['data_disk'].get('enabled') else '')" "${MON_JSON}")"
fi

OCP_DATA_MOUNT="/ocp-data"
OCP_DATA_ENABLED="false"
if [[ "${OCP_VM_ENABLED}" == "true" ]]; then
  OCP_JSON="$(python3 "${LIB_DIR}/lib/vm_profiles.py" resolve ocp --config "${CONFIG_FILE}" --format json)"
  read -r OCP_DATA_MOUNT OCP_DATA_ENABLED < <(
    python3 -c "import json,sys; o=json.loads(sys.argv[1]); print(o['data_disk'].get('mount_point','/ocp-data')); print(str(o['data_disk'].get('enabled',False)).lower())" "${OCP_JSON}"
  )
fi

OCP_HOME="$(yaml_get ocp.home_path)"
OCP_SOFT_DIR="$(yaml_get ocp.soft_dir)"
OCP_LOG_DIR="$(yaml_get ocp.log_dir)"
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

declare -a PREPARE_PIDS=()
declare -a PREPARE_LABELS=()
declare -a PREPARE_LOGS=()

prepare_log_path() {
  local host="$1" role="$2"
  local safe
  safe="$(printf '%s' "${host}" | tr './:' '___')"
  printf '%s/%s-%s.log' "${PREPARE_LOG_DIR}" "${role}" "${safe}"
}

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
  elif [[ "${role}" == "ocp" ]]; then
    need_log="false"
    data_mount="${OCP_DATA_MOUNT}"
    [[ "${OCP_DATA_ENABLED}" == "true" ]] || need_data="false"
  fi

  info "Подготовка ${host} (${role})..."

  if ! run_remote "${host}" "sudo env \
ROLE='${role}' \
DEPLOY_USER='${DEPLOY_USER}' \
DATA_DISK_ENABLED='${need_data}' \
DATA_MOUNT='${data_mount}' \
LOG_DISK_ENABLED='${need_log}' \
LOG_MOUNT='${log_mount}' \
DATA_DIR='${DATA_DIR}' \
REDO_DIR='${REDO_DIR}' \
bash -s" < "${LIB_DIR}/lib/mount-role-disks.sh"
  then
    die "Ошибка монтирования дисков на ${host} (${role})"
  fi

  if ! run_remote "${host}" "sudo bash -s" <<REMOTE
set -euo pipefail

command -v mkfs.ext4 >/dev/null 2>&1 || {
  apt-get update -qq
  apt-get install -y -qq e2fsprogs
}

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
    ARCH="\$(uname -m)"
    case "\${ARCH}" in
      x86_64) NE_ARCH=amd64 ;;
      aarch64) NE_ARCH=arm64 ;;
      *) echo "node_exporter: неподдерживаемая архитектура \${ARCH}" >&2; exit 1 ;;
    esac
    id -u node_exporter >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter
    TMPDIR="\$(mktemp -d)"
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

prepare_ocp_host() {
  local host="$1"
  info "Установка Java и clockdiff на ${host} (OCP)..."
  if ! run_remote "${host}" "sudo env \
DEPLOY_USER='${DEPLOY_USER}' \
OCP_HOME='${OCP_HOME}' \
OCP_SOFT_DIR='${OCP_SOFT_DIR}' \
OCP_LOG_DIR='${OCP_LOG_DIR}' \
bash -s" < "${LIB_DIR}/lib/prepare-ocp-host.sh"
  then
    die "Ошибка подготовки OCP на ${host}"
  fi
  info "OCP host готов: ${host}"
}

run_prepare_job() {
  local host="$1" role="$2"
  prepare_host "${host}" "${role}"
  if [[ "${role}" == "ocp" ]]; then
    prepare_ocp_host "${host}"
  fi
}

start_prepare_job() {
  local host="$1" role="$2"
  local logfile
  logfile="$(prepare_log_path "${host}" "${role}")"
  : > "${logfile}"
  info "Старт: ${host} (${role}) → ${logfile}"
  (
    run_prepare_job "${host}" "${role}"
  ) >>"${logfile}" 2>&1 &
  PREPARE_PIDS+=($!)
  PREPARE_LABELS+=("${host} (${role})")
  PREPARE_LOGS+=("${logfile}")
}

wait_prepare_jobs() {
  local failed=0 i status
  local -a failed_logs=()

  for i in "${!PREPARE_PIDS[@]}"; do
    status=0
    wait "${PREPARE_PIDS[$i]}" || status=$?
    if (( status == 0 )); then
      info "Готово: ${PREPARE_LABELS[$i]}"
    else
      warn "Ошибка: ${PREPARE_LABELS[$i]} (код ${status}) — см. ${PREPARE_LOGS[$i]}"
      failed_logs+=("${PREPARE_LOGS[$i]}")
      failed=1
    fi
  done

  if (( failed != 0 )); then
    for logfile in "${failed_logs[@]}"; do
      warn "----- tail ${logfile} -----"
      tail -n 40 "${logfile}" >&2 || true
    done
    die "Подготовка завершилась с ошибками на ${#failed_logs[@]} хост(ах). Логи: ${PREPARE_LOG_DIR}"
  fi
}

info "Фаза prepare: параллельная подготовка серверов (логи: ${PREPARE_LOG_DIR})"

if ((${#TARGET_HOSTS[@]} > 0)); then
  for host in "${TARGET_HOSTS[@]}"; do
    start_prepare_job "${host}" "observer"
  done
else
  for i in $(seq 1 "${OBSERVER_COUNT}"); do
    start_prepare_job "$(inventory_host OBSERVER "${i}")" "observer"
  done
  if [[ "${OBPROXY_COUNT:-0}" -gt 0 ]]; then
    for i in $(seq 1 "${OBPROXY_COUNT}"); do
      start_prepare_job "$(inventory_host OBPROXY "${i}")" "obproxy"
    done
  fi
  if [[ "${CONFIGSERVER_DEDICATED:-false}" == "true" && "${CONFIGSERVER_COUNT:-0}" -gt 0 ]]; then
    start_prepare_job "$(inventory_host CONFIGSERVER 1)" "configserver"
  fi
  if [[ "${MONITOR_COUNT:-0}" -gt 0 ]]; then
    for i in $(seq 1 "${MONITOR_COUNT}"); do
      start_prepare_job "$(inventory_host MONITOR "${i}")" "monitor"
    done
  fi
  if [[ "${OCP_VM_ENABLED}" == "true" && "${OCP_COUNT:-0}" -gt 0 ]]; then
    for i in $(seq 1 "${OCP_COUNT}"); do
      start_prepare_job "$(inventory_host OCP "${i}")" "ocp"
    done
  fi
fi

info "Ожидание завершения ${#PREPARE_PIDS[@]} параллельн(ых) задач(и)..."
wait_prepare_jobs

if [[ "${INSTALL_NODE_EXPORTER}" == "true" ]]; then
  info "node_exporter установлен на всех узлах (port ${NODE_EXPORTER_PORT})"
fi
info "Подготовка всех серверов завершена"
