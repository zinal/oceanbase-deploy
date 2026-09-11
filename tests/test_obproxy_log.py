#!/usr/bin/env python3
"""Тесты снижения детальности логов ODP (без кластера)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

spec = importlib.util.spec_from_file_location(
    "obproxy_log", ROOT / "scripts" / "lib" / "obproxy_log.py"
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
    assert info[0] == "ALTER PROXYCONFIG SET syslog_level = 'INFO'"
    assert "ALTER PROXYCONFIG SET enable_syslog_wf = false" in info
    assert "ALTER PROXYCONFIG SET enable_async_log = true" in info
    assert "ALTER PROXYCONFIG SET syslog_io_bandwidth_limit = '10MB'" in info
    assert "ALTER PROXYCONFIG SET log_dir_size_threshold = '8G'" in info
    warn = log.apply_statements("warn")
    assert "ALTER PROXYCONFIG SET syslog_level = 'WARN'" in warn
    assert "ALTER PROXYCONFIG SET monitor_log_level = 'WARN'" in warn
    assert "ALTER PROXYCONFIG SET route_diagnosis_level = 1" in warn
    debug = log.apply_statements("debug")
    assert "ALTER PROXYCONFIG SET syslog_level = 'WDIAG'" in debug
    assert "log_dir_size_threshold" not in "\n".join(debug)


def test_threshold_follows_boot_disk() -> None:
    small = {"vm_profiles": {"obproxy": {"boot_disk": {"size_gb": 20}}}}
    big = {"vm_profiles": {"obproxy": {"boot_disk": {"size_gb": 100}}}}
    tiny = {"vm_profiles": {"obproxy": {"boot_disk": {"size_gb": 4}}}}
    assert log.log_dir_size_threshold(small) == "8G"
    assert log.log_dir_size_threshold(big) == "16G"
    assert log.log_dir_size_threshold(tiny) == "1G"
    assert log.obd_log_settings(small) == {
        "log_dir_size_threshold": "8G",
        "log_file_percentage": 50,
        "log_cleanup_interval": "5m",
    }


def test_match_and_parse() -> None:
    assert log.parse_size_bytes("8G") == log.parse_size_bytes("8GB")
    assert log.parse_size_bytes("10MB") == log.parse_size_bytes("10M")
    assert log.settings_match(
        {
            "syslog_level": "info",
            "enable_syslog_wf": "False",
            "enable_async_log": "1",
            "enable_syslog_file_compress": "true",
            "syslog_io_bandwidth_limit": "10M",
            "log_dir_size_threshold": "8GB",
            "log_file_percentage": "50",
        },
        log.apply_settings("info"),
    )
    assert not log.settings_match(
        {"syslog_level": "WDIAG", "enable_syslog_wf": "false"},
        {"syslog_level": "INFO", "enable_syslog_wf": "false"},
    )
    assert log.mode_from_cfg({}) == "info"
    assert log.mode_from_cfg({"oceanbase": {"obproxy": {"log_mode": "WARN"}}}) == "warn"
    try:
        log.resolve_mode("silent")
        raise AssertionError("ожидали ValueError")
    except ValueError:
        pass


def test_generate_obd_puts_known_log_limits() -> None:
    mod = load_generate()
    cfg = {
        "yandex_cloud": {"zone": "ru-central1-a", "ssh_user": "demo"},
        "ssh": {"port": 22, "private_key_file": ""},
        "vm_profiles": {
            "observer": {"count": 3},
            "obproxy": {"count": 2, "boot_disk": {"size_gb": 20}},
        },
        "oceanbase": {
            "auto_tune": False,
            "cluster_name": "obcluster",
            "cpu_count": 8,
            "memory_limit": "28G",
            "system_memory": "4G",
            "datafile_size": "474G",
            "log_disk_size": "250G",
            "components": {"ob_configserver": False, "obagent": False},
        },
    }
    inv = {
        "OBSERVER_COUNT": "3",
        "OBPROXY_COUNT": "2",
        "DEPLOY_NAME": "ob-yc-prod",
        "OBSERVER_1_IP": "10.0.0.1",
        "OBSERVER_2_IP": "10.0.0.2",
        "OBSERVER_3_IP": "10.0.0.3",
        "OBPROXY_1_IP": "10.0.1.1",
        "OBPROXY_2_IP": "10.0.1.2",
    }
    obd = mod.build_obd_config(cfg, inv)
    proxy = obd["obproxy-ce"]["global"]
    assert proxy["log_dir_size_threshold"] == "8G"
    assert proxy["log_file_percentage"] == 50
    assert proxy["log_cleanup_interval"] == "5m"
    assert "syslog_level" not in proxy


def test_apply_obproxy_log_skips_without_proxies() -> None:
    spec_t = importlib.util.spec_from_file_location(
        "tenant_create_log", ROOT / "scripts" / "lib" / "tenant-create.py"
    )
    tenant = importlib.util.module_from_spec(spec_t)
    assert spec_t.loader is not None
    spec_t.loader.exec_module(tenant)
    tenant.apply_obproxy_log_defaults({}, {"OBPROXY_COUNT": "0"})


def test_cli_and_deploy_sh() -> None:
    parser = log.build_parser()
    args = parser.parse_args(["apply", "--mode", "warn", "--skip-if-ok"])
    assert args.command == "apply"
    assert args.mode == "warn"
    assert args.skip_if_ok
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "12-obproxy-log.sh" in deploy
    assert "obproxy-log" in deploy
    deploy_case = deploy.split("\n  deploy)")[1].split("\n  tenant)")[0]
    all_case = deploy.split("\n  all)")[1].split("\n  destroy)")[0]
    assert "12-obproxy-log.sh apply --skip-if-none --skip-if-ok" in deploy_case
    assert "12-obproxy-log.sh apply --skip-if-none --skip-if-ok" in all_case
    assert deploy_case.find("04-deploy-cluster.sh") < deploy_case.find("12-obproxy-log.sh")
    assert all_case.find("04-deploy-cluster.sh") < all_case.find("12-obproxy-log.sh")
    wrapper = ROOT / "scripts" / "12-obproxy-log.sh"
    assert wrapper.is_file()
    text = wrapper.read_text(encoding="utf-8")
    assert "obproxy_log.py" in text
    assert "--skip-if-none" in text
    cluster = (ROOT / "scripts" / "04-deploy-cluster.sh").read_text(encoding="utf-8")
    assert "12-obproxy-log.sh" in cluster
    recover = (ROOT / "scripts" / "07-recover-obproxy.sh").read_text(encoding="utf-8")
    assert "12-obproxy-log.sh" in recover


def test_self_test() -> None:
    log.cmd_self_test(SimpleNamespace())


if __name__ == "__main__":
    test_sql_and_modes()
    test_threshold_follows_boot_disk()
    test_match_and_parse()
    test_generate_obd_puts_known_log_limits()
    test_apply_obproxy_log_skips_without_proxies()
    test_cli_and_deploy_sh()
    test_self_test()
    print("ok")
