# Рекомендации по развёртыванию крупного кластера OceanBase

Сводка официальных требований и практических выводов для кластера из **нескольких десятков физических серверов и более**, обслуживающего в основном **одну большую нагруженную базу**.

Целевой узел в расчётах: **128 vCPU (64 физических ядра), 1 ТБ RAM, локальные NVMe**.

Документ опирается на официальную документацию OceanBase V4.2/V4.3 LTS (enterprise и community), knowledge base и связанные гайды. Где документ не даёт точной цифры под эту конфигурацию, это явно отмечено.

---

## Краткий ответ

| Вопрос | Рекомендация |
|---|---|
| Процессов `observer` на сервер | **1** в штатной продакшен-модели. Два — только как NUMA/IO-исключение |
| Ресурсы одному `observer` | **~112–120 vCPU**, **~800–900 ГБ RAM** (`memory_limit`) |
| NVMe | Минимум **3 логические области**; практически **6–8+ NVMe**. Clog отдельно от data |
| RAID | **Не обязателен**. Для NVMe — LVM stripe. Если RAID-контроллер есть — только **write-through** |
| `obproxy` | Минимум **3**, для большой нагрузки — **отдельный пул, 6–12** (по числу соединений) |
| Ресурсы одному `obproxy` | Старт **8–16 vCPU / 16–32 ГБ**; `proxy_mem_limited` **4–8 ГБ** |
| Клиенты при многих proxy | **Да, слой L4 LB** (F5 / SLB / LVS / HAProxy TCP) перед пулом `obproxy` |
| Три ДЦ в одном городе | RTT **0.5–2 мс** (не больше 2 мс), 10 Gbit оптика |
| Majority Paxos между городами | не больше **10 мс** (кластер жив, OLTP уже не «быстрый») |

---

## 1. Топология кластера

OceanBase в продакшене — это кластер, а не набор независимых инстансов.

- Минимум **3 узла / 3 полных реплики**.
- **Один процесс `observer` = один узел**.
- Для десятков машин — **3 Zone с одинаковым числом серверов** (например 12+12+12 или 16+16+16). Нечётное число Zone и равная ёмкость зон — штатная модель Paxos. Это не «рекомендация на вырост»: больше семи Zone кластер физически не забутстрапится, см. [§12](#12-zone-и-bootstrap-почему-ровно-три-zone).
- Одна нагруженная БД = **один user tenant**, растянутый на все узлы:
  - `UNIT_NUM` = числу `observer` в Zone;
  - `PRIMARY_ZONE='RANDOM'`;
  - крупные таблицы **партиционировать**, чтобы лидеры и IO разошлись по кластеру.
- Сеть: **10 Gbit/s**, лучше bond **mode 4 (802.3ad)**.
- **Swap запрещён**.

Память узла официально рекомендуют в диапазоне **256–1024 ГБ**. Отдельно: **настройка памяти OBServer не должна превышать 1 ТБ**, CPU — **не более 512 ядер**. Машина 128 vCPU / 1 ТБ находится на верхней границе по RAM.

Источники:

- [准备服务器 V4.3.5](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000002013492)
- [规划 OceanBase 集群部署](https://www.oceanbase.com/docs/community-observer-cn-10000000000449659)
- [Preparations before deployment](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_02_deploy_oceanbase_database/preparation_before_deployment)

---

## 2. Сколько `observer` на сервер 128 vCPU / 1 ТБ

Официально для продакшена:

> В производственной среде на каждой машине запускается один процесс `observer`, поэтому одна машина соответствует одному узлу.

Несколько процессов на одной машине в документации — это **учебный/тестовый** приём (разные порты и каталоги), не продакшен.

**Рекомендация: 1 `observer` на физический сервер.**

Почему не «нарезать» 128 vCPU на 2–4 процесса по умолчанию:

- изоляция CPU/RAM/IO строится на **tenant + resource unit**, а не на нескольких процессах на хосте;
- для одной большой БД второй процесс на той же машине даёт два unit’а, делящих одни и те же диски и NIC, усложняя failover и NUMA, но не увеличивая полезную ёмкость хоста;
- лимит «память observer ≤ 1 ТБ» рассчитан на **один процесс на такую машину**.

### Когда имеют смысл 2 процесса

Это исключение из материалов ODM/NUMA, не базовый гайд:

- AMD / Hygon / ARM с **включённым NUMA** (для Intel официальный KB советует NUMA **выключить**, разница ≤ 3%);
- один процесс не вытягивает IO машины (в ODM прямо сказано, что на больших NUMA-хостах один `observer` может не насытить диски).

Тогда: 2 процесса, каждый привязан к своему NUMA-node (~64 vCPU / ~400–450 ГБ), **свои** `data_dir` / `clog_dir` / порты. Для Intel с `numa=off` это обычно не нужно.

Источники:

- [规划 OceanBase 集群部署](https://www.oceanbase.com/docs/community-observer-cn-10000000000449659)
- [准备服务器](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000002013492)
- [部署时要不要打开 NUMA](https://www.oceanbase.com/knowledge-base/oceanbase-database-1000000000210146)
- [ODM: NUMA Aware и несколько OB-инстансов](https://www.oceanbase.com/docs/common-odm-doc-cn-1000000002370324)

---

## 3. Ресурсы одному `observer`

Официальные ориентиры:

- продакшен: **≥ 4 ядра, рекомендуется ≥ 32**;
- память **≥ 16 ГБ**, для долгой эксплуатации **≥ 32**, рекомендуется **256–1024 ГБ**;
- `cpu_count=0` — автодетект; CPU для тенантов **декларативный**, память и диск — **эксклюзивно преаллоцируются**;
- `memory_limit_percentage` по умолчанию **80%** (запас OS). На больших машинах допускается **90–95%**.

Практическая раскладка на 128 vCPU / 1 ТБ:

| Параметр | Значение | Зачем |
|---|---|---|
| Процессов | 1 | штатная модель |
| `cpu_count` | **112–120** (оставить 8–16 vCPU OS/irq/мониторинг) | не отдавать 100% CPU процессу |
| `memory_limit` | **800–900 ГБ** (80–90% от 1 ТБ) | не упираться в 1 ТБ и оставить запас OS |
| `system_memory` | по формуле доки для `memory_limit > 64G` | внутренний tenant 500 |
| Порты | 2881 / 2882 | sql / rpc |

Не ставить `memory_limit=1T` вплотную: Clog, page tables, OS, obagent/мониторинг живут рядом. Официальный потолок «не больше 1 ТБ» — про **настройку observer**, не про «отдать процессу всю планку».

Дополнительно:

- `merge_thread_count`: для 64C-сервера в KB рекомендуют около **10** (не больше 50).
- На Intel — NUMA off; на AMD/Hygon/ARM — NUMA on, `kernel.numa_balancing=0`, `vm.zone_reclaim_mode=0`, `vm.swappiness=0`.

Источники:

- [Preparations before deployment](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_02_deploy_oceanbase_database/preparation_before_deployment)
- [View the resource usage](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_02_deploy_oceanbase_database/view-resources)
- [日常检查配置](https://www.oceanbase.com/knowledge-base/oceanbase-database-20000001032)

---

## 4. Диски NVMe и RAID

### 4.1. Три области хранения

Официально для enterprise: **три раздельные области** на разных дисках, иначе IO Clog, datafile и syslog конкурируют.

| Точка монтирования | Назначение | Размер |
|---|---|---|
| `/home` (часто `/home/admin/oceanbase`) | бинарники + **syslog** | 100–300 ГБ, лучше ≥ 200 ГБ на 7+ дней логов |
| `/data/log1` | **Clog** (redo/Paxos) | **3–4 × `memory_limit`** |
| `/data/1` | **datafile / sstable** | по объёму данных |

Для узла при `memory_limit` ≈ 850 ГБ:

- Clog ≈ **2.5–3.4 ТБ** (лучше закладывать **4 ТБ**);
- суммарно data+log **> 6 × memory_limit** (~5+ ТБ минимум, реально намного больше под данные и compaction);
- FS: **ext4** до 16 ТБ, **xFS обязателен**, если data > 16 ТБ.

Clog occupancy: при **80%** начинается recycle, при **95%** узел **останавливает запись**. Поэтому 3–4× памяти — рабочий запас между major compaction, а не «на вырост».

Clog — плотные **4 KB** append с низкой и ровной задержкой; data — в основном **2 MB** SSTable. Их нельзя сажать на один носитель.

### 4.2. Сколько физических NVMe

Документ **не фиксирует** точное число дисков. Два официальных ориентира:

1. «Если ресурсов достаточно — **три SSD**».
2. Пример инициализации NVMe: **6 × 3.5 ТБ NVMe**, LVM stripe, на машине **512 ГБ RAM** → LV log 2 ТБ + LV data остаток.

Для 1 ТБ RAM и одной тяжёлой БД разумная комплектация:

- **1 диск OS/install** (SATA/NVMe 0.5–1 ТБ) под `/` и `/home`;
- **1–2 NVMe только под Clog** (~4 ТБ суммарно);
- **остальные NVMe (часто 4–8)** под data через **LVM stripe**.

Итого типично **6–8 NVMe** на узел (плюс системный диск). Меньше трёх физических носителей в продакшене не стоит. Больше 8 — если нужен объём/IOPS под compaction и бэкап.

### 4.3. Нужен ли RAID

**Обязательного RAID нет.** Отказоустойчивость диска закрывается **трёхрепличным Paxos**, не RAID. При IO error на Clog процесс `observer` **выходит**.

Официально:

- RAID **поддерживается**;
- если есть RAID cache — только **write-through** (особенно для log), иначе dirty cache при отказе BBU ломает согласованность Clog;
- для NVMe рекомендуют **LVM**, не аппаратный RAID;
- при нескольких одинаковых PV: `lvcreate ... --stripes=N --stripesize=...`
  - ext4: stripesize **128K**;
  - xfs: **1024K**.

Это по сути **software RAID0 (stripe)** ради пропускной способности, не ради надёжности.

Пример из официального KB (6 NVMe, xfs):

```bash
pvcreate /dev/nvme[012345]n1
vgcreate -y vgob /dev/nvme[012345]n1
lvcreate -n log  -L 2T        vgob --stripes=6 --stripesize=1024 -y
lvcreate -n data -l 100%FREE  vgob --stripes=6 --stripesize=1024 -y
mkfs.xfs -f -n ftype=1 -l su=4k /dev/vgob/data
mkfs.xfs -f -n ftype=1 -l su=4k /dev/vgob/log
```

Для тяжёлого OLTP лучше **физически отделить Clog от data** (не класть оба LV на один striped VG), даже если пример KB делает именно так.

| Область | RAID/LVM | Комментарий |
|---|---|---|
| Data (`/data/1`) | **LVM stripe (аналог RAID0)** по всем data-NVMe | Штатный приём. Локальная избыточность не нужна |
| Clog (`/data/log1`) | **Отдельный диск без RAID** или **RAID1 из 2 NVMe** | Главное — низкая и ровная latency |
| Install/syslog | отдельный диск, RAID не обязателен | |
| RAID5/6/50 | **не использовать** ни для data, ни для log | write penalty и нестабильный latency убивают Clog |
| RAID10 на весь сервер | избыточен при 3 репликах, режет ёмкость | имеет смысл только если политика железа требует локальный диск-HA |

Если контроллер всё же есть: **WB/write-back выключить**, cache = **writethrough**.

Источники:

- [规划磁盘](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000510460)
- [磁盘初始化 (NVMe + LVM)](https://www.oceanbase.com/knowledge-base/oceanbase-database-1000000003564245)
- [Clog 对本地存储的要求](https://www.oceanbase.com/knowledge-base/oceanbase-database-20000000011)
- [Factors affecting performance](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_03_test_oceanbase_database/influence_factor)

---

## 5. Сколько `obproxy` и какие ресурсы

Официально:

- продакшен: **не меньше двух**, в таблице подготовки серверов — **3**, можно соразмещать с `observer`;
- при большом объёме бизнеса — **отдельные серверы**;
- функциональный минимум: **4 CPU / 8 ГБ / 200 ГБ**;
- в простое процесс лёгкий (~0.7 CPU / ~100 МБ);
- **один `obproxy` на машину**, порт **2883**;
- `client_max_connections` по умолчанию **8192** (до 65535);
- `proxy_mem_limited` — потолок RSS, при превышении процесс **сам выходит**; в KB при запасе RAM советуют **8 ГБ**;
- ODP **stateless и горизонтально масштабируется без лимита**.

Для кластера из десятков `observer` и одной горячей БД **не ставить proxy на те же машины**, что и `observer`: они дерут CPU/сеть у лидеров.

Оценка числа экземпляров (в доке нет формулы «N proxy на M observer»):

```text
N ≈ ceil(пиковые клиентские сессии / 6000…8000) + запас HA
```

Практично:

- старт HA: **3** dedicated (по одному в Zone/AZ);
- десятки тысяч сессий / высокий QPS: **6–12**;
- дальше — добавлять узлы, а не раздувать один процесс.

Ресурсы **каждому** dedicated `obproxy` на нагруженном контуре:

| | Минимум из доки | Рабочий продакшен | Тяжёлый (много сессий, SSL, большой result set) |
|---|---|---|---|
| vCPU | 4 | **8–16** | 16–32 |
| RAM | 8 ГБ | **16–32 ГБ** | 32–64 ГБ |
| `proxy_mem_limited` | default ~0.8–2 ГБ | **4–8 ГБ** | 8–16 ГБ |
| Диск | 10–200 ГБ | SSD под логи | |
| Сеть | 1 Gbit | **10 Gbit** | 10/25 Gbit |

CPU растёт с QPS, RAM — с числом соединений. 4C8G — это «процесс встанет», не «выдержит большую БД».

Источники:

- [准备服务器 — ODP 4C8G / 3 узла](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000002013492)
- [代理概述 V4.3.5](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000002014022)
- [Deploy in production](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_02_deploy_oceanbase_database/deploy-production-environment)
- [proxy_mem_limited KB](https://www.oceanbase.com/knowledge-base/oceanbase-database-proxy-1000000000210042)

---

## 6. Клиентские подключения при большом числе `obproxy`

Официальная схема:

```text
Приложение  →  L4 load balancer (F5 / SLB / LVS / HAProxy)
            →  пул stateless obproxy (2883)
            →  нужный observer (лидер партиции)
```

**Дополнительный слой балансировки нужен**, как только `obproxy` больше одного и клиенты не умеют сами обходить список адресов. Сам `obproxy` маршрутизирует SQL на правильный `observer`; он **не** заменяет VIP/health-check перед собой.

| Вариант | Оценка |
|---|---|
| **F5 / аппаратный L4** | Штатный пример в enterprise-доке: Virtual Server **Performance (Layer 4)**, **без persistence**, Idle Timeout **≥ 1800 с**, health-check порта 2883 |
| **Облачный SLB/CLB** | Тот же L4 TCP, официально упоминается вместе с F5 |
| **LVS (ipvs) + Keepalived** | Нормальный on-prem аналог, меньше extra hop latency |
| **HAProxy `mode tcp`** | Допустимо. Для SQL-сессий официальные/партнёрские гайды часто берут **`leastconn`** |
| DNS round-robin | В доке прямо **не рекомендуется**: нет health-check и снятия мёртвого узла |
| JDBC/драйвер с несколькими host | Можно обойтись без LB, если все клиенты это умеют |

Важные детали:

- только **L4/TCP**, не HTTP reverse proxy;
- **без session affinity** в официальном F5-примере — `obproxy` сам держит сессию к бэкенду;
- idle timeout LB **не короче**, чем у приложений и `obproxy`;
- клиенты смотрят в **один VIP:порт**, а не в список из 20 proxy;
- прямые подключения к `observer:2881` в продакшене не рекомендуются.

HAProxy имеет смысл, если нет F5/SLB. Для очень большого числа proxy и высокой сессионной нагрузки **LVS/F5 обычно предпочтительнее** HAProxy.

Замечание по репозиторию: пример [`docs/haproxy-obproxy-tcp-lb.md`](haproxy-obproxy-tcp-lb.md) использует `balance source` (sticky по IP). Для длинных SQL-сессий официальные/партнёрские материалы чаще рекомендуют `leastconn`. Sticky имеет смысл для диагностики и коротких пулов; для тяжёлого OLTP с неравномерными клиентами — `leastconn`.

Источники:

- [代理概述](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000002014022)
- [OBProxy 接入 F5](https://www.oceanbase.com/docs/enterprise-oceanbase-ocp-cn-10000000000378771)
- Kunpeng / HAProxy: TCP, `leastconn`, health-check 2883

---

## 7. Сетевые задержки при размещении в 3 дата-центрах

Официальный ориентир зависит от того, **в одном городе эти три ДЦ или в разных**. Для одного Paxos-кластера с тремя полными репликами OceanBase рассчитан на **три дата-центра в одном городе**.

### 7.1. Цифры из документации

| Связь | Рекомендация | Жёсткий ориентир |
|---|---|---|
| Три ДЦ **в одном городе** (Zone = ДЦ) | **0.5–2 мс** между машинными залами | **не больше 2 мс** |
| Путь, по которому собирается **Paxos majority между городами** | держать как можно ниже | **не больше 10 мс** |
| Синхронизация Clog | — | **не больше 1 с** (потолок протокола, не целевой RT) |
| Арбитраж (`obarb`) до узлов кластера | — | **односторонняя ≤ 800 мс** |
| Расхождение часов между `observer` | NTP/chrony | **< 100 мс** (иначе выборы ломаются) |

Все межДЦ-линки — **выделенное 10 Gbit/s оптическое волокно / DWDM**, не интернет.

Документ говорит «网络延迟» без уточнения RTT vs one-way. На практике 0.5–2 мс — типичный **round-trip между ДЦ одного города**. Для арбитража явно написано «单程» (one-way).

### 7.2. Три ДЦ в одном городе — штатная схема

Это **同城三中心**: один кластер, **3 полных реплики, 3 Zone, по Zone на ДЦ**.

Почему 2 мс критичны: транзакция коммитится только после majority Clog. При трёх репликах нужно подтверждение **минимум ещё одного ДЦ**. Commit latency приложения ≈

```text
локальный fsync Clog  +  1× RTT до ближайшего из двух других ДЦ  +  RPC 2PC/GTS
```

KB: 2PC = **1 задержка лога + 2 RPC**, плюс GTS на strong-consistent read.

Следствия:

- Целиться лучше в **≤ 1 мс RTT** между всеми тремя парами ДЦ, а не в потолок 2 мс.
- **Jitter важнее среднего.** Хвост 5–10 мс даёт срывы lease, clog traffic amplification и ложные выборы.
- Потеря пакетов на Paxos-пути почти не терпится.
- Клиенты и `obproxy` должны жить **в тех же трёх ДЦ (или рядом)**, с L4 LB в каждом.
- Внутри ДЦ подразумевается leaf-spine **≪ 0.5 мс**. Узлы одной Zone не размазывать по двум ДЦ.

`PRIMARY_ZONE=RANDOM` при такой топологии нормален: лидеры размазаны, каждый коммит платит ~1 RTT до соседнего ДЦ.

### 7.3. Три ДЦ не в одном городе

Три полных реплики «по одной в каждый город» **не являются** рекомендуемой OLTP-топологией. Majority = 2 из 3, значит **каждый коммит ждёт другой город**.

Официальные альтернативы:

**Два города, три ДЦ (两地三中心, обычно 5 реплик)**

- Город A: 2 ДЦ × 2 реплики, город B: 1 реплика (или log-only / arbitration).
- Между двумя ДЦ города A: те же **0.5–2 мс**. Majority в норме собирается **внутри города**.
- До города B: до **10 мс**, но это путь **третьей** реплики, не критический путь коммита.
- Если падает один ДЦ города A, majority уезжает через город B, и RT лога скачет примерно до **5–10 мс**.

Именно из-за этого скачка RT в 4.1+ появился **Arbitration Service**: третий ДЦ голосует, **не тащит Clog**. Для арбитража one-way **≤ 800 мс** — это про liveliness, не про latency транзакции. Бандвидт до арбитража минимальный (≥ 20 Mbps). Community Edition арбитраж **не поддерживает**.

**Три города, пять центров (三地五中心)**

- Пять реплик: **2+2+1**.
- Два города должны быть географически близко, иначе majority (3 из 5) постоянно ходит через дальнюю дистанцию.
- Для тяжёлой одной БД это городской DR ценой постоянно более высокого commit RT.

**Асинхронный standby между кластерами**

- Если три ДЦ далеко и нужен только DR, а не RPO=0 на весь контур: отдельный кластер в дальнем ДЦ и асинхронная репликация redo. RPO > 0 при городской катастрофе.

### 7.4. Что ещё ломает «укладываемся в 2 мс»

1. **Часы.** Election считает просроченным сообщение при **до 100 мс** рассинхрона часов **и до 200 мс** RPC. NTP/chrony на все `observer` обязателен. `./scripts/deploy.sh prepare` ставит chrony (`scripts/lib/prepare-chrony.sh`): DHCP NTP Yandex Cloud (option 42), иначе `yandex_cloud.ntp_servers` или публичные серверы из [документации Compute Cloud](https://yandex.cloud/en/docs/compute/tutorials/ntp).
2. **Полоса.** Clog, миграции реплик и compaction едут по тем же линкам. Нехватка межДЦ-bandwidth даёт отставание follower.
3. **Не мерить только ping.** ICMP ≠ RPC 2882. Смотреть `CLOG_SYNC` / `TRANS_COMMIT_LOG_SYNC_RT` в OCP и `EVENT` в `v$sql_audit` (`sync rpc`).
4. **Не класть majority на публичный интернет** и не через NAT с большим conntrack.

### 7.5. Практический вывод по сети

Если цель — одна нагруженная БД с RPO=0 и низким RT:

- Три ДЦ **в одном метрополитене**, полноценная оптика.
- RTT **все пары ДЦ ≤ 2 мс**, лучше **0.5–1 мс**, без длинного хвоста.
- По Zone на ДЦ, равное число машин, 3 full replica.
- `obproxy` + VIP в каждом ДЦ, клиенты ходят в ближайший.

Если третий ДЦ в другом городе (50+ км / 5+ мс):

- **не** делать 1+1+1 full replica как основной OLTP;
- либо **2+2 в ближних ДЦ + 1 (log/arb) в дальнем**, либо primary-кластер в двух близких ДЦ и **асинхронный** standby в третьем.

Порог **10 мс** — это «кластер ещё способен синхронизировать majority», а не «OLTP будет быстрым».

Источники:

- [容灾架构及容灾级别 V4.2.5](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000001499785)
- [高可用部署方案](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000033158)
- [同集群跨机房部署对网络的要求](https://www.oceanbase.com/knowledge-base/oceanbase-database-20000000074)
- [选举为何依赖于时钟同步](https://www.oceanbase.com/knowledge-base/oceanbase-database-20000000165)
- [仲裁: one-way ≤ 800 ms](https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000000218705)

---

## 8. Модель данных и нагрузка для одной большой БД

1. **Партиции + `PRIMARY_ZONE=RANDOM`**, иначе весь OLTP сядет на лидеров одной Zone.
2. **Локальные индексы**, если запросы несут ключ партиции; глобальные уникальные индексы на горячем OLTP дороги (distributed write).
3. Table group со `SHARDING` — чтобы связанные таблицы жили рядом и не плодить distributed join.
4. Не колоцировать OCP/MetaDB с этим кластером. OCP HA — отдельные узлы + свой LB.
5. На одном `observer` не держать слишком много тенантов (KB: не больше ~50); в данной постановке тенант один — это плюс.

Источник: [Factors affecting performance](https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_03_test_oceanbase_database/influence_factor)

---

## 9. Системные параметры (кратко)

Из официального гайда по факторам производительности:

| Категория | Параметр | Рекомендация |
|---|---|---|
| Memory | `vm.swappiness` | `0` |
| Memory | `vm.max_map_count` | `655360` |
| AIO | `fs.aio-max-nr` | `1048576` |
| Network | `net.core.somaxconn` | `2048` |
| Network | `net.ipv4.tcp_tw_reuse` | `1` |
| Network | `net.ipv4.tcp_slow_start_after_idle` | `0` |

Syslog на том же диске, что data/clog, опасен. Ограничить:

```sql
ALTER SYSTEM SET syslog_io_bandwidth_limit = '10M';
ALTER SYSTEM SET enable_syslog_recycle = true;
ALTER SYSTEM SET max_syslog_file_count = 1000;
```

---

## 10. Сводка «что купить / как нарезать» на узел 128 vCPU / 1 ТБ

| Слой | Решение |
|---|---|
| Процесс | 1 × `observer` |
| CPU процессу | 112–120 vCPU (`cpu_count`) |
| RAM процессу | 800–900 ГБ (`memory_limit`) |
| OS/install | отдельный диск 0.5–1 ТБ, `/home` 100–300 ГБ |
| Clog | 1–2 NVMe, **~4 ТБ**, физически отдельно от data; RAID не обязателен, RAID1 допустим |
| Data | 4–8 NVMe, LVM stripe, xfs если > 16 ТБ |
| RAID-контроллер | не нужен; если есть — write-through |
| Сеть | 2×10 Gbit bond mode 4 |
| `obproxy` | не на этой машине |
| Zone | один ДЦ = одна Zone; 3 ДЦ одного города, RTT 0.5–2 мс |

Пул доступа:

| Слой | Решение |
|---|---|
| `obproxy` | 3–12 dedicated, 8–16 vCPU / 16–32 ГБ, по одному (или больше) в каждом ДЦ |
| Перед proxy | L4 VIP: F5 / SLB / LVS, иначе HAProxy TCP |
| Клиент | один VIP:2883 |

---

## 11. Отличие от профилей ВМ в этом репозитории

[`docs/component-vm-sizing.md`](component-vm-sizing.md) описывает **Yandex Cloud ВМ** для автоматизации OBD (типично 8 vCPU / 32 ГБ, облачные диски). Этот документ — про **крупный on-prem / bare-metal** контур. Цифры не взаимозаменяемы:

- в облаке data- и log-диски могут быть `network-ssd-nonreplicated`, потому что тройная репликация уже на уровне OceanBase (majority persist Paxos);
- на физике data — локальные NVMe + LVM stripe, без аппаратного RAID;
- 2 vCPU / 4 ГБ на obproxy достаточно для стенда; для большой нагруженной БД — на порядок больше.

---

## 12. Zone и bootstrap: почему ровно три Zone

Zone — это не «метка узла», а единица репликации. При `alter system bootstrap` OceanBase создаёт sys-тенант с locality `F{1}@zone1, F{1}@zone2, …` — **по одной full-реплике на каждую Zone** ([`ObBootstrap::gen_multiple_zone_deployment_sys_tenant_locality_str`](https://github.com/oceanbase/oceanbase/blob/master/src/rootserver/ob_bootstrap.cpp)), и `paxos_replica_num` лог-стрима sys-тенанта равен числу Zone. Размер Paxos-группы жёстко ограничен `OB_MAX_MEMBER_NUMBER = 7` (`deps/oblib/src/lib/ob_define.h`), поэтому **больше семи Zone — bootstrap падает**: `ObMemberList::add_member` возвращает `OB_SIZE_OVERFLOW`.

Как это выглядит в логе `obd cluster start`:

```text
oceanbase bootstrap ok
[ERROR] OBD-5000: alter system modify zone zone1 set idc = %s execute failed
...
[ERROR] OBD-5000: alter system modify zone zone30 set idc = %s execute failed
[ERROR] OBD-5000: alter user "root" IDENTIFIED BY %s execute failed
```

Или, в более новых плагинах OBD, без пачки OBD-5000 на экране:

```text
Connect to observer 10.130.0.33:2881 ok
oceanbase bootstrap ok
obshell start ok
obshell program health check ok
obshell bootstrap -
```

Читается это не буквально:

- `%s` — плейсхолдер параметра в шаблоне SQL; OBD печатает шаблон, а не подставленное значение (`EC_SQL_EXECUTE_FAILED` в `_errno.py`);
- **`oceanbase bootstrap ok` — это надпись спиннера**, а не результат SQL. Сам `alter system bootstrap` выполняется с `exc_level='verbose'`, поэтому его ошибка видна только в `~/.obd/log/obd` / `obd display-trace`;
- ошибки на `modify zone … set idc` и `alter user "root"` — это следствия: они идут по тому же соединению уже после неудачного bootstrap;
- OBD не прерывается на первой ошибке (у `Cursor.execute` значение по умолчанию `raise_exception=False`, и `raise_cursor` его не переопределяет), а затем либо крутит `select * from oceanbase.__all_server`, либо стартует obshell и зависает на **`obshell bootstrap -`**. Плагин `obshell_bootstrap` опрашивает `/api/v1/info` на всех observer (до 200×3 с) и при identity `TAKE_OVER_MASTER` вызывает `wait_dag_succeed` **без таймаута**. На незабутстрапленном кластере DAG take-over не завершается — отсюда «зависший» `obd cluster start`.

Из-за этого число OBD-5000 равно числу Zone: по строке на Zone — удобный индикатор реальной топологии кластера. Если OBD-5000 нет на экране, смотрите unique zone в `generated/obd-cluster.yaml` и в `~/.obd/cluster/<deploy>/` — `obd cluster start` читает **зарегистрированный** конфиг, не yaml из `generated/`.

Раскладка в этом репозитории (`scripts/lib/ob_zones.py`): ровно три Zone, observer распределяются по кругу — `1,4,7…` → `zone1`, `2,5,8…` → `zone2`, `3,6,9…` → `zone3`. Тем же правилом пользуются `scripts/05-scale-out.sh` и `scripts/06-recover-observer.sh`, поэтому расширение и замена узла попадают в правильную Zone. Число observer стоит держать кратным трём: `UNIT_NUM` тенанта одинаков для всех Zone, и «лишние» узлы в перекошенной Zone останутся без unit — генератор конфигурации предупреждает об этом. `./scripts/deploy.sh deploy` заново генерирует yaml; `04-deploy-cluster.sh` отказывается стартовать, если уникальных zone больше семи.

### Staged-развёртывание крупных кластеров

Подготовка инфраструктуры не меняется: `provision` и `prepare` создают диски, ВМ и каталоги сразу для всех observer. Меняется только порядок OBD:

1. Из полного `generated/obd-cluster.yaml` строится `generated/obd-seed.yaml` с `server1..server3`, по одному observer в каждой Zone.
2. OBD выполняет `cluster deploy/start` seed-кластера и дожидается успешного OBShell take-over.
3. Оставшиеся узлы добавляются `obd cluster scale_out` раундами по три: один в `zone1`, один в `zone2`, один в `zone3`. Каждый вызов OBD получает YAML только одного нового observer; три вызова выполняются последовательно.
4. Для каждого пакета сначала добавляется `oceanbase-ce`, затем отдельным вызовом — `obagent`. Перед `obd cluster start <deploy> -c obagent -s <ip>` создаются каталоги `home_path/{run,bin,lib,conf,log}`: OBD не вызывает `init` при scale-out obagent, и без `run/` старт заканчивается `fetch_admin_lock_failed`. Без этого следующего `scale_out` падает на `status_check` (`obagent is not running`).

YAML каждого `scale_out` содержит только отсутствующие узлы и не повторяет `global` исходного кластера. В named-настройки нового observer добавляется `rootservice_list` трёх seed-узлов. Это необходимо для OBD 3.5.3 с плагином OceanBase 4.6: `start_pre.py` добавляет вычисленный `obconfig_url` только при `need_bootstrap=True`, хотя для scale-out выставляется `need_bootstrap=False`. Без явного списка новый observer запускается без источника RootService (`server_list=[]`).

Перед `scale_out` целевой observer очищается (процессы, `home_path`, data/redo). Seed-узлы не трогаются. Неудачный `ADD SERVER` оставляет запущенный observer: следующий `scale_out` без очистки видит pid и не подставляет `rootservice_list`.

`ALTER SYSTEM ADD SERVER` в OBD идёт с дефолтным `ob_query_timeout=10s`. На 6-м и последующих узлах SQL часто не укладывается: `OBD-5000` ровно через ~10 с (`ERROR 4012 … 10000000(us)`), хотя `Start observer ok` уже был. Если после этого сразу повторить `ADD SERVER` по **тому же** запущенному observer — `ERROR 4179 add non-empty server`: процесс успел записать clog и для кластера уже «не пустой», хотя в `DBA_OB_SERVERS` его нет.

Нельзя: `ADD SERVER` без wipe; `06-recover-observer.sh --temporary` (`START SERVER` бесполезен, узла нет в кластере). Нужно остановить observer **только на этом IP**, очистить `home_path` / `data_dir` / `redo_dir`, поднять процесс заново и сразу `ADD SERVER` с `ob_query_timeout=3600s`. Seed (`observer-1..3`) не трогать.

```bash
# server6 / 10.130.0.37 — не seed 10.130.0.34/21/28 и не уже ACTIVE .15/.8
./scripts/join-empty-observer.sh 6 --yes
# или
./scripts/deploy.sh join-observer 10.130.0.37 --yes
```

После `STATUS=ACTIVE` продолжайте `./scripts/deploy.sh deploy` (obagent для этого узла и оставшиеся observer).

Ручной эквивалент, только `10.130.0.37`:

```bash
# только 10.130.0.37 — не seed 10.130.0.34/21/28
ssh obadmin@10.130.0.37
pkill -f /home/obadmin/observer/bin/observer || true
pkill -f /home/obadmin/observer/bin/obshell || true
rm -rf /home/obadmin/observer
find /ob-data/1 /ob-log/1 -mindepth 1 -maxdepth 1 -exec rm -rf {} +
mkdir -p /ob-data/1 /ob-log/1
```

Затем с jump host:

```sql
SET GLOBAL ob_query_timeout = 3600000000;
```

```bash
obd cluster start ob-yc-prod -c oceanbase-ce -s 10.130.0.37
# сразу, пока узел empty:
obclient -hob-yc-prod-observer-1 -P2881 -uroot@sys -p'ChangeMe1!' -e \
  "SET SESSION ob_query_timeout=3600000000; ALTER SYSTEM ADD SERVER '10.130.0.37:2882' ZONE zone3;"
```

Либо `obd cluster scale_out ob-yc-prod -c generated/staged-scale-out/01-03-server6-oceanbase.yaml` после wipe (если OBD ещё не зарегистрировал server6).

План observer сравнивается с **ACTIVE в `DBA_OB_SERVERS`**, не только с `~/.obd/cluster/<deploy>/config.yaml`. OBD может записать server6 в свои метаданные после `Start observer ok`, даже если `ADD SERVER` не прошёл (4179 / timeout). Повторный `./scripts/deploy.sh deploy` тогда раньше считал узел уже добавленным и переходил к OCP. Теперь leftover IP снова попадают в scale-out (wipe + ADD SERVER). Seed уже ACTIVE — полный `obd cluster start` пропускается, чтобы не поднимать dirty observer.

`Failed to install … oceanbase-ce-utils` при export-to-ocp — WARN. Если в логе есть `takeover task successfully submitted to ocp`, регистрация в OCP прошла (задача в UI, например `/task/22`).

Причина staged-порядка — дефект ExecutorPool OBShell 4.2.5.0–4.5.1.0: локальный take-over DAG крупного уже работающего кластера создаёт десятки READY-подзадач, а bounded-очередь и mutex могут взаимно заблокировать producer и workers. Это не ограничение OceanBase на число observer. После take-over штатные cluster DAG хранятся и координируются иначе, поэтому дальнейший `scale_out` является поддерживаемым способом собрать крупный кластер.

Кластер, уже развёрнутый со схемой «Zone на observer», починить правкой конфигурации нельзя: locality sys-тенанта фиксируется на bootstrap. Нужен `obd cluster destroy <deploy> -f` и повторный `deploy` (данных там всё равно нет — bootstrap не прошёл).

### Если SQL уже жив, а спиннер на `obshell bootstrap -`

Это другой случай. Пример с 30 observer (`ob-yc-prod`): `generated/obd-cluster.yaml` и `~/.obd/cluster/<deploy>/config.yaml` — `zone1=10, zone2=10, zone3=10`; `__all_server` — 30× ACTIVE; bootstrap SQL есть в логе:

```text
alter system bootstrap REGION "deault_region" ZONE "zone1" SERVER "10.130.0.33:2882", ...
```

(`deault_region` — опечатка OBD, на bootstrap не влияет.)

obshell при этом бывает в двух рабочих состояниях (оба — **не destroy**):

1. **Все агенты `TAKE OVER FOLLOWER`, БД `ocs` нет.** `/api/v1/info` → `"identity":"TAKE OVER FOLLOWER"`; в `obshell.log`: `Unknown database 'ocs'`, `The current database is not OCS`, lock/unlock take-over **без** `create take over dag`. Master не выбран — OBD крутит опрос всех IP (на 30 узлах один круг — десятки секунд, 200 кругов — час+). Дальше: Ctrl+C у `obd cluster start`, остановить только процессы obshell, поднять obshell **с одного** observer (чтобы он стал master и создал `ocs`), затем на остальных.

2. **Есть `TAKE OVER MASTER`, БД `ocs` уже есть.** Пример `ob-yc-prod`: 29× FOLLOWER + 1× MASTER (`10.130.0.44`, последний observer в inventory). Плагин нашёл master и зашёл в `wait_dag_succeed` **без таймаута** — спиннер так и стоит на `obshell bootstrap -`. SSH только на observer 1–3 показывает ранний follower-лог (`Unknown database 'ocs'` до создания схемы) и **не** показывает master. Смотреть DAG и лог **на master**. Голый `curl` к `/api/v1/task/dag/maintain/agent` даёт **400 Request.Header.NotFound**: нужен заголовок `X-OCS-Header` (гибридное RSA-шифрование). Diagnose подписывает запрос сам; вручную:

```bash
python3 scripts/lib/obshell_ocs.py dag --host 10.130.0.44 --port 2886 \
  --password '<ocp.root_password>' --password ''
# SSH на master: /home/obadmin/observer/log_obshell/obshell.log
# create take over dag / SUCCEED / FAILED / identity CLUSTER AGENT
# SQL: SHOW TABLES FROM ocs;
```

Не убивать obshell сразу на всех узлах: master уже выбран, массовый restart собьёт DAG. Если DAG `SUCCEED` (или identity стал `CLUSTER AGENT`) — `obd cluster start <deploy> -c obproxy-ce,obagent,ocp-server-ce`. Если DAG `FAILED` или часами `RUNNING` без прогресса — перезапустить **только** obshell master-узла.

Dashboard `:2886` на FOLLOWER отвечает `'<ip>:2886' is 'TAKE OVER FOLLOWER', instead of 'CLUSTER AGENT', does not support this operation` — это отказ UI, не сбой observer. Кластерные операции UI делает только `CLUSTER AGENT`. Пока take-over идёт, открывайте `http://<MASTER_IP>:2886/` и смотрите лог/DAG на master, а не кнопки на случайном follower (`10.130.0.13` в примере — FOLLOWER).

OBD-плагин `obshell_bootstrap` засчитывает только `TAKE OVER MASTER` (далее `wait_dag_succeed`) и `CLUSTER AGENT`. `TAKE OVER FOLLOWER` он игнорирует.

Сбор признаков:

```bash
./scripts/deploy.sh diagnose
# Trace ID start (не deploy): строка alter system bootstrap в ~/.obd/log/obd
obd display-trace <uuid-из-этой-строки>
```

Сбор признаков на управляющей машине:

```bash
./scripts/deploy.sh diagnose
./scripts/diagnose-obd-start.sh e9d653a8-abc7-11f1-847c-d00d5a7271af
# только локальные yaml/логи, без SSH:
./scripts/diagnose-obd-start.sh --local
```
