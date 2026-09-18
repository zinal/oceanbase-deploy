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
- **I/O throughput транзакций**: задержки записи clog, write throttle из‑за
  медленного dump/compaction, лаг архива clog (Binding Optional vs Mandatory);
- `SHOW CREATE TABLE` и HASH partitions (63-way binding tablegroup);
- эффективные OLTP timeout тенанта (`ob_query_timeout` / lock / trx), а не bulk
  `database.options.query_timeout` профиля.

Артефакты **не** содержат паролей, connection string, `params_value` и литералов
SQL-параметров. Текст запроса обрезается до `sql_head` (80–120 символов).

Запросы к `GV$OB_SQL_AUDIT` по умолчанию смотрят **последние 15 минут**
(`request_time > time_to_usec(now()) - 900s`) и отбрасывают `is_executor_rpc`
(дубли RPC на remote/dist). Иначе GROUP BY сканирует весь буфер на всех
observer — десятки секунд при маленьком `.tsv`. Весь буфер: `--audit-window-sec 0`.

## Команды

```bash
# полный снимок в generated/snapshots/<UTC>_<label>/
./scripts/deploy.sh snapshot collect --label w45k06

# только audit и lock waits (во время measurement)
./scripts/deploy.sh snapshot collect --label w45k06-mid --only sql_audit,lock_waits

# clog write / compaction throttle / archive lag
./scripts/deploy.sh snapshot collect --label w45k06-io --only io_throughput --skip-schema

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
| `sysstat.tsv` / `memstore-freeze.tsv` | RPC, freeze, throttle, clog/palf write |
| `sql-audit-io-waits.tsv` / `system-events.tsv` | wait: clog commit vs memstore throttle vs archive |
| `log-disk.tsv` / `log-stat.tsv` / `io-params.tsv` | заполнение clog-диска, unreclaimable LSN, пороги |
| `compaction*.tsv` | major/mini/minor progress и diagnose |
| `archive-log.tsv` / `archive-ls.tsv` / `archive-dest.tsv` | лаг архива и BINDING (без LOCATION) |
| `create-*.tsv` | schema с нужными `partitions` / без FK |
| `tenant-timeouts.tsv` | реальный OLTP timeout воркера |

Пустой `sql-audit-by-id` — сначала `enable_sql_audit` / `ob_enable_sql_audit`.
Коллектор пишет это в `manifest.json` → `warnings`.

Горячая вершина сессий на одном observer при ровных лидерах — маршрутизация ODP,
не этот снимок: [obproxy-session-routing.md](obproxy-session-routing.md),
`./scripts/deploy.sh obproxy-route diagnose`.

## Диагностика I/O-ограничений транзакций

Открытые материалы OceanBase 4.x/5.0 (wait events, `GV$SYSSTAT`, troubleshooting
clog/compaction/archive, `SET LOG_ARCHIVE_DEST`) позволяют **различить** три
причины, когда TPS упирается в пропускную способность ввода-вывода. Снимок
темы `io_throughput` как раз для этого. Одно число «диск загружен» недостаточно:
clog, data-диск compaction и архив на S3 — разные очереди и разные wait.

Интерпретация всегда **связка**: wait в SQL (`sql-audit-io-waits` за окно audit)
плюс накопительные `system-events` / `sysstat` плюс состояние хранилища
(`log-disk`, `log-stat`, `memstore-freeze`, `compaction*`, `archive-*`).

### 1. Запись тормозит из‑за задержек записи в лог (clog / PALF)

Транзакция на commit ждёт majority-записи clog. Это не «медленный UPDATE строки»,
а ожидание redo.

**Признак в wait (имена из `ob_wait_event.h`):**

| EVENT в `GV$OB_SQL_AUDIT` / `GV$SYSTEM_EVENT` | Смысл |
|---|---|
| `wait end trans`, `tx commiting wait`, `sync tx commiting wait` | commit ждёт подтверждения лога (класс COMMIT) |
| `palf write` | IO записи PALF на log-диск |
| `palf throttling sleep` | PALF **намеренно** усыпляет запись: clog-диск выше `log_disk_throttling_percentage` |
| `clog writer condition wait` | очередь писателя clog |

`sql-audit-by-id` дополнительно даёт `avg_user_io_us` / `avg_event_wait_us`.
Если `execute_time` растёт вместе с этими wait, а не с `queue_time` и не с
lock 1205/6235 — это IO лога, не CPU и не row lock.

**Подтверждение по таблицам:**

1. `sysstat.tsv`: `palf write io count to disk`, `palf write size to disk`,
   `clog write count` / `clog write time` (если есть на сборке).
   Средняя задержка ≈ `clog write time / clog write count`.
   `io write delay` — это **data-диск**, не clog; его рост без palf/commit wait
   указывает на compaction/dump, не на redo.
2. `log-disk.tsv` (`GV$OB_UNITS`): `log_used_pct`.
   ≥ `log_disk_utilization_threshold` (обычно 80%) — recycle не успевает;
   ≥ `log_disk_utilization_limit_threshold` (обычно 95%) — **отказ записи** clog.
3. `log-stat.tsv`: `unreclaimable_mb = (END_LSN − BASE_LSN) / 1MiB` — лог ещё
   нельзя recycle (данные не в SSTable / checkpoint не сдвинут).
   `uncommitted_mb = (MAX_LSN − END_LSN) / 1MiB` на **LEADER** — локальная запись
   или majority не догоняет (медленный log-диск или follower).
   `IN_SYNC=NO` на follower — репликация лога, не «медленный SQL».
4. `io-params.tsv`: `log_disk_throttling_percentage` < 100 включает PALF
   throttle (с 4.2). Факт throttle в момент снимка — wait `palf throttling sleep`
   и/или `[LOG DISK THROTTLING]` в `observer.log` (obdiag check).

Типичная связка «compaction не успевает → clog не recycle → log disk полон →
palf throttling / отказ записи» читается как **следствие** пункта 2, не как
отдельная поломка PALF.

### 2. Транзакции ждут, потому что compaction/dump основных данных не успевает

Запись идёт в MemStore. Freeze + mini/minor merge (dump в SSTable) освобождают
память. Если dump медленнее входного TPS, срабатывает write throttle, затем
остановка записи. Major compaction сам по себе не держит commit redo, но
конкурирует за data-диск и задерживает recycle clog.

**Признак в wait:**

| EVENT | Смысл |
|---|---|
| `sleep: storage writing throttle sleep` | лимит записи MemStore (не clog) |
| `memstore memory page alloc wait` | нет страниц MemStore, ждут freeze/dump |
| `db file compact write` / `db file compact read` | IO compaction на data-диске |

**Подтверждение:**

1. `memstore-freeze.tsv`: `active_pct` / `used_pct` относительно `mem_limit`.
   Рост `freeze_cnt` при `used_pct` около `freeze_trigger_pct` — freeze идёт,
   но frozen MemTable не освобождается (dump медленный или держат ref).
   `used_pct` ≥ `writing_throttling_trigger_percentage` из `io-params`
   (дефолт 60 с 4.0) — ожидаемый write throttle.
2. `compaction-progress.tsv`: `TYPE` `MINI_MERGE` / `MINOR_MERGE` со
   `STATUS` не `FINISH` и большим `unfinished_tablet_count` / `unfinished_g`
   на фоне высокого memstore — dump не успевает.
   Долгий `MAJOR_MERGE` / `CDB_OB_MAJOR_COMPACTION.STATUS` не FINISH +
   `is_error`/`is_suspended` — major застрял (смотреть diagnose).
3. `compaction-diagnose.tsv`: `FAILED`, `NOT_SCHEDULE`, `RS_UNCOMPACTED`.
   Тексты вроде `memtable can not minor merge`, `medium wait for freeze`
   — dump/freeze не доводят tablet до compaction SCN.
4. `sysstat.tsv`: `io write delay` / `io write bytes` растут вместе с compact
   wait; `major freeze trigger`.

Отличить от пункта 1: здесь доминируют throttle/memstore/compact wait, log disk
ещё не у 95%, `palf write` не главный EVENT. Если оба набора wait видны сразу —
сначала dump (п.2), clog-диск полный уже как следствие.

### 3. Транзакции ждут архива журнала

Архив пишет **лидер лог-стрима** на внешний dest. Это **не всегда** блокирует
OLTP: режим задаёт `BINDING` в `LOG_ARCHIVE_DEST` (`archive-dest.tsv`, без
LOCATION — в пути бывают ключи S3).

| BINDING | Если архив отстаёт от генерации clog |
|---|---|
| **Optional** (дефолт) | запись тенанта **не** останавливают; clog могут recycle **до** архива → `STATUS=INTERRUPTED`, дыра PITR |
| **Mandatory** | архив важнее бизнеса: dest/сеть не успевают → **запись в тенант может остановиться** |

**Признак, что архив именно тормозит транзакции (не просто «лаг для PITR»):**

1. `archive-dest.tsv`: `binding = Mandatory` (или `MANDATORY`).
2. `archive-log.tsv`: `STATUS=DOING`, но `lag_sec` стабильно больше
   `archive_lag_target` из `io-params` (дефолт 120 с; на S3 минимум 60 с).
   Либо `STATUS=INTERRUPTED` / `BEGINNING` / `SUSPEND`.
   `comment_head` вроде recycled before archived (−9087) — dest медленнее clog.
3. `archive-ls.tsv`: `unarchived_mb = END_LSN лидера − MAX_LSN архива` на
   самом медленном LS. Tenant `checkpoint_scn` = минимум по стримам.
4. Wait: `object storage write`, `archive sender cond wait` (и соседние
   archive/*). На Optional эти wait есть у **архивных** потоков; SQL тенанта
   ими не обязан наполняться. На Mandatory они коррелируют с ростом
   `wait end trans` / отказом записи.

Пустые `archive-*.tsv` — архив не включён, этот сценарий снимается.

Не путать с пунктом 1: медленный S3 при Optional **не** должен поднимать
`palf throttling sleep`. Если Optional + INTERRUPTED + высокий TPS — это
потеря архива, не stall транзакций. Если Mandatory + растущий `unarchived_mb`
+ commit wait — да, архив душит запись.

Источники: troubleshooting clog (`GV$OB_UNITS` / `GV$OB_LOG_STAT`), SYSSTAT
class CLOG/STORAGE, wait events PALF/COMMIT/throttle, `GV$OB_COMPACTION_*`,
`CDB_OB_ARCHIVELOG` / `CDB_OB_LS_LOG_ARCHIVE_PROGRESS`,
`SET LOG_ARCHIVE_DEST` BINDING, `archive_lag_target`.

## Выход коллектора

```text
generated/snapshots/20260918T142300Z_w45k06/
  manifest.json      # label, endpoint без пароля, статус каждого запроса
  SUMMARY.txt        # все таблицы подряд
  01-sql-audit-by-id.sql / .tsv / .err
  …
```

Неизвестный view на конкретной сборке не валит весь снимок: пробуются fallback
(`gv$sql_audit`, `__all_virtual_lock_wait_stat` без `table_id`, `GV$OB_LOCKS`,
`DBA_OB_TABLEGROUP_TABLES`, `CON_ID` в `GV$SYSSTAT`).
Обязательный запрос, у которого не сработал ни один SQL, даёт ненулевой код
и `*.err`.

На OceanBase 5.0.x нет публичного `GV$OB_LOCK_WAIT_STAT` и колонки `table_id`
у `__all_virtual_lock_wait_stat`; `GV$OB_MEMORY` без `limit` (есть `mod_name`);
`GV$SYSSTAT` фильтруется по `CON_ID`, не `tenant_id`; `DBA_OB_TABLEGROUPS`
без `tablegroup_id` (см. `DBA_OB_TABLEGROUP_TABLES` / `SHOW TABLEGROUPS`).

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
