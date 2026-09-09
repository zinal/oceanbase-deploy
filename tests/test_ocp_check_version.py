#!/usr/bin/env python3
"""OCP version for obd cluster check4ocp -V (skip OS-admin check on OCP ≥ 4.2.0)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from ocp_check_version import (  # noqa: E402
    DEFAULT_OCP_CHECK_VERSION,
    ocp_version_from_api_payload,
    ocp_version_from_mapping,
    ocp_version_from_yaml_file,
    resolve_ocp_check_version,
)


def test_config_version_wins() -> None:
    with tempfile.TemporaryDirectory() as raw:
        yaml_path = Path(raw) / "obd.yaml"
        yaml_path.write_text("ocp-server-ce:\n  version: 9.9.9\n", encoding="utf-8")
        assert (
            resolve_ocp_check_version(
                config_version="4.4.2",
                yaml_paths=[yaml_path],
                api_payload='{"data":{"buildVersion":"8.8.8"}}',
            )
            == "4.4.2"
        )


def test_empty_config_reads_obd_yaml() -> None:
    with tempfile.TemporaryDirectory() as raw:
        yaml_path = Path(raw) / "obd.yaml"
        yaml_path.write_text(
            "oceanbase-ce:\n  version: 4.6.0.0\nocp-server-ce:\n  version: 4.4.2\n",
            encoding="utf-8",
        )
        assert resolve_ocp_check_version(config_version="", yaml_paths=[yaml_path]) == "4.4.2"


def test_nested_repository_version() -> None:
    assert (
        ocp_version_from_mapping(
            {"ocp-server-ce": {"repository": {"version": "4.3.1-100"}}}
        )
        == "4.3.1"
    )


def test_empty_everything_falls_back() -> None:
    assert resolve_ocp_check_version() == DEFAULT_OCP_CHECK_VERSION
    assert resolve_ocp_check_version(config_version="null") == DEFAULT_OCP_CHECK_VERSION
    assert resolve_ocp_check_version(config_version="  ") == DEFAULT_OCP_CHECK_VERSION


def test_api_payload_build_version() -> None:
    assert (
        ocp_version_from_api_payload({"data": {"buildVersion": "4.4.2.0-100000162026071"}})
        == "4.4.2.0"
    )
    assert ocp_version_from_api_payload('{"data":{"version":"4.2.1"}}') == "4.2.1"


def test_missing_yaml_is_skipped() -> None:
    missing = Path("/tmp/ocp-check-version-missing.yaml")
    if missing.exists():
        missing.unlink()
    assert ocp_version_from_yaml_file(missing) == ""
    assert resolve_ocp_check_version(yaml_paths=[missing]) == DEFAULT_OCP_CHECK_VERSION


def test_cli_default() -> None:
    script = ROOT / "scripts" / "lib" / "ocp_check_version.py"
    proc = subprocess.run(
        ["python3", str(script), "--config-version", ""],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == DEFAULT_OCP_CHECK_VERSION


def test_register_script_always_passes_dash_v() -> None:
    script = (ROOT / "scripts" / "09-ocp-register.sh").read_text(encoding="utf-8")
    common = (ROOT / "scripts" / "lib" / "common.sh").read_text(encoding="utf-8")
    assert "resolve_ocp_check_version" in script
    assert 'check4ocp "${CLUSTER_NAME}" -V "${OCP_VERSION}"' in script
    assert "The current user must be the admin user" in script
    assert "edit-config user.username=admin" in script
    assert "resolve_ocp_check_version()" in common
    assert 'obd cluster check4ocp "${CLUSTER_NAME}"\n' not in script


def test_register_help_mentions_version_flag() -> None:
    script = ROOT / "scripts" / "09-ocp-register.sh"
    out = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, check=True)
    assert "-V" in out.stdout
    assert "3.1.1" in out.stdout
    subprocess.run(["bash", "-n", str(script)], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / "lib" / "common.sh")], check=True)


def test_bash_resolve_uses_config_version() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        cfg = tmp / "deploy.yaml"
        cfg.write_text("ocp:\n  version: 4.3.0\n", encoding="utf-8")
        snippet = r"""
set -euo pipefail
ROOT="$1"
CFG="$2"
GEN="$3"
# shellcheck source=scripts/lib/common.sh
source "${ROOT}/scripts/lib/common.sh"
CONFIG_FILE="${CFG}"
GENERATED_DIR="${GEN}"
resolve_ocp_check_version
"""
        proc = subprocess.run(
            ["bash", "-c", snippet, "bash", str(ROOT), str(cfg), str(tmp)],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "4.3.0"


def main() -> None:
    tests = [
        test_config_version_wins,
        test_empty_config_reads_obd_yaml,
        test_nested_repository_version,
        test_empty_everything_falls_back,
        test_api_payload_build_version,
        test_missing_yaml_is_skipped,
        test_cli_default,
        test_register_script_always_passes_dash_v,
        test_register_help_mentions_version_flag,
        test_bash_resolve_uses_config_version,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
