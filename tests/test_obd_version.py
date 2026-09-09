#!/usr/bin/env python3
"""oceanbase.version → OBD YAML и зеркала (новые кластера 5.0.1)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from obd_version import (  # noqa: E402
    DEFAULT_NEW_CLUSTER_VERSION,
    enable_remote_mirror,
    has_package,
    missing_package_hint,
    normalize_ob_version,
    parse_obd_table_rows,
    plugin_covers_version,
    plugin_versions_in,
    remote_mirrors_enabled,
    requested_oceanbase_version,
    version_matches,
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


LOCAL_TABLE = """
+----------------------------------------------------------------------------------------+
|                                   local Package List                                    |
+---------------+-----------+-------------------------+--------+------------------------+
| name          | version   | release                 | arch   | md5                  |
+---------------+-----------+-------------------------+--------+------------------------+
| oceanbase-ce  | 4.6.0.0   | 1000002720250701        | x86_64 | abc                    |
| obproxy-ce    | 4.3.5.1   | 10000000                | x86_64 | def                    |
+---------------+-----------+-------------------------+--------+------------------------+
"""

LOCAL_WITH_501 = """
| name          | version   | release | arch   | md5 |
| oceanbase-ce  | 5.0.1.0   | 100000012026072916 | x86_64 | xyz |
"""

REPO_TABLE_DISABLED = """
+----------------------------+--------+---------+----------+------------------+
| SectionName                | Type   | Enabled | Avaiable | Update Time      |
+----------------------------+--------+---------+----------+------------------+
| local                      | local  | -       | True     | 2025-02-19 15:56 |
| oceanbase.community.stable | remote | False   | False    | 2025-02-19 15:54 |
| oceanbase.development-kit  | remote | False   | False    | 2025-02-19 15:54 |
+----------------------------+--------+---------+----------+------------------+
"""

REPO_TABLE_ENABLED = """
| SectionName | Type | Enabled | Avaiable |
| local | local | - | True |
| oceanbase.community.stable | remote | True | True |
"""


def test_normalize_and_match() -> None:
    assert normalize_ob_version("") == ""
    assert normalize_ob_version("null") == ""
    assert normalize_ob_version("5.0.1") == "5.0.1.0"
    assert normalize_ob_version("5.0.1.0-100000012026072916") == "5.0.1.0"
    assert version_matches("5.0.1", "5.0.1.0")
    assert version_matches("5.0.1.0", "5.0.1")
    assert not version_matches("5.0.1", "5.0.10.0")
    assert not version_matches("5.0.1.0", "4.6.0.0")


def test_requested_from_config() -> None:
    assert requested_oceanbase_version({"oceanbase": {"version": "5.0.1"}}) == "5.0.1.0"
    assert requested_oceanbase_version({"oceanbase": {"version": ""}}) == ""
    assert requested_oceanbase_version({"oceanbase": {"version": None}}) == ""
    assert enable_remote_mirror({"oceanbase": {}}) is True
    assert enable_remote_mirror({"oceanbase": {"enable_remote_mirror": False}}) is False
    assert enable_remote_mirror({"oceanbase": {"enable_remote_mirror": "false"}}) is False


def test_parse_local_packages() -> None:
    rows = parse_obd_table_rows(LOCAL_TABLE)
    assert has_package(rows, "4.6.0.0")
    assert not has_package(rows, "5.0.1.0")
    assert has_package(parse_obd_table_rows(LOCAL_WITH_501), "5.0.1")


def test_remote_enabled_flag() -> None:
    assert remote_mirrors_enabled(parse_obd_table_rows(REPO_TABLE_DISABLED)) is False
    assert remote_mirrors_enabled(parse_obd_table_rows(REPO_TABLE_ENABLED)) is True


def test_plugin_covers_major() -> None:
    assert plugin_covers_version(["4.2.0", "4.3.0"], "4.6.0.0")
    assert not plugin_covers_version(["4.2.0", "4.3.0"], "5.0.1.0")
    assert plugin_covers_version(["4.2.0", "5.0.0"], "5.0.1.0")
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        (root / "4.2.0").mkdir()
        (root / "5.0.0").mkdir()
        (root / "README").write_text("x", encoding="utf-8")
        assert set(plugin_versions_in(root)) == {"4.2.0", "5.0.0"}


def test_hint_mentions_all_in_one() -> None:
    hint = missing_package_hint("5.0.1")
    assert "5.0.1.0" in hint
    assert "obd-mirror" in hint
    assert "All-in-One" in hint
    assert "mirrors.oceanbase.com" in hint


def test_rpm_urls_for_5_0_1() -> None:
    from obd_version import github_rpm_url, package_rpm_urls, yum_rpm_url

    urls = package_rpm_urls("5.0.1", el="8", arch="x86_64")
    assert any("mirrors.oceanbase.com" in u and "oceanbase-ce-5.0.1.0" in u for u in urls)
    assert any("github.com/oceanbase/oceanbase" in u and "oceanbase-ce-libs" in u for u in urls)
    assert (
        yum_rpm_url("oceanbase-ce", "5.0.1.0", "100000042026072912", "8", "x86_64")
        == "https://mirrors.oceanbase.com/community/stable/el/8/x86_64/oceanbase-ce-5.0.1.0-100000042026072912.el8.x86_64.rpm"
    )
    assert "v5.0.1_CE" in github_rpm_url(
        "oceanbase-ce", "5.0.1.0", "100000042026072912", "8", "x86_64"
    )


def _generate(ob_cfg: dict) -> dict:
    mod = load_module("gen_obd", ROOT / "scripts" / "03-generate-obd-config.py")
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
            "components": {
                "ob_configserver": False,
                "obproxy_ce": False,
                "obagent": False,
            },
            **ob_cfg,
        },
    }
    inv = {
        "OBSERVER_COUNT": "3",
        "DEPLOY_NAME": "ob-yc-prod",
        "OBSERVER_1_IP": "10.0.0.1",
        "OBSERVER_2_IP": "10.0.0.2",
        "OBSERVER_3_IP": "10.0.0.3",
    }
    return mod.build_obd_config(cfg, inv)


def test_generate_writes_padded_version() -> None:
    ob = _generate({"version": "5.0.1"})["oceanbase-ce"]
    assert ob["version"] == "5.0.1.0"
    ob_empty = _generate({"version": ""})["oceanbase-ce"]
    assert "version" not in ob_empty


def test_example_defaults_to_5_0_1() -> None:
    import yaml

    example = yaml.safe_load((ROOT / "config" / "deploy.yaml.example").read_text(encoding="utf-8"))
    assert requested_oceanbase_version(cfg=example) == DEFAULT_NEW_CLUSTER_VERSION
    assert example["oceanbase"]["enable_remote_mirror"] is True


def test_deploy_does_not_pass_dash_v() -> None:
    text = (ROOT / "scripts" / "04-deploy-cluster.sh").read_text(encoding="utf-8")
    assert "prepare-obd-mirror.sh" in text
    assert "cluster deploy" in text
    assert '-V "${ob_version}"' not in text
    prepare = (ROOT / "scripts" / "lib" / "prepare-obd-mirror.sh").read_text(encoding="utf-8")
    assert "obd mirror clone" in prepare
    assert "rpm-urls" in prepare
    assert "obd mirror enable remote" in prepare
    enable_at = prepare.index("obd mirror enable remote")
    plugin_die_at = prepare.index("Плагин OBD не покрывает")
    assert enable_at < plugin_die_at
    check = (ROOT / "scripts" / "00-check-prerequisites.sh").read_text(encoding="utf-8")
    assert "prepare-obd-mirror.sh" in check
    assert "--ensure" in check
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "obd-mirror" in deploy
    assert "prepare-obd-mirror.sh" in deploy


def test_scripts_bash_syntax() -> None:
    for rel in (
        "scripts/lib/prepare-obd-mirror.sh",
        "scripts/00-check-prerequisites.sh",
        "scripts/04-deploy-cluster.sh",
        "scripts/deploy.sh",
        "scripts/lib/common.sh",
    ):
        subprocess.run(["bash", "-n", str(ROOT / rel)], check=True)


def test_cli_requested_and_has_package() -> None:
    script = ROOT / "scripts" / "lib" / "obd_version.py"
    with tempfile.TemporaryDirectory() as raw:
        cfg = Path(raw) / "deploy.yaml"
        cfg.write_text("oceanbase:\n  version: 5.0.1\n", encoding="utf-8")
        proc = subprocess.run(
            ["python3", str(script), "requested", "--config", str(cfg)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip() == "5.0.1.0"
    proc = subprocess.run(
        ["python3", str(script), "has-package", "--version", "5.0.1.0"],
        input=LOCAL_TABLE,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert proc.stdout.strip() == "no"
    proc = subprocess.run(
        ["python3", str(script), "has-package", "--version", "4.6.0"],
        input=LOCAL_TABLE,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "yes"


def main() -> None:
    tests = [
        test_normalize_and_match,
        test_requested_from_config,
        test_parse_local_packages,
        test_remote_enabled_flag,
        test_plugin_covers_major,
        test_hint_mentions_all_in_one,
        test_rpm_urls_for_5_0_1,
        test_generate_writes_padded_version,
        test_example_defaults_to_5_0_1,
        test_deploy_does_not_pass_dash_v,
        test_scripts_bash_syntax,
        test_cli_requested_and_has_package,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
