#!/usr/bin/env python3
"""Детальность логов ODP (obproxy): syslog_level и соседние PROXYCONFIG.

С 4.2.3 дефолт syslog_level=WDIAG (раньше INFO). WDIAG пишет ожидаемые
диагностические ошибки на каждый запрос — на нагруженном инстансе это
десятки гигабайт в сутки и лишний диск/CPU.

Официально: https://www.oceanbase.com/docs/common-odp-doc-cn-1000000002024095
Живое изменение (без рестарта), на каждом obproxy:

    ALTER PROXYCONFIG SET syslog_level = 'INFO';
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

# info — продакшен: вернуть уровень до 4.2.3, не дублировать .wf, ограничить IO.
# warn — агрессивно: только WARN+ и меньше diagnosis/route.
# debug — штатные дефолты вендора (WDIAG / INFO / route=2).
LOG_MODES: dict[str, dict[str, str]] = {
    "info": {
        "syslog_level": "INFO",
        "enable_syslog_wf": "false",
        "enable_async_log": "true",
        "enable_syslog_file_compress": "true",
        "syslog_io_bandwidth_limit": "10MB",
    },
    "warn": {
        "syslog_level": "WARN",
        "monitor_log_level": "WARN",
        "route_diagnosis_level": "1",
        "enable_syslog_wf": "false",
        "enable_async_log": "true",
        "enable_syslog_file_compress": "true",
        "syslog_io_bandwidth_limit": "10MB",
    },
    "debug": {
        "syslog_level": "WDIAG",
        "monitor_log_level": "INFO",
        "route_diagnosis_level": "2",
        "enable_syslog_wf": "true",
        "enable_async_log": "true",
    },
}

LOG_LEVELS = ("DEBUG", "TRACE", "WDIAG", "EDIAG", "INFO", "WARN", "ERROR")
STRING_KEYS = frozenset(
    {
        "syslog_level",
        "monitor_log_level",
        "xflush_log_level",
        "syslog_io_bandwidth_limit",
        "log_dir_size_threshold",
        "max_log_file_size",
        "max_syslog_file_time",
        "log_cleanup_interval",
    }
)
BOOL_KEYS = frozenset(
    {
        "enable_syslog_wf",
        "enable_async_log",
        "enable_syslog_file_compress",
        "enable_syslog_recycle",
    }
)
REQUIRED_KEYS = frozenset({"syslog_level"})
SHOW_LIKE = (
    "%log%",
    "enable_async_log",
    "syslog_io_bandwidth_limit",
    "route_diagnosis_level",
    "enable_syslog_wf",
    "enable_syslog_file_compress",
)
SHOW_KEYS = (
    "syslog_level",
    "monitor_log_level",
    "route_diagnosis_level",
    "enable_syslog_wf",
    "enable_async_log",
    "enable_syslog_file_compress",
    "syslog_io_bandwidth_limit",
    "log_dir_size_threshold",
    "log_file_percentage",
    "max_syslog_file_count",
    "max_log_file_size",
)
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]I?B?)?$", re.IGNORECASE)

# OBD plugin obproxy знает эти ключи (parameter.yaml); syslog_level туда не входит.
OBD_LOG_FILE_PERCENTAGE = 50
OBD_LOG_CLEANUP_INTERVAL = "5m"
OBD_LOG_DIR_MIN_GB = 1
OBD_LOG_DIR_MAX_GB = 16
OBD_LOG_DIR_PCT = 40
DEFAULT_PROXY_BOOT_GB = 20


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


def resolve_mode(mode: str) -> str:
    key = (mode or "info").strip().lower()
    if key not in LOG_MODES:
        allowed = ", ".join(sorted(LOG_MODES))
        raise ValueError(f"Неизвестный режим логов {mode!r}. Допустимо: {allowed}")
    return key


def mode_from_cfg(cfg: dict[str, Any], override: str | None = None) -> str:
    if override:
        return resolve_mode(override)
    ob = cfg.get("oceanbase") or {}
    proxy = ob.get("obproxy") if isinstance(ob.get("obproxy"), dict) else {}
    return resolve_mode(str((proxy or {}).get("log_mode") or "info"))


def proxy_boot_disk_gb(cfg: dict[str, Any]) -> int:
    profiles = cfg.get("vm_profiles") or {}
    proxy = profiles.get("obproxy") or {}
    boot = proxy.get("boot_disk") or {}
    try:
        size = int(boot.get("size_gb") or DEFAULT_PROXY_BOOT_GB)
    except (TypeError, ValueError):
        size = DEFAULT_PROXY_BOOT_GB
    return max(1, size)


def log_dir_size_threshold(cfg: dict[str, Any]) -> str:
    """Потолок каталога логов под boot-диск obproxy (OBD default 64GB)."""
    size_gb = proxy_boot_disk_gb(cfg)
    threshold = size_gb * OBD_LOG_DIR_PCT // 100
    threshold = max(OBD_LOG_DIR_MIN_GB, min(OBD_LOG_DIR_MAX_GB, threshold))
    return f"{threshold}G"


def obd_log_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Параметры, которые OBD принимает в obproxy-ce.global."""
    return {
        "log_dir_size_threshold": log_dir_size_threshold(cfg),
        "log_file_percentage": OBD_LOG_FILE_PERCENTAGE,
        "log_cleanup_interval": OBD_LOG_CLEANUP_INTERVAL,
    }


def apply_settings(mode: str, cfg: dict[str, Any] | None = None) -> dict[str, str]:
    settings = dict(LOG_MODES[resolve_mode(mode)])
    if resolve_mode(mode) != "debug":
        settings["log_dir_size_threshold"] = log_dir_size_threshold(cfg or {})
        settings["log_file_percentage"] = str(OBD_LOG_FILE_PERCENTAGE)
    return settings


def sql_literal(name: str, value: str) -> str:
    if name in STRING_KEYS:
        text = str(value).replace("'", "''")
        return f"'{text}'"
    return str(value)


def proxyconfig_set_sql(name: str, value: str) -> str:
    return f"ALTER PROXYCONFIG SET {name} = {sql_literal(name, value)}"


def apply_statements(mode: str, cfg: dict[str, Any] | None = None) -> list[str]:
    return [proxyconfig_set_sql(name, value) for name, value in apply_settings(mode, cfg).items()]


def parse_size_bytes(value: str) -> int | None:
    text = (value or "").strip().strip("'\"").upper().replace(" ", "")
    if not text:
        return None
    if text.isdigit():
        return int(text)
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


def normalize_bool(value: str) -> str:
    text = (value or "").strip().strip("'\"").lower()
    if text in {"1", "true", "on", "yes"}:
        return "true"
    if text in {"0", "false", "off", "no"}:
        return "false"
    return text


def normalize_value(name: str, value: str) -> str:
    text = (value or "").strip().strip("'\"")
    if name in BOOL_KEYS:
        return normalize_bool(text)
    if name.endswith("_level"):
        return text.upper()
    size = parse_size_bytes(text)
    if size is not None and (
        name in STRING_KEYS or name in {"log_file_percentage", "max_syslog_file_count"}
    ):
        if name == "log_file_percentage" or name == "route_diagnosis_level":
            return str(int(float(text))) if text else ""
        return str(size)
    if name in {"route_diagnosis_level", "log_file_percentage", "max_syslog_file_count"}:
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            return text.lower()
    return text.lower()


def settings_match(current: dict[str, str], expected: dict[str, str]) -> bool:
    for name, want in expected.items():
        got = current.get(name)
        if got is None:
            return False
        if normalize_value(name, got) != normalize_value(name, want):
            return False
    return True


def fetch_log_proxyconfig(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
) -> dict[str, str]:
    route = _load_route()
    merged: dict[str, str] = {}
    for pattern in SHOW_LIKE:
        proc = ob_sys.run_sql(
            endpoint, password, f"SHOW PROXYCONFIG LIKE '{pattern}'", ignore_error=True
        )
        if proc.returncode != 0:
            continue
        merged.update(route.parse_proxyconfig_rows(proc.stdout or ""))
    return merged


def print_log_config(label: str, values: dict[str, str], expected: dict[str, str] | None = None) -> None:
    print(f"{label}:")
    keys = list(SHOW_KEYS)
    if expected:
        for name in expected:
            if name not in keys:
                keys.append(name)
    for key in keys:
        print(f"  {key} = {values.get(key, '<нет в выводе>')}")


def apply_on_endpoint(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
    mode: str,
    cfg: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    for name, value in apply_settings(mode, cfg).items():
        sql = proxyconfig_set_sql(name, value)
        proc = ob_sys.run_sql(endpoint, password, sql, ignore_error=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or "alter failed"
            if name in REQUIRED_KEYS:
                raise RuntimeError(f"{name}: {err}")
            errors.append(f"{name}: {err}")
    return fetch_log_proxyconfig(ob_sys, endpoint, password), errors


def apply_all(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    mode: str | None = None,
    skip_if_ok: bool = False,
) -> int:
    """Выставить режим логов на каждом obproxy. Возвращает число ошибок."""
    route = _load_route()
    ob_sys = _load_ob_sys()
    deploy_name = inv.get("DEPLOY_NAME", "")
    if not deploy_name:
        raise RuntimeError("DEPLOY_NAME не задан в inventory.env")
    resolved = mode_from_cfg(cfg, mode)
    expected = apply_settings(resolved, cfg)
    endpoints = route.pick_obproxy_endpoints(ob_sys, cfg, inv)
    print(
        "Режим "
        + resolved
        + ": "
        + ", ".join(f"{k}={v}" for k, v in expected.items())
    )
    failed = 0
    for endpoint in endpoints:
        label = route.format_proxy_label(endpoint)
        try:
            password = route.connect_proxy(ob_sys, endpoint, cfg, deploy_name)
            before = fetch_log_proxyconfig(ob_sys, endpoint, password)
            if settings_match(before, expected) and skip_if_ok:
                print(f"{label}: уже {resolved} — пропуск")
                continue
            after, errors = apply_on_endpoint(
                ob_sys, endpoint, password, resolved, cfg
            )
            print_log_config(label, after, expected)
            for item in errors:
                print(f"  WARN: {item}")
            required_ok = all(
                normalize_value(name, after.get(name, ""))
                == normalize_value(name, expected[name])
                for name in REQUIRED_KEYS
            )
            present = {k: v for k, v in expected.items() if k in after}
            if not required_ok or not settings_match(after, present):
                print(f"  ERROR: после ALTER PROXYCONFIG значения не совпали с {resolved}")
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
    mode = mode_from_cfg(cfg, getattr(args, "mode", None))
    endpoints = route.pick_obproxy_endpoints(ob_sys, cfg, inv)
    expected = apply_settings(mode, cfg)
    print(f"Целевой режим {mode}")
    for endpoint in endpoints:
        password = route.connect_proxy(ob_sys, endpoint, cfg, deploy_name)
        values = fetch_log_proxyconfig(ob_sys, endpoint, password)
        print_log_config(route.format_proxy_label(endpoint), values, expected)
        if settings_match(values, expected):
            print(f"  режим: {mode}")
        else:
            print(f"  режим: не {mode} — см. docs/obproxy-logging.md")


def cmd_apply(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    failed = apply_all(
        cfg, inv, mode=args.mode, skip_if_ok=args.skip_if_ok
    )
    if failed:
        sys.exit(1)
    print()
    print("Уровень логов действует сразу, рестарт ODP не нужен.")
    print("Какой файл рос: du -sh ~/obproxy/log/*  на каждом obproxy.")


def cmd_self_test(_args: argparse.Namespace) -> None:
    assert resolve_mode("INFO") == "info"
    assert apply_statements("info")[0] == "ALTER PROXYCONFIG SET syslog_level = 'INFO'"
    assert "enable_syslog_wf = false" in apply_statements("info")[1]
    warn_sql = apply_statements("warn")
    assert "ALTER PROXYCONFIG SET syslog_level = 'WARN'" in warn_sql
    assert "ALTER PROXYCONFIG SET monitor_log_level = 'WARN'" in warn_sql
    assert "ALTER PROXYCONFIG SET route_diagnosis_level = 1" in warn_sql
    debug_sql = apply_statements("debug")
    assert "ALTER PROXYCONFIG SET syslog_level = 'WDIAG'" in debug_sql
    cfg = {"vm_profiles": {"obproxy": {"boot_disk": {"size_gb": 20}}}}
    assert log_dir_size_threshold(cfg) == "8G"
    assert obd_log_settings(cfg)["log_file_percentage"] == 50
    assert settings_match(
        {"syslog_level": "info", "enable_syslog_wf": "False", "enable_async_log": "1"},
        {"syslog_level": "INFO", "enable_syslog_wf": "false", "enable_async_log": "true"},
    )
    assert parse_size_bytes("8G") == parse_size_bytes("8GB")
    assert parse_size_bytes("10MB") == parse_size_bytes("10M")
    assert mode_from_cfg({"oceanbase": {"obproxy": {"log_mode": "warn"}}}) == "warn"
    try:
        resolve_mode("silent")
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

    p_show = sub.add_parser("show", help="SHOW PROXYCONFIG логов на каждом obproxy")
    _add_io_args(p_show, with_defaults=False)
    p_show.add_argument("--mode", default=None, choices=sorted(LOG_MODES))
    p_show.set_defaults(func=cmd_show)

    p_apply = sub.add_parser("apply", help="ALTER PROXYCONFIG логов на каждом obproxy")
    _add_io_args(p_apply, with_defaults=False)
    p_apply.add_argument(
        "--mode",
        default=None,
        choices=sorted(LOG_MODES),
        help="info = продакшен; warn = только WARN+; debug = WDIAG. "
        "По умолчанию oceanbase.obproxy.log_mode или info",
    )
    p_apply.add_argument(
        "--skip-if-ok",
        action="store_true",
        help="Не трогать экземпляр, если значения уже совпадают",
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
