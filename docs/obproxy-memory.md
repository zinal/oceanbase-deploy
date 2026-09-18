# Память OBProxy (ODP): `do_monitor_mem` при живом `free`

На хосте может быть десятки гигабайт свободной RAM, а `obproxy.log` каждые 2 секунды пишет, что память кончилась. Это **не OOM хоста**. ODP сравнивает RSS процесса с `proxy_mem_limited` (дефолт **2G**).

Типичный хвост лога:

```text
ERROR [PROXY] do_monitor_mem (ob_proxy_main.cpp:883) […]
  obproxy's memory is out of limit, will disable alloc memory from the OS
  (mem_limited=2147483648, OTHER_MEMORY_SIZE=73400320, is_out_of_mem_limit=true, cur_pos=…)
```

| Поле в логе | Что это |
|---|---|
| `mem_limited=2147483648` | **2 GiB** — текущий `proxy_mem_limited` |
| `OTHER_MEMORY_SIZE=73400320` | ~70 МиБ служебный запас внутри лимита |
| `is_out_of_mem_limit=true` | RSS упёрся в потолок |
| `cur_pos=0…9` | слот монитора, цикл каждые ~2 с |
| `will disable alloc memory from the OS` | новые аллокации с OS **запрещены**; сессии начинают падать / тормозить |
| `will be going to commit suicide` | более старые сборки: процесс сам выходит (errcode −4080) |

Рядом `free` / `ps` при этом нормальны: ВМ 64 ГБ, used ~4 ГБ, swap 0 — процесс жив, лимит софтовый.

Официально: [`proxy_mem_limited`](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000006242430), [KB do_monitor_mem](https://www.oceanbase.com/knowledge-base/oceanbase-database-proxy-1000000000210042), [мониторинг RSS / usage](https://www.oceanbase.com/docs/common-ocp-1000000000827419).

## Что сделать сразу (живой кластер)

На **каждом** obproxy (команда действует только на тот экземпляр, к которому подключились). Рестарт **не нужен**.

```sql
SHOW PROXYCONFIG LIKE 'proxy_mem_limited';
ALTER PROXYCONFIG SET proxy_mem_limited = '8G';
SHOW PROXYCONFIG LIKE 'proxy_mem_limited';
```

Через этот репозиторий:

```bash
./scripts/deploy.sh obproxy-mem show
./scripts/deploy.sh obproxy-mem apply              # yaml или auto от RAM ВМ
./scripts/deploy.sh obproxy-mem apply --size 8G    # явно, если yaml ещё 4 GB, а ВМ уже 64
```

После ALTER через несколько секунд `do_monitor_mem` должен пропасть из хвоста лога. Если `free` на хосте ~64 ГБ, а в `config/deploy.yaml` всё ещё `vm_profiles.obproxy.memory_gb: 4` — auto останется 2G. Тогда `--size 8G` (или 16G) и поправьте yaml, иначе следующий `apply` без `--size` вернёт 2G.

С jump host на один узел:

```bash
mysql -h<obproxy_ip> -P2883 -uroot@sys#<cluster> -p -e \
  "ALTER PROXYCONFIG SET proxy_mem_limited = '8G'; SHOW PROXYCONFIG LIKE 'proxy_mem_limited';"
```

Пароль — `ocp.root_password`. Кластер в примере — `oceanbase.cluster_name`.

### Аварийно, пока нет SQL

Сигнал **34** временно снимает лимит до рестарта или до следующего `ALTER PROXYCONFIG` ([дока](https://www.oceanbase.com/docs/common-odp-doc-cn-1000000006242430)):

```bash
kill -34 <PID_OBPROXY>    # ps -ef | grep '[o]bproxy --listen_port'
```

Это не замена `proxy_mem_limited`: после рестарта снова 2G. Сразу выставите ALTER.

Не ставьте лимит вплотную к RAM ВМ: OS, page cache, логи. На 4 ГБ ВМ дефолт 2G как раз оставляет запас. На 64 ГБ ВМ 8–16G достаточно; все 50 ГБ ODP не нужны.

## Откуда берётся 2G

| Источник | Значение |
|---|---|
| ODP / плагин OBD `parameter.yaml` | `proxy_mem_limited` default **2G**, min 100MB |
| Этот репозиторий, ВМ 4 ГБ | auto **2G** |
| KB «на хосте есть RAM» | **8G** |
| Крупный контур, много сессий / SSL / широкие result set | **8–16G** на инстанс |

RAM растёт с числом клиентских соединений и размером ответов, не с `memory_limit` observer. Увеличить ВМ в Yandex Cloud **не поднимает** этот параметр.

## Auto в этом репозитории

`vm_profiles.obproxy.memory_gb` → `proxy_mem_limited`, запас OS 2 ГБ, потолок auto 16G:

| RAM ВМ obproxy | auto `proxy_mem_limited` |
|---|---|
| 4 ГБ (дефолт example) | 2G |
| 8 ГБ | 4G |
| 16 ГБ | 8G |
| 32 ГБ и больше | 16G |
| colocate на observer | не больше 4G |

Явно в yaml (побеждает auto):

```yaml
oceanbase:
  obproxy:
    log_mode: info
    proxy_mem_limited: 8G    # пустое / нет ключа = auto от RAM ВМ
```

`./scripts/deploy.sh config` пишет значение в `generated/obd-cluster.yaml` (`obproxy-ce.global.proxy_mem_limited`) — OBD этот ключ знает. Живой процесс берёт лимит из PROXYCONFIG: `deploy` / `all` / `tenant` / `07-recover-obproxy.sh` вызывают `obproxy-mem apply --skip-if-ok`. На уже поднятом кластере достаточно `./scripts/deploy.sh obproxy-mem apply`.

`./scripts/deploy.sh check` ругается, если явный лимит больше RAM ВМ, и предупреждает, если на ВМ ≥ 8 ГБ в yaml всё ещё 2G.

## Если после подъёма лимита RSS снова растёт

Тогда это не «забыли 2G», а утечка / слишком много сессий:

```bash
ps -o pid,rss,cmd -p <PID_OBPROXY>
mysql -h<obproxy_ip> -P2883 -uroot@sys#<cluster> -p -e "SHOW PROXYCONFIG LIKE '%connection%';"
# на хосте, от пользователя obadmin:
# SHOW PROXYMEMORY OBJPOOL 3\G   — через тот же mysql к :2883
```

Дальше — больше инстансов obproxy (горизонтально) и HAProxy, а не бесконечный `proxy_mem_limited`. Оценка числа узлов: [docs/large-physical-cluster-recommendations.md §5](large-physical-cluster-recommendations.md#5-сколько-obproxy-и-какие-ресурсы).
