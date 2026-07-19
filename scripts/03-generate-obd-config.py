#!/usr/bin/env python3
"""Генерация конфигурации OBD для масштабируемого кластера OceanBase."""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


def load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_inventory(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def gb_suffix(value_gb: int | float) -> str:
    if isinstance(value_gb, float) and value_gb.is_integer():
        value_gb = int(value_gb)
    return f"{value_gb}G"


def auto_tune(cfg: dict, observer_count: int) -> dict:
    vm = cfg["vm"]
    cores = int(vm.get("cores", 4))
    memory_gb = int(vm.get("memory_gb", 16))
    data_gb = int(vm.get("data_disk", {}).get("size_gb", 100))

    memory_limit_gb = max(4, memory_gb - 4)
    system_memory_gb = min(4, max(2, memory_gb // 8))
    datafile_gb = max(20, int(data_gb * 0.8))
    log_disk_gb = max(15, memory_limit_gb * 3)

    return {
        "memory_limit": gb_suffix(memory_limit_gb),
        "system_memory": gb_suffix(system_memory_gb),
        "datafile_size": gb_suffix(datafile_gb),
        "log_disk_size": gb_suffix(log_disk_gb),
        "cpu_count": cores,
    }


def build_obd_config(cfg: dict, inv: dict[str, str]) -> dict:
    obs_count = int(inv.get("OBSERVER_COUNT", cfg["nodes"]["observers"]["count"]))
    obproxy_count = int(inv.get("OBPROXY_COUNT", cfg["nodes"]["obproxy"]["count"]))

    observer_ips = [inv[f"OBSERVER_{i}_IP"] for i in range(1, obs_count + 1)]
    ob_cfg = cfg["oceanbase"]
    ssh_cfg = cfg["ssh"]
    yc = cfg["yandex_cloud"]

    tune = ob_cfg
    if ob_cfg.get("auto_tune", True):
        tune = {**ob_cfg, **auto_tune(cfg, obs_count)}

    deploy_user = ob_cfg.get("deploy_user", yc.get("ssh_user", "obadmin"))
    home_path = ob_cfg.get("home_path", f"/home/{deploy_user}/observer")
    data_dir = ob_cfg.get("data_dir", "/data/1")
    redo_dir = ob_cfg.get("redo_dir", "/data/log1")
    ports = ob_cfg.get("ports", {})

    user_block: dict = {
        "username": deploy_user,
        "port": int(ssh_cfg.get("port", 22)),
        "timeout": 60,
    }
    key_file = os.path.expanduser(ssh_cfg.get("private_key_file", ""))
    if key_file:
        user_block["key_file"] = key_file
    if ssh_cfg.get("password"):
        user_block["password"] = ssh_cfg["password"]

    zones = [f"zone{i}" for i in range(1, obs_count + 1)]

    servers = []
    server_overrides: dict = {}
    for idx, ip in enumerate(observer_ips, start=1):
        sname = f"server{idx}"
        servers.append({"name": sname, "ip": ip})
        server_overrides[sname] = {
            "mysql_port": int(ports.get("mysql", 2881)),
            "rpc_port": int(ports.get("rpc", 2882)),
            "obshell_port": int(ports.get("obshell", 2886)),
            "home_path": home_path,
            "data_dir": data_dir,
            "redo_dir": redo_dir,
            "zone": zones[(idx - 1) % len(zones)],
        }

    components = ob_cfg.get("components", {})
    result: dict = {"user": user_block}

    if components.get("ob_configserver", True):
        result["ob-configserver"] = {
            "servers": [observer_ips[0]],
            "global": {
                "listen_port": 8080,
                "home_path": f"/home/{deploy_user}/ob-configserver",
            },
        }

    if components.get("oceanbase_ce", True):
        obd_ob: dict = {
            "depends": ["ob-configserver"] if components.get("ob_configserver", True) else [],
            "servers": servers,
            "global": {
                "appname": ob_cfg.get("cluster_name", "obcluster"),
                "cluster_id": 1,
                "memory_limit": tune["memory_limit"],
                "system_memory": tune["system_memory"],
                "datafile_size": tune["datafile_size"],
                "log_disk_size": tune["log_disk_size"],
                "cpu_count": tune["cpu_count"],
                "production_mode": obs_count >= 3,
                "enable_syslog_wf": False,
            },
        }
        for sname, override in server_overrides.items():
            obd_ob[sname] = override
        result["oceanbase-ce"] = obd_ob

    if components.get("obproxy_ce", True):
        if obproxy_count > 0:
            proxy_ips = [inv[f"OBPROXY_{i}_IP"] for i in range(1, obproxy_count + 1)]
        else:
            proxy_ips = observer_ips[:1]
        result["obproxy-ce"] = {
            "depends": ["oceanbase-ce"],
            "servers": proxy_ips,
            "global": {
                "listen_port": int(ports.get("obproxy", 2883)),
                "prometheus_listen_port": 2884,
                "home_path": f"/home/{deploy_user}/obproxy",
                "enable_cluster_checkout": False,
                "skip_proxy_sys_private_check": True,
                "enable_strict_kernel_release": False,
            },
        }

    if components.get("obagent", True):
        result["obagent"] = {
            "depends": ["oceanbase-ce"],
            "servers": [{"name": s["name"], "ip": s["ip"]} for s in servers],
            "global": {"home_path": f"/home/{deploy_user}/obagent"},
        }

    monitor_enabled = cfg["nodes"].get("monitoring", {}).get("enabled", False)
    mon_count = int(inv.get("MONITOR_COUNT", "0"))
    if components.get("prometheus", False):
        prom_ip = inv.get("MONITOR_1_IP", observer_ips[0])
        if monitor_enabled and mon_count > 0:
            prom_ip = inv["MONITOR_1_IP"]
        result["prometheus"] = {
            "depends": ["obagent"],
            "servers": [prom_ip],
            "global": {"home_path": f"/home/{deploy_user}/prometheus"},
        }

    if components.get("grafana", False):
        graf_ip = inv.get("MONITOR_1_IP", observer_ips[0])
        result["grafana"] = {
            "depends": ["prometheus"],
            "servers": [graf_ip],
            "global": {
                "home_path": f"/home/{deploy_user}/grafana",
                "login_password": "oceanbase",
            },
        }

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default="config/deploy.yaml",
        help="Path to deploy.yaml",
    )
    parser.add_argument(
        "--inventory",
        default="generated/inventory.env",
        help="Path to inventory.env",
    )
    parser.add_argument(
        "--output",
        default="generated/obd-cluster.yaml",
        help="Output OBD config path",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    cfg_path = (repo_root / args.config).resolve()
    inv_path = (repo_root / args.inventory).resolve()
    out_path = (repo_root / args.output).resolve()

    cfg = load_yaml(cfg_path)
    inv = load_inventory(inv_path)
    if not inv:
        print(f"Inventory not found or empty: {inv_path}", file=sys.stderr)
        sys.exit(1)

    obd_cfg = build_obd_config(cfg, inv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(obd_cfg, f, sort_keys=False, allow_unicode=True, default_flow_style=False)

    print(f"OBD config written: {out_path}")
    print(f"Observers: {inv.get('OBSERVER_COUNT')} | Deploy: {inv.get('DEPLOY_NAME')}")


if __name__ == "__main__":
    main()
