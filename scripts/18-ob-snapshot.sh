#!/usr/bin/env bash
# Серверный снимок OceanBase для точки TPC-C (Phase 0.4).
# Явно: ./scripts/deploy.sh snapshot [collect|list|dump-sql|print-sql]
#
# План: https://github.com/zinal/portable-tpcc/blob/main/docs/oceanbase-efficiency-improvement-plan.md
# Документация: docs/tpcc-server-snapshot.md

set -euo pipefail

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${LIB_DIR}/lib/common.sh"

ACTION="${1:-collect}"
shift || true

usage() {
  cat <<'USAGE'
Использование: ./scripts/deploy.sh snapshot <команда>

Команды:
  collect     — снять snapshot с живого кластера в generated/snapshots/
  list        — каталог запросов (sql_audit, lock waits, plan cache, …)
  dump-sql    — выгрузить SQL-пакет (по умолчанию stdout)
  print-sql   — печать одного запроса по id
  self-test   — локальные проверки без кластера

collect:
  --label NAME          метка точки (w45k06, after-run, …)
  --out-dir DIR         каталог снимка
  --tenant NAME         тенант (по умолчанию tenant.tenant_name)
  --database NAME       TPC-C database
  --only id,topic       подмножество запросов (sql_audit, io_throughput, …)
  --skip-schema         без SHOW CREATE TABLE / partitions
  --all-tenants         все USER-тенанты
  --via observer|obproxy  (по умолчанию observer:2881)
  --timeout SEC         таймаут одного SQL (по умолчанию 90). По истечении
                        клиент убивается, fallback не вызывается.
  --audit-window-sec N  окно GV$OB_SQL_AUDIT в секундах (по умолчанию 900).
                        0 = весь буфер (медленно).

Пример:
  ./scripts/deploy.sh snapshot collect --label w45k06
  ./scripts/deploy.sh snapshot collect --label after-run --only sql_audit,lock_waits
  ./scripts/deploy.sh snapshot collect --label w45k06-io --only io_throughput
USAGE
}

case "${ACTION}" in
  -h|--help)
    usage
    exit 0
    ;;
  collect|list|dump-sql|print-sql|self-test) ;;
  *)
    die "Неизвестная команда snapshot '${ACTION}'. Ожидается collect, list, dump-sql, print-sql, self-test"
    ;;
esac

if [[ "${ACTION}" == "collect" ]]; then
  require_file "${CONFIG_FILE}"
  if [[ ! -f "${GENERATED_DIR}/inventory.env" ]]; then
    die "Нет ${GENERATED_DIR}/inventory.env — сначала ./scripts/deploy.sh provision"
  fi
fi

PY=(python3 "${LIB_DIR}/lib/ob_snapshot.py" "${ACTION}")
if [[ "${ACTION}" == "collect" ]]; then
  PY+=(--config "${CONFIG_FILE}" --inventory "${GENERATED_DIR}/inventory.env")
fi
exec "${PY[@]}" "$@"
