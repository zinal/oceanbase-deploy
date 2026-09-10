# Равномерное распределение сессий OBProxy (ODP)

Лидеры лог-стримов / партиций могут быть размазаны по кластеру, а **SQL всё равно идёт на один observer**. Это не баланс лидеров и не HAProxy перед прокси: ODP сам выбирает observer для сессии, когда не может точно посчитать Leader.

Официальный разбор: [распределённый SQL весь уходит на один узел](https://www.oceanbase.com/knowledge-base/oceanbase-database-proxy-1000000003265247), [маршрутизация ODP](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000000517776), [влияние PS на роут](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000000050282), [best practices](https://www.oceanbase.com/docs/common-best-practices-1000000001591716).

## Почему `gv$ob_log_stat` здесь не помогает

Запрос вроде

```sql
SELECT svr_ip, COUNT(*) AS leaders
FROM gv$ob_log_stat
WHERE role = 'LEADER'
GROUP BY svr_ip;
```

показывает **лидеров лог-стримов**. На большом кластере это часто «по одному на observer» — и это нормально. Он **не** отвечает на вопрос «куда ODP отправил клиентскую сессию / SQL».

Смотрите сессии и audit:

```sql
SELECT svr_ip, COUNT(*) AS sessions
FROM gv$ob_processlist
GROUP BY svr_ip
ORDER BY sessions DESC;

SELECT svr_ip, COUNT(*) AS stmts
FROM gv$sql_audit
GROUP BY svr_ip
ORDER BY stmts DESC;
```

Имена view чуть отличаются по версии (`gv$session`, `gv$ob_processlist`).

## Как читать перекос сессий

Реальный TPC-C на ~30 observer часто выглядит так (фильтр `user LIKE 'tpcc%'`):

| Полоса | Сессии на узел | Что это |
|---|---|---|
| Горячая | один узел ≈ 3× середины (например 320 при 85–93) | Fallback ODP: логин, SQL без ключа партиции, `enable_cached_server` |
| Середина | ровная полка ~80–90 на многих узлах | Партиционный роутинг **работает**: INSERT/UPDATE с ключом склада идут на Leader |
| Холодный хвост | 4–27 на части узлов | Мало **unit / tablet-лидеров** на этих observer, не баг ODP |

`ALTER PROXYCONFIG` снимает **горячую** вершину (после переоткрытия пула она должна сползти к полке). Холодный хвост им не лечится: смотрите unit'ы и лидеры tablet.

```sql
-- по одному ли unit на каждый observer user-тенанта?
SELECT t.tenant_name, u.svr_ip, COUNT(*) AS units
FROM oceanbase.DBA_OB_UNITS u
JOIN oceanbase.DBA_OB_TENANTS t ON u.tenant_id = t.tenant_id
WHERE t.tenant_type = 'USER'
GROUP BY t.tenant_name, u.svr_ip
ORDER BY units, u.svr_ip;

-- куда сели лидеры tablet (не clog)
SELECT svr_ip, COUNT(*) AS tablet_leaders
FROM oceanbase.DBA_OB_TABLE_LOCATIONS
WHERE role = 'LEADER'
GROUP BY svr_ip
ORDER BY tablet_leaders DESC;
```

Если на холодных IP `units = 0` — тенант не растянут на эти узлы (`UNIT_NUM` меньше числа observer в Zone, или узел добавили scale-out и пул не расширили). Если unit есть, а `tablet_leaders` мало — партиции/PRIMARY_ZONE ещё не разъехались (`ALTER TENANT … PRIMARY_ZONE='RANDOM'`, дождаться баланса).

Имя view локаций на части сборок — `CDB_OB_TABLE_LOCATIONS` (из sys).

## Как ODP выбирает observer

1. Если в SQL есть таблица и условие партиции — запрос идёт на **Leader этой партиции**.
2. Если имя таблицы или партицию посчитать нельзя (join без ключа, нет WHERE по партиции, слишком длинный outline/hint, `SELECT 1`, часть SET/SHOW) — включается fallback:
   - `enable_primary_zone=true` — на observer из Primary Zone тенанта (логин тоже часто туда);
   - `enable_cached_server=true` — **повторно на тот же observer, что в предыдущей сессии**.
3. Если оба параметра `false` — **случайный** observer из доступных.

### Prepared statements: текст или значения параметров?

ODP **умеет** считать партицию по bind-значениям на этапе Execute, а не только по литералам в тексте. Текст с `?` нужен, чтобы понять таблицу и какие колонки — ключ партиции; сами значения берутся из пакета Execute.

| Этап | Что видит ODP | Точный роут на Leader |
|---|---|---|
| `COM_STMT_PREPARE` / `PREPARE … FROM '… ? …'` | только шаблон, значений нет | нет → fallback (Primary Zone / кэш сессии / random) |
| `COM_STMT_EXECUTE` (бинарный протокол JDBC `useServerPrepStmts=true`) | шаблон + значения из bind | да, если ключ партиции в параметрах и ODP их разобрал |
| Текстовый `EXECUTE stmt USING @a, @b` | шаблон + session-переменные | да (официальный пример `explain route execute … using`) |
| `COM_QUERY` с литералами (`useServerPrepStmts=false`) | значения уже в тексте | да, как обычный SQL |

ODP **не** приклеивает Execute к тому observer, куда ушёл Prepare. Штатный путь — [синхронизировать состояние Prepare](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000000050282) на выбранный узел и послать Execute туда, куда указывает партиция. Иначе распределённый PS был бы бесполезен.

Ограничения те же, что у текстового SQL: ключа нет в условии, функция на ключе, которую парсер ODP не считает (`abs`, `now`, …), слишком длинный SQL (буфер разбора), несколько ключей range, которые ODP не складывает. Тогда Execute тоже идёт в fallback — отсюда горячая вершина вроде `10.130.0.11`.

Проверка на живом Execute (не на `EXPLAIN ROUTE … WHERE c1=?` без значений):

```sql
-- текстовый PS
EXPLAIN ROUTE EXECUTE stmt0 USING @a, @b\G
-- в PARTITION_ID_CALC_DONE должно быть partitions:"(p…)", не (p-1)

-- бинарный PS: смотреть obproxy_diagnosis.log на COM_STMT_EXECUTE
-- EXPR_PARSE / RESOLVE_TOKEN с реальными числами, parse_sql с подставленными значениями
```

`EXPLAIN ROUTE SELECT … WHERE c1=?` без Execute **не** доказывает, что bind не работает: в этом запросе значений просто нет.

## Почему тогда прибито к одному серверу (5.0.1)

Bind на Execute **не отменяет** четыре других якоря. На TPC-C + 5.0.1 типичная картина «320 на `.11` и полка 85–93» как раз из них, а не из «ODP не видит `?`».

1. **`COM_STMT_PREPARE` / `BEGIN` / `SET` без ключа партиции.** Значений ещё нет → fallback. Логин с `enable_primary_zone=true` часто попадает на один и тот же observer (у вас `.11`, у него же 2 LS-лидера). ODP держит там server-session: в `gv$ob_processlist` это Sleep/Prepare, не обязательно горячий DML.

2. **Транзакция без intra-txn роута.** Пока `enable_transaction_internal_routing=false`, все операторы **внутри** транзакции принудительно идут на узел, где транзакция открылась ([принудительная маршрутизация](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000000517776)). TPC-C шлёт `BEGIN` или `SET autocommit=0` без ключа → открытие на `.11` → весь New Order/Payment как remote/dist **на `.11`**. Bind уже не важен: Execute не имеет права сменить узел. С 4.1 это снимается так:

   ```sql
   ALTER PROXYCONFIG SET enable_ob_protocol_v2 = true;
   ALTER PROXYCONFIG SET enable_transaction_internal_routing = true;
   ```

   После включения `ob_trx_idle_timeout` на ODP не действует.

3. **`enable_cached_server=true`.** Любой SQL, у которого партицию посчитать не вышло (нет ключа, функция, слишком длинный текст), уходит в `USE_CACHED_SESSION` — снова `.11`.

4. **Холодный хвост 4–27** — не pin ODP, а мало unit/tablet-лидеров.

`gv$ob_processlist` сам по себе не доказывает, что DML прибит: на координаторе висят Prepare и idle-сессии. Смотрите `gv$ob_sql_audit` (`request_type`, `plan_type`, `partition_hit`).

### Что снять на 5.0.1

Готовый пакет: [`docs/sql/obproxy-route-diag-501.sql`](sql/obproxy-route-diag-501.sql). Кратко.

**A. На каждом obproxy (`:2883`):**

```sql
SHOW PROXYCONFIG LIKE 'enable_cached_server';
SHOW PROXYCONFIG LIKE 'enable_primary_zone';
SHOW PROXYCONFIG LIKE 'enable_transaction_internal_routing';
SHOW PROXYCONFIG LIKE 'enable_ob_protocol_v2';
SHOW PROXYCONFIG LIKE 'target_db_server';
SHOW PROXYCONFIG LIKE 'proxy_primary_zone_name';
```

**B. В тенанте `tpcc` — сессии vs SQL:**

```sql
SELECT svr_ip, command, COUNT(*) AS sess
FROM gv$ob_processlist
WHERE user LIKE 'tpcc%'
GROUP BY svr_ip, command
ORDER BY sess DESC;

SELECT svr_ip,
       SUM(request_type = 5) AS prepares,
       SUM(request_type = 6) AS executes,
       SUM(plan_type = 1)    AS local_plan,
       SUM(plan_type = 2)    AS remote_plan,
       SUM(plan_type = 3)    AS dist_plan,
       SUM(partition_hit = 0) AS part_miss,
       COUNT(*) AS stmts
FROM gv$ob_sql_audit
WHERE is_inner_sql = 0
GROUP BY svr_ip
ORDER BY stmts DESC;
```

Как читать audit на `.11`:

| Что видно | Вывод |
|---|---|
| Много `request_type=5`, мало Execute, `command=Sleep` | висят Prepare/логин, DML размазан — снимите `enable_cached_server` и переоткройте пул |
| Много `plan_type=2/3` и `partition_hit=0` на Execute | `.11` координатор транзакций — включите intra-txn роут + even fallback |
| Много `plan_type=1` и те же INSERT, что на полке | на `.11` просто больше tablet-лидеров — смотрите `DBA_OB_TABLE_LOCATIONS` |

**C. Что за SQL на горячем узле:**

```sql
SELECT LEFT(query_sql, 80) AS sql_head, request_type, plan_type, partition_hit, COUNT(*) AS n
FROM gv$ob_sql_audit
WHERE is_inner_sql = 0 AND svr_ip = '10.130.0.11'
GROUP BY LEFT(query_sql, 80), request_type, plan_type, partition_hit
ORDER BY n DESC
LIMIT 30;
```

Если сверху `BEGIN` / `PREPARE` / `SET` / `INSERT` без ключа — это якорь 1–2. Если обычный `INSERT INTO bmsql_oorder …` с `plan_type=2` — транзакционный pin.

**D. ODP-диагноз одного живого Execute** (на прокси, кратковременно):

```sql
ALTER PROXYCONFIG SET route_diagnosis_level = 4;
```

В `~/obproxy/log/obproxy_diagnosis.log` на `COM_STMT_EXECUTE` ищите `ROUTE_INFO`:

- `USE_PARTITION_LOCATION_LOOKUP` + `partitions:"(p…)"` — bind сработал;
- `USE_CACHED_SESSION` / `USE_LAST_SESSION` — fallback или pin транзакции (`in_transaction:true`).

Потом верните уровень (обычно `0` или `1`), лог иначе раздувается.

Нужен `enable_sql_audit=true` (кластер) и `ob_enable_sql_audit=1` в тенанте, иначе audit пустой.

Типичный перекос: оба флага `true` (дефолт многих сборок). Логин попал на один узел Primary Zone, дальше весь «непосчитанный» SQL и распределённые запросы липнут к нему. Один observer становится координатором, CPU растёт, остальные простаивают.

Принудительный pin ещё сильнее:

```sql
SHOW PROXYCONFIG LIKE 'target_db_server';
SHOW PROXYCONFIG LIKE 'proxy_primary_zone_name';
```

Непустые значения — это «всегда этот IP / эта Zone». Для равномерного OLTP их быть не должно.

## Что выставить

На **каждом** экземпляре obproxy (команда действует только на тот ODP, к которому подключились):

```sql
-- root@proxysys или root@sys через порт 2883, не observer:2881
ALTER PROXYCONFIG SET enable_cached_server = false;
ALTER PROXYCONFIG SET enable_primary_zone = false;
```

| Режим | `enable_cached_server` | `enable_primary_zone` | Когда |
|---|---|---|---|
| **even** (этот репозиторий по умолчанию) | `false` | `false` | Нужно размазать сессии; много SQL без ключа партиции; TPC-C / смешанная нагрузка |
| **oltp** (официальная практика для чистого TP) | `false` | `true` | Партиционированные таблицы, ключ почти всегда в WHERE; fallback лучше слать ближе к лидерам Primary Zone |

`enable_cached_server=false` рекомендуют и в тесте, и в проде. `enable_primary_zone=true` имеет смысл только если Primary Zone тенанта — `RANDOM` или несколько равноправных Zone. Если Primary Zone = одна Zone / один узел, `true` снова соберёт fallback на неё.

Перезапуск obproxy не нужен. Уже открытые клиентские сессии держат старый observer — **переподключите пул**.

## Команда в этом репозитории

На уже работающем кластере:

```bash
./scripts/deploy.sh obproxy-route show
./scripts/deploy.sh obproxy-route diagnose
./scripts/deploy.sh obproxy-route apply            # even: оба флага false
./scripts/deploy.sh obproxy-route apply --mode oltp
```

Скрипт обходит все `OBPROXY_*` из `generated/inventory.env` и на каждом выполняет `ALTER PROXYCONFIG`. После `./scripts/deploy.sh tenant` тот же режим `even` применяется сам (идемпотентно).

Вручную, если inventory нет:

```bash
# на КАЖДЫЙ obproxy
obclient -h<obproxy_ip> -P2883 -uroot@sys#<cluster> -p'<ocp.root_password>' -e \
  "ALTER PROXYCONFIG SET enable_cached_server = false; \
   ALTER PROXYCONFIG SET enable_primary_zone = false; \
   SHOW PROXYCONFIG LIKE 'enable_cached_server'; \
   SHOW PROXYCONFIG LIKE 'enable_primary_zone'; \
   SHOW PROXYCONFIG LIKE 'target_db_server'; \
   SHOW PROXYCONFIG LIKE 'proxy_primary_zone_name';"
```

Проверка, что SQL больше не сидит на одном узле — снова `gv$ob_processlist` / `gv$sql_audit` **после** переоткрытия соединений.

## Тенант и схема — отдельные оси

ODP размазывает только то, что не смог привязать к партиции. Остальное всё равно идёт на лидеров.

1. **PRIMARY_ZONE тенанта = RANDOM.** Иначе `enable_primary_zone=true` и сам выбор лидеров тянут нагрузку в одну Zone.

   ```sql
   ALTER TENANT tpcc PRIMARY_ZONE = 'RANDOM';
   SELECT tenant_name, primary_zone FROM oceanbase.DBA_OB_TENANTS;
   ```

   `./scripts/deploy.sh tenant` передаёт `obd cluster tenant create --primary-zone RANDOM` и при необходимости делает тот же `ALTER TENANT`.

2. **Крупные таблицы партиционировать**, в горячем SQL держать ключ партиции. Без ключа ODP не попадёт в Leader и уйдёт в fallback (случайный или Primary Zone).

3. **Не путать с HAProxy.** [`docs/haproxy-obproxy-tcp-lb.md`](haproxy-obproxy-tcp-lb.md) балансирует клиентов **между obproxy**, не между observer. `balance source` клеит один клиентский IP к одному прокси; на перекос «все SQL на observer X» это не влияет.

## Чеклист

1. `SHOW PROXYCONFIG` на каждом obproxy: `enable_cached_server=false`, для even ещё `enable_primary_zone=false`.
2. `target_db_server` и `proxy_primary_zone_name` пустые.
3. User tenant: `PRIMARY_ZONE=RANDOM`, locality на все Zone, таблицы с партициями.
4. Клиентский пул переоткрыт; смотреть `gv$ob_processlist` / `gv$sql_audit`, не только `gv$ob_log_stat`.
5. Горячая вершина (~3× полки) — ODP fallback. Холодный хвост — `DBA_OB_UNITS` / `DBA_OB_TABLE_LOCATIONS`.
