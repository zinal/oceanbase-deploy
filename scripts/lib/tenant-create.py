#!/usr/bin/env python3
"""Создание user tenant после deploy (oceanbase-skills/tenant-management).

1. `obd cluster tenant create` с дефолтами OBD (max-cpu=0, memory-size=0, unit-num=0)
   — все ресурсы, доступные для тенантов (sys и OCP уже заняли свою долю).
2. SQL в тенанте: пользователь и база данных.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = Path(__file__).resolve().parent

TENANT_OBJECT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

DEFAULTS: dict[str, str] = {
    "tenant_name": "tpcc",
    "username": "tpcc",
    "database": "tpcc",
    "root_password": "ChangeMe!",
    "user_password": "ChangeMe!",
    "mode": "htap",
}

MODE_TO_OPTIMIZE = {
    "htap": "htap",
    "oltp": "express_oltp",
}


def _load_ob_sys() -> Any:
    path = LIB_DIR / "ob-sys.py"
    spec = importlib.util.spec_from_file_location("ob_sys", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось загрузить {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def resolve_tenant_cfg(cfg: dict[str, Any]) -> dict[str, str]:
    raw = cfg.get("tenant") or {}
    resolved = dict(DEFAULTS)
    for key in DEFAULTS:
        value = raw.get(key)
        if value is not None and str(value).strip() != "":
            resolved[key] = str(value)
    resolved["mode"] = resolved["mode"].strip().lower()
    return resolved


def validate_tenant_cfg(tenant_cfg: dict[str, str]) -> list[str]:
    issues: list[str] = []
    mode = tenant_cfg.get("mode", "")
    if mode not in MODE_TO_OPTIMIZE:
        issues.append(f"ERROR: tenant.mode={mode!r} — допустимо: htap, oltp")
    for label, key in (
        ("tenant_name", "tenant_name"),
        ("username", "username"),
        ("database", "database"),
    ):
        name = tenant_cfg.get(key, "")
        if not TENANT_OBJECT_RE.match(name):
            issues.append(
                f"ERROR: tenant.{key}={name!r} — буквы/цифры/_, начинается с буквы или _"
            )
    for label in ("root_password", "user_password"):
        if not tenant_cfg.get(label):
            issues.append(f"ERROR: tenant.{label} не задан")
    return issues


def sql_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def sql_identifier(name: str) -> str:
    if not TENANT_OBJECT_RE.match(name):
        raise ValueError(f"Недопустимый идентификатор SQL: {name}")
    return "`" + name.replace("`", "``") + "`"


def tenant_exists(ob_sys: Any, endpoint: dict[str, Any], sys_password: str, tenant_name: str) -> bool:
    sql = (
        "SELECT COUNT(*) FROM oceanbase.DBA_OB_TENANTS "
        f"WHERE TENANT_NAME={sql_literal(tenant_name)}"
    )
    proc = ob_sys.run_sql(endpoint, sys_password, sql)
    count = (proc.stdout or "").strip().split()[-1] if proc.stdout else "0"
    try:
        return int(count) > 0
    except ValueError:
        return False


def build_tenant_endpoint(
    ob_sys: Any,
    cfg: dict[str, Any],
    inv: dict[str, str],
    tenant_name: str,
) -> dict[str, Any]:
    cluster = ob_sys.cfg_str(cfg, "oceanbase.cluster_name", "obcluster")
    mysql_port = ob_sys.cfg_int(cfg, "oceanbase.ports.mysql", 2881)
    proxy_port = ob_sys.cfg_int(cfg, "oceanbase.ports.obproxy", 2883)

    for _idx, ip, name in ob_sys.inventory_ips(inv, "OBPROXY"):
        return {
            "ip": ip,
            "port": proxy_port,
            "user": f"root@{tenant_name}#{cluster}",
            "via": "obproxy",
            "name": name,
        }

    for _idx, ip, name in ob_sys.inventory_ips(inv, "OBSERVER"):
        return {
            "ip": ip,
            "port": mysql_port,
            "user": f"root@{tenant_name}",
            "via": "observer",
            "name": name,
        }

    raise RuntimeError("Нет SQL-endpoint в inventory для подключения к тенанту")


def run_obd_tenant_create(deploy_name: str, tenant_cfg: dict[str, str]) -> None:
    optimize = MODE_TO_OPTIMIZE[tenant_cfg["mode"]]
    cmd = [
        "obd",
        "cluster",
        "tenant",
        "create",
        deploy_name,
        "-n",
        tenant_cfg["tenant_name"],
        "--password",
        tenant_cfg["root_password"],
        "-o",
        optimize,
        "-s",
        "ob_tcp_invited_nodes='%'",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"obd cluster tenant create не удался: {err}")
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")


def setup_user_and_database(
    ob_sys: Any,
    endpoint: dict[str, Any],
    root_password: str,
    username: str,
    user_password: str,
    database: str,
) -> None:
    db = sql_identifier(database)
    user = sql_identifier(username)
    pwd = sql_literal(user_password)
    statements = [
        f"CREATE DATABASE IF NOT EXISTS {db}",
        f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY {pwd}",
        f"GRANT ALL PRIVILEGES ON {db}.* TO {user}",
    ]
    for sql in statements:
        ob_sys.run_sql(endpoint, root_password, sql)


def verify_tenant_login(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
) -> None:
    ob_sys.run_sql(endpoint, password, "SELECT 1")


def cmd_create(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    inv = ob_sys.load_inventory(Path(args.inventory))
    deploy_name = inv.get("DEPLOY_NAME", "")
    if not deploy_name:
        raise RuntimeError("DEPLOY_NAME не задан в inventory.env")

    tenant_cfg = resolve_tenant_cfg(cfg)
    issues = validate_tenant_cfg(tenant_cfg)
    if issues:
        for item in issues:
            print(item, file=sys.stderr)
        sys.exit(1)

    sys_endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    sys_password = ob_sys.discover_root_password(cfg, deploy_name)

    tenant_name = tenant_cfg["tenant_name"]
    exists = tenant_exists(ob_sys, sys_endpoint, sys_password, tenant_name)

    if exists:
        print(f"Тенант {tenant_name} уже существует — пропуск obd cluster tenant create")
    else:
        print(
            f"Создание тенанта {tenant_name} (режим {tenant_cfg['mode']}, "
            "ресурсы: все доступные OBD по умолчанию)..."
        )
        run_obd_tenant_create(deploy_name, tenant_cfg)

    tenant_endpoint = build_tenant_endpoint(ob_sys, cfg, inv, tenant_name)
    print(
        f"Подключение к тенанту через {tenant_endpoint['via']} "
        f"{tenant_endpoint['ip']}:{tenant_endpoint['port']}"
    )
    verify_tenant_login(ob_sys, tenant_endpoint, tenant_cfg["root_password"])

    print(
        f"Создание пользователя {tenant_cfg['username']} и БД {tenant_cfg['database']}..."
    )
    setup_user_and_database(
        ob_sys,
        tenant_endpoint,
        tenant_cfg["root_password"],
        tenant_cfg["username"],
        tenant_cfg["user_password"],
        tenant_cfg["database"],
    )

    cluster = ob_sys.cfg_str(cfg, "oceanbase.cluster_name", "obcluster")
    proxy_port = ob_sys.cfg_int(cfg, "oceanbase.ports.obproxy", 2883)
    print()
    print("Тенант готов.")
    print(f"  Tenant:   {tenant_name}")
    print(f"  Mode:     {tenant_cfg['mode']} ({MODE_TO_OPTIMIZE[tenant_cfg['mode']]})")
    print(f"  Database: {tenant_cfg['database']}")
    print(f"  User:     {tenant_cfg['username']}")
    print()
    print("Подключение (через OBProxy, если включён):")
    print(
        f"  mysql -h<{tenant_endpoint['ip']}> -P{proxy_port} "
        f"-u{tenant_cfg['username']}@{tenant_name}#{cluster} -p"
    )
    print(f"  (пароль пользователя — tenant.user_password в config/deploy.yaml)")


def cmd_validate(args: argparse.Namespace) -> None:
    ob_sys = _load_ob_sys()
    cfg = ob_sys.load_yaml(Path(args.config))
    tenant_cfg = resolve_tenant_cfg(cfg)
    issues = validate_tenant_cfg(tenant_cfg)
    has_error = False
    for item in issues:
        if item.startswith("ERROR"):
            print(item, file=sys.stderr)
            has_error = True
        else:
            print(item)
    if has_error:
        sys.exit(1)


def cmd_self_test(_args: argparse.Namespace) -> None:
    cfg = {"tenant": {"mode": "oltp", "tenant_name": "tpcc"}}
    resolved = resolve_tenant_cfg(cfg)
    assert resolved["mode"] == "oltp"
    assert resolved["username"] == "tpcc"
    assert MODE_TO_OPTIMIZE["oltp"] == "express_oltp"
    assert sql_identifier("tpcc") == "`tpcc`"
    assert sql_literal("a'b") == "'a''b'"
    issues = validate_tenant_cfg({"mode": "bad", **{k: v for k, v in DEFAULTS.items() if k != "mode"}})
    assert any("tenant.mode" in i for i in issues)
    print("self-test ok")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "deploy.yaml"))
    parser.add_argument("--inventory", default=str(REPO_ROOT / "generated" / "inventory.env"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", help="Создать тенант, пользователя и БД")
    p_create.set_defaults(func=cmd_create)

    p_val = sub.add_parser("validate", help="Проверить секцию tenant в config")
    p_val.set_defaults(func=cmd_validate)

    p_test = sub.add_parser("self-test", help="Локальные проверки без кластера")
    p_test.set_defaults(func=cmd_self_test)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
