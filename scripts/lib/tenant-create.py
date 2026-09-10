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

# Значения `obd cluster tenant create -o` / `--optimize` (OceanBase ≥ 4.3).
TENANT_OPTIMIZE_MODES = frozenset(
    {
        "express_oltp",  # простой OLTP, высокая конкуренция, короткие запросы
        "complex_oltp",  # сложные транзакции, join, PL, длинные транзакции
        "olap",          # аналитика / real-time DW, колоночное хранение
        "htap",          # смешанные OLTP и OLAP
        "kv",            # key-value и wide-column нагрузки
    }
)

# Краткие алиасы (не передаются в OBD как есть).
TENANT_MODE_ALIASES = {
    "oltp": "express_oltp",
}

ALLOWED_TENANT_MODES = TENANT_OPTIMIZE_MODES | frozenset(TENANT_MODE_ALIASES)


def resolve_optimize(mode: str) -> str:
    """Преобразовать tenant.mode в значение для `obd -o`."""
    return TENANT_MODE_ALIASES.get(mode, mode)


def allowed_modes_help() -> str:
    modes = ", ".join(sorted(TENANT_OPTIMIZE_MODES))
    aliases = ", ".join(f"{k}={v}" for k, v in sorted(TENANT_MODE_ALIASES.items()))
    return f"{modes} ({aliases})"


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
    if mode not in ALLOWED_TENANT_MODES:
        issues.append(
            f"ERROR: tenant.mode={mode!r} — допустимо: {allowed_modes_help()}"
        )
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
    optimize = resolve_optimize(tenant_cfg["mode"])
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


def alter_user_password_sql(username: str, password: str) -> str:
    return f"ALTER USER {sql_identifier(username)} IDENTIFIED BY {sql_literal(password)}"


def user_setup_sql(username: str, user_password: str, database: str) -> list[str]:
    db = sql_identifier(database)
    user = sql_identifier(username)
    pwd = sql_literal(user_password)
    return [
        f"CREATE DATABASE IF NOT EXISTS {db}",
        f"CREATE USER IF NOT EXISTS {user} IDENTIFIED BY {pwd}",
        # IF NOT EXISTS не меняет пароль уже существующего пользователя.
        alter_user_password_sql(username, user_password),
        f"GRANT ALL PRIVILEGES ON {db}.* TO {user}",
    ]


def tenant_password_candidates(tenant_cfg: dict[str, str]) -> list[str]:
    """tenant.root_password, затем пустой (дефолт OBD после CREATE TENANT)."""
    seen: set[str] = set()
    out: list[str] = []
    for item in (tenant_cfg.get("root_password") or "", ""):
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def connect_tenant_password(
    ob_sys: Any,
    endpoint: dict[str, Any],
    tenant_cfg: dict[str, str],
) -> str:
    last_err = ""
    for password in tenant_password_candidates(tenant_cfg):
        proc = ob_sys.run_sql(endpoint, password, "SELECT 1", ignore_error=True)
        if proc.returncode == 0:
            return password
        last_err = (proc.stderr or proc.stdout or "").strip()
    raise RuntimeError(
        "Нет SQL к тенанту "
        f"{endpoint.get('user')} @ {endpoint.get('ip')}:{endpoint.get('port')}. "
        "Сначала попробуйте пустой пароль root@<tenant> на observer:2881, "
        "затем ALTER USER root IDENTIFIED BY '<tenant.root_password>'. "
        f"Последняя ошибка: {last_err or 'access denied'}"
    )


def ensure_root_password(
    ob_sys: Any,
    endpoint: dict[str, Any],
    current_password: str,
    desired: str,
) -> None:
    if current_password == desired:
        return
    print("Пароль root тенанта не совпадает с tenant.root_password — ставлю из config...")
    ob_sys.run_sql(endpoint, current_password, alter_user_password_sql("root", desired))


def setup_user_and_database(
    ob_sys: Any,
    endpoint: dict[str, Any],
    root_password: str,
    username: str,
    user_password: str,
    database: str,
) -> None:
    for sql in user_setup_sql(username, user_password, database):
        ob_sys.run_sql(endpoint, root_password, sql)


def verify_tenant_login(
    ob_sys: Any,
    endpoint: dict[str, Any],
    password: str,
) -> None:
    ob_sys.run_sql(endpoint, password, "SELECT 1")


def print_connect_help(
    tenant_cfg: dict[str, str],
    endpoint: dict[str, Any],
    cluster: str,
    proxy_port: int,
) -> None:
    tenant_name = tenant_cfg["tenant_name"]
    print()
    print("Тенант готов.")
    print(f"  Tenant:   {tenant_name}")
    print(f"  Mode:     {tenant_cfg['mode']} (obd -o {resolve_optimize(tenant_cfg['mode'])})")
    print(f"  Database: {tenant_cfg['database']}")
    print(f"  User:     {tenant_cfg['username']}")
    if endpoint.get("via") == "obproxy":
        app_user = f"{tenant_cfg['username']}@{tenant_name}#{cluster}"
        app_port = proxy_port
    else:
        app_user = f"{tenant_cfg['username']}@{tenant_name}"
        app_port = int(endpoint["port"])
    print()
    print("Подключение через obclient (пароль в кавычках: в bash `!` — history expansion):")
    print(
        f"  obclient -h{endpoint['ip']} -P{endpoint['port']} "
        f"-u{endpoint['user']} -p'<tenant.root_password>' -e 'SELECT 1'"
    )
    print(
        f"  obclient -h{endpoint['ip']} -P{app_port} "
        f"-u{app_user} -p'<tenant.user_password>'"
    )
    print("  Пароли — tenant.root_password / tenant.user_password в config/deploy.yaml")
    print("  Системный mysql (MariaDB 11+): не --ssl-mode=DISABLED, а --skip-ssl либо obclient")


def _load_tenant_context(
    args: argparse.Namespace,
) -> tuple[Any, dict[str, Any], dict[str, str], dict[str, str], str]:
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
    return ob_sys, cfg, inv, tenant_cfg, deploy_name


def apply_tenant_credentials(
    ob_sys: Any,
    cfg: dict[str, Any],
    inv: dict[str, str],
    tenant_cfg: dict[str, str],
) -> dict[str, Any]:
    tenant_name = tenant_cfg["tenant_name"]
    tenant_endpoint = build_tenant_endpoint(ob_sys, cfg, inv, tenant_name)
    print(
        f"Подключение к тенанту через {tenant_endpoint['via']} "
        f"{tenant_endpoint['ip']}:{tenant_endpoint['port']}"
    )
    current = connect_tenant_password(ob_sys, tenant_endpoint, tenant_cfg)
    ensure_root_password(
        ob_sys, tenant_endpoint, current, tenant_cfg["root_password"]
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
    return tenant_endpoint


def cmd_create(args: argparse.Namespace) -> None:
    ob_sys, cfg, inv, tenant_cfg, deploy_name = _load_tenant_context(args)
    sys_endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    sys_password = ob_sys.connect_sys_password(sys_endpoint, cfg, deploy_name)

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

    tenant_endpoint = apply_tenant_credentials(ob_sys, cfg, inv, tenant_cfg)
    cluster = ob_sys.cfg_str(cfg, "oceanbase.cluster_name", "obcluster")
    proxy_port = ob_sys.cfg_int(cfg, "oceanbase.ports.obproxy", 2883)
    print_connect_help(tenant_cfg, tenant_endpoint, cluster, proxy_port)


def cmd_passwd(args: argparse.Namespace) -> None:
    """Поставить tenant.root_password / user_password, даже если тенант уже есть."""
    ob_sys, cfg, inv, tenant_cfg, deploy_name = _load_tenant_context(args)
    sys_endpoint = ob_sys.pick_sql_endpoint(cfg, inv)
    sys_password = ob_sys.connect_sys_password(sys_endpoint, cfg, deploy_name)
    tenant_name = tenant_cfg["tenant_name"]
    if not tenant_exists(ob_sys, sys_endpoint, sys_password, tenant_name):
        raise RuntimeError(
            f"Тенант {tenant_name} не найден. Сначала: ./scripts/deploy.sh tenant"
        )
    tenant_endpoint = apply_tenant_credentials(ob_sys, cfg, inv, tenant_cfg)
    cluster = ob_sys.cfg_str(cfg, "oceanbase.cluster_name", "obcluster")
    proxy_port = ob_sys.cfg_int(cfg, "oceanbase.ports.obproxy", 2883)
    print_connect_help(tenant_cfg, tenant_endpoint, cluster, proxy_port)


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
    cfg = {"tenant": {"mode": "express_oltp", "tenant_name": "tpcc"}}
    resolved = resolve_tenant_cfg(cfg)
    assert resolved["mode"] == "express_oltp"
    assert resolved["username"] == "tpcc"
    assert resolve_optimize("oltp") == "express_oltp"
    assert resolve_optimize("htap") == "htap"
    assert sql_identifier("tpcc") == "`tpcc`"
    assert sql_literal("a'b") == "'a''b'"
    issues = validate_tenant_cfg({"mode": "bad", **{k: v for k, v in DEFAULTS.items() if k != "mode"}})
    assert any("tenant.mode" in i for i in issues)
    for mode in TENANT_OPTIMIZE_MODES:
        assert validate_tenant_cfg({**DEFAULTS, "mode": mode}) == []
    assert tenant_password_candidates({"root_password": "ChangeMe!"}) == ["ChangeMe!", ""]
    assert tenant_password_candidates({"root_password": ""}) == [""]
    assert alter_user_password_sql("root", "a'b") == "ALTER USER `root` IDENTIFIED BY 'a''b'"
    setup = user_setup_sql("tpcc", "ChangeMe!", "tpcc")
    assert any(s.startswith("ALTER USER `tpcc` IDENTIFIED BY") for s in setup)
    print("self-test ok")


def _add_io_args(parser: argparse.ArgumentParser, *, with_defaults: bool) -> None:
    """--config/--inventory: на родителе с дефолтами, на подкомандах с SUPPRESS.

    Иначе argparse не принимает флаги после подкоманды (`validate --config ...`),
    а дефолты подпарсера затирают значение, заданное до подкоманды.
    """
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

    p_create = sub.add_parser("create", help="Создать тенант, пользователя и БД")
    _add_io_args(p_create, with_defaults=False)
    p_create.set_defaults(func=cmd_create)

    p_passwd = sub.add_parser(
        "passwd",
        help="Поставить пароли root/user из config (тенант уже существует)",
    )
    _add_io_args(p_passwd, with_defaults=False)
    p_passwd.set_defaults(func=cmd_passwd)

    p_val = sub.add_parser("validate", help="Проверить секцию tenant в config")
    _add_io_args(p_val, with_defaults=False)
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
