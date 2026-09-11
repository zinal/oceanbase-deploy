#!/usr/bin/env python3
"""Детальность логов observer: syslog_level и соседние ALTER SYSTEM.

Дефолт syslog_level=WDIAG: ожидаемые DIAG-сообщения на горячем пути
(транзакции, RPC, replay) раздувают observer.log / election.log /
rootservice.log и конкурируют с clog за IO на boot-диске.

Официально (кластер, sys-тенант, без рестарта):
https://oceanbase.github.io/oceanbase/logging/

    ALTER SYSTEM SET syslog_level = 'INFO';
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

# info — продакшен: INFO, без .wf, recycle, IO ≤ 10M.
# warn — только WARN+ и без trace-лога.
# debug — вендорский WDIAG, recycle оставляем чтобы не забить диск.
LOG_MODES: dict[str, dict[str, str]] = {
    "info": {
        "syslog_level": "INFO",
        "enable_syslog_wf": "false",
        "enable_async_syslog": "true",
        "enable_syslog_recycle": "true",
        "max_syslog_file_count": "20",
        "syslog_io_bandwidth_limit": "10M",
    },
    "warn": {
        "syslog_level": "WARN",
        "enable_syslog_wf": "false",
        "enable_async_syslog": "true",
        "enable_syslog_recycle": "true",
        "max_syslog_file_count": "20",
        "syslog_io_bandwidth_limit": "10M",
        "enable_record_trace_log": "false",
    },
    "debug": {
        "syslog_level": "WDIAG",
        "enable_syslog_wf": "true",
        "enable_async_syslog": "true",
        "enable_syslog_recycle": "true",
        "max_syslog_file_count": "20",
    },
}

STRING_KEYS = frozenset({"syslog_level", "syslog_io_bandwidth_limit"})
BOOL_KEYS = frozenset(
    {
        "enable_syslog_wf",
        "enable_async_syslog",
        "enable_syslog_recycle",
        "enable_record_trace_log",
    }
)
INT_KEYS = frozenset({"max_syslog_file_count"})
REQUIRED_KEYS = frozenset({"syslog_level"})
SHOW_KEYS = (
    "syslog_level",
    "enable_syslog_wf",
    "enable_async_syslog",
    "enable_syslog_recycle",
    "max_syslog_file_count",
    "syslog_io_bandwidth_limit",
    "enable_record_trace_log",
)
PARAM_NAMES = frozenset(SHOW_KEYS)
_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]I?B?)?$", re.IGNORECASE)
_TYPE_HINTS = frozenset(
    {"VARCHAR", "BOOL", "INT", "BIGINT", "DOUBLE", "CAPACITY", "STRING", "TIME", "ENUM"}
)
GV_PARAMETERS_SQL = (
    "SELECT name, value FROM oceanbase.GV$OB_PARAMETERS WHERE name IN ({names})"
)


def _load_ob_sys() -> Any:
    path = LIB_DIR / "ob-sys.py"
    spec = importlib.util.spec_from_file_location("ob_sys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    raw = ob.get("log_mode")
    if isinstance(raw, dict):
        raw = None
    return resolve_mode(str(raw or "info"))


def apply_settings(mode: str) -> dict[str, str]:
    return dict(LOG_MODES[resolve_mode(mode)])


def obd_log_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    """Параметры oceanbase-ce.global, которые знает плагин OBD."""
    mode = mode_from_cfg(cfg)
    raw = apply_settings(mode)
    out: dict[str, Any] = {
        "syslog_level": raw["syslog_level"],
        "enable_syslog_wf": raw["enable_syslog_wf"] == "true",
        "enable_async_syslog": raw["enable_async_syslog"] == "true",
        "enable_syslog_recycle": raw["enable_syslog_recycle"] == "true",
        "max_syslog_file_count": int(raw["max_syslog_file_count"]),
        "syslog_io_bandwidth_limit": "10MB",
    }
    if "enable_record_trace_log" in raw:
        out["enable_record_trace_log"] = raw["enable_record_trace_log"] == "true"
    return out


def sql_literal(name: str, value: str) -> str:
    if name in STRING_KEYS:
        text = str(value).replace("'", "''")
        return f"'{text}'"
    return str(value)


def alter_system_sql(name: str, value: str) -> str:
    return f"ALTER SYSTEM SET {name} = {sql_literal(name, value)}"


def apply_statements(mode: str) -> list[str]:
    return [alter_system_sql(name, value) for name, value in apply_settings(mode).items()]


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
    if name in INT_KEYS:
        try:
            return str(int(float(text)))
        except (TypeError, ValueError):
            return text.lower()
    size = parse_size_bytes(text)
    if size is not None and name in STRING_KEYS:
        return str(size)
    return text.lower()


def settings_match(current: dict[str, str], expected: dict[str, str]) -> bool:
    for name, want in expected.items():
        got = current.get(name)
        if got is None:
            return False
        if normalize_value(name, got) != normalize_value(name, want):
            return False
    return True


def parse_name_value_rows(stdout: str) -> dict[str, str]:
    """SELECT name, value → две колонки; SHOW PARAMETERS — name среди полей."""
    values: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) == 2 and parts[0] in PARAM_NAMES:
            values.setdefault(parts[0], parts[1])
            continue
        for idx, part in enumerate(parts):
            if part not in PARAM_NAMES:
                continue
            if idx + 2 < len(parts) and parts[idx + 1].upper() in _TYPE_HINTS:
                values.setdefault(part, parts[idx + 2])
            elif idx + 1 < len(parts):
                values.setdefault(part, parts[idx + 1])
            break
    return values


def fetch_log_parameters(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
) -> dict[str, str]:
    names = ", ".join(f"'{name}'" for name in SHOW_KEYS)
    proc = ob_sys.run_sql(
        endpoint, password, GV_PARAMETERS_SQL.format(names=names), ignore_error=True
    )
    values: dict[str, str] = {}
    if proc.returncode == 0:
        values.update(parse_name_value_rows(proc.stdout or ""))
    missing = [name for name in SHOW_KEYS if name not in values]
    for name in missing:
        proc = ob_sys.run_sql(
            endpoint, password, f"SHOW PARAMETERS LIKE '{name}'", ignore_error=True
        )
        if proc.returncode != 0:
            continue
        values.update(parse_name_value_rows(proc.stdout or ""))
    return values


def print_log_config(
    label: str, values: dict[str, str], expected: dict[str, str] | None = None
) -> None:
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
) -> tuple[dict[str, str], list[str]]:
    errors: list[str] = []
    for name, value in apply_settings(mode).items():
        sql = alter_system_sql(name, value)
        proc = ob_sys.run_sql(endpoint, password, sql, ignore_error=True)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip() or "alter failed"
            if name in REQUIRED_KEYS:
                raise RuntimeError(f"{name}: {err}")
            errors.append(f"{name}: {err}")
    return fetch_log_parameters(ob_sys, endpoint, password), errors


def apply_all(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    mode: str | None = None,
    skip_if_ok: bool = False,
) -> int:
    """Выставить режим логов кластера (один ALTER SYSTEM). Возвращает 0/1."""
    ob_sys = _load_ob_sys()
    deploy_name = inv.get("DEPLOY_NAME", "")
    if not deploy_name:
        raise RuntimeError("DEPLOY_NAME не задан в inventory.env")
    if int(inv.get("OBSERVER_COUNT", "0") or 0) < 1:
        raise RuntimeError("В inventory нет OBSERVER_*_IP")
    resolved = mode_from_cfg(cfg, mode)
    expected = apply_settings(resolved)
    endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    password = ob_sys.connect_sys_password(endpoint, cfg, deploy_name)
    label = f"{endpoint.get('via')} {endpoint.get('ip')}:{endpoint.get('port')}"
    print("Режим " + resolved + ": " + ", ".join(f"{k}={v}" for k, v in expected.items()))
    before = fetch_log_parameters(ob_sys, endpoint, password)
    if settings_match(before, expected) and skip_if_ok:
        print(f"{label}: уже {resolved} — пропуск")
        return 0
    after, errors = apply_on_endpoint(ob_sys, endpoint, password, resolved)
    print_log_config(label, after, expected)
    for item in errors:
        print(f"  WARN: {item}")
    required_ok = all(
        normalize_value(name, after.get(name, "")) == normalize_value(name, expected[name])
        for name in REQUIRED_KEYS
    )
    present = {k: v for k, v in expected.items() if k in after}
    if not required_ok or not settings_match(after, present):
        print(f"  ERROR: после ALTER SYSTEM значения не совпали с {resolved}")
        return 1
    return 0


def cmd_show(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    deploy_name = inv.get("DEPLOY_NAME", "")
    mode = mode_from_cfg(cfg, getattr(args, "mode", None))
    expected = apply_settings(mode)
    endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    password = ob_sys.connect_sys_password(endpoint, cfg, deploy_name)
    values = fetch_log_parameters(ob_sys, endpoint, password)
    print(f"Целевой режим {mode}")
    print_log_config(
        f"{endpoint.get('via')} {endpoint.get('ip')}:{endpoint.get('port')}",
        values,
        expected,
    )
    if settings_match(values, expected):
        print(f"  режим: {mode}")
    else:
        print(f"  режим: не {mode} — см. docs/observer-logging.md")


def cmd_apply(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    failed = apply_all(cfg, inv, mode=args.mode, skip_if_ok=args.skip_if_ok)
    if failed:
        sys.exit(1)
    print()
    print("Уровень логов observer действует сразу на весь кластер, рестарт не нужен.")
    print("Какой файл рос: du -sh ~/observer/log/*  на каждом observer.")


def cmd_self_test(_args: argparse.Namespace) -> None:
    assert resolve_mode("INFO") == "info"
    assert apply_statements("info")[0] == "ALTER SYSTEM SET syslog_level = 'INFO'"
    assert "ALTER SYSTEM SET enable_syslog_wf = false" in apply_statements("info")
    assert "ALTER SYSTEM SET enable_syslog_recycle = true" in apply_statements("info")
    assert "ALTER SYSTEM SET syslog_io_bandwidth_limit = '10M'" in apply_statements("info")
    warn_sql = apply_statements("warn")
    assert "ALTER SYSTEM SET syslog_level = 'WARN'" in warn_sql
    assert "ALTER SYSTEM SET enable_record_trace_log = false" in warn_sql
    debug_sql = apply_statements("debug")
    assert "ALTER SYSTEM SET syslog_level = 'WDIAG'" in debug_sql
    parsed = parse_name_value_rows("syslog_level\tINFO\nenable_syslog_wf\tFalse\n")
    assert parsed["syslog_level"] == "INFO"
    show_row = (
        "zone1\tobserver\t10.0.0.1\t2882\tsyslog_level\tVARCHAR\tWDIAG\t"
        "log level\tOBSERVER\tCLUSTER"
    )
    assert parse_name_value_rows(show_row)["syslog_level"] == "WDIAG"
    assert settings_match(
        {
            "syslog_level": "info",
            "enable_syslog_wf": "False",
            "enable_async_syslog": "1",
            "enable_syslog_recycle": "true",
            "max_syslog_file_count": "20",
            "syslog_io_bandwidth_limit": "10MB",
        },
        apply_settings("info"),
    )
    assert mode_from_cfg({"oceanbase": {"log_mode": "warn"}}) == "warn"
    assert obd_log_settings({})["syslog_level"] == "INFO"
    assert obd_log_settings({})["enable_syslog_recycle"] is True
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

    p_show = sub.add_parser("show", help="SHOW PARAMETERS логов observer")
    _add_io_args(p_show, with_defaults=False)
    p_show.add_argument("--mode", default=None, choices=sorted(LOG_MODES))
    p_show.set_defaults(func=cmd_show)

    p_apply = sub.add_parser("apply", help="ALTER SYSTEM логов observer")
    _add_io_args(p_apply, with_defaults=False)
    p_apply.add_argument(
        "--mode",
        default=None,
        choices=sorted(LOG_MODES),
        help="info = продакшен; warn = только WARN+; debug = WDIAG. "
        "По умолчанию oceanbase.log_mode или info",
    )
    p_apply.add_argument(
        "--skip-if-ok",
        action="store_true",
        help="Не трогать кластер, если значения уже совпадают",
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
