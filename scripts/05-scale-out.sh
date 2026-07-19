#!/usr/bin/env bash
# Горизонтальное масштабирование: добавление observer-узлов (OBD scale_out).

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"
# shellcheck source=yc-instance.sh
source "${LIB_DIR}/lib/yc-instance.sh"

require_file "${CONFIG_FILE}"
load_inventory

ADD_COUNT="${1:-1}"
if ! [[ "${ADD_COUNT}" =~ ^[0-9]+$ ]] || [[ "${ADD_COUNT}" -lt 1 ]]; then
  die "Использование: $0 <количество_новых_observer>"
fi

command -v obd >/dev/null 2>&1 || die "OBD не установлен"

current="${OBSERVER_COUNT}"
deploy_name="${DEPLOY_NAME}"
new_ips=()

for (( n=1; n<=ADD_COUNT; n++ )); do
  idx=$((current + n))
  name="${deploy_name}-observer-${idx}"
    create_instance "${name}" "observer"
  ip="$(get_instance_ip "${name}")"
  [[ -n "${ip}" ]] || die "Не удалось получить IP для ${name}"

  echo "OBSERVER_${idx}_NAME=${name}" >> "${GENERATED_DIR}/inventory.env"
  echo "OBSERVER_${idx}_IP=${ip}" >> "${GENERATED_DIR}/inventory.env"
  new_ips+=("${ip}")
  wait_for_ssh "${ip}"
done

new_total=$((current + ADD_COUNT))
sed -i "s/^OBSERVER_COUNT=.*/OBSERVER_COUNT=${new_total}/" "${GENERATED_DIR}/inventory.env"

bash "${LIB_DIR}/02-prepare-servers.sh" "${new_ips[@]}"

python3 "${LIB_DIR}/03-generate-obd-config.py" --output "${GENERATED_DIR}/scale-out.yaml"

info "Масштабирование через OBD scale_out..."
obd cluster scale_out "${deploy_name}" -c "${GENERATED_DIR}/scale-out.yaml"
obd cluster display "${deploy_name}"

info "Добавлено ${ADD_COUNT} observer-узлов. Всего: ${new_total}"
