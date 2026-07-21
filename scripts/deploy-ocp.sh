#!/usr/bin/env bash
# Развёртывание OceanBase Cloud Platform (OCP) на отдельной ВМ в Yandex Cloud.
# OCP устанавливается через OBD (ocp-server-ce) поверх кластера OceanBase.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export PYTHONUNBUFFERED=1

# shellcheck source=scripts/lib/common.sh
source "${ROOT}/scripts/lib/common.sh"

STEP="${1:-all}"

run_cmd() {
  if command -v stdbuf >/dev/null 2>&1; then
    stdbuf -oL -eL "$@"
  else
    "$@"
  fi
}

run_step() {
  local script="$1"
  shift
  info "=== ${script}${*:+ ${*}} ==="
  run_cmd bash "${ROOT}/scripts/${script}" "$@"
}

run_python_step() {
  local script="$1"
  shift
  info "=== ${script}${*:+ ${*}} ==="
  run_cmd python3 "${ROOT}/scripts/${script}" "$@"
}

ocp_is_enabled() {
  [[ "$(yaml_get ocp.enabled)" == "true" && "$(yaml_get vm_profiles.ocp.enabled)" == "true" ]]
}

require_ocp_enabled() {
  require_file "${CONFIG_FILE}"
  if ! ocp_is_enabled; then
    die "OCP отключён. Установите ocp.enabled: true и vm_profiles.ocp.enabled: true в config/deploy.yaml"
  fi
}

case "${STEP}" in
  check)
    require_ocp_enabled
    run_step 00-check-prerequisites.sh
    info "Проверка профиля OCP..."
    python3 "${ROOT}/scripts/lib/vm_profiles.py" resolve ocp --config "${CONFIG_FILE}" >/dev/null
    ;;
  provision)
    require_ocp_enabled
    run_step 00-check-prerequisites.sh
    run_step 01-provision-vms.sh create
    ;;
  prepare)
    require_ocp_enabled
    load_inventory 2>/dev/null || die "Сначала выполните: ./scripts/deploy-ocp.sh provision"
    run_step 02-prepare-ocp.sh
    ;;
  config)
    require_ocp_enabled
    run_python_step 03-generate-obd-config.py
    ;;
  deploy)
    require_ocp_enabled
    run_step 04-deploy-cluster.sh
    ;;
  all)
    require_ocp_enabled
    run_step 00-check-prerequisites.sh
    run_step 01-provision-vms.sh create
    load_inventory
    if [[ "${OCP_COUNT:-0}" -lt 1 ]]; then
      die "OCP ВМ не создана (OCP_COUNT=0)"
    fi
    run_step 02-prepare-servers.sh
    run_step 02-prepare-ocp.sh
    run_python_step 03-generate-obd-config.py
    run_step 04-deploy-cluster.sh
    ;;
  destroy)
    run_cmd bash "${ROOT}/scripts/99-destroy.sh" "${2:-}"
    ;;
  *)
    cat <<'USAGE'
Использование: ./scripts/deploy-ocp.sh [команда]

OceanBase Cloud Platform (OCP) — веб-консоль управления кластером.
Требует ocp.enabled: true и vm_profiles.ocp.enabled: true в config/deploy.yaml.

Команды:
  check      — проверка зависимостей и профиля OCP
  provision  — создание отдельной OCP-ВМ в Yandex Cloud
  prepare    — подготовка OCP-ВМ (диски, Java, clockdiff)
  config     — генерация obd-cluster.yaml с ocp-server-ce
  deploy     — развёртывание через OBD (включая OCP)
  all        — полный цикл OCP (provision → prepare → config → deploy)
  destroy    — удаление ВМ [--destroy-obd]

Пример:
  cp config/deploy.yaml.example config/deploy.yaml
  # включите ocp.enabled и vm_profiles.ocp.enabled
  ./scripts/deploy-ocp.sh all

Интеграция с основным сценарием:
  ./scripts/deploy.sh all   # при включённом OCP в config выполнит полный цикл
USAGE
    exit 1
    ;;
esac

if ocp_is_enabled 2>/dev/null; then
  load_inventory 2>/dev/null || true
  if [[ "${OCP_COUNT:-0}" -gt 0 ]]; then
    ocp_port="$(yaml_get ocp.port)"
  [[ -z "${ocp_port}" || "${ocp_port}" == "null" ]] && ocp_port=8080
    ocp_ip="${OCP_1_IP:-}"
    if [[ -n "${ocp_ip}" ]]; then
      info "OCP console (после deploy): http://${ocp_ip}:${ocp_port}"
      info "OCP admin: $(yaml_get ocp.admin_username) / см. ocp.admin_password в config"
    fi
  fi
fi
