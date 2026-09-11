#!/usr/bin/env python3
"""Тесты снижения детальности логов observer (без кластера)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

spec = importlib.util.spec_from_file_location(
    "observer_log", ROOT / "scripts" / "lib" / "observer_log.py"
)
log = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(log)


def load_generate():
    path = ROOT / "scripts" / "03-generate-obd-config.py"
    gen_spec = importlib.util.spec_from_file_location("gen_obd", path)
    mod = importlib.util.module_from_spec(gen_spec)
    assert gen_spec.loader is not None
    gen_spec.loader.exec_module(mod)
    return mod


def test_sql_and_modes() -> None:
    info = log.apply_statements("info")
    assert info[0] == "ALTER SYSTEM SET syslog_level = 'INFO'"
    assert "ALTER SYSTEM SET enable_syslog_wf = false" in info
    assert "ALTER SYSTEM SET enable_async_syslog = true" in info
    assert "ALTER SYSTEM SET enable_syslog_recycle = true" in info
    assert "ALTER SYSTEM SET max_syslog_file_count = 20" in info
    assert "ALTER SYSTEM SET syslog_io_bandwidth_limit = '10M'" in info
    warn = log.apply_statements("warn")
    assert "ALTER SYSTEM SET syslog_level = 'WARN'" in warn
    assert "ALTER SYSTEM SET enable_record_trace_log = false" in warn
    debug = log.apply_statements("debug")
    assert "ALTER SYSTEM SET syslog_level = 'WDIAG'" in debug
    assert "enable_record_trace_log" not in "\n".join(debug)


def test_parse_and_match() -> None:
    parsed = log.parse_name_value_rows("syslog_level\tINFO\nenable_syslog_wf\tFalse\n")
    assert parsed["syslog_level"] == "INFO"
    show_row = (
        "zone1\tobserver\t10.0.0.1\t2882\tsyslog_level\tVARCHAR\tWDIAG\t"
        "log level\tOBSERVER\tCLUSTER"
    )
    assert log.parse_name_value_rows(show_row)["syslog_level"] == "WDIAG"
    assert log.parse_size_bytes("10M") == log.parse_size_bytes("10MB")
    assert log.settings_match(
        {
            "syslog_level": "info",
            "enable_syslog_wf": "False",
            "enable_async_syslog": "1",
            "enable_syslog_recycle": "true",
            "max_syslog_file_count": "20",
            "syslog_io_bandwidth_limit": "10MB",
        },
        log.apply_settings("info"),
    )
    assert not log.settings_match(
        {"syslog_level": "WDIAG", "enable_syslog_wf": "false"},
        {"syslog_level": "INFO", "enable_syslog_wf": "false"},
    )
    assert log.mode_from_cfg({}) == "info"
    assert log.mode_from_cfg({"oceanbase": {"log_mode": "WARN"}}) == "warn"
    try:
        log.resolve_mode("silent")
        raise AssertionError("ожидали ValueError")
    except ValueError:
        pass


def test_obd_yaml_settings() -> None:
    info = log.obd_log_settings({})
    assert info["syslog_level"] == "INFO"
    assert info["enable_syslog_wf"] is False
    assert info["enable_syslog_recycle"] is True
    assert info["max_syslog_file_count"] == 20
    assert info["syslog_io_bandwidth_limit"] == "10MB"
    warn = log.obd_log_settings({"oceanbase": {"log_mode": "warn"}})
    assert warn["syslog_level"] == "WARN"
    assert warn["enable_record_trace_log"] is False


def test_generate_obd_puts_syslog_level() -> None:
    mod = load_generate()
    cfg = {
        "yandex_cloud": {"zone": "ru-central1-a", "ssh_user": "demo"},
        "ssh": {"port": 22, "private_key_file": ""},
        "vm_profiles": {"observer": {"count": 3}},
        "oceanbase": {
            "auto_tune": False,
            "cluster_name": "obcluster",
            "cpu_count": 8,
            "memory_limit": "28G",
            "system_memory": "4G",
            "datafile_size": "474G",
            "log_disk_size": "250G",
            "log_mode": "info",
            "components": {"ob_configserver": False, "obproxy_ce": False, "obagent": False},
        },
    }
    inv = {
        "OBSERVER_COUNT": "3",
        "DEPLOY_NAME": "ob-yc-prod",
        "OBSERVER_1_IP": "10.0.0.1",
        "OBSERVER_2_IP": "10.0.0.2",
        "OBSERVER_3_IP": "10.0.0.3",
    }
    obd = mod.build_obd_config(cfg, inv)
    glob = obd["oceanbase-ce"]["global"]
    assert glob["syslog_level"] == "INFO"
    assert glob["enable_syslog_wf"] is False
    assert glob["enable_syslog_recycle"] is True
    assert glob["max_syslog_file_count"] == 20
    assert glob["syslog_io_bandwidth_limit"] == "10MB"
    assert glob["enable_async_syslog"] is True


def test_apply_observer_log_skips_without_observers() -> None:
    spec_t = importlib.util.spec_from_file_location(
        "tenant_create_observer_log", ROOT / "scripts" / "lib" / "tenant-create.py"
    )
    tenant = importlib.util.module_from_spec(spec_t)
    assert spec_t.loader is not None
    spec_t.loader.exec_module(tenant)
    tenant.apply_observer_log_defaults({}, {"OBSERVER_COUNT": "0"})


def test_cli_and_deploy_sh() -> None:
    parser = log.build_parser()
    args = parser.parse_args(["apply", "--mode", "warn", "--skip-if-ok"])
    assert args.command == "apply"
    assert args.mode == "warn"
    assert args.skip_if_ok
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "13-observer-log.sh" in deploy
    assert "observer-log" in deploy
    deploy_case = deploy.split("\n  deploy)")[1].split("\n  tenant)")[0]
    all_case = deploy.split("\n  all)")[1].split("\n  destroy)")[0]
    assert "13-observer-log.sh apply --skip-if-none --skip-if-ok" in deploy_case
    assert "13-observer-log.sh apply --skip-if-none --skip-if-ok" in all_case
    wrapper = ROOT / "scripts" / "13-observer-log.sh"
    assert wrapper.is_file()
    text = wrapper.read_text(encoding="utf-8")
    assert "observer_log.py" in text
    assert "--skip-if-none" in text
    cluster = (ROOT / "scripts" / "04-deploy-cluster.sh").read_text(encoding="utf-8")
    assert "13-observer-log.sh" in cluster
    recover = (ROOT / "scripts" / "06-recover-observer.sh").read_text(encoding="utf-8")
    assert "13-observer-log.sh" in recover


def test_self_test() -> None:
    log.cmd_self_test(SimpleNamespace())


if __name__ == "__main__":
    test_sql_and_modes()
    test_parse_and_match()
    test_obd_yaml_settings()
    test_generate_obd_puts_syslog_level()
    test_apply_observer_log_skips_without_observers()
    test_cli_and_deploy_sh()
    test_self_test()
    print("ok")
