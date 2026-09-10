#!/usr/bin/env python3
"""Тесты маршрутизации ODP (без кластера)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

spec = importlib.util.spec_from_file_location(
    "obproxy_route", ROOT / "scripts" / "lib" / "obproxy_route.py"
)
route = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(route)

ob_sys_spec = importlib.util.spec_from_file_location(
    "ob_sys", ROOT / "scripts" / "lib" / "ob-sys.py"
)
ob_sys = importlib.util.module_from_spec(ob_sys_spec)
assert ob_sys_spec.loader is not None
ob_sys_spec.loader.exec_module(ob_sys)


def test_even_and_oltp_sql() -> None:
    assert route.apply_statements("even") == [
        "ALTER PROXYCONFIG SET enable_cached_server = false",
        "ALTER PROXYCONFIG SET enable_primary_zone = false",
    ]
    assert route.apply_statements("oltp") == [
        "ALTER PROXYCONFIG SET enable_cached_server = false",
        "ALTER PROXYCONFIG SET enable_primary_zone = true",
    ]


def test_diagnose_queries_cover_units_and_tablets() -> None:
    joined = "\n".join(sql for _title, sql in route.CLUSTER_DIAGNOSE_QUERIES)
    assert "DBA_OB_UNITS" in joined
    assert "DBA_OB_TABLE_LOCATIONS" in joined
    assert "gv$ob_processlist" in joined
    assert "tenant_type = 'USER'" in joined


def test_show_covers_pin_keys() -> None:
    sqls = route.show_statements()
    joined = "\n".join(sqls)
    assert "enable_cached_server" in joined
    assert "enable_primary_zone" in joined
    assert "target_db_server" in joined
    assert "proxy_primary_zone_name" in joined


def test_parse_and_match() -> None:
    parsed = route.parse_proxyconfig_rows(
        "enable_cached_server\tFalse\nenable_primary_zone\t0\ntarget_db_server\t\n"
    )
    assert route.settings_match(parsed, "even")
    assert route.is_pin_value("10.130.0.11:2881")
    assert not route.is_pin_value("")
    assert not route.is_pin_value("NULL")


def test_pick_obproxy_endpoints() -> None:
    cfg = {"oceanbase": {"cluster_name": "obcluster", "ports": {"obproxy": 2883}}}
    inv = {
        "OBPROXY_COUNT": "2",
        "OBPROXY_1_IP": "10.0.1.1",
        "OBPROXY_1_NAME": "ob-yc-prod-obproxy-1",
        "OBPROXY_2_IP": "10.0.1.2",
        "OBPROXY_2_NAME": "ob-yc-prod-obproxy-2",
    }
    endpoints = route.pick_obproxy_endpoints(ob_sys, cfg, inv)
    assert [ep["ip"] for ep in endpoints] == ["10.0.1.1", "10.0.1.2"]
    assert all(ep["port"] == 2883 for ep in endpoints)
    assert all(ep["user"] == "root@sys#obcluster" for ep in endpoints)
    assert "ob-yc-prod-obproxy-1" in route.format_proxy_label(endpoints[0])


def test_pick_obproxy_requires_inventory() -> None:
    try:
        route.pick_obproxy_endpoints(ob_sys, {}, {"OBPROXY_COUNT": "0"})
    except RuntimeError as exc:
        assert "OBPROXY" in str(exc)
    else:
        raise AssertionError("ожидали RuntimeError без obproxy")


def test_apply_obproxy_even_skips_without_proxies() -> None:
    spec_t = importlib.util.spec_from_file_location(
        "tenant_create", ROOT / "scripts" / "lib" / "tenant-create.py"
    )
    tenant = importlib.util.module_from_spec(spec_t)
    assert spec_t.loader is not None
    spec_t.loader.exec_module(tenant)
    tenant.apply_obproxy_even_routing({}, {"OBPROXY_COUNT": "0"})


def test_ensure_primary_zone_skips_random() -> None:
    spec_t = importlib.util.spec_from_file_location(
        "tenant_create2", ROOT / "scripts" / "lib" / "tenant-create.py"
    )
    tenant = importlib.util.module_from_spec(spec_t)
    assert spec_t.loader is not None
    spec_t.loader.exec_module(tenant)

    class Fake:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def run_sql(self, _ep, _pwd, sql):
            self.sql.append(sql)
            if "SELECT primary_zone" in sql:
                return SimpleNamespace(stdout="RANDOM\n")
            return SimpleNamespace(stdout="")

    fake = Fake()
    tenant.ensure_tenant_primary_zone_random(fake, {"ip": "10.0.0.1"}, "pwd", "tpcc")
    assert any("SELECT primary_zone" in s for s in fake.sql)
    assert not any(s.startswith("ALTER TENANT") for s in fake.sql)

    fake.sql.clear()

    class FakeZone1(Fake):
        def run_sql(self, _ep, _pwd, sql):
            self.sql.append(sql)
            if "SELECT primary_zone" in sql:
                return SimpleNamespace(stdout="zone1\n")
            return SimpleNamespace(stdout="")

    z1 = FakeZone1()
    tenant.ensure_tenant_primary_zone_random(z1, {"ip": "10.0.0.1"}, "pwd", "tpcc")
    assert any("ALTER TENANT `tpcc` PRIMARY_ZONE='RANDOM'" in s for s in z1.sql)


def test_cli_and_deploy_sh() -> None:
    parser = route.build_parser()
    args = parser.parse_args(["apply", "--mode", "even", "--skip-if-ok"])
    assert args.command == "apply"
    assert args.mode == "even"
    assert args.skip_if_ok
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "11-obproxy-route.sh" in deploy
    assert "obproxy-route" in deploy
    wrapper = ROOT / "scripts" / "11-obproxy-route.sh"
    assert wrapper.is_file()
    text = wrapper.read_text(encoding="utf-8")
    assert "obproxy_route.py" in text


def test_self_test() -> None:
    route.cmd_self_test(SimpleNamespace())


if __name__ == "__main__":
    test_even_and_oltp_sql()
    test_diagnose_queries_cover_units_and_tablets()
    test_show_covers_pin_keys()
    test_parse_and_match()
    test_pick_obproxy_endpoints()
    test_pick_obproxy_requires_inventory()
    test_apply_obproxy_even_skips_without_proxies()
    test_ensure_primary_zone_skips_random()
    test_cli_and_deploy_sh()
    test_self_test()
    print("ok")
