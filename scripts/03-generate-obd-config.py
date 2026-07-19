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


def auto_tune(cfg: dict, observer_count: int) -> dict:
    """Auto-tune OceanBase от профиля observer (делегирование vm_profiles)."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "vm_profiles",
        Path(__file__).resolve().parent / "lib" / "vm_profiles.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.observer_auto_tune(cfg)


def monitoring_cfg(cfg: dict) -> dict:
    return cfg.get("monitoring", {}) or {}


def collect_cluster_ips(inv: dict[str, str]) -> list[str]:
    ips: list[str] = []
    obs_count = int(inv.get("OBSERVER_COUNT", 0))
    obproxy_count = int(inv.get("OBPROXY_COUNT", 0))
    configserver_count = int(inv.get("CONFIGSERVER_COUNT", 0))
    monitor_count = int(inv.get("MONITOR_COUNT", 0))

    for i in range(1, obs_count + 1):
        ips.append(inv[f"OBSERVER_{i}_IP"])
    for i in range(1, obproxy_count + 1):
        ips.append(inv[f"OBPROXY_{i}_IP"])
    for i in range(1, configserver_count + 1):
        key = f"CONFIGSERVER_{i}_IP"
        if inv.get(key):
            ips.append(inv[key])
    for i in range(1, monitor_count + 1):
        ips.append(inv[f"MONITOR_{i}_IP"])
    return ips


def build_prometheus_scrape_configs(
    cfg: dict,
    inv: dict[str, str],
    observer_ips: list[str],
) -> list[dict]:
    mon = monitoring_cfg(cfg)
    node_cfg = mon.get("node_exporter", {})
    obagent_cfg = mon.get("obagent", {})
    components = cfg.get("oceanbase", {}).get("components", {})

    node_port = int(node_cfg.get("port", 9100))
    obagent_port = int(obagent_cfg.get("http_port", 8088))
    auth_user = obagent_cfg.get("basic_auth_user", "admin")
    auth_pass = obagent_cfg.get("basic_auth_password", "oceanbase")

    scrape_configs: list[dict] = [
        {
            "job_name": "prometheus",
            "metrics_path": "/metrics",
            "static_configs": [{"targets": ["localhost:9090"]}],
        },
    ]

    if node_cfg.get("enabled", True):
        node_targets = [f"{ip}:{node_port}" for ip in collect_cluster_ips(inv)]
        scrape_configs.append(
            {
                "job_name": "node_exporter",
                "metrics_path": "/metrics",
                "static_configs": [{"targets": node_targets}],
            }
        )

    if components.get("obagent", True):
        obagent_targets = [f"{ip}:{obagent_port}" for ip in observer_ips]
        for job_name, path in (
            ("node", "/metrics/node/host"),
            ("ob_basic", "/metrics/ob/basic"),
            ("ob_extra", "/metrics/ob/extra"),
            ("agent", "/metrics/stat"),
        ):
            scrape_configs.append(
                {
                    "job_name": job_name,
                    "metrics_path": path,
                    "basic_auth": {"username": auth_user, "password": auth_pass},
                    "static_configs": [{"targets": obagent_targets}],
                }
            )

    return scrape_configs


def build_obd_config(cfg: dict, inv: dict[str, str]) -> dict:
    profiles = cfg.get("vm_profiles", {})
    obs_count = int(inv.get("OBSERVER_COUNT", profiles.get("observer", {}).get("count", 3)))
    obproxy_count = int(inv.get("OBPROXY_COUNT", profiles.get("obproxy", {}).get("count", 0)))
    configserver_dedicated = inv.get("CONFIGSERVER_DEDICATED", "false").lower() == "true"

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
    mon = monitoring_cfg(cfg)
    monitor_vm_enabled = profiles.get("monitoring", {}).get("enabled", False)
    prometheus_enabled = components.get("prometheus", False) or monitor_vm_enabled
    grafana_enabled = components.get("grafana", False) or monitor_vm_enabled
    obagent_cfg = mon.get("obagent", {})
    prom_cfg = mon.get("prometheus", {})
    result: dict = {"user": user_block}

    if components.get("ob_configserver", True):
        if configserver_dedicated and inv.get("CONFIGSERVER_1_IP"):
            cs_servers = [inv["CONFIGSERVER_1_IP"]]
        else:
            cs_servers = [observer_ips[0]]
        result["ob-configserver"] = {
            "servers": cs_servers,
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
        obagent_global = {
            "home_path": f"/home/{deploy_user}/obagent",
            "monagent_http_port": int(obagent_cfg.get("http_port", 8088)),
            "http_basic_auth_user": obagent_cfg.get("basic_auth_user", "admin"),
            "http_basic_auth_password": obagent_cfg.get("basic_auth_password", "oceanbase"),
            "ob_monitor_status": "active",
        }
        result["obagent"] = {
            "depends": ["oceanbase-ce"],
            "servers": [{"name": s["name"], "ip": s["ip"]} for s in servers],
            "global": obagent_global,
        }

    mon_count = int(inv.get("MONITOR_COUNT", "0"))
    if prometheus_enabled:
        prom_ip = observer_ips[0]
        if monitor_vm_enabled and mon_count > 0:
            prom_ip = inv["MONITOR_1_IP"]
        prom_port = int(prom_cfg.get("port", 9090))
        result["prometheus"] = {
            "depends": ["obagent"] if components.get("obagent", True) else [],
            "servers": [prom_ip],
            "global": {
                "home_path": f"/home/{deploy_user}/prometheus",
                "port": prom_port,
                "config": {
                    "global": {
                        "scrape_interval": "15s",
                        "evaluation_interval": "15s",
                    },
                    "scrape_configs": build_prometheus_scrape_configs(cfg, inv, observer_ips),
                },
            },
        }

    if grafana_enabled:
        graf_ip = inv.get("MONITOR_1_IP", observer_ips[0])
        if monitor_vm_enabled and mon_count > 0:
            graf_ip = inv["MONITOR_1_IP"]
        result["grafana"] = {
            "depends": ["prometheus"],
            "servers": [graf_ip],
            "global": {
                "home_path": f"/home/{deploy_user}/grafana",
                "login_password": mon.get("grafana_password", "oceanbase"),
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
