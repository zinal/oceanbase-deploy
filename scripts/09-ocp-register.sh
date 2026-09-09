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
его в OCP: obd cluster check4ocp -V <ocp.version|установленная> + export-to-ocp.
Без -V OBD считает OCP 3.1.1 и ошибочно требует OS-пользователя admin.

  --clockdiff-only  только wrapper clockdiff на /usr/sbin и /usr/bin OCP-ВМ
                    (без SQL/API; для deploy до start JVM)
  --clockdiff       wrapper + System Parameters (enable=false / mode=1), затем
                    Retry takeover в UI. Нужен живой OCP :8080.

OCP-ВМ (ocp-server-ce) не содержит oceanbase-ce. Пустой список кластеров в UI
после start ocp-server-ce — нормально, пока не выполнен export-to-ocp.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

CLOCKDIFF_ONLY_CMD=false
CLOCKDIFF_WITH_PARAM=false
if [[ "${1:-}" == "--clockdiff-only" ]]; then
  CLOCKDIFF_ONLY_CMD=true
elif [[ "${1:-}" == "--clockdiff" ]]; then
  CLOCKDIFF_WITH_PARAM=true
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
if [[ "${CLOCKDIFF_ONLY_CMD}" != "true" && "${CLOCKDIFF_WITH_PARAM}" != "true" ]]; then
  command -v obd >/dev/null 2>&1 || die "obd не в PATH"
  obd_cluster_registered "${CLUSTER_NAME}" || die "Кластер OBD ${CLUSTER_NAME} не зарегистрирован"
fi

DEPLOY_USER="$(yaml_get oceanbase.deploy_user)"
[[ -z "${DEPLOY_USER}" || "${DEPLOY_USER}" == "null" ]] && DEPLOY_USER="$(yaml_get yandex_cloud.ssh_user)"
[[ -z "${DEPLOY_USER}" || "${DEPLOY_USER}" == "null" ]] && DEPLOY_USER=obadmin

install_ocp_clockdiff_wrapper() {
  info "clockdiff на OCP-ВМ ${OCP_1_IP}: wrapper в /usr/sbin и /usr/bin (-o + CAP_NET_RAW)"
  if ! run_remote_with_apt "${OCP_1_IP}" \
    "sudo env DEPLOY_USER='${DEPLOY_USER}' CLOCKDIFF_ONLY=true CLOCKDIFF_TEST_IP='${OBSERVER_1_IP:-127.0.0.1}' bash -s" \
    < "${LIB_DIR}/lib/prepare-ocp-host.sh"; then
    die "не удалось установить clockdiff wrapper на ${OCP_1_IP}"
  fi
}

load_ocp_login() {
  OCP_PORT="$(yaml_get ocp.port)"
  [[ -z "${OCP_PORT}" || "${OCP_PORT}" == "null" ]] && OCP_PORT=8080
  OCP_USER="$(yaml_get ocp.admin_username)"
  [[ -z "${OCP_USER}" || "${OCP_USER}" == "null" ]] && OCP_USER=admin
  OCP_PASSWORD="$(yaml_get ocp.admin_password)"
  [[ -n "${OCP_PASSWORD}" && "${OCP_PASSWORD}" != "null" ]] || die "Пустой ocp.admin_password"
  OCP_URL="http://${OCP_1_IP}:${OCP_PORT}"
}

apply_ocp_clockdiff_params() {
  load_ocp_login
  info "OCP System Parameters на ${OCP_URL}: выключить clock-diff precheck (YC режет ICMP TIMESTAMP)"
  if python3 "${LIB_DIR}/lib/ocp_clockdiff.py" apply \
    --url "${OCP_URL}" --user "${OCP_USER}" --password "${OCP_PASSWORD}"; then
    info "OCP clock-diff precheck отключён или mode=1. В UI: Retry той же задачи takeover."
    return 0
  fi
  warn "API не сменила параметры. В UI: Системные параметры → ocp.host.check.clock-diff.enable=false"
  warn "затем Retry «Pre check for create host». Не запускайте второй takeover."
  return 1
}

# До obd cluster start нет observer/OCP API — только OS-wrapper на OCP-ВМ.
if [[ "${CLOCKDIFF_ONLY_CMD}" == "true" ]]; then
  install_ocp_clockdiff_wrapper
  exit 0
fi

if [[ "${CLOCKDIFF_WITH_PARAM}" == "true" ]]; then
  install_ocp_clockdiff_wrapper
  apply_ocp_clockdiff_params || true
  exit 0
fi

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

install_ocp_clockdiff_wrapper
apply_ocp_clockdiff_params || true

OCP_VERSION="$(resolve_ocp_check_version "${CLUSTER_NAME}")"
info "obd cluster check4ocp ${CLUSTER_NAME} -V ${OCP_VERSION}"
info "user.username=${DEPLOY_USER} — OS/SSH, не admin консоли OCP. Для OCP ≥ 4.2.0 это нормально; не меняйте его на admin."

check_log="$(mktemp)"
check_rc=0
set +e
set +o pipefail
obd cluster check4ocp "${CLUSTER_NAME}" -V "${OCP_VERSION}" 2>&1 | tee "${check_log}"
check_rc="${PIPESTATUS[0]}"
set -e
set -o pipefail
if [[ "${check_rc}" -ne 0 ]]; then
  if grep -q "oceanbase-ce Check passed" "${check_log}"; then
    warn "check4ocp вернул ${check_rc}, но oceanbase-ce Check passed — продолжаем export-to-ocp."
  elif grep -q "The current user must be the admin user" "${check_log}"; then
    warn "check4ocp требует user.username=admin только для OCP < 4.2.0 (OBD без -V берёт 3.1.1)."
    warn "Не выполняйте edit-config user.username=admin — сломается SSH (${DEPLOY_USER}). Передано -V ${OCP_VERSION}."
    warn "Продолжаем export-to-ocp."
  else
    cat "${check_log}" >&2 || true
    rm -f "${check_log}"
    die "obd cluster check4ocp ${CLUSTER_NAME} -V ${OCP_VERSION} не прошёл"
  fi
fi
rm -f "${check_log}"

HOST_TYPE="$(yaml_get ocp.host_type)"
[[ -z "${HOST_TYPE}" || "${HOST_TYPE}" == "null" ]] && HOST_TYPE=yandex-cloud
CRED_NAME="$(yaml_get ocp.credential_name)"
[[ -z "${CRED_NAME}" || "${CRED_NAME}" == "null" ]] && CRED_NAME="${DEPLOY_USER}-ssh"

OBD_CLUSTER_DIR="${HOME}/.obd/cluster/${CLUSTER_NAME}"
info "OBD takeOver читает mysql_port из oceanbase-ce.global (не из serverN). Проверка ${OBD_CLUSTER_DIR}/config.yaml..."
python3 "${LIB_DIR}/lib/ocp_takeover.py" patch-config \
  --cluster-dir "${OBD_CLUSTER_DIR}" \
  --mysql-port "${MYSQL_PORT}"

info "obd cluster export-to-ocp ${CLUSTER_NAME} → ${OCP_URL} (user ${OCP_USER}, host_type ${HOST_TYPE})"
info "Сбой oceanbase-ce-utils при export — предупреждение OBD, takeover всё равно идёт."
export_log="$(mktemp)"
export_rc=0
set +e
set +o pipefail
obd cluster export-to-ocp "${CLUSTER_NAME}" \
  -a "${OCP_URL}" \
  -u "${OCP_USER}" \
  -p "${OCP_PASSWORD}" \
  --host_type "${HOST_TYPE}" \
  --credential_name "${CRED_NAME}" 2>&1 | tee "${export_log}"
export_rc="${PIPESTATUS[0]}"
set -e
set -o pipefail
if [[ "${export_rc}" -ne 0 ]]; then
  if python3 "${LIB_DIR}/lib/ocp_takeover.py" log-ok --log-file "${export_log}"; then
    warn "export-to-ocp вернул ${export_rc}, но takeover уже в OCP — считаем успехом (utils RPM / WARN)."
  else
    cat "${export_log}" >&2 || true
    rm -f "${export_log}"
    die "obd cluster export-to-ocp ${CLUSTER_NAME} не прошёл"
  fi
fi
rm -f "${export_log}"

cat <<EOF

Задача takeover уходит в OCP (меню «Задачи»). Когда она SUCCEED, в «Кластеры»
должен появиться ${APPNAME} (cluster_id=1), а не отдельный кластер на ${OCP_1_IP}.

Если задача зависла в Taking over и «Pre check for create host» FAILED
(Execute clock diff failed / diffWithIcmpTimestamp): 
  ./scripts/deploy.sh ocp-clockdiff
(wrapper в /usr/sbin + ocp.host.check.clock-diff.enable=false), затем Retry в UI.
Баннер abnormal Cgroup на Ubuntu 22.04 (cgroup v2) — не этот FAIL.

Если export-to-ocp недоступен, вручную в UI OCP: Take over cluster
  адрес:     <OBPROXY_1_IP>   порт: 2883   режим: proxy
  имя:       ${APPNAME}
  cluster id: 1
  пароль:    ocp.root_password (root@sys)

EOF
