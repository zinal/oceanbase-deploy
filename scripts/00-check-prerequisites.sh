#!/usr/bin/env bash
# Проверка зависимостей и соответствия профилей ВМ рекомендациям OceanBase.

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/common.sh"

require_file "${CONFIG_FILE}" 2>/dev/null || die "Создайте config/deploy.yaml на основе config/deploy.yaml.example"

require_cmd yc
require_cmd python3
require_cmd ssh

python3 -c "import yaml" 2>/dev/null || die "Установите PyYAML: pip install pyyaml"

if ! yc config list >/dev/null 2>&1; then
  die "Yandex Cloud CLI не настроен. Выполните: yc init"
fi

ssh_key="$(expand_path "$(yaml_get yandex_cloud.ssh_public_key_file)")"
ssh_priv="$(expand_path "$(yaml_get ssh.private_key_file)")"
require_file "$ssh_key"
require_file "$ssh_priv"

info "Проверка профилей ВМ (OceanBase recommendations)..."
python3 "${LIB_DIR}/lib/vm_profiles.py" validate --config "${CONFIG_FILE}"

if command -v obd >/dev/null 2>&1; then
  info "OBD установлен: $(obd --version 2>/dev/null || obd -V 2>/dev/null || echo 'unknown')"
else
  warn "OBD не установлен. Будет предложена установка на шаге 04-deploy-cluster.sh"
fi

info "Проверка зависимостей успешно завершена"
