#!/usr/bin/env python3
"""Потолок памяти ODP (obproxy): proxy_mem_limited.

Дефолт вендора 2G. do_monitor_mem сравнивает RSS с этим лимитом, а не с
RAM хоста. На ВМ 64 GB при 2G в логе: «memory is out of limit, will disable
alloc memory from the OS». free при этом может показывать десятки гигабайт.

Официально: https://www.oceanbase.com/docs/common-odp-doc-cn-1000000006242430
Живое изменение (без рестарта), на каждом obproxy:

    ALTER PROXYCONFIG SET proxy_mem_limited = '8G';
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = Path(__file__).resolve().parent

PARAM = "proxy_mem_limited"
VENDOR_DEFAULT = "2G"
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]I?B?)?$", re.IGNORECASE)
MIN_BYTES = 100 * 1024 * 1024  # OBD / ODP min 100MB


def _load_mod(name: str, filename: str) -> Any:
    path = LIB_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_route() -> Any:
    return _load_mod("obproxy_route", "obproxy_route.py")


def _load_ob_sys() -> Any:
    return _load_mod("ob_sys", "ob-sys.py")


def _load_vm() -> Any:
    return _load_mod("vm_profiles", "vm_profiles.py")


def parse_size_bytes(value: str, *, bare_number: str = "bytes") -> int | None:
    """Разбор 8G / 8GB / 8192M. Голое число: bytes (PROXYCONFIG) или gb (yaml)."""
    text = (value or "").strip().strip("'\"").upper().replace(" ", "")
    if not text:
        return None
    if text.isdigit():
        n = int(text)
        if bare_number == "gb":
            return n * 1024**3
        return n
    match = _SIZE_RE.fullmatch(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "").replace("IB", "I").replace("B", "")
    factors = {
        "": 1,
        "K": 1024,
        "KI": 1024,
        "M": 1024**2,
        "MI": 1024**2,
        "G": 1024**3,
        "GI": 1024**3,
        "T": 1024**4,
        "TI": 1024**4,
    }
    if unit not in factors:
        return None
    return int(amount * factors[unit])


def format_capacity(nbytes: int) -> str:
    vm = _load_vm()
    gb = nbytes / float(1024**3)
    if gb >= 1 - 1e-9:
        return vm.fmt_gb(gb)
    mb = nbytes / float(1024**2)
    if abs(mb - round(mb)) < 1e-6:
        return f"{int(round(mb))}M"
    return f"{mb:.0f}M"


def parse_capacity(value: str, *, bare_number: str = "gb") -> int:
    nbytes = parse_size_bytes(value, bare_number=bare_number)
    if nbytes is None:
        raise ValueError(
            f"Некорректный размер {value!r}. Нужно вроде 8G / 8GB / 8192M"
        )
    if nbytes < MIN_BYTES:
        raise ValueError(
            f"{PARAM}={value} меньше минимума 100MB (ODP/OBD)"
        )
    return nbytes


def yaml_proxy_mem_raw(cfg: dict[str, Any]) -> str | None:
    ob = cfg.get("oceanbase") or {}
    proxy = ob.get("obproxy") if isinstance(ob.get("obproxy"), dict) else {}
    raw = (proxy or {}).get("proxy_mem_limited")
    if raw is None or raw == "":
        return None
    return str(raw).strip()


def resolve_proxy_mem_limited(cfg: dict[str, Any], override: str | None = None) -> str:
    """Явный --size / yaml, иначе auto от RAM ВМ obproxy."""
    vm = _load_vm()
    if override:
        return format_capacity(parse_capacity(override, bare_number="gb"))
    raw = yaml_proxy_mem_raw(cfg)
    if raw:
        return format_capacity(parse_capacity(raw, bare_number="gb"))
    ctx = vm.proxy_vm_context(cfg)
    if ctx is None:
        return VENDOR_DEFAULT
    mem_gb, dedicated = ctx
    return vm.recommended_proxy_mem_limited(mem_gb, dedicated=dedicated)


def obd_mem_settings(cfg: dict[str, Any]) -> dict[str, str]:
    """Параметр, который OBD принимает в obproxy-ce.global."""
    return {PARAM: resolve_proxy_mem_limited(cfg)}


def proxyconfig_set_sql(value: str) -> str:
    text = str(value).replace("'", "''")
    return f"ALTER PROXYCONFIG SET {PARAM} = '{text}'"


def apply_statements(cfg: dict[str, Any], override: str | None = None) -> list[str]:
    return [proxyconfig_set_sql(resolve_proxy_mem_limited(cfg, override))]


def values_match(current: str, expected: str) -> bool:
    got = parse_size_bytes(current, bare_number="bytes")
    want = parse_size_bytes(expected, bare_number="gb")
    if got is None or want is None:
        return (current or "").strip().lower() == (expected or "").strip().lower()
    # 8G vs 8GB vs 8589934592
    return abs(got - want) < 1024 * 1024


def fetch_mem_proxyconfig(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
) -> dict[str, str]:
    route = _load_route()
    proc = ob_sys.run_sql(
        endpoint, password, f"SHOW PROXYCONFIG LIKE '{PARAM}'", ignore_error=True
    )
    if proc.returncode != 0:
        return {}
    return route.parse_proxyconfig_rows(proc.stdout or "")


def print_mem_config(
    label: str,
    values: dict[str, str],
    expected: str | None = None,
) -> None:
    current = values.get(PARAM, "<нет в выводе>")
    print(f"{label}:")
    print(f"  {PARAM} = {current}")
    if expected:
        print(f"  целевой = {expected}")


def apply_on_endpoint(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
    value: str,
) -> dict[str, str]:
    sql = proxyconfig_set_sql(value)
    proc = ob_sys.run_sql(endpoint, password, sql, ignore_error=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or "alter failed"
        raise RuntimeError(f"{PARAM}: {err}")
    return fetch_mem_proxyconfig(ob_sys, endpoint, password)


def apply_all(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    size: str | None = None,
    skip_if_ok: bool = False,
) -> int:
    """Выставить proxy_mem_limited на каждом obproxy. Возвращает число ошибок."""
    route = _load_route()
    ob_sys = _load_ob_sys()
    deploy_name = inv.get("DEPLOY_NAME", "")
    if not deploy_name:
        raise RuntimeError("DEPLOY_NAME не задан в inventory.env")
    expected = resolve_proxy_mem_limited(cfg, size)
    endpoints = route.pick_obproxy_endpoints(ob_sys, cfg, inv)
    source = "--size" if size else ("yaml" if yaml_proxy_mem_raw(cfg) else "auto")
    print(f"{PARAM}={expected} ({source})")
    failed = 0
    for endpoint in endpoints:
        label = route.format_proxy_label(endpoint)
        try:
            password = route.connect_proxy(ob_sys, endpoint, cfg, deploy_name)
            before = fetch_mem_proxyconfig(ob_sys, endpoint, password)
            if values_match(before.get(PARAM, ""), expected) and skip_if_ok:
                print(f"{label}: уже {expected} — пропуск")
                continue
            after = apply_on_endpoint(ob_sys, endpoint, password, expected)
            print_mem_config(label, after, expected)
            if not values_match(after.get(PARAM, ""), expected):
                print(f"  ERROR: после ALTER PROXYCONFIG значение не {expected}")
                failed += 1
        except Exception as exc:
            print(f"{label}: ERROR {exc}", file=sys.stderr)
            failed += 1
    return failed


def cmd_show(args: argparse.Namespace) -> None:
    route = _load_route()
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    deploy_name = inv.get("DEPLOY_NAME", "")
    expected = resolve_proxy_mem_limited(cfg, getattr(args, "size", None))
    vm = _load_vm()
    ctx = vm.proxy_vm_context(cfg)
    if ctx is None:
        print(f"Целевой {PARAM}={expected} (yaml/auto; профиль ВМ obproxy не найден)")
    else:
        mem_gb, dedicated = ctx
        kind = "dedicated" if dedicated else "colocated observer"
        print(
            f"Целевой {PARAM}={expected} (ВМ {kind} {mem_gb} GB). "
            "Если free на хосте больше, чем memory_gb в yaml — "
            "ВМ увеличили в YC, лимит процесса нет: --size 8G"
        )
    endpoints = route.pick_obproxy_endpoints(ob_sys, cfg, inv)
    for endpoint in endpoints:
        password = route.connect_proxy(ob_sys, endpoint, cfg, deploy_name)
        values = fetch_mem_proxyconfig(ob_sys, endpoint, password)
        print_mem_config(route.format_proxy_label(endpoint), values, expected)
        current = values.get(PARAM, "")
        if values_match(current, expected):
            print(f"  статус: совпадает с {expected}")
        else:
            print(f"  статус: не {expected} — docs/obproxy-memory.md")


def cmd_apply(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    failed = apply_all(
        cfg, inv, size=args.size, skip_if_ok=args.skip_if_ok
    )
    if failed:
        sys.exit(1)
    print()
    print("Лимит действует сразу, рестарт ODP не нужен.")
    print("Проверка: tail ~/obproxy/log/obproxy.log | grep do_monitor_mem")


def cmd_self_test(_args: argparse.Namespace) -> None:
    tiny = {"vm_profiles": {"obproxy": {"count": 2, "memory_gb": 4}}}
    mid = {"vm_profiles": {"obproxy": {"count": 2, "memory_gb": 16}}}
    huge = {"vm_profiles": {"obproxy": {"count": 1, "memory_gb": 64}}}
    pinned = {
        "vm_profiles": {"obproxy": {"count": 2, "memory_gb": 64}},
        "oceanbase": {"obproxy": {"proxy_mem_limited": "8G"}},
    }
    assert resolve_proxy_mem_limited(tiny) == "2G"
    assert resolve_proxy_mem_limited(mid) == "8G"
    assert resolve_proxy_mem_limited(huge) == "16G"
    assert resolve_proxy_mem_limited(pinned) == "8G"
    assert resolve_proxy_mem_limited(tiny, "8G") == "8G"
    assert apply_statements(mid) == ["ALTER PROXYCONFIG SET proxy_mem_limited = '8G'"]
    assert values_match("2G", "2GB")
    assert values_match("2147483648", "2G")
    assert values_match("8GB", "8G")
    assert not values_match("2G", "8G")
    assert obd_mem_settings(mid) == {PARAM: "8G"}
    try:
        parse_capacity("50MB")
        raise AssertionError("ожидали ValueError на 50MB")
    except ValueError:
        pass
    print("self-test ok")


def _add_io_args(parser: argparse.ArgumentParser, *, with_defaults: bool) -> None:
    if with_defaults:
        parser.add_argument("--config", default=str(REPO_ROOT / "config" / "deploy.yaml"))
        parser.add_argument(
            "--inventory", default=str(REPO_ROOT / "generated" / "inventory.env")
        )
        return
    parser.add_argument("--config", default=argparse.SUPPRESS)
    parser.add_argument("--inventory", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    _add_io_args(parser, with_defaults=True)
    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("show", help="SHOW PROXYCONFIG proxy_mem_limited на каждом obproxy")
    _add_io_args(p_show, with_defaults=False)
    p_show.add_argument(
        "--size",
        default=None,
        help="Сверить с этим размером вместо yaml/auto (например 8G)",
    )
    p_show.set_defaults(func=cmd_show)

    p_apply = sub.add_parser("apply", help="ALTER PROXYCONFIG proxy_mem_limited на каждом obproxy")
    _add_io_args(p_apply, with_defaults=False)
    p_apply.add_argument(
        "--size",
        default=None,
        help="Явный лимит (8G, 16G). Иначе oceanbase.obproxy.proxy_mem_limited "
        "или auto от vm_profiles.obproxy.memory_gb",
    )
    p_apply.add_argument(
        "--skip-if-ok",
        action="store_true",
        help="Не трогать экземпляр, если значение уже совпадает",
    )
    p_apply.set_defaults(func=cmd_apply)

    p_test = sub.add_parser("self-test", help="Локальные проверки без кластера")
    p_test.set_defaults(func=cmd_self_test)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
