#!/usr/bin/env bash
# Проверка и подготовка зеркал OBD под oceanbase.version (новые кластера 5.0.1).
#
# All-in-One отключает remote и оставляет только RPM своей сборки. На хосте
# с 4.6.x без явной версии ставится 4.6.0. Этот скрипт:
#   --check-only  — только отчёт (шаг check)
#   --ensure      — при отсутствии пакета включить remote и obd mirror update
#
# Уже развёрнутые кластера не трогает.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"

OBD_VERSION_PY="${LIB_DIR}/obd_version.py"

MODE="ensure"
if [[ "${1:-}" == "--check-only" ]]; then
  MODE="check"
elif [[ "${1:-}" == "--ensure" || -z "${1:-}" ]]; then
  MODE="ensure"
elif [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'EOF'
Использование: scripts/lib/prepare-obd-mirror.sh [--check-only|--ensure]

Проверяет, что в зеркалах OBD есть oceanbase-ce версии oceanbase.version
(по умолчанию в примере — 5.0.1.0).

  --check-only  только диагностика (не включает remote)
  --ensure      если пакета нет — obd mirror enable remote && obd mirror update

All-in-One после установки держит remote выключенным, поэтому на хосте 4.6.x
без этого шага новые кластера продолжат ставить 4.6.0.
EOF
  exit 0
else
  die "Неизвестный аргумент: $1 (ожидается --check-only или --ensure)"
fi

obd_py() {
  python3 "${OBD_VERSION_PY}" "$@"
}

if [[ ! -f "${CONFIG_FILE}" ]]; then
  warn "Нет ${CONFIG_FILE} — пропуск проверки зеркал OBD"
  exit 0
fi

requested="$(obd_py requested --config "${CONFIG_FILE}")"
enable_remote="$(yaml_get oceanbase.enable_remote_mirror)"
if [[ -z "${enable_remote}" || "${enable_remote}" == "null" ]]; then
  enable_remote="true"
fi

if ! source_obd_env; then
  if [[ -n "${requested}" ]]; then
    warn "OBD не в PATH. Для OceanBase ${requested} установите All-in-One 5.0.1:"
    obd_py hint --version "${requested}" >&2
    if [[ "${MODE}" == "ensure" ]]; then
      die "OBD не установлен"
    fi
  else
    warn "OBD не установлен. Будет предложен на шаге deploy (04-deploy-cluster.sh)"
  fi
  exit 0
fi

info "OBD: $(obd --version 2>/dev/null || obd -V 2>/dev/null || echo unknown)"

list_or_empty() {
  local repo="$1"
  obd mirror list "${repo}" 2>/dev/null || true
}

repo_table="$(obd mirror list 2>/dev/null || true)"
local_table="$(list_or_empty local)"
local_versions="$(printf '%s\n' "${local_table}" | obd_py list-versions --component oceanbase-ce || true)"
if [[ -z "${local_versions}" ]]; then
  local_versions="(нет oceanbase-ce в local)"
fi
info "Local oceanbase-ce: ${local_versions}"

if printf '%s\n' "${repo_table}" | obd_py remote-enabled >/dev/null; then
  info "Удалённые зеркала OBD включены"
else
  info "Удалённые зеркала OBD выключены (так делает All-in-One)"
fi

if [[ -z "${requested}" ]]; then
  info "oceanbase.version не задан — OBD возьмёт latest из доступных зеркал"
  info "На All-in-One 4.6.x это обычно 4.6.0. Для новых кластеров 5.0.1 задайте:"
  info "  oceanbase.version: \"5.0.1.0\""
  info "и выполните: ./scripts/deploy.sh obd-mirror"
  exit 0
fi

info "Целевая версия oceanbase-ce: ${requested}"

package_in() {
  local table="$1"
  printf '%s\n' "${table}" | obd_py has-package --version "${requested}" --component oceanbase-ce
}

found_local=false
if package_in "${local_table}"; then
  found_local=true
fi

found_remote=false
remote_table=""
if printf '%s\n' "${repo_table}" | obd_py remote-enabled >/dev/null; then
  remote_table="$(list_or_empty oceanbase.community.stable)"
  if package_in "${remote_table}"; then
    found_remote=true
  fi
fi

plugin_status=0
plugin_out="$(obd_py plugin-covers --version "${requested}" 2>/dev/null)" || plugin_status=$?
if [[ "${plugin_status}" -eq 1 ]]; then
  warn "В OBD нет плагина oceanbase-ce 5.x — пакет ${requested} OBD может не развернуть."
  warn "Обновите All-in-One до 5.0.1 (идёт OBD 4.5.0) или выполните obd update."
  if [[ "${MODE}" == "ensure" ]]; then
    obd_py hint --version "${requested}" >&2
    die "Плагин OBD не покрывает oceanbase-ce ${requested}"
  fi
elif [[ "${plugin_status}" -eq 0 ]]; then
  info "Плагин OBD покрывает oceanbase-ce ${requested} (${plugin_out})"
fi

if [[ "${found_local}" == "true" || "${found_remote}" == "true" ]]; then
  info "Пакет oceanbase-ce ${requested} есть в зеркалах OBD"
  exit 0
fi

if [[ "${MODE}" == "check" ]]; then
  warn "oceanbase-ce ${requested} нет в зеркалах OBD — новые кластера так не развернуть."
  obd_py hint --version "${requested}" >&2
  warn "Исправление: ./scripts/deploy.sh obd-mirror"
  exit 0
fi

if [[ "${enable_remote}" != "true" ]]; then
  obd_py hint --version "${requested}" >&2
  die "oceanbase-ce ${requested} нет в local, а oceanbase.enable_remote_mirror=false"
fi

info "Пакет ${requested} не найден локально — включаю remote и обновляю метаданные зеркал..."
obd mirror enable remote
obd mirror update
remote_table="$(list_or_empty oceanbase.community.stable)"
local_table="$(list_or_empty local)"

if package_in "${local_table}" || package_in "${remote_table}"; then
  info "Пакет oceanbase-ce ${requested} доступен после обновления зеркал"
  exit 0
fi

obd_py hint --version "${requested}" >&2
die "Не удалось получить oceanbase-ce ${requested} из зеркал OBD"
