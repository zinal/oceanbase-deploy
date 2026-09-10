# Равномерное распределение сессий OBProxy (ODP)

Лидеры лог-стримов / партиций могут быть размазаны по кластеру, а **SQL всё равно идёт на один observer**. Это не баланс лидеров и не HAProxy перед прокси: ODP сам выбирает observer для сессии, когда не может точно посчитать Leader.

Официальный разбор: [распределённый SQL весь уходит на один узел](https://www.oceanbase.com/knowledge-base/oceanbase-database-proxy-1000000003265247), [маршрутизация ODP](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000000517776), [best practices](https://www.oceanbase.com/docs/common-best-practices-1000000001591716).

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

## Как ODP выбирает observer

1. Если в SQL есть таблица и условие партиции — запрос идёт на **Leader этой партиции**.
2. Если имя таблицы или партицию посчитать нельзя (join без ключа, нет WHERE по партиции, слишком длинный outline/hint, `SELECT 1`, часть SET/SHOW) — включается fallback:
   - `enable_primary_zone=true` — на observer из Primary Zone тенанта (логин тоже часто туда);
   - `enable_cached_server=true` — **повторно на тот же observer, что в предыдущей сессии**.
3. Если оба параметра `false` — **случайный** observer из доступных.

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
