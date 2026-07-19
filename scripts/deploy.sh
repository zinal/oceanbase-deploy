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
    run_step 04-deploy-cluster.sh
    ;;
  all)
    run_step 00-check-prerequisites.sh
    run_step 01-provision-vms.sh create
    run_step 02-prepare-servers.sh
    run_python_step 03-generate-obd-config.py
    run_step 04-deploy-cluster.sh
    ;;
  destroy)
    run_cmd bash "${ROOT}/scripts/99-destroy.sh" "${2:-}"
    ;;
  *)
    cat <<'USAGE'
Использование: ./scripts/deploy.sh [команда]

Команды:
  check      — проверка зависимостей
  provision  — создание ВМ в Yandex Cloud
  prepare    — подготовка серверов (диски, sysctl)
  config     — генерация obd-cluster.yaml
  deploy     — подготовка серверов + развёртывание через OBD
  all        — полный цикл (по умолчанию)
  destroy    — удаление ВМ [--destroy-obd]

Пример:
  cp config/deploy.yaml.example config/deploy.yaml
  # отредактируйте config/deploy.yaml
  ./scripts/deploy.sh all
USAGE
    exit 1
    ;;
esac
