#!/usr/bin/env python3
"""Patch oceanbase-ce.global.mysql_port for OBD export-to-ocp / OCP takeOver."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from ocp_takeover import (  # noqa: E402
    export_to_ocp_log_ok,
    insert_global_mysql_port,
    needs_mysql_port,
    patch_obd_cluster_dir,
    patch_obd_config_text,
)

SAMPLE = """\
user:
  username: obadmin
oceanbase-ce:
  servers:
    - name: server1
      ip: 10.130.0.34
  global:
    appname: obcluster
    root_password: ChangeMe1!
  server1:
    mysql_port: 2881
    zone: zone1
ocp-server-ce:
  servers:
    - 10.130.0.22
"""

ALREADY = """\
oceanbase-ce:
  global:
    mysql_port: 2881
    appname: obcluster
  server1:
    mysql_port: 2881
"""


def test_needs_port_when_only_per_server() -> None:
    import yaml

    data = yaml.safe_load(SAMPLE)
    assert needs_mysql_port(data)
    already = yaml.safe_load(ALREADY)
    assert not needs_mysql_port(already)


def test_insert_preserves_server_and_password() -> None:
    patched, did = patch_obd_config_text(SAMPLE, mysql_port=2881)
    assert did
    assert "    mysql_port: 2881\n" in patched
    assert "root_password: ChangeMe1!" in patched
    assert patched.index("global:") < patched.index("mysql_port: 2881")
    # per-server block still present
    assert "  server1:\n    mysql_port: 2881" in patched
    again, did_again = patch_obd_config_text(patched, mysql_port=2881)
    assert not did_again
    assert again == patched


def test_insert_skips_when_global_has_port() -> None:
    out = insert_global_mysql_port(ALREADY, mysql_port=2882)
    assert out == ALREADY


def test_patch_cluster_dir() -> None:
    with tempfile.TemporaryDirectory() as raw:
        cluster = Path(raw)
        cfg = cluster / "config.yaml"
        cfg.write_text(SAMPLE, encoding="utf-8")
        changed = patch_obd_cluster_dir(cluster, mysql_port=2881)
        assert changed == [cfg]
        text = cfg.read_text(encoding="utf-8")
        assert "    mysql_port: 2881\n" in text
        assert patch_obd_cluster_dir(cluster, mysql_port=2881) == []


def test_cli_patch() -> None:
    script = ROOT / "scripts" / "lib" / "ocp_takeover.py"
    with tempfile.TemporaryDirectory() as raw:
        cluster = Path(raw)
        (cluster / "config.yaml").write_text(SAMPLE, encoding="utf-8")
        proc = subprocess.run(
            [
                "python3",
                str(script),
                "patch-config",
                "--cluster-dir",
                str(cluster),
                "--mysql-port",
                "2881",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "patched" in proc.stdout
        proc2 = subprocess.run(
            ["python3", str(script), "patch-config", "--cluster-dir", str(cluster)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "already" in proc2.stdout


def test_register_script_passes_host_type() -> None:
    script = (ROOT / "scripts" / "09-ocp-register.sh").read_text(encoding="utf-8")
    assert "--host_type" in script
    assert "--credential_name" in script
    assert "ocp_takeover.py" in script
    assert 'check4ocp "${CLUSTER_NAME}" -V "${OCP_VERSION}"' in script
    assert "log-ok" in script
    assert "PIPESTATUS" in script


def test_export_to_ocp_log_ok_after_utils_error() -> None:
    log = """
[ERROR] Failed to install repository oceanbase-ce-utils
[WARN] Failed to install utils to servers
takeover task successfully submitted to ocp, you can check task at http://10.130.0.22:8080/task/22
"""
    assert export_to_ocp_log_ok(log)
    assert not export_to_ocp_log_ok("Failed to install utils to servers\nTrace ID: abc")
    proc = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts" / "lib" / "ocp_takeover.py"),
            "log-ok",
        ],
        input=log,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0


def test_generated_obd_global_has_mysql_port() -> None:
    sys.path.insert(0, str(ROOT / "tests"))
    from test_ob_zones import obd_config_for

    ob = obd_config_for(3)["oceanbase-ce"]
    assert ob["global"]["mysql_port"] == 2881
    assert ob["server1"]["mysql_port"] == 2881


def main() -> None:
    tests = [
        test_needs_port_when_only_per_server,
        test_insert_preserves_server_and_password,
        test_insert_skips_when_global_has_port,
        test_patch_cluster_dir,
        test_cli_patch,
        test_register_script_passes_host_type,
        test_export_to_ocp_log_ok_after_utils_error,
        test_generated_obd_global_has_mysql_port,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
