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
    ZONE_COUNT,
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


def main() -> None:
    tests = [
        test_three_zones_round_robin,
        test_zone_sizes_are_even_when_divisible,
        test_uneven_and_small_clusters_warn,
        test_obd_config_never_exceeds_three_zones,
        test_obd_config_small_cluster_uses_first_zones,
        test_scale_out_reuses_zone_of_replaced_node,
        test_cli_used_by_recover_script,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
