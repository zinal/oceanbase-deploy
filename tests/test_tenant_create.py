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


def test_user_and_database_sql_grants_global_create() -> None:
    """OceanBase: GRANT ALL ON db.* недостаточно для CREATE TABLE (ERROR 1227)."""
    stmts = tenant_create.user_and_database_sql("tpcc", "s3cret", "tpcc")
    joined = ";\n".join(stmts)
    assert "CREATE DATABASE IF NOT EXISTS `tpcc`" in joined
    assert "CREATE USER IF NOT EXISTS `tpcc` IDENTIFIED BY 's3cret'" in joined
    assert "GRANT ALL PRIVILEGES ON *.* TO `tpcc`" in joined
    assert "GRANT CREATE ON *.* TO `tpcc`" in joined
    assert "GRANT ALL PRIVILEGES ON `tpcc`.* TO `tpcc`" in joined


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
    test_user_and_database_sql_grants_global_create()
    test_cli_config_after_subcommand()
    test_cli_config_before_subcommand()
    tenant_create.cmd_self_test(type("Args", (), {})())
    print("ok")
