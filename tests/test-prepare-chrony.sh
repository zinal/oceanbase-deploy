#!/usr/bin/env bash
# Синтаксис prepare-chrony и разбор yandex_cloud.ntp_servers.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
bash -n "${ROOT}/scripts/lib/prepare-chrony.sh"
bash -n "${ROOT}/scripts/02-prepare-servers.sh"
bash -n "${ROOT}/scripts/02-prepare-ocp.sh"

# shellcheck source=../scripts/lib/common.sh
source "${ROOT}/scripts/lib/common.sh"
CONFIG_FILE="$(mktemp)"
trap 'rm -f "${CONFIG_FILE}"' EXIT
cat >"${CONFIG_FILE}" <<'YAML'
yandex_cloud:
  ntp_servers:
    - ntp.ix.ru
    - ntp2.vniiftri.ru
YAML
got="$(yaml_get_list yandex_cloud.ntp_servers)"
[[ "${got}" == "ntp.ix.ru ntp2.vniiftri.ru" ]] || {
  echo "FAIL yaml_get_list: got '${got}'" >&2
  exit 1
}
echo "OK test-prepare-chrony"
