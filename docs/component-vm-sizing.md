# Профили виртуальных машин для компонентов OceanBase

## Вывод

**Использовать разные типы и размеры ВМ для разных компонентов OceanBase целесообразно.** Компоненты имеют разный профиль нагрузки (CPU, RAM, I/O, отказоустойчивость), и совмещение всего на одинаковых «тяжёлых» observer-ВМ ведёт к переплате за obproxy и к конкуренции monitoring за ресурсы с базой данных.

Скрипты развёртывания используют **`vm_profiles`** — отдельный профиль ресурсов для каждой роли.

## Компоненты и рекомендуемые профили

| Компонент | Отдельная ВМ | Нагрузка | Рекомендуемые ресурсы | Обоснование |
|-----------|--------------|----------|----------------------|-------------|
| **oceanbase-ce** (observer) + **obagent** | Да, N≥3 | CPU/RAM/I/O | 8 vCPU, 32 GB RAM (мин. 4/16) | Основное хранилище и вычисления; горизонтальное масштабирование |
| **obproxy-ce** | Да, 1–N | Низкая, stateless | 2 vCPU, 4 GB RAM | Прокси не хранит данные; дешёвые ВМ, отдельное масштабирование |
| **ob-configserver** | Опционально | Очень низкая | 2 vCPU, 4 GB RAM или colocate | Метаданные конфигурации; по умолчанию на observer-1 |
| **prometheus + grafana** | Да (если включены) | Средняя, disk I/O | 4 vCPU, 16 GB RAM | Сбор метрик не должен конкурировать с observer |
| **OBD** (control) | Локально | Низкая | 4 vCPU, 8 GB (если отдельная ВМ) | oceanbase-skills: достаточно управляющей машины оператора |

## Типы дисков Yandex Cloud

| Назначение | Тип по умолчанию | Почему |
|------------|------------------|--------|
| **Data** (SSTable, `data_dir`) | `network-ssd-nonreplicated` | Данные реплицируются между observer (3 реплики Paxos); отказ одного диска/узла перекрывается репликацией. Максимальная производительность без избыточности на уровне блока |
| **Log** (clog/redo, `redo_dir`) | `network-ssd-io-m3` | Журнал транзакций критичен для восстановления; потеря недопустима. Трёхкратная репликация на уровне YC + высокая скорость |
| **Boot** (observer) | `network-ssd-io-m3` | Бинарники и `home_path`; потеря усложняет восстановление |
| **Boot** (obproxy, monitoring) | `network-ssd` | Достаточно для лёгких компонентов |
| **Monitoring data** | `network-ssd-io-m3` | История метрик; потеря нежелательна |

### Ограничение Yandex Cloud

Для `network-ssd-nonreplicated` и `network-ssd-io-m3` **размер диска должен быть кратен 93 GB**. Скрипт `vm_profiles.py` округляет размеры автоматически.

## Соответствие рекомендациям OceanBase

Источники: [oceanbase-skills/cluster-management](https://github.com/oceanbase/oceanbase-skills), [OceanBase quickstart](https://en.oceanbase.com/quickstart).

| Параметр | Требование OceanBase | Значение по умолчанию | Статус |
|----------|---------------------|----------------------|--------|
| Observer count | ≥3 для HA | 3 | OK |
| Observer vCPU | ≥4 | 8 | OK |
| Observer RAM | ≥16 GB | 32 GB | OK |
| Data disk | ≥100 GB SSD | 558 GB non-replicated | OK |
| Log disk | ≥1× RAM, реком. 3–4× | 279 GB io-m3 (~3× memory_limit) | OK |
| OBProxy | Лёгкий компонент | 2 vCPU, 4 GB | OK |
| Отдельные диски data/log | Рекомендуется (enterprise) | enabled | OK |

Проверка конфигурации:

```bash
python3 scripts/lib/vm_profiles.py validate --config config/deploy.yaml
```
