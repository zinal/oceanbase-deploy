#!/usr/bin/env python3
"""Маршрутизация ODP: равномерное распределение сессий по observer.

Официальный KB (V4.x): если ODP не может вычислить Leader партиции, то
`enable_cached_server=true` + `enable_primary_zone=true` клеят SQL к одному
observer (часто к узлу логина / Primary Zone). Случайный fallback:

    ALTER PROXYCONFIG SET enable_cached_server = false;
    ALTER PROXYCONFIG SET enable_primary_zone = false;

Команда действует только на тот экземпляр ODP, к которому подключились.
При нескольких obproxy нужно применить на каждом.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = Path(__file__).resolve().parent

# even — случайный fallback (равномерные сессии).
# oltp — официальная практика для TP: без кэша сессии, fallback в Primary Zone.
ROUTING_MODES: dict[str, dict[str, str]] = {
    "even": {
        "enable_cached_server": "false",
        "enable_primary_zone": "false",
    },
    "oltp": {
        "enable_cached_server": "false",
        "enable_primary_zone": "true",
    },
}

PIN_KEYS = ("target_db_server", "proxy_primary_zone_name")
ROUTE_KEYS = ("enable_cached_server", "enable_primary_zone")


def _load_ob_sys() -> Any:
    path = LIB_DIR / "ob-sys.py"
    spec = importlib.util.spec_from_file_location("ob_sys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_mode(mode: str) -> str:
    key = (mode or "even").strip().lower()
    if key not in ROUTING_MODES:
        allowed = ", ".join(sorted(ROUTING_MODES))
        raise ValueError(f"Неизвестный режим маршрутизации {mode!r}. Допустимо: {allowed}")
    return key


def proxyconfig_set_sql(name: str, value: str) -> str:
    return f"ALTER PROXYCONFIG SET {name} = {value}"


def apply_statements(mode: str) -> list[str]:
    settings = ROUTING_MODES[resolve_mode(mode)]
    return [proxyconfig_set_sql(name, value) for name, value in settings.items()]


def show_statements() -> list[str]:
    keys = ROUTE_KEYS + PIN_KEYS
    return [f"SHOW PROXYCONFIG LIKE '{key}'" for key in keys]


def pick_obproxy_endpoints(
    ob_sys: Any,
    cfg: dict[str, Any],
    inv: dict[str, str],
) -> list[dict[str, Any]]:
    """Все obproxy из inventory. ALTER PROXYCONFIG — только через порт 2883."""
    proxy_port = ob_sys.cfg_int(cfg, "oceanbase.ports.obproxy", 2883)
    cluster = ob_sys.cfg_str(cfg, "oceanbase.cluster_name", "obcluster")
    rows = ob_sys.inventory_ips(inv, "OBPROXY")
    if not rows:
        raise RuntimeError(
            "В inventory нет OBPROXY_*_IP — ALTER PROXYCONFIG нужно выполнять "
            "через obproxy:2883, не через observer:2881"
        )
    return [
        {
            "ip": ip,
            "port": proxy_port,
            "user": f"root@sys#{cluster}",
            "via": "obproxy",
            "name": name,
            "idx": idx,
        }
        for idx, ip, name in rows
    ]


def parse_proxyconfig_rows(stdout: str) -> dict[str, str]:
    """Разобрать табличный вывод SHOW PROXYCONFIG (-N -B: name\\tvalue...)."""
    values: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        if name:
            values[name] = parts[1].strip()
    return values


def is_pin_value(value: str) -> bool:
    text = (value or "").strip().strip("'\"")
    return bool(text) and text.lower() not in {"null", "none", "nil"}


def normalize_bool(value: str) -> str:
    text = (value or "").strip().strip("'\"").lower()
    if text in {"1", "true", "on", "yes"}:
        return "true"
    if text in {"0", "false", "off", "no"}:
        return "false"
    return text


def settings_match(current: dict[str, str], mode: str) -> bool:
    expected = ROUTING_MODES[resolve_mode(mode)]
    for name, want in expected.items():
        got = normalize_bool(current.get(name, ""))
        if got != want:
            return False
    return True


def format_proxy_label(endpoint: dict[str, Any]) -> str:
    name = endpoint.get("name") or ""
    ip = endpoint.get("ip")
    port = endpoint.get("port")
    if name:
        return f"{name} ({ip}:{port})"
    return f"{ip}:{port}"


def connect_proxy(
    ob_sys: Any,
    endpoint: dict[str, Any],
    cfg: dict[str, Any],
    deploy_name: str,
) -> str:
    return ob_sys.connect_sys_password(endpoint, cfg, deploy_name)


def fetch_proxyconfig(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    for sql in show_statements():
        proc = ob_sys.run_sql(endpoint, password, sql)
        merged.update(parse_proxyconfig_rows(proc.stdout or ""))
    return merged


def apply_on_endpoint(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
    mode: str,
) -> dict[str, str]:
    for sql in apply_statements(mode):
        ob_sys.run_sql(endpoint, password, sql)
    return fetch_proxyconfig(ob_sys, endpoint, password)


def print_proxyconfig(label: str, values: dict[str, str]) -> None:
    print(f"{label}:")
    for key in ROUTE_KEYS + PIN_KEYS:
        print(f"  {key} = {values.get(key, '<нет в выводе>')}")


def cmd_show(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    deploy_name = inv.get("DEPLOY_NAME", "")
    endpoints = pick_obproxy_endpoints(ob_sys, cfg, inv)
    for endpoint in endpoints:
        password = connect_proxy(ob_sys, endpoint, cfg, deploy_name)
        values = fetch_proxyconfig(ob_sys, endpoint, password)
        print_proxyconfig(format_proxy_label(endpoint), values)
        for key in PIN_KEYS:
            if is_pin_value(values.get(key, "")):
                print(
                    f"  WARN: {key} задан — ODP принудительно шлёт SQL "
                    "на указанный observer/zone"
                )


def cmd_apply(args: argparse.Namespace) -> None:
    mode = resolve_mode(args.mode)
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    deploy_name = inv.get("DEPLOY_NAME", "")
    if not deploy_name:
        raise RuntimeError("DEPLOY_NAME не задан в inventory.env")

    endpoints = pick_obproxy_endpoints(ob_sys, cfg, inv)
    print(
        f"Режим {mode}: "
        + ", ".join(f"{k}={v}" for k, v in ROUTING_MODES[mode].items())
    )
    failed = 0
    for endpoint in endpoints:
        label = format_proxy_label(endpoint)
        try:
            password = connect_proxy(ob_sys, endpoint, cfg, deploy_name)
            before = fetch_proxyconfig(ob_sys, endpoint, password)
            if settings_match(before, mode) and args.skip_if_ok:
                print(f"{label}: уже {mode} — пропуск")
                continue
            after = apply_on_endpoint(ob_sys, endpoint, password, mode)
            print_proxyconfig(label, after)
            if not settings_match(after, mode):
                print(f"  ERROR: после ALTER PROXYCONFIG значения не совпали с режимом {mode}")
                failed += 1
            for key in PIN_KEYS:
                if is_pin_value(after.get(key, "")):
                    print(
                        f"  WARN: {key}={after[key]!r} — снимите pin, иначе "
                        "случайный fallback не сработает"
                    )
        except Exception as exc:
            print(f"{label}: ERROR {exc}", file=sys.stderr)
            failed += 1
    if failed:
        sys.exit(1)
    print()
    print("Новые сессии пойдут по новой маршрутизации.")
    print("Уже открытые соединения держат старый observer — переподключите пул.")


def cmd_diagnose(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    deploy_name = inv.get("DEPLOY_NAME", "")
    endpoints = pick_obproxy_endpoints(ob_sys, cfg, inv)
    queries = [
        (
            "PRIMARY_ZONE тенантов",
            "SELECT tenant_name, primary_zone FROM oceanbase.DBA_OB_TENANTS "
            "ORDER BY tenant_name",
        ),
        (
            "Сессии по observer (gv$ob_processlist)",
            "SELECT svr_ip, COUNT(*) AS sessions FROM gv$ob_processlist "
            "GROUP BY svr_ip ORDER BY sessions DESC",
        ),
        (
            "SQL audit по observer (gv$sql_audit)",
            "SELECT svr_ip, COUNT(*) AS stmts FROM gv$sql_audit "
            "GROUP BY svr_ip ORDER BY stmts DESC",
        ),
    ]

    print("=== ODP routing на каждом obproxy ===")
    first_password = ""
    for endpoint in endpoints:
        try:
            password = connect_proxy(ob_sys, endpoint, cfg, deploy_name)
            if not first_password:
                first_password = password
            values = fetch_proxyconfig(ob_sys, endpoint, password)
            print_proxyconfig(format_proxy_label(endpoint), values)
            for key in PIN_KEYS:
                if is_pin_value(values.get(key, "")):
                    print(f"  WARN: {key} задан — принудительный pin")
            if settings_match(values, "even"):
                print("  режим: even")
            elif settings_match(values, "oltp"):
                print("  режим: oltp (fallback в Primary Zone)")
            else:
                print("  режим: не even — SQL без ключа партиции липнет к одному observer")
        except Exception as exc:
            print(f"{format_proxy_label(endpoint)}: ERROR {exc}")
        print()

    first = endpoints[0]
    if not first_password:
        password = connect_proxy(ob_sys, first, cfg, deploy_name)
    else:
        password = first_password
    print(f"Кластерные запросы через {format_proxy_label(first)}")
    print()
    for title, sql in queries:
        print(f"=== {title} ===")
        proc = ob_sys.run_sql(first, password, sql, ignore_error=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            print(f"(пропуск: {err})")
        else:
            out = (proc.stdout or "").rstrip()
            print(out if out else "(пусто)")
        print()
    print("gv$ob_log_stat (лидеры лог-стримов) не показывает, куда ODP шлёт SQL.")
    print("Смотрите sessions/stmts выше и SHOW PROXYCONFIG на каждом obproxy.")


def cmd_self_test(_args: argparse.Namespace) -> None:
    assert resolve_mode("EVEN") == "even"
    assert apply_statements("even") == [
        "ALTER PROXYCONFIG SET enable_cached_server = false",
        "ALTER PROXYCONFIG SET enable_primary_zone = false",
    ]
    assert apply_statements("oltp") == [
        "ALTER PROXYCONFIG SET enable_cached_server = false",
        "ALTER PROXYCONFIG SET enable_primary_zone = true",
    ]
    parsed = parse_proxyconfig_rows(
        "enable_cached_server\ttrue\n"
        "enable_primary_zone\tTrue\n"
        "target_db_server\t\n"
        "proxy_primary_zone_name\tzone1\n"
    )
    assert parsed["enable_cached_server"] == "true"
    assert is_pin_value(parsed["proxy_primary_zone_name"])
    assert not is_pin_value(parsed["target_db_server"])
    assert settings_match(
        {"enable_cached_server": "False", "enable_primary_zone": "0"},
        "even",
    )
    assert not settings_match(
        {"enable_cached_server": "true", "enable_primary_zone": "false"},
        "even",
    )
    try:
        resolve_mode("sticky")
        raise AssertionError("ожидали ValueError")
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

    p_show = sub.add_parser("show", help="SHOW PROXYCONFIG на каждом obproxy")
    _add_io_args(p_show, with_defaults=False)
    p_show.set_defaults(func=cmd_show)

    p_apply = sub.add_parser("apply", help="ALTER PROXYCONFIG на каждом obproxy")
    _add_io_args(p_apply, with_defaults=False)
    p_apply.add_argument(
        "--mode",
        default="even",
        choices=sorted(ROUTING_MODES),
        help="even = случайный fallback; oltp = fallback в Primary Zone",
    )
    p_apply.add_argument(
        "--skip-if-ok",
        action="store_true",
        help="Не трогать экземпляр, если значения уже совпадают",
    )
    p_apply.set_defaults(func=cmd_apply)

    p_diag = sub.add_parser("diagnose", help="PROXYCONFIG + сессии/SQL по observer")
    _add_io_args(p_diag, with_defaults=False)
    p_diag.set_defaults(func=cmd_diagnose)

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
