#!/usr/bin/env python3
"""Build a three-observer seed config and idempotent OBD scale-out batches."""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


SEED_OBSERVER_COUNT = 3
SCALE_OUT_BATCH_SIZE = 3
OCEANBASE_COMPONENT_KEYS = ("oceanbase-ce", "oceanbase")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML mapping")
    return data


def dump_yaml(data: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)


def oceanbase_component_key(cfg: dict[str, Any]) -> str:
    for key in OCEANBASE_COMPONENT_KEYS:
        if isinstance(cfg.get(key), dict):
            return key
    raise ValueError("OBD config has no oceanbase-ce/oceanbase component")


def server_ip(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and entry.get("ip"):
        return str(entry["ip"])
    raise ValueError(f"invalid OBD server entry: {entry!r}")


def server_name(entry: Any) -> str | None:
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"])
    return None


def component_servers(component: dict[str, Any]) -> list[Any]:
    servers = component.get("servers")
    if not isinstance(servers, list):
        raise ValueError("OBD component has no servers list")
    return servers


def selected_component(
    component: dict[str, Any],
    selected_ips: set[str],
    *,
    keep_settings: bool,
) -> dict[str, Any] | None:
    """Filter servers and their named overrides by IP."""
    servers = component_servers(component)
    selected = [copy.deepcopy(entry) for entry in servers if server_ip(entry) in selected_ips]
    if not selected:
        return None

    if keep_settings:
        result = copy.deepcopy(component)
        all_names = {name for entry in servers if (name := server_name(entry))}
        selected_names = {name for entry in selected if (name := server_name(entry))}
        for name in all_names - selected_names:
            result.pop(name, None)
    else:
        result = {}
        for entry in selected:
            name = server_name(entry)
            if name and isinstance(component.get(name), dict):
                result[name] = copy.deepcopy(component[name])
    result["servers"] = selected
    return result


def validate_seed_zones(component: dict[str, Any]) -> None:
    servers = component_servers(component)
    zones: list[str] = []
    for entry in servers:
        name = server_name(entry)
        override = component.get(name) if name else None
        zone = override.get("zone") if isinstance(override, dict) else None
        if not zone:
            raise ValueError(f"seed observer {name or server_ip(entry)} has no zone")
        zones.append(str(zone))
    if len(zones) != SEED_OBSERVER_COUNT or len(set(zones)) != SEED_OBSERVER_COUNT:
        raise ValueError(
            "seed must contain exactly three observers in three distinct zones; "
            f"got {', '.join(zones)}"
        )


def build_seed_config(full_cfg: dict[str, Any]) -> dict[str, Any]:
    """Keep all service components, but only the first observer in each zone."""
    result = copy.deepcopy(full_cfg)
    ob_key = oceanbase_component_key(result)
    ob_component = result[ob_key]
    servers = component_servers(ob_component)
    if len(servers) < SEED_OBSERVER_COUNT:
        raise ValueError(
            f"staged deployment requires at least {SEED_OBSERVER_COUNT} observers, "
            f"got {len(servers)}"
        )

    seed_ips = {server_ip(entry) for entry in servers[:SEED_OBSERVER_COUNT]}
    seed_ob = selected_component(ob_component, seed_ips, keep_settings=True)
    assert seed_ob is not None
    validate_seed_zones(seed_ob)
    result[ob_key] = seed_ob

    if isinstance(result.get("obagent"), dict):
        seed_agent = selected_component(result["obagent"], seed_ips, keep_settings=True)
        if seed_agent is None:
            result.pop("obagent")
        else:
            result["obagent"] = seed_agent
    return result


def registered_component_ips(cfg: dict[str, Any], component_key: str) -> set[str]:
    component = cfg.get(component_key)
    if not isinstance(component, dict):
        return set()
    return {server_ip(entry) for entry in component_servers(component)}


def server_config(component: dict[str, Any], entry: Any) -> dict[str, Any]:
    """Return global, inline, and named settings for one server."""
    config = copy.deepcopy(component.get("global") or {})
    if isinstance(entry, dict):
        config.update(
            {
                key: copy.deepcopy(value)
                for key, value in entry.items()
                if key not in {"name", "ip"}
            }
        )
    name = server_name(entry)
    if name and isinstance(component.get(name), dict):
        config.update(copy.deepcopy(component[name]))
    return config


def observer_scale_out_specs(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Observers in a scale-out YAML: ip, ports, zone (one dict per server)."""
    key = oceanbase_component_key(cfg)
    component = cfg[key]
    result: list[dict[str, Any]] = []
    for entry in component_servers(component):
        settings = server_config(component, entry)
        zone = str(settings.get("zone") or "")
        if not zone:
            raise ValueError(
                f"scale-out observer {server_name(entry) or server_ip(entry)} has no zone"
            )
        result.append(
            {
                "ip": server_ip(entry),
                "rpc_port": int(settings.get("rpc_port", 2882)),
                "mysql_port": int(settings.get("mysql_port", 2881)),
                "zone": zone,
                "rootservice_list": str(settings.get("rootservice_list") or ""),
            }
        )
    if not result:
        raise ValueError("observer scale-out YAML has no servers")
    return result


def observer_scale_out_spec(cfg: dict[str, Any]) -> dict[str, Any]:
    """Exactly one observer (join-empty / one-node YAML)."""
    specs = observer_scale_out_specs(cfg)
    if len(specs) != 1:
        raise ValueError(
            f"observer scale-out YAML must contain exactly one server, got {len(specs)}"
        )
    return specs[0]


def rootservice_list(
    desired_component: dict[str, Any],
    registered_ips: set[str],
) -> str:
    """Build a stable discovery list from the first registered server per zone."""
    candidates: list[str] = []
    seen_zones: set[str] = set()
    for entry in component_servers(desired_component):
        if server_ip(entry) not in registered_ips:
            continue
        config = server_config(desired_component, entry)
        zone = str(config.get("zone") or "")
        if zone and zone in seen_zones:
            continue
        if zone:
            seen_zones.add(zone)
        rpc_port = int(config.get("rpc_port", 2882))
        mysql_port = int(config.get("mysql_port", 2881))
        candidates.append(f"{server_ip(entry)}:{rpc_port}:{mysql_port}")
        if len(candidates) == SEED_OBSERVER_COUNT:
            break
    if not candidates:
        raise ValueError("cannot build rootservice_list: no registered observers")
    return ";".join(candidates)


def set_rootservice_list(component: dict[str, Any], value: str) -> None:
    """Set discovery explicitly on each new node for OBD scale-out startup."""
    for entry in component_servers(component):
        name = server_name(entry)
        if not name:
            raise ValueError(
                "scale-out observer entries must have names to set rootservice_list"
            )
        override = component.setdefault(name, {})
        if not isinstance(override, dict):
            raise ValueError(f"invalid settings for scale-out observer {name}")
        override["rootservice_list"] = value


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[pos : pos + size] for pos in range(0, len(items), size)]


def parse_joined_ips(raw: str | None) -> set[str] | None:
    """Parse ACTIVE SVR_IP list. None = unset (use OBD metadata); empty = invalid."""
    if raw is None:
        return None
    ips = {tok.strip() for tok in raw.replace(",", " ").split() if tok.strip()}
    return ips or None


def observer_ips(cfg: dict[str, Any]) -> list[str]:
    key = oceanbase_component_key(cfg)
    return [server_ip(entry) for entry in component_servers(cfg[key])]


def build_scale_out_plan(
    full_cfg: dict[str, Any],
    registered_cfg: dict[str, Any],
    joined_ob_ips: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return missing oceanbase/obagent nodes grouped by desired zone-balanced triples.

    Observer membership comes from DBA_OB_SERVERS (joined_ob_ips) when given.
    OBD ~/.obd/cluster config can list leftover nodes that never passed ADD SERVER.
    """
    ob_key = oceanbase_component_key(full_cfg)
    desired_ob = full_cfg[ob_key]
    desired_servers = component_servers(desired_ob)
    if len(desired_servers) < SEED_OBSERVER_COUNT:
        raise ValueError("full OBD config contains fewer than three observers")

    registered_ob_ips = registered_component_ips(registered_cfg, ob_key)
    member_ob_ips = joined_ob_ips if joined_ob_ips is not None else registered_ob_ips
    rs_list = rootservice_list(desired_ob, member_ob_ips)
    registered_agent_ips = registered_component_ips(registered_cfg, "obagent")
    desired_agent = full_cfg.get("obagent")
    desired_agent_by_ip: dict[str, Any] = {}
    if isinstance(desired_agent, dict):
        desired_agent_by_ip = {
            server_ip(entry): entry for entry in component_servers(desired_agent)
        }

    plan: list[dict[str, Any]] = []
    for batch in chunks(desired_servers[SEED_OBSERVER_COUNT:], SCALE_OUT_BATCH_SIZE):
        desired_ips = [server_ip(entry) for entry in batch]
        missing_ob_ips = {ip for ip in desired_ips if ip not in member_ob_ips}
        missing_agent_ips = {
            ip
            for ip in desired_ips
            if ip in desired_agent_by_ip and ip not in registered_agent_ips
        }
        ob_block = selected_component(desired_ob, missing_ob_ips, keep_settings=False)
        if ob_block is not None:
            # OBD 3.5.3's OceanBase 4.6 start_pre only injects cfg_url while
            # bootstrapping. During scale-out that leaves a fresh observer with
            # no RootService discovery source, so ADD SERVER times out.
            set_rootservice_list(ob_block, rs_list)
        agent_block = None
        if isinstance(desired_agent, dict):
            agent_block = selected_component(
                desired_agent,
                missing_agent_ips,
                keep_settings=False,
            )
        if ob_block is not None or agent_block is not None:
            plan.append(
                {
                    "first": server_name(batch[0]) or desired_ips[0],
                    "last": server_name(batch[-1]) or desired_ips[-1],
                    "servers": copy.deepcopy(batch),
                    "oceanbase": {ob_key: ob_block} if ob_block is not None else None,
                    "obagent": {"obagent": agent_block} if agent_block is not None else None,
                }
            )
    return plan


def cmd_seed(args: argparse.Namespace) -> None:
    full_cfg = load_yaml(args.input)
    seed_cfg = build_seed_config(full_cfg)
    dump_yaml(seed_cfg, args.output)
    ob_key = oceanbase_component_key(seed_cfg)
    print(f"Seed config written: {args.output} ({len(component_servers(seed_cfg[ob_key]))} observers)")


def cmd_ips(args: argparse.Namespace) -> None:
    for ip in observer_ips(load_yaml(args.input)):
        print(ip)


def cmd_scale_out(args: argparse.Namespace) -> None:
    full_cfg = load_yaml(args.input)
    registered_cfg = load_yaml(args.registered_config)
    plan = build_scale_out_plan(
        full_cfg,
        registered_cfg,
        joined_ob_ips=parse_joined_ips(args.joined_ips),
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for round_number, batch in enumerate(plan, start=1):
        round_label = f"{batch['first']}-{batch['last']}"
        batch_ob = batch["oceanbase"]
        batch_agent = batch["obagent"]
        ob_path: Path | None = None
        agent_path: Path | None = None
        if batch_ob is not None:
            ob_path = args.output_dir / f"{round_number:02d}-{round_label}-oceanbase.yaml"
            dump_yaml(batch_ob, ob_path)
        if batch_agent is not None:
            agent_path = args.output_dir / f"{round_number:02d}-{round_label}-obagent.yaml"
            dump_yaml(batch_agent, agent_path)
        if ob_path is not None or agent_path is not None:
            lines.append(f"{round_label}|{ob_path or '-'}|{agent_path or '-'}")

    args.manifest.write_text(
        "".join(f"{line}\n" for line in lines),
        encoding="utf-8",
    )
    print(
        f"Scale-out plan written: {args.manifest} "
        f"({len(plan)} rounds, up to {SCALE_OUT_BATCH_SIZE} observers per OBD scale_out)"
    )


def cmd_spec(args: argparse.Namespace) -> None:
    for spec in observer_scale_out_specs(load_yaml(args.input)):
        print(
            f"{spec['ip']} {spec['rpc_port']} {spec['mysql_port']} {spec['zone']}"
        )


def build_one_node_config(full_cfg: dict[str, Any], ip: str) -> dict[str, Any]:
    """Single-node scale-out YAML for one observer IP, with seed rootservice_list."""
    ob_key = oceanbase_component_key(full_cfg)
    desired_ob = full_cfg[ob_key]
    ob_block = selected_component(desired_ob, {ip}, keep_settings=False)
    if ob_block is None:
        raise ValueError(f"no observer with ip {ip} in OBD config")
    seed_ips = {
        server_ip(entry) for entry in component_servers(desired_ob)[:SEED_OBSERVER_COUNT]
    }
    set_rootservice_list(ob_block, rootservice_list(desired_ob, seed_ips))
    return {ob_key: ob_block}


def cmd_one_node(args: argparse.Namespace) -> None:
    cfg = build_one_node_config(load_yaml(args.input), args.ip)
    dump_yaml(cfg, args.output)
    spec = observer_scale_out_spec(cfg)
    print(f"{spec['ip']} {spec['rpc_port']} {spec['mysql_port']} {spec['zone']} -> {args.output}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="write initial three-observer OBD config")
    seed.add_argument("--input", type=Path, required=True)
    seed.add_argument("--output", type=Path, required=True)
    seed.set_defaults(func=cmd_seed)

    scale_out = sub.add_parser(
        "scale-out",
        help="write missing observer/obagent batches relative to OBD registered config",
    )
    scale_out.add_argument("--input", type=Path, required=True)
    scale_out.add_argument("--registered-config", type=Path, required=True)
    scale_out.add_argument("--output-dir", type=Path, required=True)
    scale_out.add_argument("--manifest", type=Path, required=True)
    scale_out.add_argument(
        "--joined-ips",
        default=None,
        help="ACTIVE SVR_IP from DBA_OB_SERVERS (whitespace/comma-separated). "
        "Leftover IPs in OBD metadata that never joined are scaled out again.",
    )
    scale_out.set_defaults(func=cmd_scale_out)

    ips_cmd = sub.add_parser("ips", help="print observer IPs from an OBD YAML")
    ips_cmd.add_argument("--input", type=Path, required=True)
    ips_cmd.set_defaults(func=cmd_ips)

    spec = sub.add_parser(
        "spec",
        help="print ip rpc_port mysql_port zone (one line per observer in the YAML)",
    )
    spec.add_argument("--input", type=Path, required=True)
    spec.set_defaults(func=cmd_spec)

    one_node = sub.add_parser(
        "one-node",
        help="write a one-observer scale-out YAML (ERROR 4179 join) from the full config",
    )
    one_node.add_argument("--input", type=Path, required=True)
    one_node.add_argument("--ip", required=True)
    one_node.add_argument("--output", type=Path, required=True)
    one_node.set_defaults(func=cmd_one_node)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
