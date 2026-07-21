# TPC-C 25000 warehouses — sizing от базы 2400 wh

Оценка для кластера в формате этого репозитория (`config/deploy.yaml` + OBD),
с опорой на:

- результаты `bench/tpcc` (2400 wh → **28040 tpmC**, efficiency **90.8%**);
- `docs/component-vm-sizing.md` и auto-tune в `scripts/lib/vm_profiles.py`;
- [oceanbase-skills](https://github.com/oceanbase/oceanbase-skills): `cluster-management`, `tenant-management`, `testing-and-benchmark`;
- официальные рекомендации OceanBase по дискам (`log ≈ 3× memory_limit`, data+log ≥ 6× memory)
  и ориентир ~50–70 MB/warehouse (сжатый/несжатый TPC-C).

Предположение пользователя: **примерно линейное** масштабирование по CPU/RAM
при scale-out с тем же профилем observer.

## База (факт)

| Параметр | Значение |
|----------|----------|
| Warehouses (run) | 2400 |
| Observers | 3 × 16 vCPU / 64 GB |
| Aggregate | 48 vCPU / 192 GB RAM |
| data_disk / datafile | 930 GB NR / `datafile_size=474G` |
| log_disk / log_disk_size | 930 GB io-m3 / `250G` (~4× `memory_limit`) |
| `memory_limit` | 60G |
| OBProxy | 2 × 4 vCPU / 8 GB |
| tpmC | 28039.94 (max ≈ 2400×12.86 = 30864) |
| Partitions (init) | 18 |
| max-inflight (run) | 500 |

Плотность базы:

- **50 warehouses / vCPU**
- **12.5 warehouses / GB RAM** (VM)

## Цель: 25000 warehouses

| Метрика | Расчёт |
|---------|--------|
| Scale factor `k` | 25000 / 2400 ≈ **10.42** |
| Нужно vCPU | 48 × 10.42 ≈ **500** |
| Нужно RAM | 192 × 10.42 ≈ **2000 GB** |
| Теоретический max tpmC | 25000 × 12.86 ≈ **321 500** |
| Ожидаемый tpmC @ 90% | ≈ **289 000** |

При `unit_num = N/3` и locality `F@zone1,F@zone2,F@zone3` данные шардируются
по unit’ам внутри зоны: **per-node data ≈ baseline**, если число узлов ×k.

## Рекомендуемая конфигурация (вариант A — линейный scale-out)

Тот же shape observer, что на 2400 wh → максимально честная проверка линейности.

Файл: [`deploy-25000wh.yaml`](deploy-25000wh.yaml)

| Роль | Count × shape | Aggregate |
|------|---------------|-----------|
| observer | **33** × 16c / 64G | 528 vCPU (+10%), 2112 GB RAM |
| Logical zones | 11 + 11 + 11 | `unit_num=11` |
| obproxy | **8** × 8c / 16G | запас по коннектам/QPS |
| monitoring | 1 × 8c / 32G | не конкурирует с observer |
| client (отдельно) | 1 × 32c / 64G | import + run |

Per-observer OceanBase (как в базе; `auto_tune: false`, иначе генератор OBD
перезапишет `datafile_size` под 85% data_disk):

```yaml
auto_tune: false
memory_limit: 60G
system_memory: 4G
datafile_size: 474G
log_disk_size: 250G
cpu_count: 16
```

Диски observer: data **930** GB `network-ssd-nonreplicated`, log **372** GB `network-ssd-io-m3`
(достаточно для `log_disk_size=250G`; physical чуть больше базы по log, без лишних 930G).

### Tenant

```sql
CREATE RESOURCE UNIT tpcc_unit
  MAX_CPU 14, MEMORY_SIZE '52G', LOG_DISK_SIZE '180G';

CREATE RESOURCE POOL tpcc_pool
  UNIT = 'tpcc_unit', UNIT_NUM = 11,
  ZONE_LIST = ('zone1', 'zone2', 'zone3');

CREATE TENANT tpcc
  RESOURCE_POOL_LIST = ('tpcc_pool'),
  ZONE_LIST ('zone1', 'zone2', 'zone3'),
  PRIMARY_ZONE = RANDOM,
  LOCALITY = 'F@zone1,F@zone2,F@zone3'
  SET VARIABLES ob_compatibility_mode = 'mysql', ob_tcp_invited_nodes = '%';
```

Запас CPU/RAM на sys + background (как в доке OceanBase: на 32c-ноде tenant часто берёт ~26c).

После создания: `obd cluster tenant optimize <deploy> tpcc -o express_oltp`
(skill `tenant-management`).

### BenchmarkSQL / obtpcc команды

См. [`Commands-25000wh.txt`](Commands-25000wh.txt):

- `--partitions 198` (≈ 6 на observer, как 18/3 на базе)
- import `--threads 128` (при узком месте поднять до 256)
- run `-w 25000 --max-inflight 5200` (500 × 10.42)

## Альтернатива B — меньше узлов, толще ВМ

Файл: [`deploy-25000wh-compact.yaml`](deploy-25000wh-compact.yaml)

| Роль | Count × shape | Aggregate |
|------|---------------|-----------|
| observer | **18** × 32c / 128G | 576 vCPU, 2304 GB |
| zones / unit_num | 6+6+6 / `unit_num=6` | |
| obproxy | 6 × 8c / 16G | |

Per-node data ≈ 2× базы → `data_disk=1116`, `datafile_size=950G`,
`memory_limit=112G`, `log_disk_size=336G`, log disk **372** GB io-m3.

Плюсы: проще оперировать 18 ВМ вместо 33.  
Минусы: сильнее зависит от IOPS/bandwidth одного диска; меньше «чистая» проверка линейности shape’а.

## Что не масштабируется идеально линейно

1. **Латентность базы**: p90/p99 NewOrder уже упирались в ~8192 ms при 90.8% —
   на 25k wh нужны запас по proxy/сети и, при просадке efficiency, +10–15% CPU
   (вариант A уже даёт ~+10%).
2. **OBProxy / HAProxy**: клиентский путь должен быть вне observer; при росте tpmC
   ×10 узкое место часто здесь, а не в storage.
3. **Единая AZ** в текущем `bench/tpcc/deploy.yaml` (`ru-central1-d`): для 33 ВМ
   проверьте квоты YC; для HA лучше разнести logical zones по `ru-central1-a/b/d`
   (потребует доработки multi-zone в provision-скриптах).
4. **Официальный TPC-C (60 дней роста)** здесь не целится: datafile рассчитан на
   короткий performance-run, как на 2400 wh, а не на сертифицированный объём.

## Сводка

| | 2400 wh (факт) | 25000 wh (вариант A) | 25000 wh (вариант B) |
|--|----------------|----------------------|----------------------|
| Observers | 3×16c/64G | **33×16c/64G** | 18×32c/128G |
| Aggregate CPU/RAM | 48 / 192G | 528 / 2112G | 576 / 2304G |
| unit_num | 1 (ожид.) | 11 | 6 |
| Ожидаемый tpmC | 28040 | ~289k @ ~90% | ~289k @ ~90% |

**Стартовать лучше с варианта A** (`deploy-25000wh.yaml`): та же удельная нагрузка
на ядро/ГБ, что уже доказала 90.8% efficiency.
