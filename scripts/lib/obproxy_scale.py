#!/usr/bin/env python3
"""План добавления obproxy на уже живом кластере.

Не удаляет ВМ. Цель — довести состав до vm_profiles.obproxy.count:
создать недостающие {deploy}-obproxy-N, scale_out новых IP, вычистить из
OBD адреса, которых больше нет среди 1..N, переписать inventory.
HAProxy обновляет вызывающий скрипт по новому inventory.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

LIB_DIR = Path(__file__).resolve().parent


def _load_ob_sys() -> ModuleType:
    path = LIB_DIR / "ob-sys.py"
    spec = importlib.util.spec_from_file_location("ob_sys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_OB_SYS: ModuleType | None = None


def ob_sys() -> ModuleType:
    global _OB_SYS
    if _OB_SYS is None:
        _OB_SYS = _load_ob_sys()
    return _OB_SYS


def canonical_name(deploy_name: str, index: int) -> str:
    if index < 1:
        raise ValueError(f"index должен быть >= 1, получено {index}")
    if not deploy_name:
        raise ValueError("пустое deployment.name")
    return f"{deploy_name}-obproxy-{index}"


def parse_existing(data: dict[str, Any] | list[str]) -> dict[str, str]:
    """name → IP. Пустой IP значит «ВМ есть, адрес ещё не известен»."""
    out: dict[str, str] = {}
    if isinstance(data, list):
        for name in data:
            name = str(name).strip()
            if name:
                out[name] = ""
        return out
    if not isinstance(data, dict):
        raise ValueError("existing должен быть объектом name→ip или списком имён")
    for name, ip in data.items():
        name = str(name).strip()
        if not name:
            continue
        out[name] = "" if ip is None else str(ip).strip()
    return out


def _extra_live_names(
    deploy_name: str,
    desired_count: int,
    inv_count: int,
    existing: dict[str, str],
    inventory: dict[str, str],
) -> list[str]:
    extra: list[str] = []
    seen: set[str] = set()
    for idx in range(desired_count + 1, max(inv_count, desired_count) + 1):
        name = inventory.get(f"OBPROXY_{idx}_NAME") or canonical_name(deploy_name, idx)
        if name in existing and name not in seen:
            extra.append(name)
            seen.add(name)
    prefix = f"{deploy_name}-obproxy-"
    for name in existing:
        if name in seen or not name.startswith(prefix):
            continue
        suffix = name[len(prefix) :]
        if suffix.isdigit() and int(suffix) > desired_count:
            extra.append(name)
            seen.add(name)
    return extra


def plan_obproxy_scale(
    *,
    deploy_name: str,
    desired_count: int,
    existing: dict[str, str],
    inventory: dict[str, str],
    obd_ips: list[str],
    force_scale_ips: list[str] | None = None,
) -> dict[str, Any]:
    if desired_count < 1:
        raise ValueError("vm_profiles.obproxy.count должен быть >= 1")
    if not deploy_name:
        raise ValueError("пустое deployment.name")

    inv_count = int(inventory.get("OBPROXY_COUNT") or 0)
    extra_live = _extra_live_names(deploy_name, desired_count, inv_count, existing, inventory)
    if extra_live and desired_count < inv_count:
        raise ValueError(
            "Уменьшение числа obproxy при живых лишних ВМ не поддерживается. "
            f"Лишние ВМ ({', '.join(sorted(extra_live))}) удалите сами, "
            "затем повторите команду — inventory/OBD/HAProxy синхронизируются."
        )

    create: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    missing_ip: list[str] = []
    final: list[dict[str, Any]] = []
    warnings: list[str] = []

    for idx in range(1, desired_count + 1):
        name = canonical_name(deploy_name, idx)
        ip = existing.get(name, "")
        row = {"index": idx, "name": name, "ip": ip}
        if name not in existing:
            create.append({"index": idx, "name": name})
        else:
            keep.append(row)
            if not ip:
                missing_ip.append(name)
        final.append(row)

    known_final_ips = {row["ip"] for row in final if row["ip"]}
    force = {ip for ip in (force_scale_ips or []) if ip}
    # Пересозданная ВМ с тем же IP: OBD думает, что obproxy уже стоит,
    # а процесса/бинаря нет — scale_out других узлов падает с
    # «obproxy-ce is not running». Снимаем запись и ставим заново.
    healthy_obd = {ip for ip in obd_ips if ip and ip not in force}
    scale_out = [
        {"index": row["index"], "name": row["name"], "ip": row["ip"]}
        for row in final
        if row["ip"] and row["ip"] not in healthy_obd
    ]
    stale_inventory_ips: list[str] = []
    for _idx, ip, _name in ob_sys().inventory_ips(inventory, "OBPROXY"):
        if ip and ip not in known_final_ips and ip not in stale_inventory_ips:
            stale_inventory_ips.append(ip)
    clean_obd_ips = [ip for ip in obd_ips if ip and ip not in known_final_ips]
    for ip in stale_inventory_ips:
        if ip not in clean_obd_ips:
            clean_obd_ips.append(ip)
    for ip in obd_ips:
        if ip in force and ip not in clean_obd_ips:
            clean_obd_ips.append(ip)

    if desired_count < inv_count:
        warnings.append(
            f"inventory OBPROXY_COUNT={inv_count} > желаемого {desired_count}: "
            "лишние слоты будут убраны из inventory/HAProxy, ВМ не удаляются"
        )
    if extra_live and desired_count >= inv_count:
        warnings.append(
            "В YC есть лишние obproxy-ВМ сверх нового count; скрипт их не удаляет: "
            + ", ".join(sorted(extra_live))
        )
    if force:
        warnings.append(
            "OBD знает эти IP, но obproxy не работает — сниму из метаданных "
            "и сделаю scale_out заново: " + ", ".join(sorted(force))
        )

    return {
        "deploy_name": deploy_name,
        "desired_count": desired_count,
        "inventory_count": inv_count,
        "create": create,
        "keep": keep,
        "final": final,
        "scale_out": scale_out,
        "clean_obd_ips": clean_obd_ips,
        "force_scale_ips": sorted(force),
        "stale_inventory_ips": stale_inventory_ips,
        "missing_ip": missing_ip,
        "warnings": warnings,
    }


def render_plan_text(plan: dict[str, Any]) -> str:
    lines = [
        f"План obproxy → {plan['desired_count']} шт. "
        f"(сейчас в inventory: {plan['inventory_count']}):",
    ]
    if plan["create"]:
        names = ", ".join(item["name"] for item in plan["create"])
        lines.append(f"  создать ВМ: {names}")
    else:
        lines.append("  создать ВМ: нет (все имена 1..N уже есть в YC)")
    if plan["keep"]:
        kept = ", ".join(f"{item['name']}={item['ip'] or '?'}" for item in plan["keep"])
        lines.append(f"  оставить: {kept}")
    if plan["clean_obd_ips"]:
        lines.append(
            "  сначала убрать из OBD мёртвые IP (иначе OBD-1013 на scale_out): "
            + ", ".join(plan["clean_obd_ips"])
        )
    else:
        lines.append("  убрать из OBD: нечего")
    if plan["scale_out"]:
        ips = ", ".join(f"{item['name']}={item['ip']}" for item in plan["scale_out"])
        lines.append(f"  затем OBD scale_out: {ips}")
    else:
        lines.append("  OBD scale_out: нет новых IP")
    lines.append("  ВМ скрипт не удаляет. Затем HAProxy на всех runner.")
    for warn in plan.get("warnings") or []:
        lines.append(f"  WARN: {warn}")
    return "\n".join(lines) + "\n"


def _load_existing_arg(args: argparse.Namespace) -> dict[str, str]:
    if args.existing_json:
        data = json.loads(Path(args.existing_json).read_text(encoding="utf-8"))
        return parse_existing(data)
    if args.existing_file:
        rows: dict[str, str] = {}
        for raw in Path(args.existing_file).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                rows[line] = ""
                continue
            name, ip = line.split("=", 1)
            rows[name.strip()] = ip.strip()
        return rows
    return {}


def cmd_plan(args: argparse.Namespace) -> None:
    inv = ob_sys().load_inventory(Path(args.inventory)) if args.inventory else {}
    obd_ips = [ip.strip() for ip in (args.obd_ip or []) if ip.strip()]
    if args.obd_ips_file:
        for raw in Path(args.obd_ips_file).read_text(encoding="utf-8").splitlines():
            ip = raw.strip()
            if ip and ip not in obd_ips:
                obd_ips.append(ip)
    force_ips = [ip.strip() for ip in (args.force_scale_ip or []) if ip.strip()]
    if args.force_scale_file and Path(args.force_scale_file).exists():
        for raw in Path(args.force_scale_file).read_text(encoding="utf-8").splitlines():
            ip = raw.strip()
            if ip and ip not in force_ips:
                force_ips.append(ip)
    plan = plan_obproxy_scale(
        deploy_name=args.deploy_name,
        desired_count=args.desired_count,
        existing=_load_existing_arg(args),
        inventory=inv,
        obd_ips=obd_ips,
        force_scale_ips=force_ips,
    )
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.text:
        sys.stdout.write(render_plan_text(plan))
    elif not args.output:
        json.dump(plan, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")


def cmd_names(args: argparse.Namespace) -> None:
    for idx in range(1, args.desired_count + 1):
        print(canonical_name(args.deploy_name, idx))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="JSON-план create / scale_out / clean-obd")
    p_plan.add_argument("--deploy-name", required=True)
    p_plan.add_argument("--desired-count", type=int, required=True)
    p_plan.add_argument("--inventory", default="")
    p_plan.add_argument("--existing-json", default="")
    p_plan.add_argument("--existing-file", default="")
    p_plan.add_argument("--obd-ip", action="append", default=[])
    p_plan.add_argument("--obd-ips-file", default="")
    p_plan.add_argument(
        "--force-scale-ip",
        action="append",
        default=[],
        help="IP уже в OBD, но процесс/бинарь отсутствует — clean + scale_out",
    )
    p_plan.add_argument("--force-scale-file", default="")
    p_plan.add_argument("--output", default="")
    p_plan.add_argument("--text", action="store_true")
    p_plan.set_defaults(func=cmd_plan)

    p_names = sub.add_parser("names", help="Канонические имена {deploy}-obproxy-N")
    p_names.add_argument("--deploy-name", required=True)
    p_names.add_argument("--desired-count", type=int, required=True)
    p_names.set_defaults(func=cmd_names)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
