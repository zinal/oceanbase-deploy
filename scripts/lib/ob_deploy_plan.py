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


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[pos : pos + size] for pos in range(0, len(items), size)]


def build_scale_out_plan(
    full_cfg: dict[str, Any],
    registered_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return missing oceanbase/obagent nodes grouped by desired zone-balanced triples."""
    ob_key = oceanbase_component_key(full_cfg)
    desired_ob = full_cfg[ob_key]
    desired_servers = component_servers(desired_ob)
    if len(desired_servers) < SEED_OBSERVER_COUNT:
        raise ValueError("full OBD config contains fewer than three observers")

    registered_ob_ips = registered_component_ips(registered_cfg, ob_key)
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
        missing_ob_ips = {ip for ip in desired_ips if ip not in registered_ob_ips}
        missing_agent_ips = {
            ip
            for ip in desired_ips
            if ip in desired_agent_by_ip and ip not in registered_agent_ips
        }
        ob_block = selected_component(desired_ob, missing_ob_ips, keep_settings=False)
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


def cmd_scale_out(args: argparse.Namespace) -> None:
    full_cfg = load_yaml(args.input)
    registered_cfg = load_yaml(args.registered_config)
    plan = build_scale_out_plan(full_cfg, registered_cfg)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    for number, batch in enumerate(plan, start=1):
        label = f"{batch['first']}-{batch['last']}"
        ob_path: Path | None = None
        agent_path: Path | None = None
        if batch["oceanbase"] is not None:
            ob_path = args.output_dir / f"{number:02d}-{label}-oceanbase.yaml"
            dump_yaml(batch["oceanbase"], ob_path)
        if batch["obagent"] is not None:
            agent_path = args.output_dir / f"{number:02d}-{label}-obagent.yaml"
            dump_yaml(batch["obagent"], agent_path)
        lines.append(f"{label}|{ob_path or '-'}|{agent_path or '-'}")

    args.manifest.write_text(
        "".join(f"{line}\n" for line in lines),
        encoding="utf-8",
    )
    print(f"Scale-out plan written: {args.manifest} ({len(plan)} batches)")


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
    scale_out.set_defaults(func=cmd_scale_out)
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
