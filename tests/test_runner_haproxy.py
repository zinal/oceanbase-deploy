#!/usr/bin/env python3
"""Генерация HAProxy для runner: backend — имена obproxy, не IP."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from runner_haproxy import (  # noqa: E402
    assert_backends_are_names,
    backend_hosts_from_cfg,
    generate,
    obproxy_names,
    parse_inventory,
    render_haproxy_cfg,
)
from vm_profiles import resolve_profile  # noqa: E402


def _write_inv(path: Path, body: str) -> None:
    path.write_text(body.strip() + "\n", encoding="utf-8")


def test_obproxy_names_not_ips(tmp_path: Path) -> None:
    inv_file = tmp_path / "inventory.env"
    _write_inv(
        inv_file,
        """
OBPROXY_1_NAME=ob-yc-prod-obproxy-1
OBPROXY_1_IP=10.128.0.11
OBPROXY_2_NAME=ob-yc-prod-obproxy-2
OBPROXY_2_IP=10.128.0.12
OBPROXY_COUNT=2
RUNNER_1_NAME=ob-runner-1
RUNNER_COUNT=1
""",
    )
    names = obproxy_names(parse_inventory(inv_file))
    assert names == ["ob-yc-prod-obproxy-1", "ob-yc-prod-obproxy-2"]
    text = generate(inv_file, None, None)
    hosts = backend_hosts_from_cfg(text)
    assert hosts == ["ob-yc-prod-obproxy-1", "ob-yc-prod-obproxy-2"]
    assert "10.128.0.11" not in text
    assert "10.128.0.12" not in text
    assert "server obproxy1 ob-yc-prod-obproxy-1:2883 check" in text
    assert "bind 127.0.0.1:2883" in text
    assert_backends_are_names(text)


def test_rejects_ip_as_name(tmp_path: Path) -> None:
    inv_file = tmp_path / "inventory.env"
    _write_inv(
        inv_file,
        """
OBPROXY_1_NAME=10.1.2.3
OBPROXY_COUNT=1
""",
    )
    try:
        obproxy_names(parse_inventory(inv_file))
    except ValueError as exc:
        assert "IP" in str(exc)
    else:
        raise AssertionError("expected ValueError for IP in NAME")


def test_missing_name_is_error(tmp_path: Path) -> None:
    inv_file = tmp_path / "inventory.env"
    _write_inv(
        inv_file,
        """
OBPROXY_1_IP=10.1.2.3
OBPROXY_COUNT=1
""",
    )
    try:
        obproxy_names(parse_inventory(inv_file))
    except ValueError as exc:
        assert "OBPROXY_1_NAME" in str(exc)
    else:
        raise AssertionError("expected ValueError when NAME is missing")


def test_custom_port(tmp_path: Path) -> None:
    inv_file = tmp_path / "inventory.env"
    cfg_file = tmp_path / "deploy.yaml"
    _write_inv(
        inv_file,
        """
OBPROXY_1_NAME=ob-runner-obproxy-1
OBPROXY_COUNT=1
""",
    )
    cfg_file.write_text("oceanbase:\n  ports:\n    obproxy: 28830\n", encoding="utf-8")
    text = generate(inv_file, cfg_file, None)
    assert "server obproxy1 ob-runner-obproxy-1:28830 check" in text
    assert "bind 127.0.0.1:28830" in text


def test_render_matches_example_shape() -> None:
    text = render_haproxy_cfg(["ob-yc-prod-obproxy-1", "ob-yc-prod-obproxy-2"], 2883)
    example = (ROOT / "bench" / "tpcc" / "haproxy.cfg").read_text(encoding="utf-8")
    for needle in (
        "frontend obproxy_mysql",
        "backend obproxy_servers",
        "balance roundrobin",
        "option tcp-check",
        "bind 127.0.0.1:2883",
    ):
        assert needle in text
        assert needle in example


def test_runner_profile_defaults() -> None:
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
    assert profile["boot_disk"]["type"] == "network-ssd"
    assert profile["boot_disk"]["size_gb"] == 150
    assert profile["data_disk"].get("enabled") not in (True, "true")


def test_example_yaml_runner_optional() -> None:
    import yaml

    example = ROOT / "config" / "deploy.yaml.example"
    cfg = yaml.safe_load(example.read_text(encoding="utf-8"))
    runner = cfg["vm_profiles"]["runner"]
    assert runner["enabled"] is False
    assert runner["count"] == 5
    assert runner["name_prefix"] == "ob-runner"
    assert runner["cores"] == 8
    assert runner["memory_gb"] == 32
    assert runner["boot_disk"]["size_gb"] == 150
    profile = resolve_profile(cfg, "runner")
    assert profile["name_prefix"] == "ob-runner"
    assert profile["cores"] == 8


def main() -> None:
    import tempfile

    tests = [
        test_render_matches_example_shape,
        test_runner_profile_defaults,
        test_example_yaml_runner_optional,
    ]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        test_obproxy_names_not_ips(tmp_path)
        print("OK test_obproxy_names_not_ips")
        test_rejects_ip_as_name(tmp_path)
        print("OK test_rejects_ip_as_name")
        test_missing_name_is_error(tmp_path)
        print("OK test_missing_name_is_error")
        test_custom_port(tmp_path)
        print("OK test_custom_port")
        for fn in tests:
            fn()
            print(f"OK {fn.__name__}")
    print(f"OK: {4 + len(tests)} tests")


if __name__ == "__main__":
    main()
