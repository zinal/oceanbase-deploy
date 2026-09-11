# Логи OBProxy (ODP): как убрать десятки гигабайт в сутки

На нагруженном инстансе ODP легко даёт **>25 ГБ логов за сутки**. Это не «просто диск»: синхронная запись WDIAG на каждый запрос отбирает CPU и IO у SQL-прокси. В этом репозитории boot-диск obproxy по умолчанию **20 ГБ** — без ограничения каталога логов он забивается за часы.

Логи **observer** — отдельно: [observer-logging.md](observer-logging.md).

Официально: [журнал ODP](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000002024095), [`syslog_level`](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000005053813) (с 4.2.3 дефолт **WDIAG**, раньше INFO), [`monitor_log_level`](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000005053945), [`route_diagnosis_level`](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000005053963).

## Что писать в первую очередь

На каждом obproxy:

```bash
du -sh ~/obproxy/log/*
ls -lhS ~/obproxy/log | head
```

| Файл | Откуда растёт | Главный рычаг |
|---|---|---|
| `obproxy.log` | системный лог, самый полный | **`syslog_level`** (дефолт `WDIAG`) |
| `obproxy.log.wf` | дубль WARN+ из `obproxy.log` | `enable_syslog_wf` |
| `obproxy_diagnosis.log` | логин, дисконнект, ошибки роута | `monitor_log_level`, `route_diagnosis_level` |
| `obproxy_digest.log` | SQL дольше `query_digest_time_threshold` (100 ms) и ошибки | порог / `monitor_log_level` |
| `obproxy_slow.log` | SQL дольше `slow_query_time_threshold` (500 ms) | порог |
| `obproxy_error.log` | упавшие SQL | обычно мало |
| `obproxy_trace.log` | полная трасса | `monitor_log_level` |
| `obproxy_stat.log` | снимок раз в `stat_dump_interval` | почти не объём |

Если раздут **`obproxy.log`** — это `syslog_level=WDIAG`. С 4.2.3 ODP пишет туда ожидаемые диагностические ошибки (`WDIAG` = Warning Diagnosis). На TPC-C / высоком QPS это основной источник десятков гигабайт.

Если раздут **`obproxy_diagnosis.log`** — частые connect/disconnect или оставленный `route_diagnosis_level=4` после отладки роута (см. [obproxy-session-routing.md](obproxy-session-routing.md)).

## Уровни `syslog_level` / `monitor_log_level`

От подробного к короткому: `DEBUG` → `TRACE` → `WDIAG` → `EDIAG` → `INFO` → `WARN` → `ERROR`.

Пишется всё **не ниже** выбранного уровня. `WDIAG` включает и INFO, и WARN, и кучу «ожидаемых» DIAG. `INFO` — состояние процесса, без по-запросной диагностики. `WARN` — только неожиданное.

`monitor_log_level` режет **диагностические** файлы (`diagnosis` / `digest` / `slow` / `error` / `trace`), не `obproxy.log`. Поднимать его до `WARN` имеет смысл, если растёт diagnosis/digest, а не основной лог: digest/slow при INFO полезны в эксплуатации.

Рестарт ODP **не нужен**. `ALTER PROXYCONFIG` действует сразу и **только на тот экземпляр**, к которому подключились.

## Что выставлять в продакшене

Минимальный безопасный шаг (вернуть поведение до 4.2.3):

```sql
ALTER PROXYCONFIG SET syslog_level = 'INFO';
ALTER PROXYCONFIG SET enable_syslog_wf = false;
ALTER PROXYCONFIG SET enable_async_log = true;
ALTER PROXYCONFIG SET enable_syslog_file_compress = true;
ALTER PROXYCONFIG SET syslog_io_bandwidth_limit = '10MB';
```

Если после этого `obproxy.log` всё ещё большой — `syslog_level='WARN'`. Если большой `obproxy_diagnosis.log`:

```sql
ALTER PROXYCONFIG SET monitor_log_level = 'WARN';
ALTER PROXYCONFIG SET route_diagnosis_level = 1;  -- 0 полностью выключает модуль
```

Не оставляйте `route_diagnosis_level=4` и `monitor_log_level='TRACE'` после разбора роута.

Пороги аудита (если раздут digest/slow, а не syslog):

```sql
SHOW PROXYCONFIG LIKE 'query_digest_time_threshold';
SHOW PROXYCONFIG LIKE 'slow_query_time_threshold';
-- дефолт 100ms / 500ms; поднимать только если digest.log — основной объём
```

Защита диска (не снижает QPS записи, но не даёт забить 20 ГБ boot):

```sql
ALTER PROXYCONFIG SET log_dir_size_threshold = '8G';  -- OBD default 64G > нашего диска
ALTER PROXYCONFIG SET log_file_percentage = 50;
```

`enable_async_log=true` и `syslog_io_bandwidth_limit` не уменьшают объём, но ограничивают влияние записи на латентность SQL.

## Скрипт в этом репозитории

```bash
./scripts/deploy.sh obproxy-log show
./scripts/deploy.sh obproxy-log apply              # mode из yaml или info
./scripts/deploy.sh obproxy-log apply --mode warn
./scripts/deploy.sh obproxy-log apply --mode debug  # вернуть WDIAG на время инцидента
```

Режимы:

| Режим | `syslog_level` | Ещё | Когда |
|---|---|---|---|
| **`info`** (по умолчанию) | `INFO` | без `.wf`, async, compress, IO ≤ 10 MB, каталог логов ~40% boot-диска | продакшен, 25 ГБ/сутки |
| **`warn`** | `WARN` | `monitor_log_level=WARN`, `route_diagnosis_level=1` | всё ещё слишком много логов |
| **`debug`** | `WDIAG` | штатные дефолты вендора | короткий разбор, потом вернуть |

В `config/deploy.yaml`:

```yaml
oceanbase:
  obproxy:
    log_mode: info    # info | warn | debug
```

`./scripts/deploy.sh deploy` и `./scripts/deploy.sh tenant` применяют режим сами (`--skip-if-ok`). После замены узла `07-recover-obproxy.sh` — тоже. На уже живом кластере достаточно `obproxy-log apply`.

В `generated/obd-cluster.yaml` для **новых** obproxy пишутся только ключи, которые знает плагин OBD: `log_dir_size_threshold`, `log_file_percentage`, `log_cleanup_interval`. `syslog_level` OBD как параметр YAML не принимает — его ставит `ALTER PROXYCONFIG`.

Проверка на одном хосте:

```bash
mysql -h<obproxy_ip> -P2883 -uroot@sys#<cluster> -p -e \
  "SHOW PROXYCONFIG LIKE '%log_level%'; \
   SHOW PROXYCONFIG LIKE 'enable_syslog_wf'; \
   SHOW PROXYCONFIG LIKE 'log_dir_size_threshold';"
```

Команда действует на **этот** ODP. При нескольких инстансах обходите все `OBPROXY_*` из inventory (скрипт так и делает).
