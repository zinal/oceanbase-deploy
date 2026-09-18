# Ошибка 5930: лимит prepared statement handles

Под нагрузкой приложение получает `-5930 maximum open cursors exceeded` / `maximum open prepared statement handles exceeded`. На observer в логе:

```text
WDIAG [SQL] handle_ps_prepare (ob_sql.cpp:…)
  exceeds the maximum number of ps handles allowed to open on the session
  (ret=-5930, cur_ps_handle_size=50, open_cursors_limit=50)
```

Это **не** лимит HAProxy, не OBProxy и не размер пула соединений. На **одной сессии** observer одновременно открыто больше prepared statement (PS), чем разрешает tenant-параметр `open_cursors`.

Официально: [лимит PS на session](https://www.oceanbase.com/knowledge-base/oceanbase-database-1000000000442612), [`open_cursors`](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000005685318), [Connector/J vs `open_cursors`](https://www.oceanbase.com/knowledge-base/oceanbase-connector-j-1000000001848292), [OBE-01000 / 5930](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003977162).

## Что ограничивает лимит

| Параметр | Где | Что считает | Дефолт вендора |
|---|---|---|---|
| **`open_cursors`** | tenant-конфиг observer (`SHOW PARAMETERS`, scope=`TENANT`) | максимум **курсоров** на сессию **и отдельно** максимум **PS-хендлов** на сессию | **50** (диапазон 0…65535; `0` = без лимита) |
| `prepStmtCacheSize` | JDBC URL (OceanBase Connector/J) | сколько PS драйвер держит открытыми **на одно соединение** | **250** в Connector/J **2.x**, 25 в 1.x |
| `cachePrepStmts` | JDBC URL | кэшировать PS на клиенте (серверные хендлы не закрываются) | `true` в 2.x |
| `useServerPrepStmts` | JDBC URL | бинарный `COM_STMT_PREPARE` на observer; `false` — текст, серверных PS нет | `true` в 2.x |
| `_ob_enable_prepared_statement` | переменная тенанта (`SHOW VARIABLES`) | observer вообще принимает server-side PS | `TRUE` |

Лимит, который даёт **5930**, — только **`open_cursors`**. JDBC-параметры задают, **сколько** хендлов сессия пытается открыть. Пул на 500 соединений сам по себе лимит не умножает: 50 — на **каждое** соединение. Под нагрузкой на коннекте просто исполняется больше разных SQL, JDBC-кэш наполняется до 250 и упирается в 50.

`open_cursors` — **не** сумма cursor+PS. При значении 100 на одной сессии можно держать 100 курсоров **и** 100 PS. С V3.2.4 PS считают тем же параметром (код ошибки по-прежнему 5930 / ORA-01000).

Рестарт observer **не нужен**. `ALTER SYSTEM SET` действует сразу.

ODP (`ALTER PROXYCONFIG`) отдельного лимита PS-хендлов не выставляет: прокси пробрасывает Prepare на observer, хендл живёт в сессии observer.

## Что выставлять

| Сценарий | `open_cursors` | JDBC |
|---|---|---|
| **OLTP / TPC-C / Connector/J 2.x** (этот репозиторий) | **1000** | `prepStmtCacheSize` оставить **250** (или явно ≤ 1000) |
| Тяжёлый ORM, сотни уникальных SQL на соединение | **2000** | `prepStmtCacheSize` ≈ числу уникальных SQL, **строго меньше** `open_cursors` |
| Авария / апгрейд, пока не подобрали значение | **0** (без лимита) | не трогать; сразу вернуть конечное число |
| Не нужен бинарный PS | 50 может хватить | `useServerPrepStmts=false` (теряется PS-роут ODP, см. [obproxy-session-routing.md](obproxy-session-routing.md)) |

**1000** закрывает классический разрыв «кэш JDBC 250 vs лимит 50» и оставляет запас на неявные Prepare (логин, `BEGIN`, служебный SQL). **Не** ставьте `0` в проде надолго: без потолка модуль `PsSessionInfo` на observer раздувается (в KB — десятки гигабайт на тенант 500). Потолок 65535 почти то же самое.

Правило: **`prepStmtCacheSize` < `open_cursors`**. Если кэш драйвера больше лимита observer, 5930 ожидаем.

Не лечите утечку хендлов одним только увеличением лимита: незакрытые `PreparedStatement` / курсоры в долгих сессиях всё равно упрутся в любой потолок.

### На живом кластере

Из **sys** (observer `:2881` или OBProxy `:2883` как `root@sys`):

```sql
SHOW PARAMETERS LIKE 'open_cursors' TENANT = tpcc;
ALTER SYSTEM SET open_cursors = 1000 TENANT = tpcc;
SHOW PARAMETERS LIKE 'open_cursors' TENANT = tpcc;
```

Из самого user-тенанта `TENANT = …` не нужен:

```sql
ALTER SYSTEM SET open_cursors = 1000;
```

Через этот репозиторий:

```bash
./scripts/deploy.sh open-cursors show
./scripts/deploy.sh open-cursors apply              # tenant.open_cursors или 1000
./scripts/deploy.sh open-cursors apply --value 2000
```

`./scripts/deploy.sh tenant` выставляет то же значение идемпотентно. Пароль — `ocp.root_password`. Имя тенанта — `tenant.tenant_name`.

В `config/deploy.yaml`:

```yaml
tenant:
  tenant_name: tpcc
  open_cursors: 1000    # 0…65535; пустое = 1000, не вендорские 50
```

### JDBC URL

OceanBase Connector/J **2.x** по умолчанию уже включает server PS и кэш на 250. Явно согласовать с `open_cursors`:

```text
jdbc:oceanbase://host:2883/tpcc?useServerPrepStmts=true&cachePrepStmts=true&prepStmtCacheSize=250
```

| Цель | Что менять |
|---|---|
| Оставить PS, убрать 5930 | поднять `open_cursors` (≥ кэша) |
| Уменьшить число хендлов на сессии | снизить `prepStmtCacheSize` |
| Вообще не открывать server PS | `useServerPrepStmts=false` (или `cachePrepStmts=false` — тогда Prepare/Close на каждый вызов) |

Connector/J **1.x**: `useServerPrepStmts=false`, кэш 25 — до 50 обычно не добивает. После перехода на 2.x ошибка появляется «сама».

`_ob_enable_prepared_statement=TRUE` нужен, если `useServerPrepStmts=true`. Не выключайте его, чтобы обойти 5930: драйвер начнёт получать другие ошибки Prepare.

## Диагностика

На observer в момент ошибки: `cur_ps_handle_size` и `open_cursors_limit` в `handle_ps_prepare`. Если оба 50 — вендорский дефолт, JDBC ещё не трогали.

Сколько PS открыто сейчас (имена колонок чуть отличаются по версии):

```sql
SHOW PARAMETERS LIKE 'open_cursors';
SHOW VARIABLES LIKE '_ob_enable_prepared_statement';

-- по сессиям (OceanBase ≥ 4.x)
SELECT svr_ip, COUNT(*) AS ps_handles
FROM oceanbase.GV$OB_SESSION_PS_INFO
GROUP BY svr_ip
ORDER BY ps_handles DESC;
```

Если view нет — смотрите `gv$ob_sql_audit` (`request_type=5` = Prepare) и закрывает ли приложение statement после использования.

Типичная картина TPC-C + Connector/J 2.x: на каждом коннекте кэш дорастает до ~250 уникальных SQL, `open_cursors=50` → 5930 только на горячих транзакциях, на прогреве тишина.
