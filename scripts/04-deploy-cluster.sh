#!/usr/bin/env bash
# Развёртывание кластера OceanBase через OBD (oceanbase-skills/cluster-management).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory

OBD_CONFIG="${GENERATED_DIR}/obd-cluster.yaml"
CLUSTER_NAME="${DEPLOY_NAME}"

run_obd() {
  if command -v stdbuf >/dev/null 2>&1; then
    stdbuf -oL -eL obd "$@"
  else
    obd "$@"
  fi
}

install_obd_if_needed() {
  if command -v obd >/dev/null 2>&1; then
    return 0
  fi
  info "Установка OBD..."
  if [[ -f /etc/redhat-release ]] || grep -qiE 'centos|rhel|rocky|almalinux|anolis' /etc/os-release 2>/dev/null; then
    local pkg_url="https://mirrors.oceanbase.com/community/stable/el/8/x86_64/ob-deploy-*.rpm"
    sudo yum install -y "https://mirrors.oceanbase.com/community/stable/el/8/x86_64/"*.rpm 2>/dev/null \
      || sudo yum install -y ob-deploy \
      || die "Установите OBD вручную: https://mirrors.oceanbase.com/community/stable/el/"
  elif command -v apt-get >/dev/null 2>&1; then
    warn "Для Ubuntu/Debian установите OBD с зеркала OceanBase или используйте управляющую ВМ на CentOS/RHEL"
    die "OBD не установлен. См. https://www.oceanbase.com/docs/common-obd-cn-1000000005246289"
  else
    die "OBD не установлен"
  fi
}

[[ -f "${OBD_CONFIG}" ]] || die "Сначала выполните: scripts/03-generate-obd-config.py"

install_obd_if_needed

verify_all_observer_storage

ob_version="$(yaml_get oceanbase.version)"

if obd_cluster_registered "${CLUSTER_NAME}"; then
  warn "Кластер ${CLUSTER_NAME} уже развёрнут в OBD — пропуск obd cluster deploy"
  warn "Для пересоздания: obd cluster destroy ${CLUSTER_NAME} -f (удалит данные) или obd cluster redeploy ${CLUSTER_NAME}"
else
  info "Развёртывание кластера ${CLUSTER_NAME}..."
  if [[ -n "${ob_version}" && "${ob_version}" != "null" ]]; then
    run_obd cluster deploy "${CLUSTER_NAME}" -c "${OBD_CONFIG}" -V "${ob_version}"
  else
    run_obd cluster deploy "${CLUSTER_NAME}" -c "${OBD_CONFIG}"
  fi
fi

info "Запуск кластера..."
run_obd cluster start "${CLUSTER_NAME}"

info "Статус кластера:"
run_obd cluster display "${CLUSTER_NAME}"

ocp_enabled="$(yaml_get ocp.enabled)"
ocp_vm_enabled="$(yaml_get vm_profiles.ocp.enabled)"
if [[ "${ocp_enabled}" == "true" && "${ocp_vm_enabled}" == "true" && "${OCP_COUNT:-0}" -gt 0 ]]; then
  ocp_port="$(yaml_get ocp.port)"
  [[ -z "${ocp_port}" || "${ocp_port}" == "null" ]] && ocp_port=8080
  ocp_user="$(yaml_get ocp.admin_username)"
  [[ -z "${ocp_user}" || "${ocp_user}" == "null" ]] && ocp_user=admin
  cat <<EOF

OceanBase Cloud Platform (OCP):
  URL:      http://${OCP_1_IP}:${ocp_port}
  Username: ${ocp_user}
  Password: см. ocp.admin_password в config/deploy.yaml

EOF
fi

cat <<EOF

Кластер развёрнут.

Подключение через OBProxy (если включён):
  mysql -h<obproxy_ip> -P$(yaml_get oceanbase.ports.obproxy) -uroot -p

Obshell dashboard (порт $(yaml_get oceanbase.ports.obshell)):
  http://<observer_ip>:$(yaml_get oceanbase.ports.obshell)

Дальнейшие операции (oceanbase-skills):
  obd cluster display ${CLUSTER_NAME}
  obd cluster scale_out ${CLUSTER_NAME} -c <expansion.yaml>
  obd cluster stop|restart ${CLUSTER_NAME}

EOF
