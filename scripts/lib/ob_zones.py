#!/usr/bin/env python3
"""Раскладка observer-узлов по зонам OceanBase.

Кластер всегда состоит ровно из трёх zone, observer распределяются между ними
по кругу (server1 -> zone1, server2 -> zone2, server3 -> zone3, server4 -> zone1 ...).

Почему не «одна zone на observer»:

- при bootstrap sys-тенант получает по одной full-реплике на каждую zone
  (`F{1}@zone1, F{1}@zone2, ...`), а Paxos-группа лога ограничена
  `OB_MAX_MEMBER_NUMBER = 7`. Больше семи zone — `alter system bootstrap`
  падает; OBD всё равно печатает «oceanbase bootstrap ok» и дальше зависает
  на ожидании серверов или на «obshell bootstrap -»;
- majority Paxos требует нечётного числа zone, штатная модель OceanBase —
  3 zone с равным числом серверов.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ZONE_COUNT = 3
# deps/oblib/src/lib/ob_define.h OB_MAX_MEMBER_NUMBER — размер Paxos-группы sys-тенанта
# равен числу zone при bootstrap. Больше семи zone — ALTER SYSTEM BOOTSTRAP падает.
MAX_PAXOS_ZONES = 7
OCEANBASE_COMPONENT_KEYS = ("oceanbase-ce", "oceanbase")


def zone_names() -> list[str]:
    """Имена всех zone кластера."""
    return [f"zone{i}" for i in range(1, ZONE_COUNT + 1)]


def zone_for_index(idx: int) -> str:
    """Zone для observer с индексом idx из inventory (1-based)."""
    if idx < 1:
        raise ValueError(f"observer index must be >= 1, got {idx}")
    return f"zone{(idx - 1) % ZONE_COUNT + 1}"


def zone_sizes(observer_count: int) -> dict[str, int]:
    """Сколько observer попадает в каждую zone."""
    sizes = {name: 0 for name in zone_names()}
    for idx in range(1, observer_count + 1):
        sizes[zone_for_index(idx)] += 1
    return sizes


def uneven_zones_warning(observer_count: int) -> str | None:
    """Предупреждение о неравных zone (unit_num тенанта одинаков для всех zone)."""
    if observer_count < ZONE_COUNT:
        return (
            f"observer count = {observer_count}: для HA нужно минимум {ZONE_COUNT} узла "
            f"(по одному на zone)"
        )
    if observer_count % ZONE_COUNT == 0:
        return None
    sizes = zone_sizes(observer_count)
    layout = ", ".join(f"{name}={count}" for name, count in sizes.items())
    return (
        f"observer count = {observer_count} не кратно {ZONE_COUNT} ({layout}): "
        f"unit_num тенанта одинаков для всех zone, лишние узлы останутся без unit"
    )


def oceanbase_component(obd_cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    """Блок oceanbase-ce / oceanbase из конфигурации OBD."""
    if not isinstance(obd_cfg, dict):
        return None
    for key in OCEANBASE_COMPONENT_KEYS:
        block = obd_cfg.get(key)
        if isinstance(block, dict):
            return block
    return None


def oceanbase_zones_from_obd(obd_cfg: dict[str, Any] | None) -> list[str]:
    """Zone каждого observer в порядке server-override (с повторами)."""
    comp = oceanbase_component(obd_cfg)
    if not comp:
        return []
    zones: list[str] = []
    for key, val in comp.items():
        if key in ("servers", "global", "depends", "version") or not isinstance(val, dict):
            continue
        zone = val.get("zone")
        if zone is not None and str(zone).strip():
            zones.append(str(zone).strip())
    return zones


def unique_zones(zones: list[str]) -> list[str]:
    """Уникальные имена zone в порядке первого появления."""
    seen: set[str] = set()
    out: list[str] = []
    for zone in zones:
        if zone not in seen:
            seen.add(zone)
            out.append(zone)
    return out


def too_many_zones_error(zones: list[str], source: str) -> str | None:
    """Ошибка, если в конфиге OBD больше MAX_PAXOS_ZONES уникальных zone."""
    names = unique_zones(zones)
    if len(names) <= MAX_PAXOS_ZONES:
        return None
    counts = Counter(zones)
    layout = ", ".join(f"{name}={counts[name]}" for name in names)
    shown = ", ".join(names[:12])
    extra = "" if len(names) <= 12 else f" … ещё {len(names) - 12}"
    return (
        f"{source}: {len(names)} уникальных zone ({shown}{extra}; {layout}). "
        f"При bootstrap sys-тенант получает full-реплику на каждую zone, "
        f"а Paxos-группа ограничена OB_MAX_MEMBER_NUMBER={MAX_PAXOS_ZONES}. "
        f"OBD печатает «oceanbase bootstrap ok», SQL при этом падает, и start "
        f"зависает на ожидании серверов / obshell bootstrap. "
        f"Перегенерируйте generated/obd-cluster.yaml, затем "
        f"`obd cluster destroy <deploy> -f` и повторный deploy "
        f"(локальность sys-тенанта иначе не исправить)."
    )


def load_obd_yaml(path: Path) -> dict[str, Any] | None:
    try:
        import yaml
    except ImportError:
        print("ERROR: нужен PyYAML (pip install pyyaml)", file=sys.stderr)
        return None
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def check_obd_yaml_path(path: Path) -> str | None:
    """None если zone в пределах лимита или файла нет / нет oceanbase-блока."""
    if not path.is_file():
        return None
    cfg = load_obd_yaml(path)
    if cfg is None:
        return None
    zones = oceanbase_zones_from_obd(cfg)
    if not zones:
        return None
    return too_many_zones_error(zones, str(path))


def registered_cluster_yaml_paths(deploy_name: str, obd_home: Path | None = None) -> list[Path]:
    """YAML метаданных зарегистрированного в OBD кластера (~/.obd/cluster/<name>)."""
    root = (obd_home or Path.home() / ".obd") / "cluster" / deploy_name
    if root.is_file():
        return [root]
    if not root.is_dir():
        return []
    paths: list[Path] = []
    for pattern in ("*.yaml", "*.yml"):
        paths.extend(sorted(root.glob(pattern)))
    return paths


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_name = sub.add_parser("name", help="Zone для индекса observer")
    p_name.add_argument("index", type=int)

    p_sizes = sub.add_parser("sizes", help="Распределение observer по zone")
    p_sizes.add_argument("count", type=int)

    sub.add_parser("list", help="Имена всех zone")

    p_check = sub.add_parser(
        "check-obd",
        help="Проверить, что в YAML OBD не больше 7 уникальных zone",
    )
    p_check.add_argument("path", type=Path)
    p_check.add_argument(
        "--dump",
        action="store_true",
        help="Печатать уникальные zone и их кратность",
    )

    args = parser.parse_args()
    if args.command == "name":
        try:
            print(zone_for_index(args.index))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            sys.exit(1)
    elif args.command == "sizes":
        for name, count in zone_sizes(args.count).items():
            print(f"{name}={count}")
    elif args.command == "check-obd":
        path: Path = args.path
        if not path.is_file():
            print(f"ERROR: файл не найден: {path}", file=sys.stderr)
            sys.exit(1)
        cfg = load_obd_yaml(path)
        if cfg is None:
            print(f"ERROR: не удалось прочитать YAML: {path}", file=sys.stderr)
            sys.exit(1)
        zones = oceanbase_zones_from_obd(cfg)
        names = unique_zones(zones)
        if args.dump:
            counts = Counter(zones)
            if names:
                print(", ".join(f"{name}={counts[name]}" for name in names))
            else:
                print("zones=none")
        err = too_many_zones_error(zones, str(path))
        if err:
            print(f"ERROR: {err}", file=sys.stderr)
            sys.exit(1)
        if not args.dump:
            print(f"OK: {len(names)} unique zone in {path}")
    else:
        for name in zone_names():
            print(name)


if __name__ == "__main__":
    main()
