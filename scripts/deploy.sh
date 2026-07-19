#!/usr/bin/env bash
# Главный сценарий развёртывания OceanBase в Yandex Cloud.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

STEP="${1:-all}"

run_step() {
  local script="$1"
  info "=== ${script} ==="
  bash "${ROOT}/scripts/${script}"
}

# shellcheck source=scripts/lib/common.sh
source "${ROOT}/scripts/lib/common.sh"

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
    python3 "${ROOT}/scripts/03-generate-obd-config.py"
    ;;
  deploy)
    run_step 04-deploy-cluster.sh
    ;;
  all)
    run_step 00-check-prerequisites.sh
    run_step 01-provision-vms.sh create
    run_step 02-prepare-servers.sh
    python3 "${ROOT}/scripts/03-generate-obd-config.py"
    run_step 04-deploy-cluster.sh
    ;;
  destroy)
    bash "${ROOT}/scripts/99-destroy.sh" "${2:-}"
    ;;
  *)
    cat <<'USAGE'
Использование: ./scripts/deploy.sh [команда]

Команды:
  check      — проверка зависимостей
  provision  — создание ВМ в Yandex Cloud
  prepare    — подготовка серверов (диски, sysctl)
  config     — генерация obd-cluster.yaml
  deploy     — развёртывание через OBD
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
