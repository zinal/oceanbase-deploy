#!/usr/bin/env bash
# Регрессия: retry при rate limit не должен падать из-за ((attempt++)) и set -e.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib/yc-async.sh
source "${ROOT}/scripts/lib/yc-async.sh"

GENERATED_DIR="$(mktemp -d)"
trap 'rm -rf "${GENERATED_DIR}"' EXIT
YC_OP_LOG="${GENERATED_DIR}/yc-op.log"
YC_RATE_LIMIT_SLEEP=0
YC_MAX_INFLIGHT=0
YC_ASYNC_MAX_RETRIES=5

FAILS_LEFT="${GENERATED_DIR}/fails"
echo 2 > "${FAILS_LEFT}"

fake_yc_create() {
  local left
  left="$(cat "${FAILS_LEFT}")"
  if (( left > 0 )); then
    echo "$((left - 1))" > "${FAILS_LEFT}"
    echo "ERROR: rpc error: code = ResourceExhausted desc = The limit on maximum number of active operations has exceeded" >&2
    return 1
  fi
  echo "id: fake-operation"
  return 0
}

yc_async_retry "создание тестовой ВМ" fake_yc_create

left="$(cat "${FAILS_LEFT}")"
[[ "${left}" == "0" ]] || { echo "FAIL: ожидались 2 rate-limit и успех, осталось fails=${left}"; exit 1; }
[[ -f "${YC_OP_LOG}" ]] || { echo "FAIL: нет ${YC_OP_LOG}"; exit 1; }
grep -q "fake-operation" "${YC_OP_LOG}" || { echo "FAIL: в логе нет успешного ответа"; exit 1; }

echo "OK: yc_async_retry пережил rate limit при set -e"
