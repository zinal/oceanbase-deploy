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
    assert tenant_create.MODE_TO_OPTIMIZE["htap"] == "htap"
    assert tenant_create.MODE_TO_OPTIMIZE["oltp"] == "express_oltp"


def test_validate_ok() -> None:
    cfg = {"tenant": {"mode": "oltp"}}
    issues = tenant_create.validate_tenant_cfg(tenant_create.resolve_tenant_cfg(cfg))
    assert issues == []


def test_validate_bad_mode() -> None:
    cfg = {"tenant": {"mode": "olap"}}
    issues = tenant_create.validate_tenant_cfg(tenant_create.resolve_tenant_cfg(cfg))
    assert any("tenant.mode" in i for i in issues)


def test_validate_bad_name() -> None:
    cfg = {"tenant": {"tenant_name": "1bad"}}
    issues = tenant_create.validate_tenant_cfg(tenant_create.resolve_tenant_cfg(cfg))
    assert any("tenant.tenant_name" in i for i in issues)


def test_vm_profiles_integration() -> None:
    cfg = {"tenant": {"mode": "htap"}}
    assert validate_tenant_section(cfg) == []


def test_sql_helpers() -> None:
    assert tenant_create.sql_literal("a'b") == "'a''b'"
    assert tenant_create.sql_identifier("tpcc") == "`tpcc`"


if __name__ == "__main__":
    test_defaults()
    test_mode_mapping()
    test_validate_ok()
    test_validate_bad_mode()
    test_validate_bad_name()
    test_vm_profiles_integration()
    test_sql_helpers()
    tenant_create.cmd_self_test(type("Args", (), {})())
    print("ok")
