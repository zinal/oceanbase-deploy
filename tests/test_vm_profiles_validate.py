#!/usr/bin/env python3
"""Сверка oceanbase.* с профилями ВМ (check)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from vm_profiles import (  # noqa: E402
    parse_size_to_gb,
    recommended_system_memory_range,
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
    low, high = recommended_system_memory_range(100)
    rec = 3 * (100**0.5 - 3)
    assert abs(low - rec * 0.8) < 1e-6


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


def test_auto_tune_yaml_mismatch_is_warn() -> None:
    cfg = base_cfg()
    cfg["oceanbase"]["auto_tune"] = True
    cfg["oceanbase"]["cpu_count"] = 4
    cfg["oceanbase"]["memory_limit"] = "16G"
    warns = kinds(validate_oceanbase_against_vms(cfg), "WARN")
    assert any("auto_tune=true" in i and "заменены" in i for i in warns), warns


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
        "memory_size": "32G",
        "meta_tenant": {"max_cpu": 2.0, "memory_size": "4G"},
        "monitor_tenant": {"max_cpu": 2.0, "memory_size": "4G"},
    }
    errors = kinds(validate_oceanbase_against_vms(cfg), "ERROR")
    assert any("ocp.memory_size=32G" in i for i in errors), errors


def test_example_yaml_has_no_errors() -> None:
    example = ROOT / "config" / "deploy.yaml.example"
    import yaml

    cfg = yaml.safe_load(example.read_text(encoding="utf-8"))
    issues = validate_profiles(cfg) + validate_oceanbase_against_vms(cfg)
    errors = kinds(issues, "ERROR")
    assert errors == [], errors


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
        test_auto_tune_yaml_mismatch_is_warn,
        test_auto_tune_yaml_exceed_still_error,
        test_ocp_heap_exceeds_vm,
        test_example_yaml_has_no_errors,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
