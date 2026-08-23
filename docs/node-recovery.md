# Восстановление после полной потери observer или obproxy

Скрипты рассчитаны на **полную гибель одного хоста** (ВМ удалена, диск недоступен, ОС не загружается). Они следуют публичной документации OceanBase, а не эвристикам этого репозитория.

Если хост ещё жив и процесс можно поднять — не используйте эти скрипты. Официально: перезапустить `./bin/observer` из `home_path` или `ALTER SYSTEM START SERVER`.

## Источники

| Тема | Документ |
|------|----------|
| Авария узла, majority, `STOP SERVER` | [Node Failures / 节点宕机](https://oceanbase.github.io/docs/user_manual/operation_and_maintenance/en-US/emergency_handbook/node_breakdown) |
| Замена узла: add → migrate unit → delete | [替换节点](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000003378853) |
| Добавление узла / `ADD SERVER` | [添加节点](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000507927) |
| Удаление узла / `DELETE SERVER` | [删除节点](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000507928) |
| Изоляция / `STOP SERVER` | [停止或启动 OBServer](https://www.oceanbase.com/docs/common-oceanbase-database-cn-10000000001579248) |
| `server_permanent_offline_time` | [server_permanent_offline_time](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000510222) |
| OBD `scale_out` (только новый узел) | [扩容与组件变更](https://www.oceanbase.com/docs/common-obd-cn-1000000005151031) |
| OBD не умеет scale_in | [Perform O&M by using OBD](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_05_operation_and_maintenance/o_m_by_obd) |
| Правка метаданных OBD после `DELETE SERVER` | [OBD 集群中执行删除节点 SQL 后…](https://www.oceanbase.com/knowledge-base/oceanbase-database-proxy-1000000001277967) |
| Замена OBProxy: сначала add, потом delete | [替换 OBProxy 节点](https://www.oceanbase.com/docs/common-ocp-1000000001739900) |

## Предусловия

- Жив **majority** исходного состава observer (для 3 узлов — не меньше двух ACTIVE). Иначе официально: physical backup/restore или standby, не замена узла.
- Есть `obd`, `obclient` или `mysql`, `yc`, SSH-ключ из `config/deploy.yaml`.
- Пароль `root` sys-тенанта: переменная `OB_ROOT_PASSWORD`, иначе `ocp.root_password`, иначе значение из `~/.obd/cluster/<deploy>/`.
- SQL идёт на **оставшийся** observer (`root@sys`, порт 2881) или оставшийся obproxy (`root@sys#<cluster>`, порт 2883).

## Observer: `scripts/06-recover-observer.sh`

Типовой вызов после гибели `observer-2`:

```bash
./scripts/06-recover-observer.sh 2 --yes
```

Индекс совпадает с inventory: `1` → `zone1` / `OBSERVER_1_*`. Скрипт **пересоздаёт ВМ с тем же именем**; IP обычно новый.

### Что делает скрипт

1. **Проверка majority**  
   `SELECT SVR_IP, STATUS FROM oceanbase.DBA_OB_SERVERS`.  
   Если число ACTIVE без потерянного узла не больше `N/2` — останов. Это прямое следствие аварийного handbook: без majority замена узла не восстанавливает сервис.

2. **`ALTER SYSTEM STOP SERVER 'old_ip:2882'`**  
   Официально самый безопасный способ изолировать узел: трафик уходит, остальные реплики остаются majority. На уже мёртвом хосте команда может завершиться ошибкой — это ожидаемо.

3. **Удаление ВМ и дисков в Yandex Cloud**  
   Локальные data/clog на погибшем хосте нечитаемы. Скрипт ждёт исчезновения instance и дисков `{name}-data`, `{name}-log`, `{name}-boot`.

4. **Создание замены и prepare**  
   Те же профили, что при первом provision: диски, cloud-init, `02-prepare-servers.sh` (sysctl, limits, mount data/log). Каталоги `data_dir` / `redo_dir` пустые — так и должно быть для нового observer.

5. **`obd cluster scale_out` только нового узла**  
   OBD ставит пакет, стартует процесс и выполняет эквивалент `ALTER SYSTEM ADD SERVER ... ZONE 'zoneN'`.  
   В YAML нет `global`/`depends` исходного кластера — этого требует документ по расширению OBD.  
   Имя в OBD: `server{N}r`, чтобы не конфликтовать со старым `server{N}`.  
   При включённом obagent — второй `scale_out` с тем же `name`/`ip` (официальное требование совпадения servers).

6. **`ALTER SYSTEM MIGRATE UNIT`**  
   Unit со старого IP переносятся на `new_ip:2882` в той же zone. В 1-1-1 без нового узла в zone удаление зависает (некуда мигрировать unit) — поэтому add идёт раньше delete.

7. **`ALTER SYSTEM DELETE SERVER 'old_ip:2882' ZONE 'zoneN'`**  
   Ждёт, пока строки не исчезнет из `DBA_OB_SERVERS`. Если статус застрял в `DELETING`:

   ```sql
   ALTER SYSTEM CANCEL DELETE SERVER 'old_ip:2882' ZONE 'zoneN';
   ```

   Затем разберите нехватку ресурсов в zone (официальный документ «删除节点»).

8. **Метаданные OBD**  
   OBD **не поддерживает scale_in**. После SQL-delete старый IP вычищается из `~/.obd/cluster/<name>/config.yaml` и `inner_config.yaml` (с `.bak-*`). Inventory и `generated/obd-cluster.yaml` получают новый IP.

### Чего скрипт не делает

- Не восстанавливает данные с погибших дисков — durability даёт Paxos на оставшемся majority.
- Не меняет `server_permanent_offline_time` (по умолчанию 3600s; для замены железа официально советуют 4h, для быстрой очистки — 10m).
- Не чинит **ob-configserver**, если он был на `observer-1` (`configserver.dedicated: false`). После замены index=1 проверьте компонент и при необходимости сделайте `obd cluster scale_out` / `component add` на новый IP.
- Не покрывает гибель **двух** observer сразу.

### Полезные флаги

| Флаг | Смысл |
|------|--------|
| `--dry-run` | Только план |
| `--yes` | Без интерактива |
| `--skip-provision` | ВМ уже создана вручную |
| `--skip-sql` | Только облако, SQL не трогать |
| `--keep-old-vm` | Не удалять YC instance (её уже нет) |

Пароль:

```bash
export OB_ROOT_PASSWORD='...'
./scripts/06-recover-observer.sh 2 --yes
```

## Obproxy: `scripts/07-recover-obproxy.sh`

OBProxy stateless. Официальная замена: **сначала добавить новый экземпляр, потом убрать старый**.

```bash
./scripts/07-recover-obproxy.sh 1 --yes
```

### Что делает скрипт

1. Удаляет погибшую ВМ (дисков data/log у роли нет).
2. Создаёт замену с тем же именем, готовит ОС (`--role obproxy`).
3. `obd cluster scale_out` с YAML только из нового IP в `obproxy-ce.servers`.
4. Вычищает старый IP из метаданных OBD, обновляет inventory.

Клиенты, ходившие напрямую на старый IP, должны переключиться на новый. Если перед прокси стоит HAProxy — уберите мёртвый backend из `config/haproxy-obproxy-tcp-lb.cfg.example` / боевого конфига и сделайте `reload`.

Единственный obproxy: до окончания `scale_out` SQL через 2883 недоступен. Observer:2881 при этом жив.

## Ручные SQL (если скрипт остановился)

Подключение к sys (оставшийся observer):

```bash
obclient -h<alive_observer_ip> -P2881 -uroot@sys -p -A
```

```sql
SELECT SVR_IP, SVR_PORT, ZONE, WITH_ROOTSERVER, STATUS
FROM oceanbase.DBA_OB_SERVERS;

SELECT SVR_IP, ROLE, SCN_TO_TIMESTAMP(END_SCN)
FROM oceanbase.GV$OB_LOG_STAT
WHERE TENANT_ID = 1
ORDER BY LS_ID, ROLE;

SHOW PARAMETERS LIKE '%server_permanent_offline_time%';

ALTER SYSTEM STOP SERVER 'old_ip:2882';
-- после OBD scale_out / ADD SERVER:
SELECT UNIT_ID FROM oceanbase.DBA_OB_UNITS WHERE SVR_IP = 'old_ip';
ALTER SYSTEM MIGRATE UNIT = <unit_id> DESTINATION = 'new_ip:2882';
ALTER SYSTEM DELETE SERVER 'old_ip:2882' ZONE 'zoneN';
```

`ADD SERVER` вручную нужен только если OBD scale_out недоступен: процесс observer уже должен быть запущен на новом хосте.

```sql
ALTER SYSTEM ADD SERVER 'new_ip:2882' ZONE 'zoneN';
```

## Проверка после восстановления

```bash
obd cluster display <deployment.name>
```

```sql
SELECT SVR_IP, ZONE, STATUS FROM oceanbase.DBA_OB_SERVERS;
SELECT UNIT_ID, ZONE, SVR_IP, STATUS FROM oceanbase.DBA_OB_UNITS;
```

У нового observer `STATUS=ACTIVE`, unit той же zone сидят на новом IP, старого IP в обеих выборках нет.
