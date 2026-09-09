#!/usr/bin/env bash
# Parse the first server IP from a generated OBD scale-out YAML.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../scripts/lib/common.sh
source "${ROOT}/scripts/lib/common.sh"

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT

cat >"${tmp}/named.yaml" <<'YAML'
obagent:
  servers:
    - name: server4
      ip: 10.130.0.15
YAML
[[ "$(obd_yaml_first_ip "${tmp}/named.yaml")" == "10.130.0.15" ]]

cat >"${tmp}/plain.yaml" <<'YAML'
obagent:
  servers:
    - 10.0.0.4
YAML
[[ "$(obd_yaml_first_ip "${tmp}/plain.yaml")" == "10.0.0.4" ]]

if obd_yaml_first_ip "${tmp}/missing.yaml" >/dev/null 2>&1; then
  echo "FAIL: missing yaml was accepted" >&2
  exit 1
fi

bash -n "${ROOT}/scripts/04-deploy-cluster.sh"
bash -n "${ROOT}/scripts/05-scale-out.sh"
bash -n "${ROOT}/scripts/lib/common.sh"
bash -n "${ROOT}/scripts/lib/recover-common.sh"
echo "OK test-obd-yaml-first-ip"
