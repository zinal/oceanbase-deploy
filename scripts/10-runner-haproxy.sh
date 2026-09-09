#!/usr/bin/env bash
# Установка HAProxy на runner-ВМ: backend — имена obproxy (не IP).
# Образец: bench/tpcc/haproxy.cfg

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}"
load_inventory
ensure_generated_dir

if [[ "${RUNNER_COUNT:-0}" -lt 1 ]]; then
  die "Нет runner-ВМ в inventory (RUNNER_COUNT=0). Включите vm_profiles.runner.enabled и выполните provision"
fi
if [[ "${OBPROXY_COUNT:-0}" -lt 1 ]]; then
  die "Нет obproxy в inventory — HAProxy некуда балансировать"
fi

CFG_OUT="${GENERATED_DIR}/haproxy-runner.cfg"
python3 "${LIB_DIR}/lib/runner_haproxy.py" generate \
  --inventory "${GENERATED_DIR}/inventory.env" \
  --config "${CONFIG_FILE}" \
  --output "${CFG_OUT}"

proxy_port="$(yaml_get oceanbase.ports.obproxy)"
[[ -n "${proxy_port}" && "${proxy_port}" != "null" ]] || proxy_port=2883

info "Сгенерирован ${CFG_OUT} (backend obproxy по именам, порт ${proxy_port})"

HAPROXY_LOG_DIR="${GENERATED_DIR}/runner-haproxy-logs"
mkdir -p "${HAPROXY_LOG_DIR}"

declare -a HAP_PIDS=()
declare -a HAP_LABELS=()
declare -a HAP_LOGS=()

install_haproxy_on_host() {
  local host="$1"
  info "HAProxy на ${host}..."
  if ! run_remote "${host}" "sudo env DEBIAN_FRONTEND=noninteractive bash -s" <<'REMOTE'
set -euo pipefail
if ! command -v haproxy >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq haproxy
fi
install -d -m 0755 /etc/haproxy
REMOTE
  then
    die "Не удалось установить haproxy на ${host}"
  fi

  if ! run_remote "${host}" "sudo tee /etc/haproxy/haproxy.cfg >/dev/null" < "${CFG_OUT}"
  then
    die "Не удалось записать /etc/haproxy/haproxy.cfg на ${host}"
  fi

  if ! run_remote "${host}" "sudo bash -s" <<'REMOTE'
set -euo pipefail
haproxy -c -f /etc/haproxy/haproxy.cfg
systemctl enable haproxy >/dev/null 2>&1
if systemctl is-active --quiet haproxy 2>/dev/null; then
  systemctl reload haproxy
else
  systemctl restart haproxy
fi
systemctl is-active --quiet haproxy
REMOTE
  then
    die "haproxy не прошёл проверку/запуск на ${host}"
  fi
  info "HAProxy готов: ${host} (127.0.0.1:${proxy_port})"
}

for i in $(seq 1 "${RUNNER_COUNT}"); do
  host="$(inventory_host RUNNER "${i}")"
  logfile="${HAPROXY_LOG_DIR}/runner-${i}.log"
  : > "${logfile}"
  info "Старт HAProxy: ${host} → ${logfile}"
  (
    install_haproxy_on_host "${host}"
  ) >>"${logfile}" 2>&1 &
  HAP_PIDS+=($!)
  HAP_LABELS+=("${host}")
  HAP_LOGS+=("${logfile}")
done

failed=0
failed_logs=()
for i in "${!HAP_PIDS[@]}"; do
  status=0
  wait "${HAP_PIDS[$i]}" || status=$?
  if (( status == 0 )); then
    info "Готово: ${HAP_LABELS[$i]}"
  else
    warn "Ошибка: ${HAP_LABELS[$i]} (код ${status}) — см. ${HAP_LOGS[$i]}"
    failed_logs+=("${HAP_LOGS[$i]}")
    failed=1
  fi
done

if (( failed != 0 )); then
  for logfile in "${failed_logs[@]}"; do
    warn "----- tail ${logfile} -----"
    tail -n 40 "${logfile}" >&2 || true
  done
  die "HAProxy не установлен на ${#failed_logs[@]} runner-хост(ах). Логи: ${HAPROXY_LOG_DIR}"
fi

info "HAProxy установлен на ${RUNNER_COUNT} runner-ВМ. Клиенты: mysql -h 127.0.0.1 -P${proxy_port}"
info "Конфиг: ${CFG_OUT}"
