# OceanBase Agent Skills

Скрипты развёртывания следуют рекомендациям из [oceanbase/oceanbase-skills](https://github.com/oceanbase/oceanbase-skills).

## Установка skills для AI-ассистента

```bash
# Рекомендуемый способ
npx skills add oceanbase/oceanbase-skills --skill oceanbase-deploy

# Или вручную
git clone https://github.com/oceanbase/oceanbase-skills.git
mkdir -p .cursor/skills
cp -R oceanbase-skills/skills/oceanbase-deploy .cursor/skills/
```

## Используемые skills

| Skill | Применение в этом репозитории |
|-------|-------------------------------|
| `oceanbase-deploy` | Точка входа, маршрутизация операций |
| `cluster-management` | OBD deploy/start/scale_out, конфигурация компонентов |
| `tenant-management` | Создание tenant после развёртывания |
| `testing-and-benchmark` | Sysbench/TPC-H после проверки кластера |

## Соответствие скриптов и skills

| Скрипт | Skill / reference |
|--------|-------------------|
| `02-prepare-servers.sh` | prepare-servers, configure-sysctl-conf |
| `03-generate-obd-config.py` | config-deployment.md |
| `04-deploy-cluster.sh` | cluster lifecycle (deploy, start, display) |
| `05-scale-out.sh` | scale_out |

## Безопасность (из cluster-management)

Перед выполнением деструктивных команд требуется явное подтверждение:

- `obd cluster destroy`
- `obd cluster redeploy`
- `scripts/99-destroy.sh --destroy-obd`
