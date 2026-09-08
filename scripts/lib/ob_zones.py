#!/usr/bin/env python3
"""Раскладка observer-узлов по зонам OceanBase.

Кластер всегда состоит ровно из трёх zone, observer распределяются между ними
по кругу (server1 -> zone1, server2 -> zone2, server3 -> zone3, server4 -> zone1 ...).

Почему не «одна zone на observer»:

- при bootstrap sys-тенант получает по одной full-реплике на каждую zone
  (`F{1}@zone1, F{1}@zone2, ...`), а Paxos-группа лога ограничена
  `OB_MAX_MEMBER_NUMBER = 7`. Больше семи zone — `alter system bootstrap`
  падает, и дальше OBD сыплет OBD-5000 на `modify zone ... set idc` и
  `alter user "root"`;
- majority Paxos требует нечётного числа zone, штатная модель OceanBase —
  3 zone с равным числом серверов.
"""

from __future__ import annotations

import argparse
import sys

ZONE_COUNT = 3


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_name = sub.add_parser("name", help="Zone для индекса observer")
    p_name.add_argument("index", type=int)

    p_sizes = sub.add_parser("sizes", help="Распределение observer по zone")
    p_sizes.add_argument("count", type=int)

    sub.add_parser("list", help="Имена всех zone")

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
    else:
        for name in zone_names():
            print(name)


if __name__ == "__main__":
    main()
