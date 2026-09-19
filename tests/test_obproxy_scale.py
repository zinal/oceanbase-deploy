#!/usr/bin/env python3
"""План scale-obproxy: досоздать хосты, не удаляя старые ВМ."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_mod(name: str, rel: str):
    path = ROOT / rel
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


SCALE = load_mod("obproxy_scale", "scripts/lib/obproxy_scale.py")
OB_SYS = load_mod("ob_sys", "scripts/lib/ob-sys.py")


def _inv(**extra: str) -> dict[str, str]:
    data = {
        "OBSERVER_COUNT": "3",
        "OBSERVER_1_NAME": "ob-yc-prod-observer-1",
        "OBSERVER_1_IP": "10.0.0.1",
        "DEPLOY_NAME": "ob-yc-prod",
        "RUNNER_COUNT": "2",
        "RUNNER_1_NAME": "ob-runner-1",
        "RUNNER_1_IP": "10.0.3.1",
    }
    data.update(extra)
    return data


def test_canonical_name() -> None:
    assert SCALE.canonical_name("ob-yc-prod", 3) == "ob-yc-prod-obproxy-3"
    try:
        SCALE.canonical_name("ob-yc-prod", 0)
    except ValueError:
        pass
    else:
        raise AssertionError("index=0 должен быть ошибкой")


def test_increase_creates_missing_only() -> None:
    inv = _inv(
        OBPROXY_COUNT="2",
        OBPROXY_1_NAME="ob-yc-prod-obproxy-1",
        OBPROXY_1_IP="10.0.1.1",
        OBPROXY_2_NAME="ob-yc-prod-obproxy-2",
        OBPROXY_2_IP="10.0.1.2",
    )
    plan = SCALE.plan_obproxy_scale(
        deploy_name="ob-yc-prod",
        desired_count=4,
        existing={
            "ob-yc-prod-obproxy-1": "10.0.1.1",
            "ob-yc-prod-obproxy-2": "10.0.1.2",
        },
        inventory=inv,
        obd_ips=["10.0.1.1", "10.0.1.2"],
    )
    assert [row["name"] for row in plan["create"]] == [
        "ob-yc-prod-obproxy-3",
        "ob-yc-prod-obproxy-4",
    ]
    assert plan["scale_out"] == []
    assert plan["clean_obd_ips"] == []
    assert plan["desired_count"] == 4
    text = SCALE.render_plan_text(plan)
    assert "создать ВМ: ob-yc-prod-obproxy-3, ob-yc-prod-obproxy-4" in text
    assert "ВМ скрипт не удаляет" in text


def test_deleted_old_ip_is_cleaned_before_scale_out_text() -> None:
    inv = _inv(
        OBPROXY_COUNT="2",
        OBPROXY_1_NAME="ob-yc-prod-obproxy-1",
        OBPROXY_1_IP="10.0.1.1",
        OBPROXY_2_NAME="ob-yc-prod-obproxy-2",
        OBPROXY_2_IP="10.0.1.47",
    )
    plan = SCALE.plan_obproxy_scale(
        deploy_name="ob-yc-prod",
        desired_count=2,
        existing={
            "ob-yc-prod-obproxy-1": "10.0.1.1",
            "ob-yc-prod-obproxy-2": "10.0.1.58",
        },
        inventory=inv,
        obd_ips=["10.0.1.1", "10.0.1.47"],
    )
    assert plan["clean_obd_ips"] == ["10.0.1.47"]
    assert [row["ip"] for row in plan["scale_out"]] == ["10.0.1.58"]
    text = SCALE.render_plan_text(plan)
    clean_at = text.find("сначала убрать из OBD мёртвые IP")
    scale_at = text.find("затем OBD scale_out")
    assert 0 <= clean_at < scale_at
    assert "10.0.1.47" in text
    assert "OBD-1013" in text


def test_scale_out_after_new_ips() -> None:
    inv = _inv(OBPROXY_COUNT="2", OBPROXY_1_IP="10.0.1.1", OBPROXY_2_IP="10.0.1.2")
    plan = SCALE.plan_obproxy_scale(
        deploy_name="ob-yc-prod",
        desired_count=4,
        existing={
            "ob-yc-prod-obproxy-1": "10.0.1.1",
            "ob-yc-prod-obproxy-2": "10.0.1.2",
            "ob-yc-prod-obproxy-3": "10.0.1.3",
            "ob-yc-prod-obproxy-4": "10.0.1.4",
        },
        inventory=inv,
        obd_ips=["10.0.1.1", "10.0.1.2"],
    )
    assert plan["create"] == []
    assert [row["ip"] for row in plan["scale_out"]] == ["10.0.1.3", "10.0.1.4"]
    assert [row["ip"] for row in plan["final"]] == [
        "10.0.1.1",
        "10.0.1.2",
        "10.0.1.3",
        "10.0.1.4",
    ]


def test_user_deleted_old_vms_recreate_and_clean_obd() -> None:
    """Старые ВМ удалены вручную: слоты 1..N создаются заново, старые IP уходят из OBD."""
    inv = _inv(
        OBPROXY_COUNT="2",
        OBPROXY_1_NAME="ob-yc-prod-obproxy-1",
        OBPROXY_1_IP="10.0.1.1",
        OBPROXY_2_NAME="ob-yc-prod-obproxy-2",
        OBPROXY_2_IP="10.0.1.2",
    )
    plan = SCALE.plan_obproxy_scale(
        deploy_name="ob-yc-prod",
        desired_count=4,
        existing={},
        inventory=inv,
        obd_ips=["10.0.1.1", "10.0.1.2"],
    )
    assert [row["name"] for row in plan["create"]] == [
        "ob-yc-prod-obproxy-1",
        "ob-yc-prod-obproxy-2",
        "ob-yc-prod-obproxy-3",
        "ob-yc-prod-obproxy-4",
    ]
    assert plan["keep"] == []
    assert set(plan["clean_obd_ips"]) == {"10.0.1.1", "10.0.1.2"}


def test_refuse_shrink_while_extra_vms_live() -> None:
    inv = _inv(
        OBPROXY_COUNT="4",
        OBPROXY_1_IP="10.0.1.1",
        OBPROXY_4_NAME="ob-yc-prod-obproxy-4",
        OBPROXY_4_IP="10.0.1.4",
    )
    try:
        SCALE.plan_obproxy_scale(
            deploy_name="ob-yc-prod",
            desired_count=2,
            existing={
                "ob-yc-prod-obproxy-1": "10.0.1.1",
                "ob-yc-prod-obproxy-4": "10.0.1.4",
            },
            inventory=inv,
            obd_ips=["10.0.1.1", "10.0.1.4"],
        )
    except ValueError as exc:
        assert "Лишние ВМ" in str(exc)
        assert "ob-yc-prod-obproxy-4" in str(exc)
    else:
        raise AssertionError("ожидался ValueError при живых лишних ВМ")


def test_sync_after_user_deleted_extras() -> None:
    inv = _inv(
        OBPROXY_COUNT="4",
        OBPROXY_1_NAME="ob-yc-prod-obproxy-1",
        OBPROXY_1_IP="10.0.1.1",
        OBPROXY_2_NAME="ob-yc-prod-obproxy-2",
        OBPROXY_2_IP="10.0.1.2",
        OBPROXY_3_NAME="ob-yc-prod-obproxy-3",
        OBPROXY_3_IP="10.0.1.3",
        OBPROXY_4_NAME="ob-yc-prod-obproxy-4",
        OBPROXY_4_IP="10.0.1.4",
    )
    plan = SCALE.plan_obproxy_scale(
        deploy_name="ob-yc-prod",
        desired_count=2,
        existing={
            "ob-yc-prod-obproxy-1": "10.0.1.1",
            "ob-yc-prod-obproxy-2": "10.0.1.2",
        },
        inventory=inv,
        obd_ips=["10.0.1.1", "10.0.1.2", "10.0.1.3", "10.0.1.4"],
    )
    assert plan["create"] == []
    assert set(plan["clean_obd_ips"]) == {"10.0.1.3", "10.0.1.4"}
    assert any("OBPROXY_COUNT=4" in w for w in plan["warnings"])


def test_replace_inventory_keeps_other_roles() -> None:
    inv = _inv(
        OBPROXY_COUNT="2",
        OBPROXY_1_NAME="ob-yc-prod-obproxy-1",
        OBPROXY_1_IP="10.0.1.1",
        OBPROXY_2_NAME="ob-yc-prod-obproxy-2",
        OBPROXY_2_IP="10.0.1.2",
    )
    updated = OB_SYS.replace_inventory_hosts(
        inv,
        "OBPROXY",
        [
            (1, "ob-yc-prod-obproxy-1", "10.0.9.1"),
            (2, "ob-yc-prod-obproxy-2", "10.0.9.2"),
            (3, "ob-yc-prod-obproxy-3", "10.0.9.3"),
        ],
    )
    assert updated["OBPROXY_COUNT"] == "3"
    assert updated["OBPROXY_3_NAME"] == "ob-yc-prod-obproxy-3"
    assert updated["OBSERVER_1_IP"] == "10.0.0.1"
    assert updated["RUNNER_1_NAME"] == "ob-runner-1"
    shrunk = OB_SYS.replace_inventory_hosts(
        updated, "OBPROXY", [(1, "ob-yc-prod-obproxy-1", "10.0.9.1")]
    )
    assert shrunk["OBPROXY_COUNT"] == "1"
    assert "OBPROXY_2_IP" not in shrunk
    assert "OBPROXY_3_NAME" not in shrunk


def test_cli_plan_and_names(tmp_path: Path) -> None:
    inv = tmp_path / "inventory.env"
    inv.write_text(
        "OBPROXY_COUNT=1\nOBPROXY_1_NAME=ob-yc-prod-obproxy-1\nOBPROXY_1_IP=10.0.1.1\n",
        encoding="utf-8",
    )
    existing = tmp_path / "existing.txt"
    existing.write_text("ob-yc-prod-obproxy-1=10.0.1.1\n", encoding="utf-8")
    out = tmp_path / "plan.json"
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "lib" / "obproxy_scale.py"),
            "plan",
            "--deploy-name",
            "ob-yc-prod",
            "--desired-count",
            "3",
            "--inventory",
            str(inv),
            "--existing-file",
            str(existing),
            "--obd-ip",
            "10.0.1.1",
            "--output",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    plan = json.loads(out.read_text(encoding="utf-8"))
    assert [row["name"] for row in plan["create"]] == [
        "ob-yc-prod-obproxy-2",
        "ob-yc-prod-obproxy-3",
    ]
    names = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "lib" / "obproxy_scale.py"),
            "names",
            "--deploy-name",
            "ob-yc-prod",
            "--desired-count",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert names.stdout.splitlines() == [
        "ob-yc-prod-obproxy-1",
        "ob-yc-prod-obproxy-2",
    ]
    assert proc.returncode == 0


def test_deploy_sh_has_scale_obproxy() -> None:
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "20-scale-obproxy.sh" in deploy
    assert "scale-obproxy)" in deploy
    recover = (ROOT / "scripts" / "07-recover-obproxy.sh").read_text(encoding="utf-8")
    assert "10-runner-haproxy.sh" in recover
    assert "refresh_runner_haproxy" in recover
    script = (ROOT / "scripts" / "20-scale-obproxy.sh").read_text(encoding="utf-8")
    assert "10-runner-haproxy.sh" in script
    assert "не удаляет" in script
    assert "OBD-1013" in script
    clean_fn = script.find("clean_stale_obproxy_obd")
    scale_cmd = script.find('obd cluster scale_out "${DEPLOY_NAME}"')
    assert 0 <= clean_fn < scale_cmd
    recover = (ROOT / "scripts" / "07-recover-obproxy.sh").read_text(encoding="utf-8")
    recover_clean = recover.find('clean-obd --ip "${OLD_IP}"')
    recover_scale = recover.find('obd cluster scale_out "${DEPLOY_NAME}"')
    assert 0 <= recover_clean < recover_scale


def main() -> None:
    tests = [
        test_canonical_name,
        test_increase_creates_missing_only,
        test_deleted_old_ip_is_cleaned_before_scale_out_text,
        test_scale_out_after_new_ips,
        test_user_deleted_old_vms_recreate_and_clean_obd,
        test_refuse_shrink_while_extra_vms_live,
        test_sync_after_user_deleted_extras,
        test_replace_inventory_keeps_other_roles,
        test_deploy_sh_has_scale_obproxy,
    ]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    with tempfile.TemporaryDirectory() as tmp:
        test_cli_plan_and_names(Path(tmp))
        print("OK test_cli_plan_and_names")
    print(f"OK: {len(tests) + 1} tests")


if __name__ == "__main__":
    main()
