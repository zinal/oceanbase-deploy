#!/usr/bin/env bash
# Destroy не должен принимать DEPLOY_NAME за имя ВМ; inventory+label — уникальный список.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash -n "${ROOT}/scripts/01-provision-vms.sh"
bash -n "${ROOT}/scripts/lib/yc-instance.sh"
bash -n "${ROOT}/scripts/lib/yc-async.sh"

# shellcheck source=../scripts/lib/yc-instance.sh
source "${ROOT}/scripts/lib/yc-instance.sh"

inv="$(mktemp)"
trap 'rm -f "${inv}"' EXIT
cat >"${inv}" <<'EOF'
OBSERVER_1_NAME=ob-yc-prod-observer-1
OBSERVER_25_NAME=ob-yc-prod-observer-25
OBPROXY_1_NAME=ob-yc-prod-obproxy-1
OCP_1_NAME=ob-yc-prod-ocp-1
DEPLOY_NAME=ob-yc-prod
SSH_USER=demo
OBSERVER_COUNT=30
EOF

names=()
collect_inventory_vm_names names "${inv}"
got="$(printf '%s\n' "${names[@]}" | sort)"
want="$(printf '%s\n' \
  ob-yc-prod-obproxy-1 \
  ob-yc-prod-observer-1 \
  ob-yc-prod-observer-25 \
  ob-yc-prod-ocp-1)"
[[ "${got}" == "${want}" ]] || {
  echo "FAIL inventory names: got '${got}' want '${want}'" >&2
  exit 1
}
printf '%s\n' "${names[@]}" | grep -qx 'ob-yc-prod' && {
  echo "FAIL: DEPLOY_NAME попал в список ВМ" >&2
  exit 1
}
echo "OK: inventory VM names ignore DEPLOY_NAME"

dup=(ob-a ob-b ob-a "" ob-b ob-c)
uniq_names dup
got="$(printf '%s\n' "${dup[@]}" | tr '\n' ' ')"
[[ "${got}" == "ob-a ob-b ob-c " ]] || {
  echo "FAIL uniq_names: got '${got}'" >&2
  exit 1
}
echo "OK: uniq_names"
