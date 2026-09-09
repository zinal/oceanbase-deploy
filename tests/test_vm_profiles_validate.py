#!/usr/bin/env python3
"""Сверка oceanbase.* с профилями ВМ (check)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from vm_profiles import (  # noqa: E402
    observer_auto_tune,
    ocp_admin_password_error,
    parse_size_to_gb,
    password_complexity_error,
    recommended_system_memory_gb,
    recommended_system_memory_range,
    resolve_profile,
    validate_oceanbase_against_vms,
    validate_profiles,
)


def base_cfg() -> dict:
    return {
        "vm_defaults": {"platform": "standard-v3", "core_fraction": 100},
        "yandex_cloud": {"image_folder_id": "standard-images", "image_family": "ubuntu-2204-lts"},
        "vm_profiles": {
            "observer": {
                "count": 3,
                "cores": 8,
                "memory_gb": 32,
                "boot_disk": {"type": "network-ssd", "size_gb": 50},
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
            }
        },
        "oceanbase": {
            "auto_tune": False,
            "cpu_count": 6,
            "memory_limit": "24G",
            "system_memory": "4G",
            "datafile_size": "474G",
            "log_disk_size": "250G",
        },
    }


def kinds(issues: list[str], prefix: str) -> list[str]:
    return [i for i in issues if i.startswith(prefix)]


def test_parse_size() -> None:
    assert parse_size_to_gb("28G") == 28
    assert parse_size_to_gb("28GB") == 28
    assert parse_size_to_gb("2.0") == 2.0
    assert parse_size_to_gb(8) == 8
    assert parse_size_to_gb("1024M") == 1
    assert parse_size_to_gb("1T") == 1024
    assert parse_size_to_gb(None) is None


def test_system_memory_range() -> None:
    low, high = recommended_system_memory_range(28)
    assert (low, high) == (3.0, 5.0)
    low, high = recommended_system_memory_range(48)
    assert (low, high) == (5.0, 10.0)
    rec = recommended_system_memory_gb(100)
    assert rec == 21.0
    low, high = recommended_system_memory_range(100)
    slack = max(2.0, rec * 0.15)
    assert abs(low - (rec - slack)) < 1e-6
    assert abs(high - (rec + slack)) < 1e-6
    # gist: system_memory=19G при memory_limit=102G — в диапазоне доки
    low, high = recommended_system_memory_range(102)
    assert low <= 19.0 <= high


def test_cpu_exceeds_vm_is_error() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["cpu_count"] = 16
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("cpu_count=16" in i and "observer.cores=8" in i for i in errors), errors


def test_memory_exceeds_vm_is_error() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["memory_limit"] = "64G"
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("memory_limit=64G" in i and "observer.memory_gb=32" in i for i in errors), errors


def test_system_memory_ge_limit_is_error() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["system_memory"] = "24G"
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("system_memory" in i for i in errors), errors


def test_datafile_exceeds_data_disk_is_error() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["datafile_size"] = "2000G"
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("datafile_size=2000G" in i and "data_disk=930G" in i for i in errors), errors


def test_log_size_exceeds_log_disk_is_error() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["log_disk_size"] = "500G"
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("log_disk_size=500G" in i and "log_disk=279G" in i for i in errors), errors


def test_auto_tune_yaml_disk_exceed_still_error() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["auto_tune"] = True
    cfg["oceanbase"]["datafile_size"] = "5000G"
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("datafile_size=5000G" in i for i in errors), errors


def test_datafile_over_90_percent_disk_is_warn() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["datafile_size"] = "900G"
    warns = kinds(validate_oceanbase_against_vms(cfg), "WARN")
    assert any("datafile_size=900G" in i and "90%" in i for i in warns), warns


def test_shared_disk_sum_exceeds_is_error() -> None:
    cfg = base_cfg()
    cfg["vm_profiles"]["observer"]["log_disk"]["enabled"] = False
    cfg["oceanbase"]["datafile_size"] = "800G"
    cfg["oceanbase"]["log_disk_size"] = "250G"
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("datafile_size+log_disk_size" in i and "930G" in i for i in errors), errors


def test_yc_rounded_disk_is_used() -> None:
    """network-ssd-nonreplicated округляется до кратного 93 GB: 100 → 186."""
    cfg = base_cfg()
    cfg["vm_profiles"]["observer"]["data_disk"]["size_gb"] = 100
    cfg["oceanbase"]["datafile_size"] = "187G"
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("datafile_size=187G" in i and "data_disk=186G" in i for i in errors), errors


def test_log_disk_smaller_than_3x_is_warn() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["log_disk_size"] = "20G"
    warns = kinds(validate_oceanbase_against_vms(cfg), "WARN")
    assert any("log_disk_size" in i and "3×" in i for i in warns), warns


def test_balanced_config_has_no_errors() -> None:
    cfg = base_cfg()
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert errors == [], errors


def test_memory_over_80_percent_is_warn() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["memory_limit"] = "30G"
    cfg["oceanbase"]["log_disk_size"] = "250G"
    warns = kinds(validate_oceanbase_against_vms(cfg), "WARN")
    assert any("80%" in i for i in warns), warns


def test_auto_tune_yaml_mismatch_is_info_not_warn() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["auto_tune"] = True
    cfg["oceanbase"]["cpu_count"] = 4
    cfg["oceanbase"]["memory_limit"] = "16G"
    issues = validate_oceanbase_against_vms(cfg)
    infos = kinds(issues, "INFO")
    warns = kinds(issues, "WARN")
    assert any("auto_tune=true" in i and "заменён" in i for i in infos), infos
    assert not any("замен" in i for i in warns), warns


def test_auto_tune_yaml_exceed_still_error() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["auto_tune"] = True
    cfg["oceanbase"]["cpu_count"] = 32
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("cpu_count=32" in i for i in errors), errors


def test_ocp_heap_exceeds_vm() -> None:
    cfg = base_cfg()
    cfg["vm_profiles"]["ocp"] = {
        "enabled": True,
        "count": 1,
        "cores": 4,
        "memory_gb": 16,
        "boot_disk": {"type": "network-ssd", "size_gb": 50},
        "data_disk": {"enabled": False},
        "log_disk": {"enabled": False},
    }
    cfg["ocp"] = {
        "enabled": True,
        "admin_password": "ChangeMe1!",
        "memory_size": "32G",
        "meta_tenant": {"max_cpu": 2.0, "memory_size": "4G"},
        "monitor_tenant": {"max_cpu": 2.0, "memory_size": "4G"},
    }
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("ocp.memory_size=32G" in i for i in errors), errors


def gist_like_32c_128g() -> dict:
    """deploy.yaml из gist: 32 vCPU / 128 GB, yaml уже по best practice."""
    cfg = base_cfg()
    obs = cfg["vm_profiles"]["observer"]
    obs["cores"] = 32
    obs["memory_gb"] = 128
    obs["count"] = 30
    obs["data_disk"]["size_gb"] = 558
    obs["log_disk"]["size_gb"] = 558
    cfg["oceanbase"] = {
        "auto_tune": True,
        "cpu_count": 30,
        "memory_limit": "102G",
        "system_memory": "19G",
        "datafile_size": "500G",
        "log_disk_size": "500G",
    }
    return cfg


def test_auto_tune_32c_128g_follows_docs() -> None:
    tune = observer_auto_tune(gist_like_32c_128g())
    assert tune["cpu_count"] == 30
    assert tune["memory_limit"] == "102G"
    assert tune["system_memory"] == "21G"


def test_gist_like_yaml_has_no_false_warns() -> None:
    """yaml 30/102G/19G/500G на 32c/128G не должен пугать auto_tune-выводом 32/112G/4G."""
    issues = validate_oceanbase_against_vms(gist_like_32c_128g())
    warns = kinds(issues, "WARN")
    assert warns == [], warns
    assert not any("cpu_count=32" in i for i in issues)
    assert not any("112G" in i for i in issues)
    assert not any("system_memory=4G" in i for i in issues)
    infos = kinds(issues, "INFO")
    assert not any("cpu_count 30→" in i or "memory_limit 102G→" in i for i in infos)
    assert any("заменён" in i and "datafile_size" in i for i in infos), infos


def test_cpu_using_all_cores_is_warn() -> None:
    cfg = gist_like_32c_128g()
    cfg["oceanbase"]["auto_tune"] = False
    cfg["oceanbase"]["cpu_count"] = 32
    warns = kinds(validate_oceanbase_against_vms(cfg), "WARN")
    assert any("почти равен" in i and "cpu_count=32" in i for i in warns), warns


def test_ocp_admin_password_changeme_is_error() -> None:
    cfg = base_cfg()
    cfg["vm_profiles"]["ocp"] = {
        "enabled": True,
        "count": 1,
        "cores": 4,
        "memory_gb": 16,
        "boot_disk": {"type": "network-ssd", "size_gb": 50},
        "data_disk": {"enabled": False},
        "log_disk": {"enabled": False},
    }
    cfg["ocp"] = {"enabled": True, "admin_password": "changeme", "memory_size": "8G"}
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("admin_password" in i and "OBD-1025" in i for i in errors), errors


def test_ocp_admin_password_valid() -> None:
    assert ocp_admin_password_error("ChangeMe1!") is None
    assert ocp_admin_password_error("NoSpecial1") is None  # 3 класса без спец.
    assert ocp_admin_password_error("changeme") is not None
    assert ocp_admin_password_error("short") is not None
    assert ocp_admin_password_error("") is not None
    assert ocp_admin_password_error(None) is not None


def test_ocp_root_password_changeme_is_error() -> None:
    cfg = base_cfg()
    cfg["vm_profiles"]["ocp"] = {
        "enabled": True,
        "count": 1,
        "cores": 4,
        "memory_gb": 16,
        "boot_disk": {"type": "network-ssd", "size_gb": 50},
        "data_disk": {"enabled": False},
        "log_disk": {"enabled": False},
    }
    cfg["ocp"] = {
        "enabled": True,
        "admin_password": "ChangeMe1!",
        "root_password": "changeme",
        "memory_size": "8G",
    }
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("root_password" in i for i in errors), errors


def test_ob_user_password_two_classes() -> None:
    assert password_complexity_error("x", "ChangeMe1!", required=False, min_classes=2) is None
    assert password_complexity_error("x", "changeme", required=False, min_classes=2) is not None
    assert password_complexity_error("x", "ocp_meta_root", required=False, min_classes=2) is None
    assert password_complexity_error("x", None, required=False, min_classes=2) is None


def test_ob_idc_name() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_obd", ROOT / "scripts" / "03-generate-obd-config.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.ob_idc_name("ru-central1-d") == "ru_central1_d"
    assert mod.ob_idc_name("") == "default_idc"


def test_example_yaml_has_no_errors() -> None:
    example = ROOT / "config" / "deploy.yaml.example"
    import yaml

    cfg = yaml.safe_load(example.read_text(encoding="utf-8"))
    issues = validate_profiles(cfg) + validate_oceanbase_against_vms(cfg)
    errors = kinds(issues, "ERROR")
    assert errors == [], errors


def test_runner_resolve_defaults() -> None:
    cfg = {
        "vm_defaults": {"platform": "standard-v3", "core_fraction": 100},
        "yandex_cloud": {
            "image_folder_id": "standard-images",
            "image_family": "ubuntu-2204-lts",
        },
        "vm_profiles": {"runner": {"enabled": True}},
    }
    profile = resolve_profile(cfg, "runner")
    assert profile["cores"] == 8
    assert profile["memory_gb"] == 32
    assert profile["count"] == 5
    assert profile["name_prefix"] == "ob-runner"
    assert profile["boot_disk"] == {"type": "network-ssd", "size_gb": 150}


def test_resolve_lines_count_unchanged() -> None:
    """yc-instance.sh load_vm_params читает 15 строк из resolve --format lines."""
    cfg = base_cfg()
    profile = resolve_profile(cfg, "observer")
    assert "name_prefix" in profile
    lines = [
        profile["platform"],
        profile["cores"],
        profile["memory_gb"],
        profile["image_spec"],
        profile["core_fraction"],
        profile["boot_disk"].get("type", "network-ssd"),
        profile["boot_disk"].get("size_gb", 50),
        str(profile["data_disk"].get("enabled", False)).lower(),
        profile["data_disk"].get("type", "network-ssd"),
        profile["data_disk"].get("size_gb", 0),
        profile["data_disk"].get("mount_point", "/data"),
        str(profile["log_disk"].get("enabled", False)).lower(),
        profile["log_disk"].get("type", "network-ssd-nonreplicated"),
        profile["log_disk"].get("size_gb", 0),
        profile["log_disk"].get("mount_point", "/data/log1"),
    ]
    assert len(lines) == 15


def main() -> None:
    tests = [
        test_parse_size,
        test_system_memory_range,
        test_cpu_exceeds_vm_is_error,
        test_memory_exceeds_vm_is_error,
        test_system_memory_ge_limit_is_error,
        test_log_disk_smaller_than_3x_is_warn,
        test_datafile_exceeds_data_disk_is_error,
        test_log_size_exceeds_log_disk_is_error,
        test_auto_tune_yaml_disk_exceed_still_error,
        test_datafile_over_90_percent_disk_is_warn,
        test_shared_disk_sum_exceeds_is_error,
        test_yc_rounded_disk_is_used,
        test_balanced_config_has_no_errors,
        test_memory_over_80_percent_is_warn,
        test_auto_tune_yaml_mismatch_is_info_not_warn,
        test_auto_tune_yaml_exceed_still_error,
        test_ocp_heap_exceeds_vm,
        test_auto_tune_32c_128g_follows_docs,
        test_gist_like_yaml_has_no_false_warns,
        test_cpu_using_all_cores_is_warn,
        test_ocp_admin_password_changeme_is_error,
        test_ocp_admin_password_valid,
        test_ocp_root_password_changeme_is_error,
        test_ob_user_password_two_classes,
        test_ob_idc_name,
        test_example_yaml_has_no_errors,
        test_runner_resolve_defaults,
        test_resolve_lines_count_unchanged,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
