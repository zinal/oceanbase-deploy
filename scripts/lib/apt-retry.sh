#!/usr/bin/env bash
# Повтор apt-get при dpkg/apt lock (unattended-upgrades / packagekitd на свежей Ubuntu).
#
# Не проверяем lock заранее: fuser/pgrep/flock дают ложный busy (packagekitd держит fd,
# unattended-upgrade-shutdown совпадает с comm unattended-upgr) и prepare «висит».
# Смотрим вывод apt: lock → пауза и повтор до APT_LOCK_RETRIES.
#
# Только функции: можно source и prepend перед remote `bash -s`.

if declare -F apt_get >/dev/null 2>&1; then
  return 0 2>/dev/null || exit 0
fi

# Повторы после первой lock-ошибки. 60×10с ≈ 10 мин на unattended-upgrades при первом буте.
APT_LOCK_RETRIES="${APT_LOCK_RETRIES:-60}"
APT_GET_RETRY_SLEEP="${APT_GET_RETRY_SLEEP:-10}"

apt_output_is_lock_error() {
  grep -qiE \
    'Could not get lock|Unable to acquire the dpkg frontend lock|Unable to lock the administration directory|Unable to lock directory' \
    "$1"
}

# Обёртка apt-get: при lock-ошибке — пауза и повтор.
apt_get() {
  local retries="${APT_LOCK_RETRIES}"
  local retry_sleep="${APT_GET_RETRY_SLEEP}"
  local tmp rc attempt=0
  tmp="$(mktemp)"

  while true; do
    rc=0
    DEBIAN_FRONTEND="${DEBIAN_FRONTEND:-noninteractive}" \
      "${APT_GET_CMD:-apt-get}" "$@" >"${tmp}" 2>&1 || rc=$?
    if (( rc == 0 )); then
      cat "${tmp}"
      rm -f "${tmp}"
      return 0
    fi
    if ! apt_output_is_lock_error "${tmp}"; then
      cat "${tmp}" >&2
      rm -f "${tmp}"
      return "${rc}"
    fi
    cat "${tmp}" >&2
    if (( attempt >= retries )); then
      echo "ERROR: apt-get: dpkg/apt lock не освободился после ${retries} повтор(ов)" >&2
      rm -f "${tmp}"
      return "${rc}"
    fi
    attempt=$((attempt + 1))
    echo "WARN: apt lock занят (повтор ${attempt}/${retries}, часто unattended-upgrades), пауза ${retry_sleep}с..." >&2
    sleep "${retry_sleep}"
  done
}
