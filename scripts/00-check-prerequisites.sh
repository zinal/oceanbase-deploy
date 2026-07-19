#!/usr/bin/env bash
# Проверка зависимостей и соответствия профилей ВМ рекомендациям OceanBase.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

require_file "${CONFIG_FILE}" 2>/dev/null || die "Создайте config/deploy.yaml на основе config/deploy.yaml.example"

require_cmd yc
require_cmd python3
require_cmd ssh

python3 -c "import yaml" 2>/dev/null || die "Установите PyYAML: pip install pyyaml"

if ! yc config list >/dev/null 2>&1; then
  die "Yandex Cloud CLI не настроен. Выполните: yc init"
fi

ssh_key="$(ssh_public_key_path)"
ssh_priv="$(ssh_private_key_path)"
require_file "$ssh_key"
require_file "$ssh_priv"
validate_ssh_key_pair

info "Проверка профилей ВМ (OceanBase recommendations)..."
python3 "${LIB_DIR}/lib/vm_profiles.py" validate --config "${CONFIG_FILE}"

image_spec="$(python3 "${LIB_DIR}/lib/vm_profiles.py" image-spec --config "${CONFIG_FILE}")"
info "Образ ОС: ${image_spec}"
if command -v yc >/dev/null 2>&1; then
  folder_id="$(yaml_get yandex_cloud.image_folder_id)"
  [[ -z "${folder_id}" || "${folder_id}" == "null" ]] && folder_id="standard-images"
  image_name="$(yaml_get yandex_cloud.image_name)"
  image_family="$(yaml_get yandex_cloud.image_family)"
  if [[ -n "${image_name}" && "${image_name}" != "null" ]]; then
    found="$(yc compute image list --folder-id "${folder_id}" --format json 2>/dev/null \
      | python3 -c "import json,sys; n=sys.argv[1]; print(sum(1 for i in json.load(sys.stdin) if i.get('name')==n))" "${image_name}" 2>/dev/null || echo 0)"
    if [[ "${found}" == "0" ]]; then
      warn "Образ ${image_name} не найден в folder ${folder_id}. Проверьте: yc compute image list --folder-id ${folder_id}"
    fi
  elif [[ -n "${image_family}" && "${image_family}" != "null" ]]; then
    found="$(yc compute image list --folder-id "${folder_id}" --format json 2>/dev/null \
      | python3 -c "import json,sys; fam=sys.argv[1]; print(sum(1 for i in json.load(sys.stdin) if i.get('family')==fam or i.get('family_id')==fam))" "${image_family}" 2>/dev/null || echo 0)"
    if [[ "${found}" == "0" ]]; then
      warn "Семейство образов ${image_family} не найдено в folder ${folder_id}. Проверьте: yc compute image list --folder-id ${folder_id} --format json | jq '.[] | select(.family==\"${image_family}\")'"
    fi
  fi
fi

if command -v obd >/dev/null 2>&1; then
  info "OBD установлен: $(obd --version 2>/dev/null || obd -V 2>/dev/null || echo 'unknown')"
else
  warn "OBD не установлен. Будет предложена установка на шаге 04-deploy-cluster.sh"
fi

info "Проверка зависимостей успешно завершена"
