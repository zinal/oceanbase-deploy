#!/usr/bin/env bash
# Регрессия: test -w принимает один путь (prepare OCP падал с extra argument).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

bash -n "${ROOT}/scripts/lib/prepare-ocp-host.sh"

if grep -E 'test -w "\$\{OCP_HOME\}" "\$\{OCP_SOFT_DIR\}"' "${ROOT}/scripts/lib/prepare-ocp-host.sh"; then
  echo "FAIL: test -w всё ещё вызывается с несколькими путями" >&2
  exit 1
fi

grep -q "setcap cap_net_raw,cap_sys_nice+ep" "${ROOT}/scripts/lib/prepare-ocp-host.sh"
grep -q "CLOCKDIFF_ONLY" "${ROOT}/scripts/lib/prepare-ocp-host.sh"
grep -q "/usr/bin/clockdiff" "${ROOT}/scripts/lib/prepare-ocp-host.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
mkdir -p "${tmp}/home" "${tmp}/software" "${tmp}/logs"

# POSIX test -w с тремя путями обязан завершиться ошибкой (как на OCP-хосте).
if test -w "${tmp}/home" "${tmp}/software" "${tmp}/logs" 2>/dev/null; then
  echo "FAIL: ожидался отказ test -w с несколькими путями" >&2
  exit 1
fi

for dir in "${tmp}/home" "${tmp}/software" "${tmp}/logs"; do
  test -w "${dir}"
done

echo "OK test-prepare-ocp-host"
