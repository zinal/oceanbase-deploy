#!/usr/bin/env bash
# Ожидание dpkg/apt lock и повтор apt-get.
# На свежей Ubuntu в YC lock часто держит unattended-upgr — prepare без этого падает.
#
# Только функции: можно source и prepend перед remote `bash -s`.

if declare -F apt_get >/dev/null 2>&1; then
  return 0 2>/dev/null || exit 0
fi

# Общий дедлайн на ожидание + ретраи (unattended-upgrades на первом буте — минуты).
APT_LOCK_TIMEOUT="${APT_LOCK_TIMEOUT:-600}"
APT_LOCK_POLL="${APT_LOCK_POLL:-5}"
APT_GET_RETRY_SLEEP="${APT_GET_RETRY_SLEEP:-10}"

apt_lock_paths() {
  printf '%s\n' \
    /var/lib/dpkg/lock-frontend \
    /var/lib/dpkg/lock \
    /var/lib/apt/lists/lock \
    /var/cache/apt/archives/lock
}

# Тесты подменяют через apt_lock_busy_hook или APT_SKIP_LOCK_WAIT=1.
apt_lock_busy() {
  if [[ "${APT_SKIP_LOCK_WAIT:-}" == "1" ]]; then
    return 1
  fi
  if declare -F apt_lock_busy_hook >/dev/null 2>&1; then
    if apt_lock_busy_hook; then
      return 0
    fi
    return 1
  fi

  local path
  while IFS= read -r path; do
    [[ -e "${path}" ]] || continue
    if command -v fuser >/dev/null 2>&1; then
      fuser "${path}" >/dev/null 2>&1 && return 0
    fi
  done < <(apt_lock_paths)

  if command -v pgrep >/dev/null 2>&1; then
    pgrep -x unattended-upgr >/dev/null 2>&1 && return 0
    pgrep -x apt-get >/dev/null 2>&1 && return 0
    pgrep -x apt >/dev/null 2>&1 && return 0
    pgrep -x dpkg >/dev/null 2>&1 && return 0
    pgrep -f '/usr/bin/unattended-upgrade' >/dev/null 2>&1 && return 0
  fi
  return 1
}

wait_apt_lock_until() {
  local deadline="$1"
  local poll="${2:-${APT_LOCK_POLL}}"
  local started="${SECONDS}"
  local last_log=-30

  while apt_lock_busy; do
    if (( SECONDS >= deadline )); then
      echo "WARN: dpkg/apt lock не освободился за $((SECONDS - started))с — пробуем apt-get" >&2
      return 0
    fi
    if (( last_log < 0 || SECONDS - last_log >= 30 )); then
      echo "Ожидание освобождения dpkg/apt lock ($((SECONDS - started))с, часто unattended-upgrades)..." >&2
      last_log="${SECONDS}"
    fi
    sleep "${poll}"
  done
}

apt_output_is_lock_error() {
  grep -qiE \
    'Could not get lock|Unable to acquire the dpkg frontend lock|Unable to lock the administration directory|Unable to lock directory' \
    "$1"
}

# Обёртка apt-get: ждём lock, при занятости — повтор до APT_LOCK_TIMEOUT.
apt_get() {
  local timeout="${APT_LOCK_TIMEOUT}"
  local poll="${APT_LOCK_POLL}"
  local retry_sleep="${APT_GET_RETRY_SLEEP}"
  local deadline=$((SECONDS + timeout))
  local tmp rc attempt=0
  tmp="$(mktemp)"

  while true; do
    wait_apt_lock_until "${deadline}" "${poll}"
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
    if (( SECONDS >= deadline )); then
      echo "ERROR: apt-get: dpkg/apt lock не освободился за ${timeout}с" >&2
      rm -f "${tmp}"
      return "${rc}"
    fi
    attempt=$((attempt + 1))
    echo "WARN: apt lock занят (попытка ${attempt}, часто unattended-upgrades), повтор через ${retry_sleep}с..." >&2
    sleep "${retry_sleep}"
  done
}
