# Логи OBServer: как убрать WDIAG с горячего пути

У `observer` те же семь уровней, что у ODP, и тот же дефолт **`syslog_level=WDIAG`**: в `observer.log` / `election.log` / `rootservice.log` пишутся ожидаемые диагностические ошибки. На нагруженном узле это гигабайты в сутки и конкуренция syslog с **clog** за IO, если логи живут на том же диске, что redo (у нас syslog — boot/`home_path`, clog — отдельный log-диск, но boot всё равно конечный).

Официально: [System Log](https://oceanbase.github.io/oceanbase/logging/), [факторы производительности](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_03_test_oceanbase_database/influence_factor). Все перечисленные параметры — **кластерные**, правятся в sys-тенанте, рестарт не нужен.

OBProxy — отдельно: [obproxy-logging.md](obproxy-logging.md).

## Что писать в первую очередь

На каждом observer:

```bash
du -sh ~/observer/log/*
ls -lhS ~/observer/log | head
```

| Файл | Откуда растёт | Главный рычаг |
|---|---|---|
| `observer.log` | системный лог процесса | **`syslog_level`** (дефолт `WDIAG`) |
| `observer.log.wf` | дубль WARN+ | `enable_syslog_wf` |
| `election.log` / `rootservice.log` | выборы / RS | тот же `syslog_level` |
| `*.wf` остальных | дубль WARN+ | `enable_syslog_wf` |
| trace | полная трасса SQL | `enable_record_trace_log` |

Если раздут **`observer.log`** — это `WDIAG`. `INFO` возвращает поведение «состояние процесса», `WARN` — только неожиданное.

## Уровни

От подробного к короткому: `DEBUG` → `TRACE` → `WDIAG` → `EDIAG` → `INFO` → `WARN` → `ERROR`.

Пишется всё **не ниже** выбранного уровня. `ALTER SYSTEM SET` действует сразу на **все** observer кластера (в отличие от `ALTER PROXYCONFIG`, который только на один ODP).

## Что выставлять в продакшене

Минимальный безопасный шаг:

```sql
ALTER SYSTEM SET syslog_level = 'INFO';
ALTER SYSTEM SET enable_syslog_wf = false;
ALTER SYSTEM SET enable_async_syslog = true;
ALTER SYSTEM SET enable_syslog_recycle = true;
ALTER SYSTEM SET max_syslog_file_count = 20;
ALTER SYSTEM SET syslog_io_bandwidth_limit = '10M';
```

`enable_async_syslog` и `syslog_io_bandwidth_limit` не уменьшают объём, но ограничивают влияние записи на латентность. Recycle + `max_syslog_file_count` нужны: иначе архивы не чистятся (`max_syslog_file_count=0` по умолчанию). Каждый файл до ~256 МБ; 20 файлов на тип — запас на разбор, не бесконечный рост.

Если после `INFO` лог всё ещё большой — `syslog_level='WARN'`. Для бенчмарка официально ещё:

```sql
ALTER SYSTEM SET enable_record_trace_log = false;
-- крайняя мера в тесте, не в обычном проде:
-- ALTER SYSTEM SET syslog_level = 'ERROR';
```

Не оставляйте `DEBUG`/`TRACE` после инцидента.

## Скрипт в этом репозитории

```bash
./scripts/deploy.sh observer-log show
./scripts/deploy.sh observer-log apply              # mode из yaml или info
./scripts/deploy.sh observer-log apply --mode warn
./scripts/deploy.sh observer-log apply --mode debug  # вернуть WDIAG на время инцидента
```

| Режим | `syslog_level` | Ещё | Когда |
|---|---|---|---|
| **`info`** (по умолчанию) | `INFO` | без `.wf`, async, recycle 20 файлов, IO ≤ 10M | продакшен |
| **`warn`** | `WARN` | плюс `enable_record_trace_log=false` | всё ещё слишком много логов |
| **`debug`** | `WDIAG` | `.wf` включён, recycle остаётся | короткий разбор |

В `config/deploy.yaml`:

```yaml
oceanbase:
  log_mode: info          # observer (кластер)
  obproxy:
    log_mode: info        # ODP, docs/obproxy-logging.md
```

`./scripts/deploy.sh deploy` и `./scripts/deploy.sh all` применяют режим из `oceanbase.log_mode` (`apply --skip-if-none --skip-if-ok`). `./scripts/deploy.sh tenant` повторяет идемпотентно. После замены узла `06-recover-observer.sh` — тоже. На уже живом кластере достаточно `observer-log apply`.

В `generated/obd-cluster.yaml` те же ключи пишет плагин OBD (`syslog_level`, `enable_syslog_wf`, recycle, лимит IO) — новые observer стартуют уже с ними. `ALTER SYSTEM` нужен живым кластерам, которых OBD заново не раскатывает.

Проверка:

```bash
mysql -h<observer_ip> -P2881 -uroot@sys -p -e \
  "SHOW PARAMETERS LIKE 'syslog_level'; \
   SHOW PARAMETERS LIKE 'enable_syslog_wf'; \
   SHOW PARAMETERS LIKE 'enable_syslog_recycle'; \
   SHOW PARAMETERS LIKE 'syslog_io_bandwidth_limit';"
```
