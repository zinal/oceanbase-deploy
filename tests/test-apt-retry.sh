#!/usr/bin/env bash
# Регрессия: apt_get ждёт dpkg lock и повторяет при unattended-upgrades.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib/apt-retry.sh
source "${ROOT}/scripts/lib/apt-retry.sh"

bash -n "${ROOT}/scripts/lib/apt-retry.sh"
bash -n "${ROOT}/scripts/lib/prepare-chrony.sh"
bash -n "${ROOT}/scripts/02-prepare-servers.sh"
bash -n "${ROOT}/scripts/02-prepare-ocp.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

grep -q 'run_remote_with_apt' "${ROOT}/scripts/02-prepare-servers.sh" \
  || fail "02-prepare-servers.sh должен вызывать run_remote_with_apt"
grep -q 'run_remote_with_apt' "${ROOT}/scripts/02-prepare-ocp.sh" \
  || fail "02-prepare-ocp.sh должен вызывать run_remote_with_apt"
grep -q 'apt_get install' "${ROOT}/scripts/lib/prepare-chrony.sh" \
  || fail "prepare-chrony.sh должен ставить chrony через apt_get"
if grep -E '^[[:space:]]*apt-get (update|install)' "${ROOT}/scripts/lib/prepare-chrony.sh"; then
  fail "prepare-chrony.sh всё ещё вызывает apt-get напрямую"
fi
grep -q 'apt-retry.sh' "${ROOT}/scripts/lib/yc-instance.sh" \
  || fail "cloud-init должен встраивать apt-retry.sh в mount-скрипт"

echo "=== apt_output_is_lock_error распознаёт lock unattended-upgr ==="
cat >"${tmp}/lock.err" <<'EOF'
E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1992 (unattended-upgr)
E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?
EOF
apt_output_is_lock_error "${tmp}/lock.err" || fail "ожидался lock error"

echo "E: Unable to locate package chrony" >"${tmp}/other.err"
if apt_output_is_lock_error "${tmp}/other.err"; then
  fail "обычная ошибка apt не должна считаться lock"
fi

echo "=== wait_apt_lock_until ждёт, пока hook держит lock ==="
printf '0' >"${tmp}/busy-calls"
apt_lock_busy_hook() {
  local n
  n="$(cat "${tmp}/busy-calls")"
  printf '%s' "$((n + 1))" >"${tmp}/busy-calls"
  (( n < 3 ))
}
APT_LOCK_POLL=0
wait_apt_lock_until "$((SECONDS + 5))" 0
unset -f apt_lock_busy_hook
[[ "$(cat "${tmp}/busy-calls")" == "4" ]] || fail "ожидалось 4 проверки lock (3 busy + 1 free), got $(cat "${tmp}/busy-calls")"

echo "=== apt_get повторяет после lock и затем успех ==="
export APT_SKIP_LOCK_WAIT=1
APT_LOCK_TIMEOUT=30
APT_GET_RETRY_SLEEP=0
printf '2' >"${tmp}/fails"
: >"${tmp}/args"
cat >"${tmp}/fake-apt-get" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "${FAKE_APT_ARGS}"
left="$(cat "${FAKE_APT_FAILS}")"
if (( left > 0 )); then
  echo "$((left - 1))" > "${FAKE_APT_FAILS}"
  echo "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1992 (unattended-upgr)" >&2
  echo "E: Unable to acquire the dpkg frontend lock (/var/lib/dpkg/lock-frontend), is another process using it?" >&2
  exit 100
fi
echo "ok $*"
EOF
chmod +x "${tmp}/fake-apt-get"
export FAKE_APT_FAILS="${tmp}/fails"
export FAKE_APT_ARGS="${tmp}/args"
APT_GET_CMD="${tmp}/fake-apt-get"

out="$(apt_get install -y -qq chrony)"
[[ "$(cat "${tmp}/fails")" == "0" ]] || fail "оба lock-сбоя должны быть исчерпаны"
grep -q 'ok install -y -qq chrony' <<<"${out}" || fail "после lock должен быть успешный install: ${out}"
attempts="$(wc -l < "${tmp}/args")"
[[ "${attempts}" -eq 3 ]] || fail "ожидалось 3 вызова apt (2 lock + успех), got ${attempts}"

echo "=== apt_get не ретраит чужие ошибки ==="
cat >"${tmp}/fake-apt-get" <<'EOF'
#!/usr/bin/env bash
echo "E: Unable to locate package chrony" >&2
exit 100
EOF
chmod +x "${tmp}/fake-apt-get"
if out="$(apt_get install -y -qq chrony 2>&1)"; then
  fail "не lock-ошибка должна пробрасываться"
fi
grep -q 'Unable to locate package chrony' <<<"${out}" || fail "должна быть исходная ошибка apt"

echo "OK test-apt-retry"
