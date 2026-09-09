#!/usr/bin/env python3
"""Раскладка observer по трём zone (OBD config, scale_out, recovery)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from ob_zones import (  # noqa: E402
    MAX_PAXOS_ZONES,
    ZONE_COUNT,
    check_obd_yaml_path,
    oceanbase_zones_from_obd,
    too_many_zones_error,
    unique_zones,
    uneven_zones_warning,
    zone_for_index,
    zone_names,
    zone_sizes,
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def obd_config_for(observer_count: int) -> dict:
    mod = load_module("gen_obd", ROOT / "scripts" / "03-generate-obd-config.py")
    cfg = {
        "yandex_cloud": {"zone": "ru-central1-d", "ssh_user": "demo"},
        "ssh": {"port": 22, "private_key_file": ""},
        "vm_profiles": {"observer": {"count": observer_count}},
        "oceanbase": {
            "auto_tune": False,
            "cluster_name": "obcluster",
            "cpu_count": 16,
            "memory_limit": "48G",
            "system_memory": "4G",
            "datafile_size": "474G",
            "log_disk_size": "250G",
            "components": {
                "ob_configserver": False,
                "obproxy_ce": False,
                "obagent": False,
            },
        },
    }
    inv = {"OBSERVER_COUNT": str(observer_count), "DEPLOY_NAME": "ob-yc-prod"}
    for idx in range(1, observer_count + 1):
        inv[f"OBSERVER_{idx}_IP"] = f"10.0.0.{idx}"
    return mod.build_obd_config(cfg, inv)


def config_zones(observer_count: int) -> list[str]:
    ob = obd_config_for(observer_count)["oceanbase-ce"]
    return [ob[f"server{idx}"]["zone"] for idx in range(1, observer_count + 1)]


def test_three_zones_round_robin() -> None:
    assert ZONE_COUNT == 3
    assert zone_names() == ["zone1", "zone2", "zone3"]
    assert [zone_for_index(i) for i in range(1, 8)] == [
        "zone1",
        "zone2",
        "zone3",
        "zone1",
        "zone2",
        "zone3",
        "zone1",
    ]


def test_zone_sizes_are_even_when_divisible() -> None:
    assert zone_sizes(33) == {"zone1": 11, "zone2": 11, "zone3": 11}
    assert zone_sizes(3) == {"zone1": 1, "zone2": 1, "zone3": 1}
    assert zone_sizes(31) == {"zone1": 11, "zone2": 10, "zone3": 10}


def test_uneven_and_small_clusters_warn() -> None:
    assert uneven_zones_warning(33) is None
    assert uneven_zones_warning(18) is None
    assert uneven_zones_warning(31) is not None
    assert uneven_zones_warning(2) is not None


def test_obd_config_never_exceeds_three_zones() -> None:
    # OB_MAX_MEMBER_NUMBER = 7: sys-тенант получает full-реплику на каждую zone,
    # поэтому zone на observer ломает bootstrap на больших кластерах.
    for observer_count in (3, 18, 30, 33):
        zones = config_zones(observer_count)
        counts = Counter(zones)
        assert set(counts) == set(zone_names()), (observer_count, counts)
        assert max(counts.values()) - min(counts.values()) <= 1, (observer_count, counts)
        assert counts["zone1"] == observer_count // 3, (observer_count, counts)


def test_obd_config_small_cluster_uses_first_zones() -> None:
    assert config_zones(1) == ["zone1"]
    assert config_zones(2) == ["zone1", "zone2"]


def test_root_password_applied_without_ocp_vm() -> None:
    """Gist deploy-noocp: ocp.enabled=true, vm_profiles.ocp.enabled=false."""
    mod = load_module("gen_obd", ROOT / "scripts" / "03-generate-obd-config.py")
    cfg = {
        "yandex_cloud": {"zone": "ru-central1-d", "ssh_user": "demo"},
        "ssh": {"port": 22, "private_key_file": ""},
        "vm_profiles": {
            "observer": {"count": 3},
            "ocp": {"enabled": False, "count": 1},
        },
        "ocp": {
            "enabled": True,
            "root_password": "ChangeMe1!",
            "proxyro_password": "ChangeMe1!",
        },
        "oceanbase": {
            "auto_tune": False,
            "cluster_name": "obcluster",
            "cpu_count": 16,
            "memory_limit": "48G",
            "system_memory": "4G",
            "datafile_size": "474G",
            "log_disk_size": "250G",
            "components": {
                "ob_configserver": False,
                "obproxy_ce": False,
                "obagent": False,
            },
        },
    }
    inv = {"OBSERVER_COUNT": "3", "DEPLOY_NAME": "ob-yc-prod", "OCP_COUNT": "0"}
    for idx in range(1, 4):
        inv[f"OBSERVER_{idx}_IP"] = f"10.0.0.{idx}"
    obd = mod.build_obd_config(cfg, inv)
    global_cfg = obd["oceanbase-ce"]["global"]
    assert global_cfg["root_password"] == "ChangeMe1!", global_cfg
    assert global_cfg["proxyro_password"] == "ChangeMe1!", global_cfg
    assert "ocp_meta_tenant" not in global_cfg
    assert "ocp-server-ce" not in obd


def test_obd_config_omits_root_password_without_ocp_section() -> None:
    global_cfg = obd_config_for(3)["oceanbase-ce"]["global"]
    assert "root_password" not in global_cfg
    assert "proxyro_password" not in global_cfg


def test_scale_out_reuses_zone_of_replaced_node() -> None:
    ob_sys = load_module("ob_sys", ROOT / "scripts" / "lib" / "ob-sys.py")
    cfg = {"oceanbase": {"deploy_user": "obadmin", "ports": {"mysql": 2881, "rpc": 2882}}}
    for idx, want in ((1, "zone1"), (5, "zone2"), (30, "zone3")):
        block = ob_sys.build_observer_scale_out(cfg, idx, "10.9.9.9", include_obagent=False)
        assert block["oceanbase-ce"][f"server{idx}r"]["zone"] == want


def test_cli_used_by_recover_script() -> None:
    script = ROOT / "scripts" / "lib" / "ob_zones.py"
    out = subprocess.run(
        [sys.executable, str(script), "name", "30"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "zone3"
    recover = (ROOT / "scripts" / "06-recover-observer.sh").read_text(encoding="utf-8")
    assert "ob_zones.py" in recover
    assert 'ZONE="zone${INDEX}"' not in recover


def _thirty_zone_obd() -> dict:
    servers: dict = {}
    for idx in range(1, 31):
        servers[f"server{idx}"] = {"ip": f"10.0.0.{idx}", "zone": f"zone{idx}"}
    return {
        "oceanbase-ce": {
            "servers": [{"name": f"server{i}", "ip": f"10.0.0.{i}"} for i in range(1, 31)],
            **servers,
        }
    }


def test_too_many_zones_detected_in_obd_yaml() -> None:
    assert MAX_PAXOS_ZONES == 7
    zones = oceanbase_zones_from_obd(_thirty_zone_obd())
    assert len(unique_zones(zones)) == 30
    err = too_many_zones_error(zones, "test.yaml")
    assert err is not None
    assert "30 уникальных zone" in err
    assert "obshell bootstrap" in err
    three = oceanbase_zones_from_obd(obd_config_for(30))
    assert unique_zones(three) == ["zone1", "zone2", "zone3"]
    assert too_many_zones_error(three, "ok.yaml") is None


def test_check_obd_cli_rejects_thirty_zones() -> None:
    import tempfile

    import yaml

    data = _thirty_zone_obd()
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "obd.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "lib" / "ob_zones.py"), "check-obd", str(path), "--dump"],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1, proc.stderr
        assert "30 уникальных zone" in proc.stderr
        assert check_obd_yaml_path(path) is not None
        good = Path(td) / "good.yaml"
        good.write_text(yaml.safe_dump(obd_config_for(30)), encoding="utf-8")
        proc_ok = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "lib" / "ob_zones.py"), "check-obd", str(good), "--dump"],
            capture_output=True,
            text=True,
        )
        assert proc_ok.returncode == 0, proc_ok.stderr
        assert "zone1=" in proc_ok.stdout


def test_diagnose_script_help() -> None:
    script = ROOT / "scripts" / "diagnose-obd-start.sh"
    assert script.is_file()
    out = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, check=True)
    assert "obshell bootstrap" in out.stdout
    assert "DAG" in out.stdout
    text = script.read_text(encoding="utf-8")
    assert "wait_dag_succeed" in text
    assert "TAKE OVER MASTER" in text
    assert "Не убивайте obshell на всех" in text
    assert "dump_obshell_dag" in text
    assert "obshell_ocs.py" in text
    assert "Request.Header.NotFound" in text
    assert "TAKE OVER FOLLOWER', instead of 'CLUSTER AGENT'" in text
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    deploy_case = deploy.split("deploy)")[1].split("tenant)")[0]
    assert "diagnose-obd-start.sh" in deploy
    assert "03-generate-obd-config.py" in deploy_case
    assert "run_ocp_clockdiff_if_enabled" in deploy_case
    assert "09-ocp-register.sh" in deploy
    assert "ocp-register" in deploy


def test_ocp_register_script_help() -> None:
    script = ROOT / "scripts" / "09-ocp-register.sh"
    assert script.is_file()
    out = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, check=True)
    assert "export-to-ocp" in out.stdout
    assert "ocp-server-ce" in out.stdout
    assert "-V" in out.stdout
    text = script.read_text(encoding="utf-8")
    assert 'check4ocp "${CLUSTER_NAME}" -V "${OCP_VERSION}"' in text
    assert "--host_type" in text
    assert "ocp_takeover.py" in text


def test_dump_obshell_dag_maps_numeric_state() -> None:
    script = ROOT / "scripts" / "diagnose-obd-start.sh"
    snippet = ""
    inside = False
    for line in script.read_text(encoding="utf-8").splitlines():
        if line.startswith("dump_obshell_dag()"):
            inside = True
        if inside:
            snippet += line + "\n"
            if line == "}":
                break
    assert "STATES=" in snippet
    assert snippet.strip().startswith("dump_obshell_dag()")
    payload = '{"data":{"name":"TakeOver","state":2,"stage":1,"max_stage":4,"operator":1,"id":"d1","nodes":[{"name":"n1","state":4}]}}'
    proc = subprocess.run(
        ["bash", "-c", snippet.strip() + "\nprintf '%s' \"$1\" | dump_obshell_dag", "_", payload],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "DAG_STATE=RUNNING" in proc.stdout
    assert "name=TakeOver" in proc.stdout
    assert "node n1  state=SUCCEED" in proc.stdout


def test_obshell_ocs_header() -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tests" / "test_obshell_ocs.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: 2 tests" in proc.stdout


def main() -> None:
    tests = [
        test_three_zones_round_robin,
        test_zone_sizes_are_even_when_divisible,
        test_uneven_and_small_clusters_warn,
        test_obd_config_never_exceeds_three_zones,
        test_obd_config_small_cluster_uses_first_zones,
        test_root_password_applied_without_ocp_vm,
        test_obd_config_omits_root_password_without_ocp_section,
        test_scale_out_reuses_zone_of_replaced_node,
        test_cli_used_by_recover_script,
        test_too_many_zones_detected_in_obd_yaml,
        test_check_obd_cli_rejects_thirty_zones,
        test_diagnose_script_help,
        test_ocp_register_script_help,
        test_dump_obshell_dag_maps_numeric_state,
        test_obshell_ocs_header,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
