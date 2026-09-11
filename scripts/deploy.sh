#!/usr/bin/env bash
# Главный сценарий развёртывания OceanBase в Yandex Cloud.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

export PYTHONUNBUFFERED=1

# shellcheck source=scripts/lib/common.sh
source "${ROOT}/scripts/lib/common.sh"

STEP="${1:-all}"

# Построчный вывод при длинном прогоне `all` (без буферизации до конца шага).
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

# Wrapper clockdiff -o на OCP-ВМ до export-to-ocp (иначе Pre check = ICMP TIMESTAMP exit 1).
run_ocp_clockdiff_if_enabled() {
  if [[ "$(yaml_get ocp.enabled)" != "true" || "$(yaml_get vm_profiles.ocp.enabled)" != "true" ]]; then
    return 0
  fi
  if [[ ! -f "${GENERATED_DIR}/inventory.env" ]]; then
    warn "нет ${GENERATED_DIR}/inventory.env — пропуск ocp-clockdiff"
    return 0
  fi
  load_inventory
  if [[ "${OCP_COUNT:-0}" -lt 1 ]]; then
    warn "нет OCP_1_IP — пропуск ocp-clockdiff"
    return 0
  fi
  info "=== 09-ocp-register.sh --clockdiff-only ==="
  if ! run_cmd bash "${ROOT}/scripts/09-ocp-register.sh" --clockdiff-only; then
    warn "ocp-clockdiff не удался — takeover может упасть на Pre check for create host"
  fi
}

case "${STEP}" in
  check)
    run_step 00-check-prerequisites.sh
    ;;
  provision)
    run_step 00-check-prerequisites.sh
    run_step 01-provision-vms.sh create
    ;;
  prepare)
    run_step 02-prepare-servers.sh
    ;;
  config)
    run_python_step 03-generate-obd-config.py
    ;;
  deploy)
    run_step 02-prepare-servers.sh
    # Перегенерация yaml: иначе start подхватит старый «zone на observer».
    run_python_step 03-generate-obd-config.py
    run_ocp_clockdiff_if_enabled
    run_step 04-deploy-cluster.sh
    # syslog_level кластера (WDIAG → INFO), docs/observer-logging.md
    run_step 13-observer-log.sh apply --skip-if-none --skip-if-ok
    # ODP только что поднят: even-режим на каждом obproxy (нет узлов — пропуск).
    run_step 11-obproxy-route.sh apply --skip-if-none --skip-if-ok
    ;;
  tenant)
    run_step 08-create-tenant.sh
    ;;
  ocp-register)
    run_cmd bash "${ROOT}/scripts/09-ocp-register.sh" "${@:2}"
    ;;
  ocp-clockdiff)
    run_cmd bash "${ROOT}/scripts/09-ocp-register.sh" --clockdiff
    ;;
  obd-mirror)
    run_cmd bash "${ROOT}/scripts/lib/prepare-obd-mirror.sh" --ensure
    ;;
  diagnose)
    run_cmd bash "${ROOT}/scripts/diagnose-obd-start.sh" "${@:2}"
    ;;
  join-observer)
    run_cmd bash "${ROOT}/scripts/join-empty-observer.sh" "${@:2}"
    ;;
  ocp)
    run_cmd bash "${ROOT}/scripts/deploy-ocp.sh" "${2:-all}"
    ;;
  recover-observer)
    run_cmd bash "${ROOT}/scripts/06-recover-observer.sh" "${@:2}"
    ;;
  recover-obproxy)
    run_cmd bash "${ROOT}/scripts/07-recover-obproxy.sh" "${@:2}"
    ;;
  runner-haproxy)
    run_cmd bash "${ROOT}/scripts/10-runner-haproxy.sh" "${@:2}"
    ;;
  obproxy-route)
    run_cmd bash "${ROOT}/scripts/11-obproxy-route.sh" "${@:2}"
    ;;
  obproxy-log)
    run_cmd bash "${ROOT}/scripts/12-obproxy-log.sh" "${@:2}"
    ;;
  observer-log)
    run_cmd bash "${ROOT}/scripts/13-observer-log.sh" "${@:2}"
    ;;
  all)
    run_step 00-check-prerequisites.sh
    run_step 01-provision-vms.sh create
    run_step 02-prepare-servers.sh
    run_python_step 03-generate-obd-config.py
    run_ocp_clockdiff_if_enabled
    run_step 04-deploy-cluster.sh
    run_step 13-observer-log.sh apply --skip-if-none --skip-if-ok
    run_step 11-obproxy-route.sh apply --skip-if-none --skip-if-ok
    # Всегда вызываем шаг: сам скрипт пропускает установку, если runner-ВМ нет.
    run_step 10-runner-haproxy.sh --skip-if-none
    ;;
  destroy)
    run_cmd bash "${ROOT}/scripts/99-destroy.sh" "${2:-}"
    ;;
  *)
    cat <<'USAGE'
Использование: ./scripts/deploy.sh [команда]

Команды:
  check      — проверка зависимостей, профилей ВМ и секции oceanbase vs ВМ
  provision  — создание ВМ в Yandex Cloud
  prepare    — подготовка серверов (диски, sysctl, chrony)
  config     — генерация obd-cluster.yaml
  deploy     — подготовка + ocp-clockdiff (если OCP) + OBD start + export-to-ocp + observer-log/obproxy-route apply
  tenant     — создание user tenant, пользователя и БД (после deploy)
  diagnose   — диагностика зависания obd cluster start (obshell bootstrap)
  obd-mirror — пакет oceanbase-ce из oceanbase.version (remote / All-in-One 5.0.1)
  join-observer — leftover observer / ERROR 4179: wipe одного IP и ADD SERVER
  ocp        — развёртывание OceanBase Cloud Platform (см. deploy-ocp.sh)
  ocp-register — зарегистрировать oceanbase-ce в UI OCP (export-to-ocp)
  ocp-clockdiff — wrapper /usr/sbin/clockdiff + выключить precheck в OCP (Retry takeover)
  recover-observer — observer: --temporary или --replace (docs/node-recovery.md)
  recover-obproxy  — obproxy: --temporary или --replace
  runner-haproxy — HAProxy на runner-ВМ (backend obproxy по именам)
  obproxy-route  — маршрутизация ODP: show|apply|diagnose (docs/obproxy-session-routing.md)
  obproxy-log    — детальность логов ODP: show|apply (docs/obproxy-logging.md)
  observer-log   — детальность логов observer: show|apply (docs/observer-logging.md)
  all        — полный цикл (по умолчанию, включая observer-log/obproxy-route apply и runner-haproxy)
  destroy    — удаление ВМ [--destroy-obd]

Пример:
  cp config/deploy.yaml.example config/deploy.yaml
  # отредактируйте config/deploy.yaml
  ./scripts/deploy.sh all
USAGE
    exit 1
    ;;
esac
