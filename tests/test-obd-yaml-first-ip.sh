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

cat >"${tmp}/registered.yaml" <<'YAML'
oceanbase-ce:
  servers:
    - name: server1
      ip: 10.0.0.1
obagent:
  servers:
    - name: server4
      ip: 10.130.0.15
    - 10.0.0.4
YAML
got="$(obd_yaml_component_ips "${tmp}/registered.yaml" obagent | tr '\n' ' ')"
[[ "${got}" == "10.130.0.15 10.0.0.4 " ]] || {
  echo "FAIL component ips: got '${got}'" >&2
  exit 1
}
[[ -z "$(obd_yaml_component_ips "${tmp}/named.yaml" oceanbase-ce)" ]]

CONFIG_FILE="${tmp}/deploy.yaml"
cat >"${CONFIG_FILE}" <<'YAML'
oceanbase:
  deploy_user: obadmin
  home_path: /home/obadmin/observer
  data_dir: /ob-data/1
  redo_dir: /ob-log/1
YAML
[[ "$(obagent_home_path)" == "/home/obadmin/obagent" ]]
[[ "$(observer_home_path)" == "/home/obadmin/observer" ]]

OBSERVER_1_IP=10.0.0.1
OBSERVER_2_IP=10.0.0.2
OBSERVER_3_IP=10.0.0.3
observer_is_seed_ip 10.0.0.2
if observer_is_seed_ip 10.130.0.8; then
  echo "FAIL: scale-out IP treated as seed" >&2
  exit 1
fi
if (reset_observer_for_scale_out 10.0.0.1); then
  echo "FAIL: seed observer wipe was allowed" >&2
  exit 1
fi

bash -n "${ROOT}/scripts/04-deploy-cluster.sh"
bash -n "${ROOT}/scripts/05-scale-out.sh"
bash -n "${ROOT}/scripts/09-ocp-register.sh"
bash -n "${ROOT}/scripts/lib/common.sh"
bash -n "${ROOT}/scripts/lib/recover-common.sh"
echo "OK test-obd-yaml-first-ip"
