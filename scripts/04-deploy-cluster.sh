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

ob_version="$(yaml_get oceanbase.version)"
deploy_args=(obd cluster deploy "${CLUSTER_NAME}" -c "${OBD_CONFIG}")
if [[ -n "${ob_version}" ]]; then
  deploy_args+=(-V "${ob_version}")
fi

if obd cluster list 2>/dev/null | grep -q "${CLUSTER_NAME}"; then
  warn "Кластер ${CLUSTER_NAME} уже зарегистрирован в OBD"
else
  info "Развёртывание кластера ${CLUSTER_NAME}..."
  "${deploy_args[@]}"
fi

info "Запуск кластера..."
obd cluster start "${CLUSTER_NAME}"

info "Статус кластера:"
obd cluster display "${CLUSTER_NAME}"

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
