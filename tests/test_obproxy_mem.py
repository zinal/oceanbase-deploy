#!/usr/bin/env python3
"""Тесты потолка памяти ODP (без кластера)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

spec = importlib.util.spec_from_file_location(
    "obproxy_mem", ROOT / "scripts" / "lib" / "obproxy_mem.py"
)
mem = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mem)

from vm_profiles import (  # noqa: E402
    recommended_proxy_mem_limited,
    recommended_proxy_mem_limited_gb,
    validate_oceanbase_against_vms,
)


def load_generate():
    path = ROOT / "scripts" / "03-generate-obd-config.py"
    gen_spec = importlib.util.spec_from_file_location("gen_obd_mem", path)
    mod = importlib.util.module_from_spec(gen_spec)
    assert gen_spec.loader is not None
    gen_spec.loader.exec_module(mod)
    return mod


def test_recommended_table() -> None:
    assert recommended_proxy_mem_limited_gb(4) == 2
    assert recommended_proxy_mem_limited_gb(8) == 4
    assert recommended_proxy_mem_limited_gb(16) == 8
    assert recommended_proxy_mem_limited_gb(32) == 16
    assert recommended_proxy_mem_limited_gb(64) == 16
    assert recommended_proxy_mem_limited_gb(32, dedicated=False) == 4
    assert recommended_proxy_mem_limited(64) == "16G"


def test_resolve_and_sql() -> None:
    tiny = {"vm_profiles": {"obproxy": {"count": 2, "memory_gb": 4}}}
    huge = {"vm_profiles": {"obproxy": {"count": 1, "memory_gb": 64}}}
    pinned = {
        "vm_profiles": {"obproxy": {"count": 2, "memory_gb": 64}},
        "oceanbase": {"obproxy": {"proxy_mem_limited": "8G"}},
    }
    assert mem.resolve_proxy_mem_limited(tiny) == "2G"
    assert mem.resolve_proxy_mem_limited(huge) == "16G"
    assert mem.resolve_proxy_mem_limited(pinned) == "8G"
    assert mem.resolve_proxy_mem_limited(tiny, "16G") == "16G"
    assert mem.apply_statements(huge) == [
        "ALTER PROXYCONFIG SET proxy_mem_limited = '16G'"
    ]
    assert mem.values_match("2147483648", "2G")
    assert mem.values_match("8GB", "8G")
    assert not mem.values_match("2G", "8G")


def test_generate_obd_puts_proxy_mem_limited() -> None:
    mod = load_generate()
    cfg = {
        "yandex_cloud": {"zone": "ru-central1-a", "ssh_user": "demo"},
        "ssh": {"port": 22, "private_key_file": ""},
        "vm_profiles": {
            "observer": {"count": 3},
            "obproxy": {"count": 2, "memory_gb": 4, "boot_disk": {"size_gb": 20}},
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
    assert proxy["proxy_mem_limited"] == "2G"
    cfg["vm_profiles"]["obproxy"]["memory_gb"] = 64
    obd64 = mod.build_obd_config(cfg, inv)
    assert obd64["obproxy-ce"]["global"]["proxy_mem_limited"] == "16G"
    cfg["oceanbase"]["obproxy"] = {"proxy_mem_limited": "8G"}
    obd8 = mod.build_obd_config(cfg, inv)
    assert obd8["obproxy-ce"]["global"]["proxy_mem_limited"] == "8G"


def test_validate_warns_stale_2g_on_big_vm() -> None:
    cfg = {
        "vm_defaults": {"platform": "standard-v3", "core_fraction": 100},
        "yandex_cloud": {
            "image_folder_id": "standard-images",
            "image_family": "ubuntu-2204-lts",
        },
        "vm_profiles": {
            "observer": {
                "count": 3,
                "cores": 8,
                "memory_gb": 32,
                "data_disk": {
                    "enabled": True,
                    "type": "network-ssd-nonreplicated",
                    "size_gb": 930,
                },
                "log_disk": {
                    "enabled": True,
                    "type": "network-ssd-nonreplicated",
                    "size_gb": 279,
                },
            },
            "obproxy": {"count": 2, "cores": 2, "memory_gb": 64},
        },
        "oceanbase": {
            "auto_tune": False,
            "cpu_count": 6,
            "memory_limit": "24G",
            "system_memory": "4G",
            "datafile_size": "474G",
            "log_disk_size": "250G",
            "obproxy": {"proxy_mem_limited": "2G"},
        },
    }
    issues = validate_oceanbase_against_vms(cfg)
    warns = [i for i in issues if i.startswith("WARN")]
    errors = [i for i in issues if i.startswith("ERROR")]
    assert any("proxy_mem_limited=2G" in i and "64 GB" in i for i in warns), warns
    assert errors == [], errors
    cfg["oceanbase"]["obproxy"]["proxy_mem_limited"] = "128G"
    over = validate_oceanbase_against_vms(cfg)
    assert any("превышает" in i and i.startswith("ERROR") for i in over), over


def test_apply_skips_without_proxies() -> None:
    spec_t = importlib.util.spec_from_file_location(
        "tenant_create_mem", ROOT / "scripts" / "lib" / "tenant-create.py"
    )
    tenant = importlib.util.module_from_spec(spec_t)
    assert spec_t.loader is not None
    spec_t.loader.exec_module(tenant)
    tenant.apply_obproxy_mem_defaults({}, {"OBPROXY_COUNT": "0"})


def test_cli_and_deploy_sh() -> None:
    parser = mem.build_parser()
    args = parser.parse_args(["apply", "--size", "8G", "--skip-if-ok"])
    assert args.command == "apply"
    assert args.size == "8G"
    assert args.skip_if_ok
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "17-obproxy-mem.sh" in deploy
    assert "obproxy-mem" in deploy
    deploy_case = deploy.split("\n  deploy)")[1].split("\n  tenant)")[0]
    all_case = deploy.split("\n  all)")[1].split("\n  destroy)")[0]
    assert "17-obproxy-mem.sh apply --skip-if-none --skip-if-ok" in deploy_case
    assert "17-obproxy-mem.sh apply --skip-if-none --skip-if-ok" in all_case
    assert deploy_case.find("04-deploy-cluster.sh") < deploy_case.find("17-obproxy-mem.sh")
    assert all_case.find("04-deploy-cluster.sh") < all_case.find("17-obproxy-mem.sh")
    wrapper = ROOT / "scripts" / "17-obproxy-mem.sh"
    assert wrapper.is_file()
    text = wrapper.read_text(encoding="utf-8")
    assert "obproxy_mem.py" in text
    assert "--skip-if-none" in text
    cluster = (ROOT / "scripts" / "04-deploy-cluster.sh").read_text(encoding="utf-8")
    assert "17-obproxy-mem.sh" in cluster
    recover = (ROOT / "scripts" / "07-recover-obproxy.sh").read_text(encoding="utf-8")
    assert "17-obproxy-mem.sh" in recover


def test_self_test() -> None:
    mem.cmd_self_test(SimpleNamespace())


if __name__ == "__main__":
    test_recommended_table()
    test_resolve_and_sql()
    test_generate_obd_puts_proxy_mem_limited()
    test_validate_warns_stale_2g_on_big_vm()
    test_apply_skips_without_proxies()
    test_cli_and_deploy_sh()
    test_self_test()
    print("ok")
