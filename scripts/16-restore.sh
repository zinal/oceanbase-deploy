#!/usr/bin/env bash
# Физический restore user-тенанта из S3: создаёт НОВЫЙ standby, не перезаписывает исходный.
# Профиль: backup.s3 + backup.restore.pool_list в config/deploy.yaml.
# Нет host/bucket/ключей или pool_list — сразу ошибка, без SQL (кроме activate).
#
#   ./scripts/deploy.sh restore
#   ./scripts/deploy.sh restore run --dest-tenant tpcc_restore --pool restore_pool
#   ./scripts/deploy.sh restore activate --dest-tenant tpcc
#   ./scripts/deploy.sh restore show
#   ./scripts/deploy.sh restore validate

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

ACTION="${1:-run}"
shift || true

case "${ACTION}" in
  run|show|validate|activate) ;;
  -h|--help)
    cat <<'USAGE'
Использование: ./scripts/deploy.sh restore [run|show|validate|activate] [опции]

  run       — ALTER SYSTEM RESTORE в новый standby (по умолчанию)
  activate  — ACTIVATE STANDBY уже восстановленного тенанта (без повторного RESTORE)
  show      — CDB_OB_RESTORE_PROGRESS / HISTORY и роль dest-тенанта
  validate  — проверить backup.s3 и pool_list, напечатать SQL, без кластера

RESTORE не перезаписывает живой тенант: нужен свободный dest и пустой resource pool.
activate не требует S3 и pool: dest должен быть STANDBY после успешного restore.

Опции:
  --tenant NAME          исходный тенант (префиксы S3)
  --dest-tenant NAME     новый standby (по умолчанию {tenant}_restore)
  --pool NAME            backup.restore.pool_list (обязательно для run)
  --locality ...         опционально
  --primary-zone ...     опционально
  --concurrency N        опционально
  --method full|quick    по умолчанию full
  --until-time 'YYYY-MM-DD HH:MM:SS'   PITR
  --until-scn N          PITR по SCN (не вместе с --until-time)
  --activate             в том же run после успеха: ACTIVATE STANDBY TENANT
  --no-wait              не ждать RESTORE_SUCCESS

Профиль S3: backup.s3.{host,bucket,access_id,access_key} или OB_BACKUP_S3_*.
USAGE
    exit 0
    ;;
  -*)
    set -- "${ACTION}" "$@"
    ACTION=run
    ;;
  *)
    die "Неизвестная команда restore '${ACTION}'. Ожидается run, show, validate или activate"
    ;;
esac

require_file "${CONFIG_FILE}"

PY=(python3 "${LIB_DIR}/lib/ob_backup.py" --config "${CONFIG_FILE}")
if [[ -f "${GENERATED_DIR}/inventory.env" ]]; then
  PY+=(--inventory "${GENERATED_DIR}/inventory.env")
fi

exec "${PY[@]}" restore "${ACTION}" "$@"
