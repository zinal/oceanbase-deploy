#!/usr/bin/env python3
"""Тесты tenant-create.py (без кластера)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import importlib.util

spec = importlib.util.spec_from_file_location(
    "tenant_create", ROOT / "scripts" / "lib" / "tenant-create.py"
)
tenant_create = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(tenant_create)

from vm_profiles import validate_tenant_section  # noqa: E402


def test_defaults() -> None:
    cfg = {}
    resolved = tenant_create.resolve_tenant_cfg(cfg)
    assert resolved["tenant_name"] == "tpcc"
    assert resolved["username"] == "tpcc"
    assert resolved["database"] == "tpcc"
    assert resolved["root_password"] == "ChangeMe!"
    assert resolved["user_password"] == "ChangeMe!"
    assert resolved["mode"] == "htap"


def test_mode_mapping() -> None:
    assert tenant_create.resolve_optimize("htap") == "htap"
    assert tenant_create.resolve_optimize("oltp") == "express_oltp"
    assert tenant_create.resolve_optimize("olap") == "olap"
    assert tenant_create.resolve_optimize("kv") == "kv"


def test_validate_all_obd_modes() -> None:
    for mode in tenant_create.TENANT_OPTIMIZE_MODES:
        cfg = {"tenant": {"mode": mode}}
        issues = tenant_create.validate_tenant_cfg(tenant_create.resolve_tenant_cfg(cfg))
        assert issues == [], mode


def test_validate_oltp_alias() -> None:
    cfg = {"tenant": {"mode": "oltp"}}
    issues = tenant_create.validate_tenant_cfg(tenant_create.resolve_tenant_cfg(cfg))
    assert issues == []


def test_validate_bad_mode() -> None:
    cfg = {"tenant": {"mode": "mysql"}}
    issues = tenant_create.validate_tenant_cfg(tenant_create.resolve_tenant_cfg(cfg))
    assert any("tenant.mode" in i for i in issues)


def test_validate_bad_name() -> None:
    cfg = {"tenant": {"tenant_name": "1bad"}}
    issues = tenant_create.validate_tenant_cfg(tenant_create.resolve_tenant_cfg(cfg))
    assert any("tenant.tenant_name" in i for i in issues)


def test_vm_profiles_integration() -> None:
    cfg = {"tenant": {"mode": "complex_oltp"}}
    assert validate_tenant_section(cfg) == []


def test_sql_helpers() -> None:
    assert tenant_create.sql_literal("a'b") == "'a''b'"
    assert tenant_create.sql_identifier("tpcc") == "`tpcc`"
    assert (
        tenant_create.alter_user_password_sql("root", "ChangeMe!")
        == "ALTER USER `root` IDENTIFIED BY 'ChangeMe!'"
    )
    setup = tenant_create.user_setup_sql("tpcc", "Secret'1", "tpcc")
    assert "CREATE DATABASE IF NOT EXISTS `tpcc`" in setup
    assert "CREATE USER IF NOT EXISTS `tpcc` IDENTIFIED BY 'Secret''1'" in setup
    assert "ALTER USER `tpcc` IDENTIFIED BY 'Secret''1'" in setup


def test_password_candidates() -> None:
    assert tenant_create.tenant_password_candidates({"root_password": "x"}) == ["x", ""]
    assert tenant_create.tenant_password_candidates({"root_password": ""}) == [""]


class _FakeSql:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.root_set = False

    def run_sql(self, endpoint, password, sql, ignore_error=False):  # noqa: ANN001
        self.calls.append((password, sql))

        class Proc:
            returncode = 0
            stderr = ""
            stdout = ""

        if sql == "SELECT 1":
            ok = password == "" or (self.root_set and password == "Wanted!")
            Proc.returncode = 0 if ok else 1
            Proc.stderr = "" if ok else "Access denied"
            if not ok and not ignore_error:
                raise RuntimeError(Proc.stderr)
            return Proc()
        if sql.startswith("ALTER USER `root`"):
            self.root_set = True
        return Proc()


def test_ensure_root_password_from_empty() -> None:
    fake = _FakeSql()
    ep = {"ip": "10.0.0.1", "port": 2881, "user": "root@tpcc"}
    cfg = {"root_password": "Wanted!"}
    current = tenant_create.connect_tenant_password(fake, ep, cfg)
    assert current == ""
    tenant_create.ensure_root_password(fake, ep, current, "Wanted!")
    assert fake.root_set
    tenant_create.verify_tenant_login(fake, ep, "Wanted!")


def test_print_connect_help_uses_obclient() -> None:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    cfg = tenant_create.resolve_tenant_cfg({})
    ep = {
        "ip": "ob-yc-prod-observer-1",
        "port": 2881,
        "user": "root@tpcc",
        "via": "observer",
    }
    with redirect_stdout(buf):
        tenant_create.print_connect_help(cfg, ep, "obcluster", 2883)
    text = buf.getvalue()
    assert "obclient -hob-yc-prod-observer-1 -P2881 -uroot@tpcc" in text
    assert "mysql -h" not in text


def test_cli_config_after_subcommand() -> None:
    """08-create-tenant.sh вызывает: validate --config FILE, create --config FILE --inventory FILE."""
    parser = tenant_create.build_parser()
    cfg = "/home/demo/oceanbase-deploy/config/deploy.yaml"
    inv = "/home/demo/oceanbase-deploy/generated/inventory.env"

    args = parser.parse_args(["validate", "--config", cfg])
    assert args.command == "validate"
    assert args.config == cfg
    assert args.func is tenant_create.cmd_validate

    args = parser.parse_args(["create", "--config", cfg, "--inventory", inv])
    assert args.command == "create"
    assert args.config == cfg
    assert args.inventory == inv
    assert args.func is tenant_create.cmd_create

    args = parser.parse_args(["passwd", "--config", cfg, "--inventory", inv])
    assert args.command == "passwd"
    assert args.func is tenant_create.cmd_passwd


def test_cli_config_before_subcommand() -> None:
    parser = tenant_create.build_parser()
    cfg = "/tmp/deploy.yaml"
    args = parser.parse_args(["--config", cfg, "validate"])
    assert args.config == cfg
    assert args.command == "validate"


if __name__ == "__main__":
    test_defaults()
    test_mode_mapping()
    test_validate_all_obd_modes()
    test_validate_oltp_alias()
    test_validate_bad_mode()
    test_validate_bad_name()
    test_vm_profiles_integration()
    test_sql_helpers()
    test_password_candidates()
    test_ensure_root_password_from_empty()
    test_print_connect_help_uses_obclient()
    test_cli_config_after_subcommand()
    test_cli_config_before_subcommand()
    tenant_create.cmd_self_test(type("Args", (), {})())
    print("ok")
