#!/usr/bin/env python3
"""Seed bootstrap and idempotent staged scale-out plan tests."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "lib" / "ob_deploy_plan.py"
    spec = importlib.util.spec_from_file_location("ob_deploy_plan", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PLAN = load_module()


def full_config(count: int = 9, *, with_obagent: bool = True) -> dict:
    servers = [
        {"name": f"server{idx}", "ip": f"10.0.0.{idx}"}
        for idx in range(1, count + 1)
    ]
    ob = {
        "depends": ["ob-configserver"],
        "servers": servers,
        "global": {"appname": "obcluster", "production_mode": True},
    }
    for idx in range(1, count + 1):
        ob[f"server{idx}"] = {
            "mysql_port": 2881,
            "rpc_port": 2882,
            "obshell_port": 2886,
            "zone": f"zone{(idx - 1) % 3 + 1}",
            "idc": "ru_central1",
        }

    cfg = {
        "user": {"username": "obadmin"},
        "ob-configserver": {"servers": ["10.0.0.1"]},
        "oceanbase-ce": ob,
        "obproxy-ce": {
            "depends": ["oceanbase-ce"],
            "servers": ["10.0.1.1", "10.0.1.2"],
        },
        "prometheus": {
            "servers": ["10.0.2.1"],
            "global": {"config": {"scrape_configs": [{"job_name": "node"}]}},
        },
    }
    if with_obagent:
        cfg["obagent"] = {
            "depends": ["oceanbase-ce"],
            "servers": [dict(entry) for entry in servers],
            "global": {"monagent_http_port": 8088},
        }
    return cfg


def registered_config(observer_indices: list[int], agent_indices: list[int]) -> dict:
    cfg = {
        "oceanbase-ce": {
            "servers": [
                {"name": f"server{idx}", "ip": f"10.0.0.{idx}"}
                for idx in observer_indices
            ]
        }
    }
    if agent_indices:
        cfg["obagent"] = {
            "servers": [
                {"name": f"server{idx}", "ip": f"10.0.0.{idx}"}
                for idx in agent_indices
            ]
        }
    return cfg


def ips(component: dict) -> list[str]:
    return [PLAN.server_ip(entry) for entry in component["servers"]]


def test_seed_keeps_three_zones_and_service_components() -> None:
    full = full_config()
    seed = PLAN.build_seed_config(full)

    assert ips(seed["oceanbase-ce"]) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert ips(seed["obagent"]) == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]
    assert [seed["oceanbase-ce"][f"server{idx}"]["zone"] for idx in range(1, 4)] == [
        "zone1",
        "zone2",
        "zone3",
    ]
    assert "server4" not in seed["oceanbase-ce"]
    assert seed["oceanbase-ce"]["global"] == full["oceanbase-ce"]["global"]
    assert seed["obproxy-ce"] == full["obproxy-ce"]
    assert seed["prometheus"] == full["prometheus"]
    assert full["oceanbase-ce"]["servers"][-1]["name"] == "server9"


def test_scale_out_plan_uses_balanced_triples() -> None:
    plan = PLAN.build_scale_out_plan(
        full_config(),
        registered_config([1, 2, 3], [1, 2, 3]),
    )

    assert len(plan) == 2
    assert ips(plan[0]["oceanbase"]["oceanbase-ce"]) == [
        "10.0.0.4",
        "10.0.0.5",
        "10.0.0.6",
    ]
    assert ips(plan[0]["obagent"]["obagent"]) == [
        "10.0.0.4",
        "10.0.0.5",
        "10.0.0.6",
    ]
    assert [
        plan[0]["oceanbase"]["oceanbase-ce"][f"server{idx}"]["zone"]
        for idx in range(4, 7)
    ] == ["zone1", "zone2", "zone3"]
    assert "global" not in plan[0]["oceanbase"]["oceanbase-ce"]
    assert ips(plan[1]["oceanbase"]["oceanbase-ce"]) == [
        "10.0.0.7",
        "10.0.0.8",
        "10.0.0.9",
    ]


def test_scale_out_plan_resumes_components_independently() -> None:
    plan = PLAN.build_scale_out_plan(
        full_config(count=6),
        registered_config([1, 2, 3, 4], [1, 2, 3]),
    )

    assert len(plan) == 1
    assert ips(plan[0]["oceanbase"]["oceanbase-ce"]) == ["10.0.0.5", "10.0.0.6"]
    assert ips(plan[0]["obagent"]["obagent"]) == [
        "10.0.0.4",
        "10.0.0.5",
        "10.0.0.6",
    ]


def test_scale_out_plan_is_empty_when_cluster_is_complete() -> None:
    assert PLAN.build_scale_out_plan(
        full_config(count=6),
        registered_config([1, 2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6]),
    ) == []


def test_seed_rejects_invalid_layout() -> None:
    cfg = full_config(count=3)
    cfg["oceanbase-ce"]["server3"]["zone"] = "zone2"
    try:
        PLAN.build_seed_config(cfg)
    except ValueError as exc:
        assert "three distinct zones" in str(exc)
    else:
        raise AssertionError("invalid seed topology was accepted")


def test_without_obagent_does_not_create_agent_batches() -> None:
    plan = PLAN.build_scale_out_plan(
        full_config(count=6, with_obagent=False),
        registered_config([1, 2, 3], []),
    )
    assert len(plan) == 1
    assert plan[0]["obagent"] is None


def test_scale_out_cli_writes_resumable_manifest() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        full_path = root / "full.yaml"
        registered_path = root / "registered.yaml"
        manifest = root / "plan" / "manifest.txt"
        PLAN.dump_yaml(full_config(count=7), full_path)
        PLAN.dump_yaml(registered_config([1, 2, 3, 4], [1, 2, 3]), registered_path)

        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "lib" / "ob_deploy_plan.py"),
                "scale-out",
                "--input",
                str(full_path),
                "--registered-config",
                str(registered_path),
                "--output-dir",
                str(root / "plan"),
                "--manifest",
                str(manifest),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        lines = manifest.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4
        first_label, first_ob, first_agent = lines[0].split("|")
        assert first_label == "server4-server6/server4"
        assert first_ob == "-"
        assert ips(PLAN.load_yaml(Path(first_agent))["obagent"]) == ["10.0.0.4"]

        second_label, second_ob, second_agent = lines[1].split("|")
        assert second_label == "server4-server6/server5"
        assert ips(PLAN.load_yaml(Path(second_ob))["oceanbase-ce"]) == ["10.0.0.5"]
        assert ips(PLAN.load_yaml(Path(second_agent))["obagent"]) == ["10.0.0.5"]


def test_malformed_registered_config_fails_closed() -> None:
    registered = registered_config([1, 2, 3], [1, 2, 3])
    registered["oceanbase-ce"]["servers"].append({"name": "broken"})
    try:
        PLAN.build_scale_out_plan(full_config(count=6), registered)
    except ValueError as exc:
        assert "invalid OBD server entry" in str(exc)
    else:
        raise AssertionError("malformed OBD metadata was treated as an empty cluster")


if __name__ == "__main__":
    test_seed_keeps_three_zones_and_service_components()
    test_scale_out_plan_uses_balanced_triples()
    test_scale_out_plan_resumes_components_independently()
    test_scale_out_plan_is_empty_when_cluster_is_complete()
    test_seed_rejects_invalid_layout()
    test_without_obagent_does_not_create_agent_batches()
    test_scale_out_cli_writes_resumable_manifest()
    test_malformed_registered_config_fails_closed()
    print("ok")
