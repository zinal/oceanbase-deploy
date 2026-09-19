#!/usr/bin/env bash
# Полный или инкрементальный физический бэкап user-тенанта на S3.
# Профиль: секция backup в config/deploy.yaml. Нет host/bucket/ключей — сразу ошибка.
#
#   ./scripts/deploy.sh backup full
#   ./scripts/deploy.sh backup incremental
#   ./scripts/deploy.sh backup show
#   ./scripts/deploy.sh backup validate

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

ACTION="${1:-}"
shift || true

case "${ACTION}" in
  full|incremental|show|validate) ;;
  ""|-h|--help)
    cat <<'USAGE'
Использование: ./scripts/deploy.sh backup <full|incremental|show|validate> [опции]

  full         — ALTER SYSTEM BACKUP TENANT (полный набор)
  incremental  — ALTER SYSTEM BACKUP INCREMENTAL TENANT
  show         — STATUS архива и CDB_OB_BACKUP_JOBS
  validate     — проверить backup.s3 в профиле, без SQL

Опции (после команды): --tenant NAME, --plus-archivelog (только full; incremental его не берёт), --no-wait
Профиль S3: backup.s3.{host,bucket,access_id,access_key} в config/deploy.yaml
или OB_BACKUP_S3_HOST / OB_BACKUP_S3_BUCKET / OB_BACKUP_S3_ACCESS_ID / OB_BACKUP_S3_ACCESS_KEY.
Архив должен быть DOING: ./scripts/deploy.sh archive-log on
USAGE
    if [[ -z "${ACTION}" ]]; then
      exit 1
    fi
    exit 0
    ;;
  *)
    die "Неизвестная команда backup '${ACTION}'. Ожидается full, incremental, show или validate"
    ;;
esac

require_file "${CONFIG_FILE}"

PY=(python3 "${LIB_DIR}/lib/ob_backup.py" --config "${CONFIG_FILE}")
if [[ -f "${GENERATED_DIR}/inventory.env" ]]; then
  PY+=(--inventory "${GENERATED_DIR}/inventory.env")
fi

if [[ "${ACTION}" == "validate" || "${ACTION}" == "show" ]]; then
  exec "${PY[@]}" "${ACTION}" "$@"
fi

exec "${PY[@]}" backup "${ACTION}" "$@"
