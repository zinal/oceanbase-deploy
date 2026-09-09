#!/usr/bin/env bash
# Проверка и подготовка зеркал OBD под oceanbase.version (новые кластера 5.0.1).
#
# All-in-One отключает remote. На хосте с 4.6.x local не содержит 5.0.1, и у
# старого OBD нет плагина 5.x. --ensure:
#   1. obd mirror enable remote && obd mirror update
#   2. если пакета всё ещё нет — скачать RPM (yum, затем GitHub) и clone
#   3. если нет плагина 5.x — obd update, иначе вынуть плагины из ob-deploy RPM
#
# --check-only только отчёт (exit 1, если версии нет). Уже развёрнутые кластера не трогает.

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

  --check-only  диагностика; код 1, если oceanbase-ce нужной версии нет
  --ensure      включить remote, скачать RPM 5.0.1, при необходимости обновить плагины OBD
EOF
  exit 0
else
  die "Неизвестный аргумент: $1 (ожидается --check-only или --ensure)"
fi

obd_py() {
  python3 "${OBD_VERSION_PY}" "$@"
}

rpm_arch() {
  local m
  m="$(uname -m)"
  case "${m}" in
    x86_64|amd64) printf 'x86_64\n' ;;
    aarch64|arm64) printf 'aarch64\n' ;;
    *) printf '%s\n' "${m}" ;;
  esac
}

# All-in-One на Ubuntu кладёт el7 в local; os-release (22.04) для RPM не годится.
rpm_platforms() {
  local fallback
  fallback="$(rpm_arch)"
  printf '%s\n' "${local_table}" | obd_py rpm-platform --arch "${fallback}"
}

download_file() {
  local url="$1" dest="$2"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --retry-delay 2 -o "${dest}" "${url}"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O "${dest}" "${url}"
  else
    return 1
  fi
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
    warn "OBD не в PATH. Для OceanBase ${requested} нужен All-in-One / obd на инсталляционном хосте."
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

refresh_tables() {
  repo_table="$(obd mirror list 2>/dev/null || true)"
  local_table="$(list_or_empty local)"
  remote_table=""
  if printf '%s\n' "${repo_table}" | obd_py remote-enabled >/dev/null 2>&1; then
    remote_table="$(list_or_empty oceanbase.community.stable)"
  fi
}

package_in() {
  local table="$1"
  [[ -n "${table}" ]] || return 1
  printf '%s\n' "${table}" | obd_py has-package --version "${requested}" --component oceanbase-ce >/dev/null 2>&1
}

have_package() {
  package_in "${local_table}" || package_in "${remote_table}"
}

plugin_status=2
refresh_plugin() {
  plugin_status=0
  plugin_out="$(obd_py plugin-covers --version "${requested}" 2>/dev/null)" || plugin_status=$?
}

log_local_versions() {
  local_versions="$(printf '%s\n' "${local_table}" | obd_py list-versions --component oceanbase-ce 2>/dev/null || true)"
  if [[ -z "${local_versions}" ]]; then
    local_versions="(нет oceanbase-ce в local)"
  fi
  info "Local oceanbase-ce: ${local_versions}"
}

enable_remote_and_update() {
  info "Включаю remote-зеркала OBD и обновляю метаданные..."
  obd mirror enable remote
  obd mirror update
  refresh_tables
}

clone_downloaded_rpms() {
  local work="$1"
  shopt -s nullglob
  local rpms=("${work}"/*.rpm)
  shopt -u nullglob
  if [[ ${#rpms[@]} -eq 0 ]]; then
    return 1
  fi
  info "obd mirror clone ${#rpms[@]} RPM из ${work}"
  (cd "${work}" && obd mirror clone ./*.rpm)
}

download_ce_rpms_for() {
  local el="$1" arch="$2"
  local work url dest ok
  work="$(mktemp -d "${TMPDIR:-/tmp}/ob-ce-rpms.XXXXXX")"
  info "Скачиваю oceanbase-ce ${requested} (el${el}/${arch}) с yum/GitHub..."
  ok=0
  while IFS= read -r url; do
    [[ -n "${url}" ]] || continue
    dest="${work}/$(basename "${url}")"
    if [[ -f "${dest}" ]]; then
      continue
    fi
    info "  ${url}"
    if download_file "${url}" "${dest}"; then
      ok=1
    else
      rm -f "${dest}"
      warn "не скачалось: ${url}"
    fi
  done < <(obd_py rpm-urls --version "${requested}" --el "${el}" --arch "${arch}" || true)
  if [[ "${ok}" -eq 0 ]]; then
    rm -rf "${work}"
    return 1
  fi
  if ! clone_downloaded_rpms "${work}"; then
    warn "obd mirror clone не принял RPM el${el}/${arch}"
    rm -rf "${work}"
    return 1
  fi
  rm -rf "${work}"
  refresh_tables
}

download_ce_rpms() {
  local el arch
  while read -r el arch; do
    [[ -n "${el}" && -n "${arch}" ]] || continue
    info "Семейство RPM: el${el}/${arch} (как в local, не с os-release хоста)"
    if download_ce_rpms_for "${el}" "${arch}"; then
      if have_package; then
        return 0
      fi
    fi
  done < <(rpm_platforms)
  return 1
}

extract_plugins_from_rpm() {
  local rpm="$1" dest="$2" tmp src
  tmp="$(mktemp -d "${TMPDIR:-/tmp}/ob-plugins.XXXXXX")"
  if command -v bsdtar >/dev/null 2>&1; then
    bsdtar -C "${tmp}" -xf "${rpm}" 2>/dev/null || true
  elif command -v rpm2cpio >/dev/null 2>&1 && command -v cpio >/dev/null 2>&1; then
    (cd "${tmp}" && rpm2cpio "${rpm}" | cpio -idm --quiet)
  else
    rm -rf "${tmp}"
    return 1
  fi
  src="$(find "${tmp}" -type d -path '*/plugins/oceanbase-ce' -print -quit 2>/dev/null || true)"
  if [[ -z "${src}" ]]; then
    rm -rf "${tmp}"
    return 1
  fi
  mkdir -p "${dest}"
  local copied=0
  local d
  for d in "${src}"/*; do
    [[ -d "${d}" ]] || continue
    if [[ "$(basename "${d}")" == 5* ]]; then
      cp -a "${d}" "${dest}/"
      copied=1
    fi
  done
  rm -rf "${tmp}"
  [[ "${copied}" -eq 1 ]]
}

ensure_plugins() {
  refresh_plugin
  if [[ "${plugin_status}" -eq 0 ]]; then
    info "Плагин OBD покрывает oceanbase-ce ${requested} (${plugin_out})"
    return 0
  fi
  warn "Нет плагина oceanbase-ce 5.x — пробую obd update..."
  if obd update; then
    source_obd_env || true
    refresh_plugin
    if [[ "${plugin_status}" -eq 0 ]]; then
      info "После obd update плагин покрывает ${requested}"
      return 0
    fi
  else
    warn "obd update не удался (нужны права на каталог установки OBD)"
  fi

  local el arch url dest
  while read -r el arch; do
    [[ -n "${el}" && -n "${arch}" ]] || continue
    url="$(obd_py ob-deploy-url --version "${requested}" --el "${el}" --arch "${arch}" || true)"
    [[ -n "${url}" ]] || continue
    dest="$(mktemp "${TMPDIR:-/tmp}/ob-deploy.XXXXXX.rpm")"
    info "Скачиваю ${url} (плагины 5.x)..."
    if ! download_file "${url}" "${dest}"; then
      rm -f "${dest}"
      continue
    fi
    local plug
    plug="$(obd_py plugin-dest)"
    info "Копирую плагины oceanbase-ce 5.x в ${plug}"
    if extract_plugins_from_rpm "${dest}" "${plug}"; then
      rm -f "${dest}"
      refresh_plugin
      if [[ "${plugin_status}" -eq 0 ]]; then
        info "Плагин 5.x установлен в ${plug}"
        return 0
      fi
    fi
    rm -f "${dest}"
  done < <(rpm_platforms)
  return 1
}

refresh_tables
log_local_versions
info "Семейство RPM из local: $(rpm_platforms | paste -sd ', ' - || true)"

if printf '%s\n' "${repo_table}" | obd_py remote-enabled >/dev/null 2>&1; then
  info "Удалённые зеркала OBD включены"
else
  info "Удалённые зеркала OBD выключены (так делает All-in-One)"
fi

if [[ -z "${requested}" ]]; then
  info "oceanbase.version не задан — OBD возьмёт latest из доступных зеркал"
  info "На All-in-One 4.6.x это обычно 4.6.0. Для новых кластеров 5.0.1 задайте oceanbase.version: \"5.0.1.0\""
  exit 0
fi

info "Целевая версия oceanbase-ce: ${requested}"

if have_package && { refresh_plugin; [[ "${plugin_status}" -eq 0 ]]; }; then
  info "Пакет oceanbase-ce ${requested} и плагин OBD уже есть"
  exit 0
fi

if [[ "${MODE}" == "check" ]]; then
  if have_package; then
    refresh_plugin
    if [[ "${plugin_status}" -ne 0 ]]; then
      warn "Пакет ${requested} есть, но нет плагина OBD 5.x. Запустите: ./scripts/deploy.sh obd-mirror"
      exit 1
    fi
    info "Пакет oceanbase-ce ${requested} есть в зеркалах OBD"
    exit 0
  fi
  warn "oceanbase-ce ${requested} нет в зеркалах OBD."
  warn "Исправление: ./scripts/deploy.sh obd-mirror  (скачает RPM и при необходимости плагин 5.x)"
  exit 1
fi

if [[ "${enable_remote}" == "true" ]]; then
  enable_remote_and_update || warn "obd mirror enable/update не удался"
else
  warn "oceanbase.enable_remote_mirror=false — remote не включаю"
fi

if ! have_package; then
  download_ce_rpms || warn "не удалось скачать RPM ${requested}"
fi

if have_package; then
  info "Пакет oceanbase-ce ${requested} доступен в зеркалах OBD"
else
  obd_py hint --version "${requested}" >&2
  die "Не удалось получить oceanbase-ce ${requested}"
fi

if ! ensure_plugins; then
  warn "Пакет ${requested} есть, но плагин 5.x так и не появился."
  warn "OBD может не развернуть 5.0.1. Обновите All-in-One 5.0.1 или OBD ≥ 4.5.0."
  obd_py hint --version "${requested}" >&2
  die "Плагин OBD не покрывает oceanbase-ce ${requested}"
fi
