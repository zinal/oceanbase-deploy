#!/usr/bin/env bash
# Регрессия: apt_get повторяет при lock-ошибке apt, без предварительной проверки lock.
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

echo "=== нет предварительной проверки lock (иначе prepare висит) ==="
if grep -E '^[^#]*\b(fuser|flock|pgrep|lslocks)\b' "${ROOT}/scripts/lib/apt-retry.sh"; then
  fail "не проверять lock через fuser/flock/pgrep — только вывод apt"
fi
if grep -E '^[^#]*(wait_apt_lock|apt_lock_busy|apt_lock_paths|apt_flock_held)' "${ROOT}/scripts/lib/apt-retry.sh"; then
  fail "не ждать lock заранее — только повтор по ошибке apt"
fi
grep -q 'APT_LOCK_RETRIES' "${ROOT}/scripts/lib/apt-retry.sh" \
  || fail "нужен счётчик максимального числа повторов"

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

echo "=== apt_get повторяет после lock и затем успех ==="
APT_LOCK_RETRIES=5
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

echo "=== apt_get останавливается после APT_LOCK_RETRIES ==="
: >"${tmp}/args"
cat >"${tmp}/fake-apt-get" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_APT_ARGS}"
echo "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1992 (unattended-upgr)" >&2
exit 100
EOF
chmod +x "${tmp}/fake-apt-get"
APT_LOCK_RETRIES=2
APT_GET_RETRY_SLEEP=0
APT_GET_CMD="${tmp}/fake-apt-get"
if out="$(apt_get install -y -qq chrony 2>&1)"; then
  fail "после исчерпания повторов должна быть ошибка"
fi
grep -q 'не освободился после 2 повтор' <<<"${out}" || fail "ожидалось сообщение о лимите повторов: ${out}"
attempts="$(wc -l < "${tmp}/args")"
[[ "${attempts}" -eq 3 ]] || fail "1 попытка + 2 повтора = 3 вызова, got ${attempts}"

echo "=== apt_get не ретраит чужие ошибки ==="
: >"${tmp}/args"
cat >"${tmp}/fake-apt-get" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "${FAKE_APT_ARGS}"
echo "E: Unable to locate package chrony" >&2
exit 100
EOF
chmod +x "${tmp}/fake-apt-get"
if out="$(apt_get install -y -qq chrony 2>&1)"; then
  fail "не lock-ошибка должна пробрасываться"
fi
grep -q 'Unable to locate package chrony' <<<"${out}" || fail "должна быть исходная ошибка apt"
attempts="$(wc -l < "${tmp}/args")"
[[ "${attempts}" -eq 1 ]] || fail "чужая ошибка не должна ретраиться, got ${attempts}"

echo "OK test-apt-retry"
