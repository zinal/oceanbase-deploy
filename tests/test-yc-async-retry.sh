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

# leftover yc-op.log с прошлого rate limit не должен валить фазу «создание дисков»
cat > "${YC_OP_LOG}" <<'EOF'
ERROR: rpc error: code = ResourceExhausted desc = The limit on maximum number of active operations has exceeded.


client-request-id: 0947991f-0148-426f-bea9-3a902978e392
EOF
yc_assert_last_op_ok "создание дисков"
echo "OK: stale rate-limit log ignored by yc_assert_last_op_ok"

echo "ERROR: quota exceeded for compute.instances.count" > "${YC_OP_LOG}"
if ( yc_assert_last_op_ok "создание дисков" ); then
  echo "FAIL: настоящий ERROR должен валить фазу"
  exit 1
fi
echo "OK: non-rate-limit ERROR still fails the phase"

# --- destroy: NotFound = успех, rate limit = retry ---
cat > "${YC_OP_LOG}" <<'EOF'
ERROR: rpc error: code = NotFound desc = Instance not found
EOF
yc_op_is_gone "${YC_OP_LOG}" || { echo "FAIL: NotFound должен считаться gone"; exit 1; }

cat > "${YC_OP_LOG}" <<'EOF'
ERROR: rpc error: code = ResourceExhausted desc = The limit on maximum number of active operations has exceeded.
EOF
if yc_op_is_gone "${YC_OP_LOG}"; then
  echo "FAIL: ResourceExhausted не должен считаться gone"
  exit 1
fi
echo "OK: yc_op_is_gone отличает NotFound от rate limit"

echo 1 > "${FAILS_LEFT}"
fake_yc_delete_gone() {
  echo "ERROR: rpc error: code = NotFound desc = Instance 'ob-yc-prod-observer-25' not found" >&2
  return 1
}
yc_async_retry "удаление тестовой ВМ" --allow-gone fake_yc_delete_gone
echo "OK: yc_async_retry --allow-gone принимает NotFound"

echo 2 > "${FAILS_LEFT}"
fake_yc_delete_retry() {
  local left
  left="$(cat "${FAILS_LEFT}")"
  if (( left > 0 )); then
    echo "$((left - 1))" > "${FAILS_LEFT}"
    echo "ERROR: rpc error: code = ResourceExhausted desc = The limit on maximum number of active operations has exceeded" >&2
    return 1
  fi
  echo "id: fake-delete-operation"
  return 0
}
yc_async_retry "удаление тестовой ВМ" --allow-gone fake_yc_delete_retry
left="$(cat "${FAILS_LEFT}")"
[[ "${left}" == "0" ]] || { echo "FAIL: delete retry, осталось fails=${left}"; exit 1; }
grep -q "fake-delete-operation" "${YC_OP_LOG}" || { echo "FAIL: нет успешного delete"; exit 1; }
echo "OK: yc_async_retry --allow-gone пережил rate limit на delete"

if ( yc_async_retry "удаление тестовой ВМ" fake_yc_delete_gone ); then
  echo "FAIL: NotFound без --allow-gone должен валить команду"
  exit 1
fi
echo "OK: NotFound без --allow-gone остаётся ошибкой"
