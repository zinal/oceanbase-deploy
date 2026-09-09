#!/usr/bin/env bash
# Регистрация уже развёрнутого oceanbase-ce в консоли OCP (export-to-ocp).
#
# ocp-server-ce на OCP-ВМ — это JVM :8080, не второй observer. Список кластеров
# в UI пустой, пока OBD не сделает takeover: obd cluster export-to-ocp.
#
#   ./scripts/deploy.sh ocp-register
#   ./scripts/09-ocp-register.sh

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

usage() {
  cat <<'EOF'
Использование: ./scripts/09-ocp-register.sh

Проверяет тенанты ocp_meta / ocp_monitor на observer-кластере и регистрирует
его в OCP: obd cluster check4ocp + obd cluster export-to-ocp.

OCP-ВМ (ocp-server-ce) не содержит oceanbase-ce. Пустой список кластеров в UI
после start ocp-server-ce — нормально, пока не выполнен export-to-ocp.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

require_file "${CONFIG_FILE}"
load_inventory

CLUSTER_NAME="${DEPLOY_NAME:-}"
[[ -n "${CLUSTER_NAME}" ]] || CLUSTER_NAME="$(yaml_get deployment.name)"
[[ -n "${CLUSTER_NAME}" && "${CLUSTER_NAME}" != "null" ]] || die "Нет deployment.name / DEPLOY_NAME"

if [[ "$(yaml_get ocp.enabled)" != "true" || "$(yaml_get vm_profiles.ocp.enabled)" != "true" ]]; then
  die "OCP выключен (нужны ocp.enabled и vm_profiles.ocp.enabled)"
fi

[[ "${OCP_COUNT:-0}" -ge 1 ]] || die "Нет OCP_1_IP — выполните ./scripts/deploy.sh provision"
command -v obd >/dev/null 2>&1 || die "obd не в PATH"
obd_cluster_registered "${CLUSTER_NAME}" || die "Кластер OBD ${CLUSTER_NAME} не зарегистрирован"

OCP_PORT="$(yaml_get ocp.port)"
[[ -z "${OCP_PORT}" || "${OCP_PORT}" == "null" ]] && OCP_PORT=8080
OCP_USER="$(yaml_get ocp.admin_username)"
[[ -z "${OCP_USER}" || "${OCP_USER}" == "null" ]] && OCP_USER=admin
OCP_PASSWORD="$(yaml_get ocp.admin_password)"
[[ -n "${OCP_PASSWORD}" && "${OCP_PASSWORD}" != "null" ]] || die "Пустой ocp.admin_password"
OCP_URL="http://${OCP_1_IP}:${OCP_PORT}"
APPNAME="$(yaml_get oceanbase.cluster_name)"
[[ -z "${APPNAME}" || "${APPNAME}" == "null" ]] && APPNAME=obcluster
MYSQL_PORT="$(yaml_get oceanbase.ports.mysql)"
[[ -z "${MYSQL_PORT}" || "${MYSQL_PORT}" == "null" ]] && MYSQL_PORT=2881
ROOT_PASSWORD="$(yaml_get ocp.root_password)"
[[ "${ROOT_PASSWORD}" == "null" ]] && ROOT_PASSWORD=""
META_TENANT="$(yaml_get ocp.meta_tenant.tenant_name)"
[[ -z "${META_TENANT}" || "${META_TENANT}" == "null" ]] && META_TENANT=ocp_meta
MONITOR_TENANT="$(yaml_get ocp.monitor_tenant.tenant_name)"
[[ -z "${MONITOR_TENANT}" || "${MONITOR_TENANT}" == "null" ]] && MONITOR_TENANT=ocp_monitor

info "OCP JVM: ${OCP_URL} (хост ${OCP_1_IP} — ocp-server-ce, не observer)"
info "OBD deploy: ${CLUSTER_NAME}  appname/cluster_name: ${APPNAME}"

sql_client() {
  if command -v mysql >/dev/null 2>&1; then
    echo mysql
  elif command -v obclient >/dev/null 2>&1; then
    echo obclient
  else
    echo ""
  fi
}

run_sql() {
  local sql="$1"
  local client pass
  client="$(sql_client)"
  [[ -n "${client}" && -n "${OBSERVER_1_IP:-}" ]] || return 1
  for pass in "" "${ROOT_PASSWORD}"; do
    if [[ -z "${pass}" ]]; then
      if "${client}" -h"${OBSERVER_1_IP}" -P"${MYSQL_PORT}" -uroot -Nse "${sql}" 2>/dev/null; then
        return 0
      fi
    else
      if MYSQL_PWD="${pass}" "${client}" -h"${OBSERVER_1_IP}" -P"${MYSQL_PORT}" -uroot -Nse "${sql}" 2>/dev/null; then
        return 0
      fi
    fi
  done
  return 1
}

info "Тенанты на observer-кластере (${OBSERVER_1_IP}:${MYSQL_PORT}), не на OCP-ВМ:"
if tenants="$(run_sql "select tenant_name from oceanbase.DBA_OB_TENANTS order by tenant_id")"; then
  printf '%s\n' "${tenants}"
  if ! grep -qx "${META_TENANT}" <<<"${tenants}"; then
    warn "Нет тенанта ${META_TENANT}. OBD создаёт его при полном start oceanbase-ce (ocp_meta_tenant в yaml)."
    warn "Без него OCP не хранит список кластеров в 30-узловом OceanBase."
  fi
  if ! grep -qx "${MONITOR_TENANT}" <<<"${tenants}"; then
    warn "Нет тенанта ${MONITOR_TENANT}."
  fi
else
  warn "SQL к ${OBSERVER_1_IP}:${MYSQL_PORT} не прошёл — проверьте observer и ocp.root_password."
fi

OCP_VERSION="$(yaml_get ocp.version)"
info "obd cluster check4ocp ${CLUSTER_NAME}..."
if [[ -n "${OCP_VERSION}" && "${OCP_VERSION}" != "null" ]]; then
  obd cluster check4ocp "${CLUSTER_NAME}" -V "${OCP_VERSION}"
else
  obd cluster check4ocp "${CLUSTER_NAME}"
fi

info "obd cluster export-to-ocp ${CLUSTER_NAME} → ${OCP_URL} (user ${OCP_USER})"
obd cluster export-to-ocp "${CLUSTER_NAME}" \
  -a "${OCP_URL}" \
  -u "${OCP_USER}" \
  -p "${OCP_PASSWORD}"

cat <<EOF

Задача takeover уходит в OCP (меню «Задачи»). Когда она SUCCEED, в «Кластеры»
должен появиться ${APPNAME} (cluster_id=1), а не отдельный кластер на ${OCP_1_IP}.

Если export-to-ocp недоступен, вручную в UI OCP: Take over cluster
  адрес:     <OBPROXY_1_IP>   порт: 2883   режим: proxy
  имя:       ${APPNAME}
  cluster id: 1
  пароль:    ocp.root_password (root@sys)

EOF
