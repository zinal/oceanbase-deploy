#!/usr/bin/env python3
"""Лимит PS-хендлов / курсоров на сессии: tenant-параметр open_cursors.

Дефолт вендора 50. OceanBase Connector/J 2.x держит prepStmtCacheSize=250
на соединение → под нагрузкой -5930 (maximum open cursors / PS handles
exceeded). С V3.2.4 тем же параметром ограничены и cursor, и PS (считаются
раздельно). 0 = без лимита (только авария: память PsSessionInfo).

Официально (тенант, без рестарта):
https://www.oceanbase.com/docs/common-oceanbase-database-cn-1000000005685318

    ALTER SYSTEM SET open_cursors = 1000 TENANT = tpcc;
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

PARAM = "open_cursors"
VENDOR_DEFAULT = 50
RECOMMENDED_DEFAULT = 1000
MIN_VALUE = 0
MAX_VALUE = 65535
DEFAULT_TENANT_NAME = "tpcc"
TENANT_OBJECT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_TYPE_HINTS = frozenset(
    {"VARCHAR", "BOOL", "INT", "BIGINT", "DOUBLE", "CAPACITY", "STRING", "TIME", "ENUM"}
)


def _load_ob_sys() -> Any:
    path = LIB_DIR / "ob-sys.py"
    spec = importlib.util.spec_from_file_location("ob_sys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def parse_open_cursors(value: Any) -> int:
    """0…65535. Пустое / None → рекомендованные 1000, не вендорские 50."""
    if value is None:
        return RECOMMENDED_DEFAULT
    if isinstance(value, bool):
        raise ValueError("tenant.open_cursors: ожидалось целое 0…65535")
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        if text == "":
            return RECOMMENDED_DEFAULT
        try:
            parsed = int(text, 10)
        except ValueError as exc:
            raise ValueError(
                f"tenant.open_cursors={value!r} — целое в диапазоне "
                f"{MIN_VALUE}…{MAX_VALUE}"
            ) from exc
    if parsed < MIN_VALUE or parsed > MAX_VALUE:
        raise ValueError(
            f"tenant.open_cursors={parsed} — допустимо {MIN_VALUE}…{MAX_VALUE} "
            "(0 = без лимита)"
        )
    return parsed


def value_from_cfg(cfg: dict[str, Any], override: Any = None) -> int:
    if override is not None and str(override).strip() != "":
        return parse_open_cursors(override)
    tenant = cfg.get("tenant") or {}
    return parse_open_cursors(tenant.get("open_cursors"))


def tenant_name_from_cfg(cfg: dict[str, Any], override: str | None = None) -> str:
    if override and override.strip():
        name = override.strip()
    else:
        raw = (cfg.get("tenant") or {}).get("tenant_name")
        name = str(raw).strip() if raw not in (None, "") else DEFAULT_TENANT_NAME
    if not name:
        raise ValueError("Имя тенанта пустое")
    return tenant_sql_ident(name)


def tenant_sql_ident(name: str) -> str:
    """TENANT = ident в ALTER/SHOW PARAMETERS (без кавычек для [A-Za-z_][A-Za-z0-9_]*)."""
    if not TENANT_OBJECT_RE.match(name):
        raise ValueError(f"Недопустимое имя тенанта: {name!r}")
    return name


def alter_sql(value: int, tenant: str | None = None) -> str:
    parse_open_cursors(value)
    sql = f"ALTER SYSTEM SET {PARAM} = {int(value)}"
    if tenant:
        sql += f" TENANT = {tenant_sql_ident(tenant)}"
    return sql


def show_sql(tenant: str | None = None) -> str:
    sql = f"SHOW PARAMETERS LIKE '{PARAM}'"
    if tenant:
        sql += f" TENANT = {tenant_sql_ident(tenant)}"
    return sql


def parse_open_cursors_rows(stdout: str) -> int | None:
    """Первое целое value для open_cursors из SHOW PARAMETERS / две колонки."""
    for line in (stdout or "").splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) == 2 and parts[0] == PARAM:
            try:
                return int(str(parts[1]).strip().strip("'\""))
            except ValueError:
                continue
        for idx, part in enumerate(parts):
            if part != PARAM:
                continue
            value_idx = idx + 1
            if value_idx < len(parts) and parts[value_idx].upper() in _TYPE_HINTS:
                value_idx += 1
            if value_idx >= len(parts):
                break
            try:
                return int(str(parts[value_idx]).strip().strip("'\""))
            except ValueError:
                break
    return None


def fetch_open_cursors(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
    tenant: str,
) -> int | None:
    proc = ob_sys.run_sql(endpoint, password, show_sql(tenant), ignore_error=True)
    if proc.returncode != 0:
        return None
    return parse_open_cursors_rows(proc.stdout or "")


def apply_on_endpoint(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
    value: int,
    tenant: str,
) -> int | None:
    proc = ob_sys.run_sql(endpoint, password, alter_sql(value, tenant), ignore_error=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip() or "alter failed"
        raise RuntimeError(f"{PARAM}: {err}")
    return fetch_open_cursors(ob_sys, endpoint, password, tenant)


def apply_all(
    cfg: dict[str, Any],
    inv: dict[str, str],
    *,
    value: Any = None,
    tenant: str | None = None,
    skip_if_ok: bool = False,
) -> int:
    """Выставить open_cursors user-тенанту из sys. Возвращает 0/1."""
    ob_sys = _load_ob_sys()
    deploy_name = inv.get("DEPLOY_NAME", "")
    if not deploy_name:
        raise RuntimeError("DEPLOY_NAME не задан в inventory.env")
    if int(inv.get("OBSERVER_COUNT", "0") or 0) < 1 and int(
        inv.get("OBPROXY_COUNT", "0") or 0
    ) < 1:
        raise RuntimeError("В inventory нет OBSERVER_*_IP / OBPROXY_*_IP")
    target = value_from_cfg(cfg, value)
    tenant_name = tenant_name_from_cfg(cfg, tenant)
    endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    password = ob_sys.connect_sys_password(endpoint, cfg, deploy_name)
    label = f"{endpoint.get('via')} {endpoint.get('ip')}:{endpoint.get('port')}"
    print(f"Тенант {tenant_name}: {PARAM}={target} (вендорский дефолт {VENDOR_DEFAULT})")
    before = fetch_open_cursors(ob_sys, endpoint, password, tenant_name)
    if before is not None and before == target and skip_if_ok:
        print(f"{label}: уже {PARAM}={target} — пропуск")
        return 0
    after = apply_on_endpoint(ob_sys, endpoint, password, target, tenant_name)
    print(f"{label}: {PARAM} = {after if after is not None else '<нет в выводе>'}")
    if after != target:
        print(f"  ERROR: после ALTER SYSTEM {PARAM}={after}, ожидали {target}")
        return 1
    return 0


def cmd_show(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    deploy_name = inv.get("DEPLOY_NAME", "")
    target = value_from_cfg(cfg, getattr(args, "value", None))
    tenant_name = tenant_name_from_cfg(cfg, getattr(args, "tenant", None))
    endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    password = ob_sys.connect_sys_password(endpoint, cfg, deploy_name)
    current = fetch_open_cursors(ob_sys, endpoint, password, tenant_name)
    label = f"{endpoint.get('via')} {endpoint.get('ip')}:{endpoint.get('port')}"
    print(f"Тенант {tenant_name}")
    print(f"  цель: {PARAM}={target}")
    print(f"  {label}: {PARAM}={current if current is not None else '<нет в выводе>'}")
    if current == target:
        print("  статус: совпадает")
    else:
        print("  статус: не совпадает — ./scripts/deploy.sh open-cursors apply")
        print("  см. docs/open-cursors.md")


def cmd_apply(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    failed = apply_all(
        cfg,
        inv,
        value=getattr(args, "value", None),
        tenant=getattr(args, "tenant", None),
        skip_if_ok=args.skip_if_ok,
    )
    if failed:
        sys.exit(1)
    print()
    print("open_cursors действует сразу, рестарт observer не нужен.")
    print("Клиентский кэш PS: prepStmtCacheSize < open_cursors (docs/open-cursors.md).")


def cmd_self_test(_args: argparse.Namespace) -> None:
    assert parse_open_cursors(None) == RECOMMENDED_DEFAULT
    assert parse_open_cursors("") == RECOMMENDED_DEFAULT
    assert parse_open_cursors(2000) == 2000
    assert parse_open_cursors("0") == 0
    assert value_from_cfg({}) == RECOMMENDED_DEFAULT
    assert value_from_cfg({"tenant": {"open_cursors": 2000}}) == 2000
    assert value_from_cfg({"tenant": {"open_cursors": 50}}, 1000) == 1000
    assert alter_sql(1000, "tpcc") == "ALTER SYSTEM SET open_cursors = 1000 TENANT = tpcc"
    assert show_sql("tpcc") == "SHOW PARAMETERS LIKE 'open_cursors' TENANT = tpcc"
    show_row = (
        "zone1\tobserver\t10.0.0.1\t2882\topen_cursors\tINT\t50\t"
        "max open cursors\tOBSERVER\tTENANT"
    )
    assert parse_open_cursors_rows(show_row) == 50
    assert parse_open_cursors_rows("open_cursors\t1000\n") == 1000
    try:
        parse_open_cursors(65536)
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

    p_show = sub.add_parser("show", help="SHOW PARAMETERS open_cursors")
    _add_io_args(p_show, with_defaults=False)
    p_show.add_argument("--tenant", default=None, help="Имя тенанта (иначе tenant.tenant_name)")
    p_show.add_argument("--value", default=None, help="Ожидаемое значение для сверки")
    p_show.set_defaults(func=cmd_show)

    p_apply = sub.add_parser("apply", help="ALTER SYSTEM SET open_cursors")
    _add_io_args(p_apply, with_defaults=False)
    p_apply.add_argument("--tenant", default=None, help="Имя тенанта (иначе tenant.tenant_name)")
    p_apply.add_argument(
        "--value",
        default=None,
        help="0…65535. По умолчанию tenant.open_cursors или 1000",
    )
    p_apply.add_argument(
        "--skip-if-ok",
        action="store_true",
        help="Не трогать кластер, если значение уже совпадает",
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
