# Серверный снимок OceanBase для точки TPC-C

Сбор **синхронного** snapshot кластера на каждой точке бенчмарка, как требует
Phase 0.4 плана
[oceanbase-efficiency-improvement-plan](https://github.com/zinal/portable-tpcc/blob/main/docs/oceanbase-efficiency-improvement-plan.md):

- `GV$OB_SQL_AUDIT` с группировкой по SQL ID, plan ID, server, error code и event;
- `GV$OB_LOCK_WAIT_STAT` (waiter / holder / rowkey / tx id);
- hit/miss plan cache и смена `plan_id`;
- local / remote / distributed (`plan_type`) и `partition_hit`;
- распределение лидеров tablet и unit'ов;
- CPU, память, сессии, RPC/сеть, memstore/minor freeze, compaction, throttle;
- `SHOW CREATE TABLE` и HASH partitions (63-way binding tablegroup);
- эффективные OLTP timeout тенанта (`ob_query_timeout` / lock / trx), а не bulk
  `database.options.query_timeout` профиля.

Артефакты **не** содержат паролей, connection string, `params_value` и литералов
SQL-параметров. Текст запроса обрезается до `sql_head` (80–120 символов).

## Команды

```bash
# полный снимок в generated/snapshots/<UTC>_<label>/
./scripts/deploy.sh snapshot collect --label w45k06

# только audit и lock waits (во время measurement)
./scripts/deploy.sh snapshot collect --label w45k06-mid --only sql_audit,lock_waits

# без SHOW CREATE TABLE
./scripts/deploy.sh snapshot collect --label after-run --skip-schema

# каталог запросов / один SQL / пакет для mysql вручную
./scripts/deploy.sh snapshot list
./scripts/deploy.sh snapshot print-sql sql-audit-by-id
./scripts/deploy.sh snapshot dump-sql
```

Тот же SQL-пакет лежит в [sql/tpcc-server-snapshot-501.sql](sql/tpcc-server-snapshot-501.sql)
(OceanBase 5.0.x, view `GV$OB_*`). Его можно прогнать `obclient`/`mysql` как
`root` на observer:2881 (блок `scope=sys`) и как `root@<tenant>` (блок tenant).

## Когда снимать

На **каждой** точке матрицы (Phase 0 exit / Phase 1 concurrency):

1. сразу перед measurement (или в конце ramp);
2. в середине measurement, если смотрите lock convoy;
3. сразу после measurement, до `check --after-test`.

Метка `--label` должна совпадать с `run-id` / точкой (`w45k06`, `inflight-64`,
`after-run`). Каталог снимка — рядом с артефактами `mind-tpcc`, не вместо них.

Последовательный probe из плана (`mind-tpcc debug --repeats=30`) снимает
client-side statement cost. Этот snapshot — **server-side** evidence: 1205/6235,
лидеры, unit'ы, audit.

## Как читать

| Файл | Вопрос плана |
|------|----------------|
| `sql-audit-by-id.tsv` | какой SQL/план/observer/event даёт latency и ошибки |
| `sql-audit-errors.tsv` | классы 1205 / 6235 (и −6210 / 4012) |
| `lock-waits.tsv` | кто кого держит и **rowkey** (district / stock / customer) |
| `local-remote-dist*.tsv` | local vs remote New-Order/Payment; `part_miss` |
| `plan-cache-stat.tsv` | hit/miss по observer |
| `plan-changes.tsv` | несколько `plan_id` на один `sql_id` |
| `tablet-leaders*.tsv` | 63 партиции размазаны по intended units? |
| `units.tsv` + `sessions.tsv` | headroom CPU/RAM/`max_session_num` |
| `sysstat.tsv` / `memstore-freeze.tsv` / `compaction.tsv` | RPC, freeze, throttle |
| `create-*.tsv` | schema с нужными `partitions` / без FK |
| `tenant-timeouts.tsv` | реальный OLTP timeout воркера |

Пустой `sql-audit-by-id` — сначала `enable_sql_audit` / `ob_enable_sql_audit`.
Коллектор пишет это в `manifest.json` → `warnings`.

Горячая вершина сессий на одном observer при ровных лидерах — маршрутизация ODP,
не этот снимок: [obproxy-session-routing.md](obproxy-session-routing.md),
`./scripts/deploy.sh obproxy-route diagnose`.

## Выход коллектора

```text
generated/snapshots/20260918T142300Z_w45k06/
  manifest.json      # label, endpoint без пароля, статус каждого запроса
  SUMMARY.txt        # все таблицы подряд
  01-sql-audit-by-id.sql / .tsv / .err
  …
```

Неизвестный view на конкретной сборке не валит весь снимок: пробуются fallback
(имя 4.x `gv$sql_audit`, `__all_virtual_lock_wait_stat`, `CDB_OB_*`).
Обязательный запрос, у которого не сработал ни один SQL, даёт ненулевой код
и `*.err`.

Sys-запросы по умолчанию идут на **observer:2881** (`root@sys`): GV$ полнее, без
pin ODP. Tenant-запросы — `root@<tenant>` через obproxy:2883, если он есть.
`--via obproxy` для sys, если observer недоступен с jump host.

Каждый SQL ограничен `--timeout` (по умолчанию 90 с): коллектор ставит
`SET SESSION ob_query_timeout`, закрывает stdin клиента и убивает процессную
группу, если `obclient`/`mysql` не вышел. Иначе GV$ (например `GV$OB_PARAMETERS`
при недоступном observer) и глобальный `ob_query_timeout=3600s` после ADD SERVER
держат `snapshot` бесконечно, а Ctrl-C оставляет traceback в `subprocess.run`.
Прогресс пишется в stderr (`[n/m] query-id`). Timeout одного запроса — это
`*.err` со строкой `timeout after 90s`, без цепочки fallback на тот же GV$.
`--timeout 180`, если sql_audit на большой нагрузке не укладывается в 90 с.
