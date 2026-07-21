#!/usr/bin/env bash
# Монтирование secondary-дисков OceanBase на ВМ (cloud-init / prepare-servers).
# Запускать от root. Параметры — из /etc/oceanbase-deploy-role-marker и env.

set -euo pipefail

MARKER_FILE="${MARKER_FILE:-/etc/oceanbase-deploy-role-marker}"

command -v mkfs.ext4 >/dev/null 2>&1 || {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y -qq e2fsprogs
  fi
}

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

ROLE="${ROLE:-$(read_marker role)}"
DEPLOY_USER="${DEPLOY_USER:-$(read_marker deploy_user)}"
DATA_DISK_ENABLED="${DATA_DISK_ENABLED:-$(read_marker data_disk_enabled false)}"
DATA_MOUNT="${DATA_MOUNT:-$(read_marker data_mount /ob-data)}"
LOG_DISK_ENABLED="${LOG_DISK_ENABLED:-$(read_marker log_disk_enabled false)}"
LOG_MOUNT="${LOG_MOUNT:-$(read_marker log_mount /ob-log)}"
DATA_DIR="${DATA_DIR:-$(read_marker data_dir)}"
REDO_DIR="${REDO_DIR:-$(read_marker redo_dir)}"

find_yc_disk() {
  local device_name="$1"
  local candidate resolved
  for candidate in \
    "/dev/disk/by-id/virtio-${device_name}" \
    /dev/disk/by-id/*-"${device_name}" \
    /dev/disk/by-path/*-"${device_name}"; do
    [[ -e "${candidate}" ]] || continue
    resolved="$(readlink -f "${candidate}")"
    [[ -b "${resolved}" ]] || continue
    printf '%s\n' "${resolved}"
    return 0
  done
  return 1
}

mount_device() {
  local device="$1" mount_point="$2"
  [[ -b "${device}" ]] || return 1
  if mountpoint -q "${mount_point}"; then
    return 0
  fi
  if ! blkid "${device}" >/dev/null 2>&1; then
    mkfs.ext4 -F "${device}" >/dev/null 2>&1
  fi
  mkdir -p "${mount_point}"
  if ! grep -q "[[:space:]]${mount_point}[[:space:]]" /etc/fstab; then
    local uuid
    uuid="$(blkid -s UUID -o value "${device}")"
    echo "UUID=${uuid} ${mount_point} ext4 defaults,noatime,nodiratime,nodelalloc 0 2" >> /etc/fstab
  fi
  mount "${mount_point}" 2>/dev/null || mount -a
  mountpoint -q "${mount_point}"
}

mount_role_disk() {
  local device_name="$1" mount_point="$2"
  local device mounted=false
  if mountpoint -q "${mount_point}"; then
    return 0
  fi
  if device="$(find_yc_disk "${device_name}")"; then
    mount_device "${device}" "${mount_point}" && mounted=true
  fi
  if [[ "${mounted}" != "true" ]]; then
    local d
    for d in "/dev/disk/by-id/virtio-${device_name}" /dev/vd? /dev/sd? /dev/nvme*n*; do
      [[ -b "${d}" ]] || continue
      [[ "${d}" == /dev/vda || "${d}" == /dev/sda ]] && continue
      findmnt -rn -S "${d}" >/dev/null 2>&1 && continue
      if mount_device "${d}" "${mount_point}"; then
        mounted=true
        break
      fi
    done
  fi
  [[ "${mounted}" == "true" ]]
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

need_data="false"
need_log="false"
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
