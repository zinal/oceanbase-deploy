#!/usr/bin/env python3
"""Тесты лимита open_cursors / PS-хендлов (без кластера)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

spec = importlib.util.spec_from_file_location(
    "open_cursors", ROOT / "scripts" / "lib" / "open_cursors.py"
)
oc = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(oc)

spec_tc = importlib.util.spec_from_file_location(
    "tenant_create", ROOT / "scripts" / "lib" / "tenant-create.py"
)
tenant_create = importlib.util.module_from_spec(spec_tc)
assert spec_tc.loader is not None
spec_tc.loader.exec_module(tenant_create)


def test_parse_and_defaults() -> None:
    assert oc.parse_open_cursors(None) == 1000
    assert oc.parse_open_cursors("") == 1000
    assert oc.parse_open_cursors(0) == 0
    assert oc.parse_open_cursors("2000") == 2000
    assert oc.value_from_cfg({}) == 1000
    assert oc.value_from_cfg({"tenant": {}}) == 1000
    assert oc.value_from_cfg({"tenant": {"open_cursors": 50}}) == 50
    assert oc.value_from_cfg({"tenant": {"open_cursors": 50}}, "2000") == 2000
    try:
        oc.parse_open_cursors(-1)
        raise AssertionError("ожидали ValueError")
    except ValueError:
        pass
    try:
        oc.parse_open_cursors(65536)
        raise AssertionError("ожидали ValueError")
    except ValueError:
        pass
    try:
        oc.parse_open_cursors("1.5")
        raise AssertionError("ожидали ValueError")
    except ValueError:
        pass


def test_sql() -> None:
    assert oc.alter_sql(1000, "tpcc") == (
        "ALTER SYSTEM SET open_cursors = 1000 TENANT = tpcc"
    )
    assert oc.alter_sql(0) == "ALTER SYSTEM SET open_cursors = 0"
    assert oc.show_sql("tpcc") == "SHOW PARAMETERS LIKE 'open_cursors' TENANT = tpcc"
    assert oc.show_sql() == "SHOW PARAMETERS LIKE 'open_cursors'"
    try:
        oc.alter_sql(1000, "1bad")
        raise AssertionError("ожидали ValueError")
    except ValueError:
        pass


def test_parse_show_parameters() -> None:
    show_row = (
        "zone1\tobserver\t10.0.0.1\t2882\topen_cursors\tINT\t50\t"
        "max open cursors\tOBSERVER\tTENANT"
    )
    assert oc.parse_open_cursors_rows(show_row) == 50
    assert oc.parse_open_cursors_rows("open_cursors\t1000\n") == 1000
    assert oc.parse_open_cursors_rows("open_cursors\tINT\t2000\tTENANT\n") == 2000
    assert oc.parse_open_cursors_rows("") is None


def test_tenant_cfg_roundtrip() -> None:
    resolved = tenant_create.resolve_tenant_cfg({})
    assert resolved["open_cursors"] == "1000"
    resolved = tenant_create.resolve_tenant_cfg({"tenant": {"open_cursors": 2000}})
    assert resolved["open_cursors"] == "2000"
    issues = tenant_create.validate_tenant_cfg(
        tenant_create.resolve_tenant_cfg({"tenant": {"open_cursors": 1000}})
    )
    assert issues == []
    bad = tenant_create.resolve_tenant_cfg({"tenant": {"open_cursors": 70000}})
    issues = tenant_create.validate_tenant_cfg(bad)
    assert any("open_cursors" in item for item in issues)


def test_cli() -> None:
    parser = oc.build_parser()
    args = parser.parse_args(["show", "--tenant", "tpcc"])
    assert args.command == "show"
    assert args.tenant == "tpcc"
    args = parser.parse_args(["apply", "--value", "2000", "--skip-if-ok"])
    assert args.command == "apply"
    assert args.value == "2000"
    assert args.skip_if_ok is True


if __name__ == "__main__":
    test_parse_and_defaults()
    test_sql()
    test_parse_show_parameters()
    test_tenant_cfg_roundtrip()
    test_cli()
    oc.cmd_self_test(type("Args", (), {})())
    print("ok")
