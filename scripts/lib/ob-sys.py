#!/usr/bin/env python3
"""SQL и метаданные OBD для восстановления observer/obproxy.

Операции соответствуют публичной документации OceanBase:
- STOP/ADD/DELETE SERVER, MIGRATE UNIT (sys-тенант)
- obd cluster scale_out — только новый узел
- правка ~/.obd/cluster/<name>/{config,inner_config}.yaml после DELETE SERVER
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_inventory(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.exists():
        return data
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    return data


def write_inventory(path: Path, inv: dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in inv.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def update_inventory_ip(inv: dict[str, str], prefix: str, idx: int, ip: str, name: str | None = None) -> dict[str, str]:
    updated = dict(inv)
    updated[f"{prefix}_{idx}_IP"] = ip
    if name:
        updated[f"{prefix}_{idx}_NAME"] = name
    return updated


def cfg_int(cfg: dict[str, Any], dotted: str, default: int) -> int:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    try:
        return int(node)
    except (TypeError, ValueError):
        return default


def cfg_str(cfg: dict[str, Any], dotted: str, default: str = "") -> str:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    if node is None:
        return default
    return str(node)


def inventory_ips(inv: dict[str, str], prefix: str) -> list[tuple[int, str, str]]:
    count = int(inv.get(f"{prefix}_COUNT", "0") or 0)
    rows: list[tuple[int, str, str]] = []
    for idx in range(1, count + 1):
        ip = inv.get(f"{prefix}_{idx}_IP", "")
        name = inv.get(f"{prefix}_{idx}_NAME", "")
        if ip:
            rows.append((idx, ip, name))
    return rows


def discover_root_password(cfg: dict[str, Any], deploy_name: str) -> str:
    if "OB_ROOT_PASSWORD" in os.environ:
        return os.environ["OB_ROOT_PASSWORD"]
    ocp = cfg.get("ocp") or {}
    if ocp.get("root_password"):
        return str(ocp["root_password"])
    obd_dir = Path.home() / ".obd" / "cluster" / deploy_name
    for name in ("inner_config.yaml", "config.yaml", "inner_config.yml", "config.yml"):
        path = obd_dir / name
        if not path.exists():
            continue
        data = load_yaml(path)
        pwd = _find_key(data, "root_password")
        if pwd is not None:
            return str(pwd)
    return ""


def _find_key(node: Any, key: str) -> Any | None:
    if isinstance(node, dict):
        if key in node and node[key] not in (None, ""):
            return node[key]
        for value in node.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def pick_sql_endpoint(
    cfg: dict[str, Any],
    inv: dict[str, str],
    skip_observer: int | None = None,
    skip_obproxy: int | None = None,
) -> dict[str, Any]:
    mysql_port = cfg_int(cfg, "oceanbase.ports.mysql", 2881)
    proxy_port = cfg_int(cfg, "oceanbase.ports.obproxy", 2883)
    cluster = cfg_str(cfg, "oceanbase.cluster_name", "obcluster")

    for idx, ip, name in inventory_ips(inv, "OBSERVER"):
        if skip_observer is not None and idx == skip_observer:
            continue
        return {
            "ip": ip,
            "port": mysql_port,
            "user": "root@sys",
            "via": "observer",
            "name": name,
            "idx": idx,
        }

    for idx, ip, name in inventory_ips(inv, "OBPROXY"):
        if skip_obproxy is not None and idx == skip_obproxy:
            continue
        return {
            "ip": ip,
            "port": proxy_port,
            "user": f"root@sys#{cluster}",
            "via": "obproxy",
            "name": name,
            "idx": idx,
        }

    raise RuntimeError("Нет живого SQL-endpoint в inventory (все observer/obproxy пропущены)")


def _client_bin() -> list[str]:
    for candidate in ("obclient", "mysql"):
        if shutil.which(candidate):
            return [candidate]
    raise RuntimeError("Нужен obclient или mysql в PATH (официальный OBD scale_out тоже требует OBClient)")


def run_sql(
    endpoint: dict[str, Any],
    password: str,
    sql: str,
    *,
    ignore_error: bool = False,
) -> subprocess.CompletedProcess[str]:
    cmd = _client_bin() + [
        f"-h{endpoint['ip']}",
        f"-P{endpoint['port']}",
        f"-u{endpoint['user']}",
        "--connect-timeout=8",
        "-N",
        "-B",
        "-e",
        sql,
    ]
    env = os.environ.copy()
    env["MYSQL_PWD"] = password
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    if proc.returncode != 0 and not ignore_error:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"SQL ошибка ({endpoint['ip']}:{endpoint['port']}): {err}")
    return proc


def majority_ok(total: int, remaining_active: int) -> bool:
    """Большинство исходного состава Paxos-группы ещё живо."""
    if total < 1:
        return False
    return remaining_active > total / 2


def replacement_server_name(idx: int) -> str:
    return f"server{idx}r"


def build_observer_scale_out(
    cfg: dict[str, Any],
    idx: int,
    new_ip: str,
    include_obagent: bool,
) -> dict[str, Any]:
    """Только новый observer (+obagent). Официальный OBD: не трогать global/depends исходного кластера."""
    ports = (cfg.get("oceanbase") or {}).get("ports") or {}
    deploy_user = cfg_str(cfg, "oceanbase.deploy_user") or cfg_str(cfg, "yandex_cloud.ssh_user", "obadmin")
    home_path = cfg_str(cfg, "oceanbase.home_path", f"/home/{deploy_user}/observer")
    sname = replacement_server_name(idx)
    block: dict[str, Any] = {
        "oceanbase-ce": {
            "servers": [{"name": sname, "ip": new_ip}],
            sname: {
                "mysql_port": int(ports.get("mysql", 2881)),
                "rpc_port": int(ports.get("rpc", 2882)),
                "obshell_port": int(ports.get("obshell", 2886)),
                "home_path": home_path,
                "data_dir": cfg_str(cfg, "oceanbase.data_dir", "/data/1"),
                "redo_dir": cfg_str(cfg, "oceanbase.redo_dir", "/data/log1"),
                "zone": f"zone{idx}",
            },
        }
    }
    if include_obagent:
        block["obagent"] = {
            "servers": [{"name": sname, "ip": new_ip}],
        }
    return block


def build_obproxy_scale_out(cfg: dict[str, Any], new_ip: str) -> dict[str, Any]:
    return {"obproxy-ce": {"servers": [new_ip]}}


def dump_yaml(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _server_ip(entry: Any) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and entry.get("ip"):
        return str(entry["ip"])
    return None


def remove_ip_from_obd_component(component: dict[str, Any], old_ip: str) -> bool:
    """Удалить сервер с old_ip из компонента OBD. Возвращает True, если что-то убрали."""
    changed = False
    servers = component.get("servers")
    if isinstance(servers, list):
        kept: list[Any] = []
        removed_names: list[str] = []
        for entry in servers:
            ip = _server_ip(entry)
            if ip == old_ip:
                changed = True
                if isinstance(entry, dict) and entry.get("name"):
                    removed_names.append(str(entry["name"]))
                continue
            kept.append(entry)
        if changed:
            component["servers"] = kept
        for name in removed_names:
            if name in component:
                del component[name]
                changed = True
    for key in list(component):
        if key in {"servers", "global", "depends", "version"}:
            continue
        node = component[key]
        if isinstance(node, dict) and str(node.get("ip", "")) == old_ip:
            del component[key]
            changed = True
    return changed


def clean_obd_metadata(obd_dir: Path, old_ip: str, *, backup: bool = True) -> list[str]:
    """Официальный KB: после DELETE SERVER вычистить IP из config.yaml и inner_config.yaml."""
    changed_files: list[str] = []
    if not obd_dir.is_dir():
        return changed_files
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for name in ("config.yaml", "inner_config.yaml", "config.yml", "inner_config.yml", "conf.yaml"):
        path = obd_dir / name
        if not path.exists():
            continue
        data = load_yaml(path)
        file_changed = False
        for key, value in list(data.items()):
            if isinstance(value, dict) and remove_ip_from_obd_component(value, old_ip):
                file_changed = True
        if file_changed:
            if backup:
                shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
            dump_yaml(data, path)
            changed_files.append(str(path))
    return changed_files


def parse_units(stdout: str) -> list[str]:
    units: list[str] = []
    for line in stdout.splitlines():
        unit_id = line.strip().split()[0] if line.strip() else ""
        if unit_id.isdigit():
            units.append(unit_id)
    return units


def cmd_sql(args: argparse.Namespace) -> None:
    cfg = load_yaml(Path(args.config))
    inv = load_inventory(Path(args.inventory))
    if args.host:
        endpoint = {
            "ip": args.host,
            "port": int(args.port or cfg_int(cfg, "oceanbase.ports.mysql", 2881)),
            "user": args.user or "root@sys",
        }
    else:
        endpoint = pick_sql_endpoint(cfg, inv, args.skip_observer, args.skip_obproxy)
    password = discover_root_password(cfg, inv.get("DEPLOY_NAME", ""))
    proc = run_sql(endpoint, password, args.sql, ignore_error=args.ignore_error)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        sys.exit(proc.returncode)


def cmd_pick_sql(args: argparse.Namespace) -> None:
    cfg = load_yaml(Path(args.config))
    inv = load_inventory(Path(args.inventory))
    endpoint = pick_sql_endpoint(cfg, inv, args.skip_observer, args.skip_obproxy)
    print(f"{endpoint['ip']}\t{endpoint['port']}\t{endpoint['user']}\t{endpoint['via']}")


def cmd_write_scale_out(args: argparse.Namespace) -> None:
    cfg = load_yaml(Path(args.config))
    if args.role == "observer":
        data = build_observer_scale_out(cfg, args.index, args.ip, include_obagent=args.obagent)
    else:
        data = build_obproxy_scale_out(cfg, args.ip)
    out = Path(args.output)
    dump_yaml(data, out)
    print(out)


def cmd_update_inventory(args: argparse.Namespace) -> None:
    path = Path(args.inventory)
    inv = update_inventory_ip(load_inventory(path), args.prefix, args.index, args.ip, args.name)
    write_inventory(path, inv)
    print(f"{args.prefix}_{args.index}_IP={args.ip}")


def cmd_clean_obd(args: argparse.Namespace) -> None:
    deploy = args.deploy_name
    obd_dir = Path(args.obd_dir) if args.obd_dir else Path.home() / ".obd" / "cluster" / deploy
    changed = clean_obd_metadata(obd_dir, args.ip, backup=not args.no_backup)
    if not changed:
        print(f"OBD metadata: IP {args.ip} не найден в {obd_dir}")
        return
    print("Обновлены:")
    for item in changed:
        print(f"  {item}")


def cmd_self_test(_args: argparse.Namespace) -> None:
    cfg = {
        "yandex_cloud": {"ssh_user": "obadmin"},
        "oceanbase": {
            "deploy_user": "obadmin",
            "home_path": "/home/obadmin/observer",
            "data_dir": "/ob-data/1",
            "redo_dir": "/ob-log/1",
            "cluster_name": "obcluster",
            "ports": {"mysql": 2881, "rpc": 2882, "obshell": 2886, "obproxy": 2883},
        },
    }
    inv = {
        "OBSERVER_COUNT": "3",
        "OBSERVER_1_IP": "10.0.0.1",
        "OBSERVER_2_IP": "10.0.0.2",
        "OBSERVER_3_IP": "10.0.0.3",
        "OBPROXY_COUNT": "2",
        "OBPROXY_1_IP": "10.0.1.1",
        "OBPROXY_2_IP": "10.0.1.2",
        "DEPLOY_NAME": "ob-yc-prod",
    }
    assert majority_ok(3, 2)
    assert not majority_ok(3, 1)
    endpoint = pick_sql_endpoint(cfg, inv, skip_observer=2)
    assert endpoint["ip"] == "10.0.0.1"
    so = build_observer_scale_out(cfg, 2, "10.9.9.9", include_obagent=True)
    assert so["oceanbase-ce"]["servers"][0]["ip"] == "10.9.9.9"
    assert so["oceanbase-ce"]["server2r"]["zone"] == "zone2"
    assert "global" not in so["oceanbase-ce"]
    assert so["obagent"]["servers"][0]["name"] == "server2r"
    po = build_obproxy_scale_out(cfg, "10.8.8.8")
    assert po["obproxy-ce"]["servers"] == ["10.8.8.8"]

    sample = {
        "oceanbase-ce": {
            "servers": [
                {"name": "server1", "ip": "10.0.0.1"},
                {"name": "server2", "ip": "10.0.0.2"},
            ],
            "server1": {"zone": "zone1"},
            "server2": {"zone": "zone2"},
            "global": {"cluster_id": 1},
        },
        "obproxy-ce": {"servers": ["10.0.1.1", "10.0.1.2"]},
    }
    assert remove_ip_from_obd_component(sample["oceanbase-ce"], "10.0.0.2")
    assert sample["oceanbase-ce"]["servers"] == [{"name": "server1", "ip": "10.0.0.1"}]
    assert "server2" not in sample["oceanbase-ce"]
    assert remove_ip_from_obd_component(sample["obproxy-ce"], "10.0.1.1")
    assert sample["obproxy-ce"]["servers"] == ["10.0.1.2"]
    assert update_inventory_ip(inv, "OBSERVER", 2, "10.9.9.9")["OBSERVER_2_IP"] == "10.9.9.9"
    print("self-test ok")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "deploy.yaml"))
    parser.add_argument("--inventory", default=str(REPO_ROOT / "generated" / "inventory.env"))
    sub = parser.add_subparsers(dest="command", required=True)

    p_sql = sub.add_parser("sql", help="SQL в sys-тенант через живой observer/obproxy")
    p_sql.add_argument("sql")
    p_sql.add_argument("--skip-observer", type=int, default=None)
    p_sql.add_argument("--skip-obproxy", type=int, default=None)
    p_sql.add_argument("--host", default=None, help="Явный observer/obproxy IP вместо auto-pick")
    p_sql.add_argument("--port", type=int, default=None)
    p_sql.add_argument("--user", default=None)
    p_sql.add_argument("--ignore-error", action="store_true")
    p_sql.set_defaults(func=cmd_sql)

    p_pick = sub.add_parser("pick-sql", help="Выбрать живой SQL endpoint")
    p_pick.add_argument("--skip-observer", type=int, default=None)
    p_pick.add_argument("--skip-obproxy", type=int, default=None)
    p_pick.set_defaults(func=cmd_pick_sql)

    p_so = sub.add_parser("write-scale-out", help="YAML только для нового узла (OBD scale_out)")
    p_so.add_argument("--role", choices=("observer", "obproxy"), required=True)
    p_so.add_argument("--index", type=int, default=1)
    p_so.add_argument("--ip", required=True)
    p_so.add_argument("--output", required=True)
    p_so.add_argument("--obagent", action="store_true")
    p_so.set_defaults(func=cmd_write_scale_out)

    p_inv = sub.add_parser("update-inventory", help="Записать новый IP в inventory.env")
    p_inv.add_argument("--prefix", required=True)
    p_inv.add_argument("--index", type=int, required=True)
    p_inv.add_argument("--ip", required=True)
    p_inv.add_argument("--name", default=None)
    p_inv.set_defaults(func=cmd_update_inventory)

    p_obd = sub.add_parser("clean-obd", help="Убрать старый IP из метаданных OBD")
    p_obd.add_argument("--ip", required=True)
    p_obd.add_argument("--deploy-name", required=True)
    p_obd.add_argument("--obd-dir", default=None)
    p_obd.add_argument("--no-backup", action="store_true")
    p_obd.set_defaults(func=cmd_clean_obd)

    p_t = sub.add_parser("self-test", help="Локальные проверки без кластера")
    p_t.set_defaults(func=cmd_self_test)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
