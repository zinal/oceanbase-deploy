#!/usr/bin/env bash
# Регрессия: пустой UUID= в fstab ломает mount /ob-data (observer-17).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="${ROOT}/scripts/lib/mount-role-disks.sh"

bash -n "${SCRIPT}"
bash -n "${ROOT}/scripts/02-prepare-servers.sh"

# shellcheck source=../scripts/lib/mount-role-disks.sh
source "${SCRIPT}"

# Скрипт sourced — main не должен был трогать систему.
[[ "${FSTAB_FILE}" == "/etc/fstab" ]] || {
  echo "FAIL: после source FSTAB_FILE должен остаться /etc/fstab по умолчанию" >&2
  exit 1
}

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
FSTAB_FILE="${tmp}/fstab"
UUID_WAIT_SECONDS=0
DISK_WAIT_SECONDS=0
MARKER_FILE="${tmp}/marker"

fail() { echo "FAIL: $*" >&2; exit 1; }

echo "=== rewrite_fstab_uuid заменяет отравленную запись UUID= ==="
cat >"${FSTAB_FILE}" <<'EOF'
# comment keep
UUID=aaaaaaaa-1111-2222-3333-444444444444 /boot ext4 defaults 0 1
UUID= /ob-data ext4 defaults,noatime,nodiratime,nodelalloc 0 2
UUID=bbbbbbbb-1111-2222-3333-444444444444 /ob-log ext4 defaults 0 2
EOF
rewrite_fstab_uuid /ob-data "cccccccc-1111-2222-3333-444444444444"
grep -q '^# comment keep$' "${FSTAB_FILE}" || fail "потерян комментарий"
grep -q ' /boot ' "${FSTAB_FILE}" || fail "потерян /boot"
grep -q ' /ob-log ' "${FSTAB_FILE}" || fail "потерян /ob-log"
grep -q '^UUID=cccccccc-1111-2222-3333-444444444444 /ob-data ' "${FSTAB_FILE}" \
  || fail "нет новой UUID-строки для /ob-data"
if grep -qE '^UUID=[[:space:]]+/ob-data ' "${FSTAB_FILE}"; then
  fail "битая UUID= запись осталась"
fi
# ровно одна строка на /ob-data
[[ "$(awk '$2=="/ob-data" {c++} END{print c+0}' "${FSTAB_FILE}")" == "1" ]] \
  || fail "ожидалась ровно одна запись /ob-data"

echo "=== rewrite_fstab_uuid отказывается писать пустой UUID ==="
if rewrite_fstab_uuid /ob-data "" 2>/dev/null; then
  fail "пустой UUID не должен попадать в fstab"
fi

LOOP_DEV=""
loop_mnt="${tmp}/loop-mnt"
cleanup_loop() {
  sudo umount "${loop_mnt}" 2>/dev/null || true
  if [[ -n "${LOOP_DEV}" ]]; then
    sudo losetup -d "${LOOP_DEV}" 2>/dev/null || true
  fi
  rm -rf "${tmp}"
}
trap cleanup_loop EXIT

if sudo -n true 2>/dev/null && command -v losetup >/dev/null && command -v mkfs.ext4 >/dev/null; then
  echo "=== loop: mkfs + UUID в fstab + mount (реальный blkid) ==="
  img="${tmp}/disk.img"
  dd if=/dev/zero of="${img}" bs=1M count=64 status=none
  LOOP_DEV="$(sudo losetup --find --show "${img}")"
  [[ -n "${LOOP_DEV}" ]] || fail "losetup не вернул устройство"
  : > "${FSTAB_FILE}"
  sudo env \
    FSTAB_FILE="${FSTAB_FILE}" \
    UUID_WAIT_SECONDS=15 \
    PATH="/usr/sbin:/usr/bin:/sbin:/bin" \
    bash -c 'source "$1"; mount_device "$2" "$3"' _ "${SCRIPT}" "${LOOP_DEV}" "${loop_mnt}" \
    || fail "mount_device на loop-устройстве"
  loop_uuid="$(sudo blkid -c /dev/null -s UUID -o value "${LOOP_DEV}")"
  [[ -n "${loop_uuid}" ]] || fail "blkid не вернул UUID loop-устройства"
  grep -Fq "UUID=${loop_uuid} ${loop_mnt} " "${FSTAB_FILE}" \
    || fail "fstab без UUID=${loop_uuid}: $(cat "${FSTAB_FILE}")"
  if grep -qE '^UUID=[[:space:]]+' "${FSTAB_FILE}"; then
    fail "в fstab пустой UUID= после реального mkfs"
  fi
  sudo mountpoint -q "${loop_mnt}" || fail "${loop_mnt} не смонтирован"
  sudo umount "${loop_mnt}"
  sudo losetup -d "${LOOP_DEV}"
  LOOP_DEV=""
else
  echo "SKIP loop-тест (нет sudo/losetup/mkfs.ext4)"
fi

echo "=== wait_for_uuid повторяет blkid, пока UUID не появится ==="
mkdir -p "${tmp}/bin"
cat >"${tmp}/bin/blkid" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
state="${BLKID_STATE:?}"
if [[ "$*" == *"-s TYPE"* ]]; then
  if [[ -f "${state}.fstype" ]]; then
    cat "${state}.fstype"
  fi
  exit 0
fi
if [[ "$*" == *"-s UUID"* ]]; then
  n=0
  [[ -f "${state}.n" ]] && n="$(cat "${state}.n")"
  n=$((n + 1))
  echo "${n}" > "${state}.n"
  if (( n >= 3 )); then
    printf 'deadbeef-0000-0000-0000-000000000001'
    exit 0
  fi
  exit 2
fi
exit 2
EOF
chmod +x "${tmp}/bin/blkid"
cat >"${tmp}/bin/udevadm" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${tmp}/bin/udevadm"
export PATH="${tmp}/bin:${PATH}"
export BLKID_STATE="${tmp}/blkid-state"
rm -f "${BLKID_STATE}.n" "${BLKID_STATE}.fstype"
UUID_WAIT_SECONDS=5
got="$(wait_for_uuid /dev/vdb)"
[[ "${got}" == "deadbeef-0000-0000-0000-000000000001" ]] || fail "wait_for_uuid: got '${got}'"
[[ "$(cat "${BLKID_STATE}.n")" == "3" ]] || fail "ожидались 3 попытки blkid"

echo "=== mount_device не пишет UUID= если UUID так и не появился ==="
cat >"${FSTAB_FILE}" <<'EOF'
UUID=keep-me /boot ext4 defaults 0 1
EOF
cat >"${tmp}/bin/blkid" <<'EOF'
#!/usr/bin/env bash
exit 2
EOF
chmod +x "${tmp}/bin/blkid"
cat >"${tmp}/bin/mkfs.ext4" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "${tmp}/bin/mkfs.ext4"
cat >"${tmp}/bin/mountpoint" <<'EOF'
#!/usr/bin/env bash
exit 1
EOF
chmod +x "${tmp}/bin/mountpoint"
cat >"${tmp}/bin/mount" <<'EOF'
#!/usr/bin/env bash
echo "mount: ${*: -1}: special device UUID= does not exist." >&2
exit 1
EOF
chmod +x "${tmp}/bin/mount"
is_block_dev() { return 0; }
UUID_WAIT_SECONDS=0
empty_mnt="${tmp}/mnt-empty"
if mount_device /dev/vdb "${empty_mnt}" 2>/dev/null; then
  fail "mount_device должен упасть без UUID"
fi
if grep -Fq "UUID= ${empty_mnt}" "${FSTAB_FILE}"; then
  fail "mount_device записал пустой UUID= в fstab"
fi
if grep -qE '^UUID=[[:space:]]+' "${FSTAB_FILE}"; then
  fail "в fstab появилась запись с пустым UUID"
fi
grep -q 'UUID=keep-me /boot' "${FSTAB_FILE}" || fail "fstab испорчен при отказе mount_device"

echo "=== mount_device чинит отравленный fstab когда UUID уже есть ==="
repair_mnt="${tmp}/mnt-repair"
cat >"${FSTAB_FILE}" <<EOF
UUID= ${repair_mnt} ext4 defaults,noatime,nodiratime,nodelalloc 0 2
EOF
cat >"${tmp}/bin/blkid" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == *"-s TYPE"* ]]; then
  echo ext4
  exit 0
fi
if [[ "$*" == *"-s UUID"* ]]; then
  printf '11111111-2222-3333-4444-555555555555'
  exit 0
fi
exit 2
EOF
chmod +x "${tmp}/bin/blkid"
cat >"${tmp}/bin/mountpoint" <<'EOF'
#!/usr/bin/env bash
if [[ -f "${MOUNT_FLAG:-/nonexistent}" ]]; then
  exit 0
fi
exit 1
EOF
chmod +x "${tmp}/bin/mountpoint"
cat >"${tmp}/bin/mount" <<'EOF'
#!/usr/bin/env bash
touch "${MOUNT_FLAG}"
exit 0
EOF
chmod +x "${tmp}/bin/mount"
export MOUNT_FLAG="${tmp}/mounted"
rm -f "${MOUNT_FLAG}"
UUID_WAIT_SECONDS=0
mount_device /dev/vdb "${repair_mnt}" || fail "mount_device должен смонтировать при валидном UUID"
grep -Fq "UUID=11111111-2222-3333-4444-555555555555 ${repair_mnt} " "${FSTAB_FILE}" \
  || fail "отравленный fstab не исправлен: $(cat "${FSTAB_FILE}")"
if grep -Fq "UUID= ${repair_mnt} " "${FSTAB_FILE}"; then
  fail "пустой UUID= остался после ремонта"
fi

echo "=== sourcing не запускает main; bash -s (как prepare-servers) для obproxy не трогает диски ==="
# shellcheck source=../scripts/lib/mount-role-disks.sh
source "${SCRIPT}"
ROLE=obproxy DATA_DISK_ENABLED=false LOG_DISK_ENABLED=false \
  MARKER_FILE="${tmp}/missing-marker" \
  FSTAB_FILE="${tmp}/fstab-unused" \
  bash -s < <(cat "${ROOT}/scripts/lib/apt-retry.sh" "${SCRIPT}") \
  || fail "bash -s для obproxy должен завершиться успешно"

echo "OK test-mount-role-disks"
