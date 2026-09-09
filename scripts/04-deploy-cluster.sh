#!/usr/bin/env bash
# Развёртывание кластера OceanBase через OBD (oceanbase-skills/cluster-management).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory

OBD_CONFIG="${GENERATED_DIR}/obd-cluster.yaml"
OBD_SEED_CONFIG="${GENERATED_DIR}/obd-seed.yaml"
SCALE_OUT_DIR="${GENERATED_DIR}/staged-scale-out"
SCALE_OUT_MANIFEST="${SCALE_OUT_DIR}/manifest.txt"
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

info "Формирование seed-конфигурации (3 observer, по одному на zone)..."
python3 "${LIB_DIR}/lib/ob_deploy_plan.py" seed \
  --input "${OBD_CONFIG}" \
  --output "${OBD_SEED_CONFIG}"

check_obd_zone_layout() {
  local yaml_path="$1" label="$2"
  [[ -f "${yaml_path}" ]] || return 0
  python3 "${LIB_DIR}/lib/ob_zones.py" check-obd "${yaml_path}" --dump \
    || die "${label}: слишком много zone в ${yaml_path}. Сначала: ./scripts/deploy.sh config, затем obd cluster destroy ${CLUSTER_NAME} -f (если кластер уже зарегистрирован) и повторный deploy."
}

info "Проверка числа OceanBase zone (не больше 7)..."
check_obd_zone_layout "${OBD_CONFIG}" "generated/obd-cluster.yaml"
check_obd_zone_layout "${OBD_SEED_CONFIG}" "generated/obd-seed.yaml"
if [[ -d "${HOME}/.obd/cluster/${CLUSTER_NAME}" ]]; then
  shopt -s nullglob
  registered_yamls=("${HOME}/.obd/cluster/${CLUSTER_NAME}"/*.yaml "${HOME}/.obd/cluster/${CLUSTER_NAME}"/*.yml)
  shopt -u nullglob
  if [[ ${#registered_yamls[@]} -gt 0 ]]; then
    for yaml_path in "${registered_yamls[@]}"; do
      check_obd_zone_layout "${yaml_path}" "зарегистрированный кластер OBD ${CLUSTER_NAME}"
    done
  fi
fi

if [[ "$(yaml_get ocp.enabled)" == "true" && "$(yaml_get vm_profiles.ocp.enabled)" == "true" ]]; then
  python3 - "${LIB_DIR}/lib/vm_profiles.py" "${CONFIG_FILE}" <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("vm_profiles", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
cfg = mod.load_config(Path(sys.argv[2]))
ocp = cfg.get("ocp") or {}
err = mod.ocp_admin_password_error(ocp.get("admin_password"))
if err:
    print(err, file=sys.stderr)
    sys.exit(1)
PY
fi

ob_version="$(yaml_get oceanbase.version)"

if obd_cluster_registered "${CLUSTER_NAME}"; then
  warn "Кластер ${CLUSTER_NAME} уже развёрнут в OBD — пропуск obd cluster deploy"
  warn "Для пересоздания: obd cluster destroy ${CLUSTER_NAME} -f (удалит данные) или obd cluster redeploy ${CLUSTER_NAME}"
else
  info "Развёртывание seed-кластера ${CLUSTER_NAME}: 3 observer (zone1/zone2/zone3)..."
  if [[ -n "${ob_version}" && "${ob_version}" != "null" ]]; then
    run_obd cluster deploy "${CLUSTER_NAME}" -c "${OBD_SEED_CONFIG}" -V "${ob_version}"
  else
    run_obd cluster deploy "${CLUSTER_NAME}" -c "${OBD_SEED_CONFIG}"
  fi
fi

info "Запуск seed-кластера и ожидание завершения OBShell take-over..."
if obd_cluster_registered "${CLUSTER_NAME}" && seed_observers_active; then
  info "Seed observer уже ACTIVE — пропуск obd cluster start (иначе OBD поднимает leftover observer с clog)"
elif ! run_obd cluster start "${CLUSTER_NAME}"; then
  warn "obd cluster start не завершился (ошибка или зависание/Ctrl+C)."
  warn "«oceanbase bootstrap ok» — надпись спиннера, не факт что SQL прошёл."
  warn "Если спиннер остановился на «obshell bootstrap -» — это ожидание take-over агентов;"
  warn "часто это следствие неудачного ALTER SYSTEM BOOTSTRAP (слишком много zone)."
  warn "Диагностика: ./scripts/deploy.sh diagnose"
  warn "Проверьте: mysql -h<OBSERVER_1_IP> -P$(yaml_get oceanbase.ports.mysql) -uroot"
  warn "          (сначала пустой пароль, затем ocp.root_password)"
  warn "          obd cluster display ${CLUSTER_NAME}"
  warn "          obd display-trace   # последний Trace ID из вывода OBD"
  warn "После правки пароля: obd cluster edit-config ${CLUSTER_NAME}, затем снова start."
  if [[ -x "${LIB_DIR}/diagnose-obd-start.sh" ]]; then
    warn "Снимаю локальную диагностику..."
    bash "${LIB_DIR}/diagnose-obd-start.sh" --local || true
  fi
  die "obd cluster start ${CLUSTER_NAME} не завершился успешно"
fi

registered_obd_config() {
  local cluster_dir="${HOME}/.obd/cluster/${CLUSTER_NAME}"
  local candidate
  for candidate in \
    "${cluster_dir}/config.yaml" \
    "${cluster_dir}/config.yml" \
    "${cluster_dir}/inner_config.yaml" \
    "${cluster_dir}/inner_config.yml"; do
    if [[ -f "${candidate}" ]]; then
      printf '%s\n' "${candidate}"
      return 0
    fi
  done
  return 1
}

registered_config="$(registered_obd_config)" \
  || die "OBD не сохранил конфигурацию ${CLUSTER_NAME} после start"

mkdir -p "${SCALE_OUT_DIR}"
JOINED_IPS_ARGS=()
scale_out_joined_ips_args || true
python3 "${LIB_DIR}/lib/ob_deploy_plan.py" scale-out \
  --input "${OBD_CONFIG}" \
  --registered-config "${registered_config}" \
  --output-dir "${SCALE_OUT_DIR}" \
  --manifest "${SCALE_OUT_MANIFEST}" \
  "${JOINED_IPS_ARGS[@]}"

# OBD scale_out for obagent only installs/registers the node. The next
# scale_out then fails status_check if that agent was never started.
info "Запуск уже зарегистрированных obagent, если они ещё не работают..."
start_registered_obagents "${CLUSTER_NAME}" "${registered_config}"

while IFS='|' read -r batch_label observer_yaml obagent_yaml; do
  [[ -n "${batch_label}" ]] || continue
  info "Пакетный scale-out ${batch_label}..."
  if [[ "${observer_yaml}" != "-" ]]; then
    scale_out_observer "${CLUSTER_NAME}" "${observer_yaml}"
  fi
  if [[ "${obagent_yaml}" != "-" ]]; then
    info "Добавление obagent из ${obagent_yaml}"
    run_obd cluster scale_out "${CLUSTER_NAME}" -c "${obagent_yaml}"
    agent_ip="$(obd_yaml_first_ip "${obagent_yaml}")"
    info "Запуск obagent на ${agent_ip}: OBD scale_out его не стартует"
    start_obagent_node "${CLUSTER_NAME}" "${agent_ip}"
  fi
done < "${SCALE_OUT_MANIFEST}"

missing="$(missing_observer_ips "${OBD_CONFIG}" || true)"
if [[ -n "${missing}" ]]; then
  warn "После плана OBD в DBA_OB_SERVERS нет: $(printf '%s' "${missing}" | tr '\n' ' ')"
  while read -r miss_ip; do
    [[ -n "${miss_ip}" ]] || continue
    miss_yaml="${SCALE_OUT_DIR}/missing-${miss_ip}-oceanbase.yaml"
    python3 "${LIB_DIR}/lib/ob_deploy_plan.py" one-node \
      --input "${OBD_CONFIG}" --ip "${miss_ip}" --output "${miss_yaml}"
    scale_out_observer "${CLUSTER_NAME}" "${miss_yaml}"
  done <<< "${missing}"
fi
missing="$(missing_observer_ips "${OBD_CONFIG}" || true)"
if [[ -n "${missing}" ]]; then
  die "Не все observer ACTIVE: $(printf '%s' "${missing}" | tr '\n' ' '). OCP не регистрируем. Повторите ./scripts/deploy.sh deploy или ./scripts/join-empty-observer.sh <ip> --yes"
fi

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
  Хост ${OCP_1_IP} — только ocp-server-ce (JVM), не observer.
  Пустой список кластеров в UI: ./scripts/deploy.sh ocp-register

EOF
  info "clockdiff wrapper на OCP-ВМ (до takeover Pre check for create host)..."
  if ! bash "${LIB_DIR}/09-ocp-register.sh" --clockdiff-only; then
    warn "ocp-clockdiff не удался — takeover может упасть на ICMP TIMESTAMP"
  fi
  info "Регистрация кластера в OCP (export-to-ocp)..."
  if ! bash "${LIB_DIR}/09-ocp-register.sh"; then
    warn "export-to-ocp не удался. Повторите: ./scripts/deploy.sh ocp-register"
    warn "Частые причины: нет mysql_port в oceanbase-ce.global; OCP takeOver без --host_type;"
    warn "или obshell не CLUSTER AGENT. Utils RPM — только WARN, не стоп."
  fi
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
