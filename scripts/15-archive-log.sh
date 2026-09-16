#!/usr/bin/env bash
# Включение / выключение архива clog user-тенанта (ARCHIVELOG / NOARCHIVELOG).
# Включение требует полный профиль backup.s3; выключение — только имя тенанта.
#
#   ./scripts/deploy.sh archive-log on
#   ./scripts/deploy.sh archive-log off
#   ./scripts/deploy.sh archive-log show

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

ACTION="${1:-}"
shift || true

case "${ACTION}" in
  on|off|show|validate) ;;
  ""|-h|--help)
    cat <<'USAGE'
Использование: ./scripts/deploy.sh archive-log <on|off|show|validate> [опции]

  on        — SET LOG_ARCHIVE_DEST + ALTER SYSTEM ARCHIVELOG, ждать STATUS=DOING
  off       — ALTER SYSTEM NOARCHIVELOG (S3 не нужен)
  show      — STATUS архива и backup jobs
  validate  — проверить backup.s3 в профиле, без SQL

Опции: --tenant NAME, --no-wait
Для on обязательны backup.s3.{host,bucket,access_id,access_key} в config/deploy.yaml
или OB_BACKUP_S3_HOST / OB_BACKUP_S3_BUCKET / OB_BACKUP_S3_ACCESS_ID / OB_BACKUP_S3_ACCESS_KEY.
USAGE
    if [[ -z "${ACTION}" ]]; then
      exit 1
    fi
    exit 0
    ;;
  *)
    die "Неизвестная команда archive-log '${ACTION}'. Ожидается on, off, show или validate"
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

exec "${PY[@]}" archive "${ACTION}" "$@"
