#!/usr/bin/env bash
# Монтирование secondary-дисков OceanBase на ВМ (cloud-init / prepare-servers).
# Запускать от root. Параметры — из /etc/oceanbase-deploy-role-marker и env.

set -euo pipefail

if ! declare -F apt_get >/dev/null 2>&1; then
  if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
    # shellcheck source=apt-retry.sh
    source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/apt-retry.sh"
  else
    echo "ERROR: apt_get не определён — prepend scripts/lib/apt-retry.sh" >&2
    exit 1
  fi
fi

MARKER_FILE="${MARKER_FILE:-/etc/oceanbase-deploy-role-marker}"
FSTAB_FILE="${FSTAB_FILE:-/etc/fstab}"
DISK_WAIT_SECONDS="${DISK_WAIT_SECONDS:-90}"
UUID_WAIT_SECONDS="${UUID_WAIT_SECONDS:-30}"
FSTAB_OPTS="${FSTAB_OPTS:-defaults,noatime,nodiratime,nodelalloc}"

read_marker() {
  local key="$1" default="${2:-}"
  local line value
  [[ -f "${MARKER_FILE}" ]] || return 0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line//$'\r'/}"
    [[ "${line}" == "${key}="* ]] || continue
    value="${line#${key}=}"
    printf '%s' "${value}"
    return 0
  done < "${MARKER_FILE}"
  printf '%s' "${default}"
}

is_block_dev() {
  [[ -b "$1" ]]
}

device_fs_type() {
  blkid -c /dev/null -s TYPE -o value "$1" 2>/dev/null || true
}

device_uuid() {
  local uuid
  uuid="$(blkid -c /dev/null -s UUID -o value "$1" 2>/dev/null || true)"
  uuid="${uuid//$'\n'/}"
  uuid="${uuid//$'\r'/}"
  uuid="${uuid// /}"
  printf '%s' "${uuid}"
}

udev_settle() {
  udevadm settle --timeout=5 2>/dev/null || true
}

wait_for_uuid() {
  local device="$1"
  local uuid="" elapsed=0
  udev_settle
  while true; do
    uuid="$(device_uuid "${device}")"
    if [[ -n "${uuid}" ]]; then
      printf '%s' "${uuid}"
      return 0
    fi
    (( elapsed < UUID_WAIT_SECONDS )) || break
    sleep 1
    elapsed=$((elapsed + 1))
    udev_settle
  done
  return 1
}

# Удаляет прежние записи точки монтирования (в том числе битые UUID=) и пишет UUID.
rewrite_fstab_uuid() {
  local mount_point="$1" uuid="$2"
  local tmp
  [[ -n "${uuid}" ]] || {
    echo "ERROR: отказ записать пустой UUID для ${mount_point} в ${FSTAB_FILE}" >&2
    return 1
  }
  tmp="$(mktemp)"
  if [[ -f "${FSTAB_FILE}" ]]; then
    awk -v mp="${mount_point}" '
      /^[[:space:]]*#/ { print; next }
      NF >= 2 && $2 == mp { next }
      { print }
    ' "${FSTAB_FILE}" > "${tmp}"
  fi
  printf 'UUID=%s %s ext4 %s 0 2\n' "${uuid}" "${mount_point}" "${FSTAB_OPTS}" >> "${tmp}"
  cat "${tmp}" > "${FSTAB_FILE}"
  rm -f "${tmp}"
}

find_yc_disk() {
  local device_name="$1"
  local candidate resolved
  local -a candidates=()
  local nullglob_was_on=0
  shopt -q nullglob && nullglob_was_on=1
  shopt -s nullglob
  candidates=(
    "/dev/disk/by-id/virtio-${device_name}"
    /dev/disk/by-id/*-"${device_name}"
    /dev/disk/by-path/*-"${device_name}"
  )
  if (( nullglob_was_on == 0 )); then
    shopt -u nullglob
  fi
  for candidate in "${candidates[@]}"; do
    [[ -e "${candidate}" ]] || continue
    resolved="$(readlink -f "${candidate}")"
    is_block_dev "${resolved}" || continue
    printf '%s\n' "${resolved}"
    return 0
  done
  return 1
}

wait_for_yc_disk() {
  local device_name="$1"
  local device="" elapsed=0
  udev_settle
  while true; do
    if device="$(find_yc_disk "${device_name}")"; then
      printf '%s\n' "${device}"
      return 0
    fi
    (( elapsed < DISK_WAIT_SECONDS )) || break
    sleep 2
    elapsed=$((elapsed + 2))
    udev_settle
  done
  return 1
}

is_system_disk() {
  local d="$1" resolved src
  resolved="$(readlink -f "${d}" 2>/dev/null || printf '%s' "${d}")"
  case "${resolved}" in
    /dev/vda|/dev/vda[0-9]*|/dev/sda|/dev/sda[0-9]*|/dev/nvme0n1|/dev/nvme0n1p*)
      return 0
      ;;
  esac
  src="$(findmnt -n -o SOURCE / 2>/dev/null || true)"
  if [[ -n "${src}" ]]; then
    src="$(readlink -f "${src}" 2>/dev/null || printf '%s' "${src}")"
    [[ "${src}" == "${resolved}" ]] && return 0
  fi
  return 1
}

ensure_ext4() {
  local device="$1"
  local fstype
  fstype="$(device_fs_type "${device}")"
  if [[ "${fstype}" == "ext4" ]]; then
    return 0
  fi
  if [[ -n "${fstype}" ]]; then
    echo "ERROR: ${device} содержит ${fstype}, не форматирую в ext4" >&2
    return 1
  fi
  mkfs.ext4 -F -q "${device}" || {
    echo "ERROR: mkfs.ext4 ${device} не удался" >&2
    return 1
  }
  udev_settle
}

mount_device() {
  local device="$1" mount_point="$2"
  local uuid
  is_block_dev "${device}" || return 1

  if mountpoint -q "${mount_point}"; then
    uuid="$(device_uuid "${device}")"
    if [[ -n "${uuid}" ]]; then
      rewrite_fstab_uuid "${mount_point}" "${uuid}" || true
    fi
    return 0
  fi

  ensure_ext4 "${device}" || return 1
  mkdir -p "${mount_point}" || return 1

  uuid="$(wait_for_uuid "${device}")" || {
    echo "ERROR: нет UUID у ${device} после подготовки ФС (не пишем UUID= в fstab)" >&2
    return 1
  }

  rewrite_fstab_uuid "${mount_point}" "${uuid}" || return 1

  # Монтируем по имени устройства: /dev/disk/by-uuid может появиться позже udev.
  if mount "${device}" "${mount_point}" 2>/dev/null; then
    mountpoint -q "${mount_point}" && return 0
  fi
  mount -U "${uuid}" "${mount_point}" 2>/dev/null || \
    mount "${mount_point}" 2>/dev/null || \
    mount -a 2>/dev/null || true
  mountpoint -q "${mount_point}"
}

dump_disk_debug() {
  echo "ERROR: диагностика блочных устройств:" >&2
  lsblk -o NAME,SIZE,TYPE,FSTYPE,UUID,MOUNTPOINT >&2 || true
  echo "ERROR: /dev/disk/by-id:" >&2
  ls -l /dev/disk/by-id/ >&2 || true
  echo "ERROR: ${FSTAB_FILE}:" >&2
  cat "${FSTAB_FILE}" >&2 || true
}

mount_role_disk() {
  local device_name="$1" mount_point="$2"
  local device d mounted=false
  if mountpoint -q "${mount_point}"; then
    return 0
  fi
  if device="$(wait_for_yc_disk "${device_name}")"; then
    echo "INFO: диск ${device_name} → ${device}" >&2
    if mount_device "${device}" "${mount_point}"; then
      mounted=true
    fi
  fi
  if [[ "${mounted}" != "true" ]]; then
    local nullglob_was_on=0
    shopt -q nullglob && nullglob_was_on=1
    shopt -s nullglob
    local -a fallback=("/dev/disk/by-id/virtio-${device_name}" /dev/vd? /dev/sd? /dev/nvme*n*)
    if (( nullglob_was_on == 0 )); then
      shopt -u nullglob
    fi
    for d in "${fallback[@]}"; do
      is_block_dev "${d}" || continue
      is_system_disk "${d}" && continue
      findmnt -rn -S "${d}" >/dev/null 2>&1 && continue
      echo "INFO: fallback-монтирование ${d} → ${mount_point}" >&2
      if mount_device "${d}" "${mount_point}"; then
        mounted=true
        break
      fi
    done
  fi
  if [[ "${mounted}" != "true" ]]; then
    dump_disk_debug
    return 1
  fi
  return 0
}

ensure_deploy_user() {
  [[ -n "${DEPLOY_USER}" ]] || return 0
  id -u "${DEPLOY_USER}" >/dev/null 2>&1 || useradd -m -s /bin/bash "${DEPLOY_USER}"
  usermod -aG sudo "${DEPLOY_USER}" 2>/dev/null || usermod -aG wheel "${DEPLOY_USER}" 2>/dev/null || true
}

prepare_data_paths() {
  local mount_point="$1" target_dir="$2" label="$3"
  mountpoint -q "${mount_point}" || {
    echo "ERROR: ${label} не смонтирован в ${mount_point}" >&2
    return 1
  }
  mkdir -p "${target_dir}"
  chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${mount_point}"
  install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" -m 0755 "${target_dir}"
  sudo -u "${DEPLOY_USER}" test -w "${target_dir}" || {
    echo "ERROR: пользователь ${DEPLOY_USER} не может писать в ${target_dir}" >&2
    return 1
  }
}

ensure_mkfs() {
  command -v mkfs.ext4 >/dev/null 2>&1 || {
    if command -v apt-get >/dev/null 2>&1; then
      apt_get update -qq
      apt_get install -y -qq e2fsprogs
    fi
  }
}

mount_role_disks_main() {
  local need_data="false" need_log="false"

  ensure_mkfs

  # env из prepare-servers имеет приоритет над marker (cloud-init).
  ROLE="${ROLE:-$(read_marker role)}"
  DEPLOY_USER="${DEPLOY_USER:-$(read_marker deploy_user)}"
  DATA_DISK_ENABLED="${DATA_DISK_ENABLED:-$(read_marker data_disk_enabled false)}"
  DATA_MOUNT="${DATA_MOUNT:-$(read_marker data_mount /ob-data)}"
  LOG_DISK_ENABLED="${LOG_DISK_ENABLED:-$(read_marker log_disk_enabled false)}"
  LOG_MOUNT="${LOG_MOUNT:-$(read_marker log_mount /ob-log)}"
  DATA_DIR="${DATA_DIR:-$(read_marker data_dir)}"
  REDO_DIR="${REDO_DIR:-$(read_marker redo_dir)}"

  case "${ROLE}" in
    observer)
      [[ "${DATA_DISK_ENABLED}" == "true" ]] && need_data="true"
      [[ "${LOG_DISK_ENABLED}" == "true" ]] && need_log="true"
      ;;
    monitor|monitoring)
      [[ "${DATA_DISK_ENABLED}" == "true" ]] && need_data="true"
      ;;
    ocp)
      [[ "${DATA_DISK_ENABLED}" == "true" ]] && need_data="true"
      ;;
  esac

  ensure_deploy_user

  if [[ "${need_data}" == "true" ]]; then
    mount_role_disk data "${DATA_MOUNT}" || {
      echo "ERROR: не удалось смонтировать data-диск в ${DATA_MOUNT}" >&2
      exit 1
    }
  fi
  if [[ "${need_log}" == "true" ]]; then
    mount_role_disk log "${LOG_MOUNT}" || {
      echo "ERROR: не удалось смонтировать log-диск в ${LOG_MOUNT}" >&2
      exit 1
    }
  fi

  if [[ "${ROLE}" == "observer" || "${ROLE}" == "monitor" || "${ROLE}" == "monitoring" ]]; then
    [[ "${need_data}" != "true" || -z "${DATA_DIR}" ]] || prepare_data_paths "${DATA_MOUNT}" "${DATA_DIR}" "data-диск"
    [[ "${need_log}" != "true" || -z "${REDO_DIR}" ]] || prepare_data_paths "${LOG_MOUNT}" "${REDO_DIR}" "log-диск"
  fi

  if [[ "${ROLE}" == "ocp" && "${need_data}" == "true" ]]; then
    chown -R "${DEPLOY_USER}:${DEPLOY_USER}" "${DATA_MOUNT}"
    install -d -o "${DEPLOY_USER}" -g "${DEPLOY_USER}" -m 0755 "${DATA_MOUNT}"
  fi
}

if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "${0}" ]]; then
  mount_role_disks_main "$@"
fi
