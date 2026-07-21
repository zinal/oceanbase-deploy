#!/usr/bin/env python3
"""Профили ВМ по ролям OceanBase и валидация ресурсов."""

from __future__ import annotations

import argparse
import json
import math
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# Диски YC с шагом размера 93 GB
DISK_SIZE_STEP_GB = 93
DISK_TYPES_STEP_93GB = frozenset({"network-ssd-nonreplicated", "network-ssd-io-m3"})

# Рекомендации OceanBase / oceanbase-skills (cluster-management, prepare-servers)
OCEANBASE_MIN = {
    "observer": {"cores": 4, "memory_gb": 16, "data_disk_gb": 100},
    "obproxy": {"cores": 2, "memory_gb": 4},
    "configserver": {"cores": 2, "memory_gb": 4},
    "monitoring": {"cores": 4, "memory_gb": 8},
    "ocp": {"cores": 4, "memory_gb": 16},
}

ROLE_ALIASES = {
    "observers": "observer",
    "obproxy": "obproxy",
    "monitoring": "monitoring",
    "monitor": "monitoring",
    "configserver": "configserver",
    "observer": "observer",
    "ocp": "ocp",
}


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def round_disk_size_gb(size_gb: int, disk_type: str) -> int:
    """Округление размера диска под ограничения Yandex Cloud."""
    size_gb = max(1, int(size_gb))
    if disk_type in DISK_TYPES_STEP_93GB:
        return max(DISK_SIZE_STEP_GB, math.ceil(size_gb / DISK_SIZE_STEP_GB) * DISK_SIZE_STEP_GB)
    return size_gb


def _merge_disk(base: dict | None, override: dict | None) -> dict:
    result = deepcopy(base or {})
    if override:
        for k, v in override.items():
            if v is not None:
                result[k] = v
    return result


def build_image_spec(profile: dict[str, Any], yc: dict[str, Any]) -> str:
    """Сформировать спецификацию образа (yandex_cloud + опциональный override в vm_profiles)."""
    folder = profile.get("image_folder_id") or yc.get("image_folder_id") or "standard-images"
    image_name = profile.get("image_name") or yc.get("image_name")
    if image_name:
        return f"image-folder-id={folder},image-name={image_name}"
    image_family = profile.get("image_family") or yc.get("image_family") or "ubuntu-2204-lts"
    return f"image-folder-id={folder},image-family={image_family}"


def resolve_profile(cfg: dict[str, Any], role: str) -> dict[str, Any]:
    """Собрать итоговый профиль ВМ для роли."""
    role = ROLE_ALIASES.get(role, role)
    defaults = cfg.get("vm_defaults", {})
    profiles = cfg.get("vm_profiles", {})
    yc = cfg.get("yandex_cloud", {})

    if role not in profiles:
        raise KeyError(f"Unknown vm profile role: {role}")

    profile = deepcopy(profiles[role])
    core_fraction = profile.pop("core_fraction", defaults.get("core_fraction", 100))

    boot = _merge_disk(defaults.get("boot_disk"), profile.get("boot_disk"))
    data = _merge_disk(defaults.get("data_disk"), profile.get("data_disk"))
    log = _merge_disk(defaults.get("log_disk"), profile.get("log_disk"))

    for disk in (boot, data, log):
        if disk.get("type") and disk.get("size_gb"):
            disk["size_gb"] = round_disk_size_gb(int(disk["size_gb"]), disk["type"])

    image_spec = build_image_spec(profile, yc)

    return {
        "role": role,
        "platform": profile.get("platform", defaults.get("platform", "standard-v3")),
        "cores": int(profile.get("cores", defaults.get("cores", 4))),
        "memory_gb": int(profile.get("memory_gb", defaults.get("memory_gb", 16))),
        "image_spec": image_spec,
        "core_fraction": int(core_fraction),
        "boot_disk": boot,
        "data_disk": data,
        "log_disk": log,
        "count": int(profile.get("count", 1)),
        "enabled": profile.get("enabled", True),
        "dedicated": profile.get("dedicated", True),
    }


def observer_auto_tune(cfg: dict[str, Any]) -> dict[str, Any]:
    """Auto-tune OceanBase от профиля observer."""
    obs = resolve_profile(cfg, "observer")
    cores = obs["cores"]
    memory_gb = obs["memory_gb"]
    data_gb = int(obs["data_disk"].get("size_gb", 100))
    log_gb = int(obs["log_disk"].get("size_gb", 0)) if obs["log_disk"].get("enabled") else 0

    memory_limit_gb = max(4, memory_gb - max(4, memory_gb // 8))
    system_memory_gb = min(4, max(2, memory_gb // 8))
    datafile_gb = max(20, int(data_gb * 0.85))
    if log_gb:
        log_disk_gb = max(15, int(log_gb * 0.9))
    else:
        log_disk_gb = max(15, memory_limit_gb * 3)

    def gb(n: int) -> str:
        return f"{n}G"

    return {
        "memory_limit": gb(memory_limit_gb),
        "system_memory": gb(system_memory_gb),
        "datafile_size": gb(datafile_gb),
        "log_disk_size": gb(log_disk_gb),
        "cpu_count": cores,
    }


def validate_profiles(cfg: dict[str, Any]) -> list[str]:
    """Проверка соответствия профилей рекомендациям OceanBase."""
    issues: list[str] = []
    profiles = cfg.get("vm_profiles", {})
    obs_count = int(profiles.get("observer", {}).get("count", 0))

    if obs_count < 3:
        issues.append(
            f"WARN: observer.count={obs_count} < 3 — для production HA нужно минимум 3 узла"
        )

    for role, minimums in OCEANBASE_MIN.items():
        if role not in profiles:
            continue
        p = profiles[role]
        if role == "configserver" and not p.get("dedicated", False):
            continue
        if role == "monitoring" and not p.get("enabled", False):
            continue
        if role == "ocp" and not p.get("enabled", False):
            continue
        try:
            resolved = resolve_profile(cfg, role)
        except KeyError:
            continue
        if resolved["cores"] < minimums["cores"]:
            issues.append(
                f"ERROR: {role}: cores={resolved['cores']} < рекомендуемый минимум {minimums['cores']}"
            )
        if resolved["memory_gb"] < minimums["memory_gb"]:
            issues.append(
                f"ERROR: {role}: memory_gb={resolved['memory_gb']} < минимум {minimums['memory_gb']}"
            )
        if role == "observer" and resolved["data_disk"].get("enabled"):
            data_size = int(resolved["data_disk"].get("size_gb", 0))
            if data_size < minimums["data_disk_gb"]:
                issues.append(
                    f"ERROR: observer data_disk.size_gb={data_size} < минимум {minimums['data_disk_gb']}"
                )
            dtype = resolved["data_disk"].get("type", "")
            if dtype != "network-ssd-nonreplicated":
                issues.append(
                    f"WARN: observer data_disk.type={dtype} — для реплицируемых данных "
                    "рекомендуется network-ssd-nonreplicated"
                )
            if resolved["log_disk"].get("enabled"):
                ltype = resolved["log_disk"].get("type", "")
                if ltype != "network-ssd-io-m3":
                    issues.append(
                        f"WARN: observer log_disk.type={ltype} — для clog рекомендуется network-ssd-io-m3"
                    )

    return issues


def cmd_resolve(args: argparse.Namespace) -> None:
    cfg = load_config(Path(args.config))
    profile = resolve_profile(cfg, args.role)
    if args.format == "json":
        print(json.dumps(profile, indent=2))
    else:
        boot = profile["boot_disk"]
        data = profile["data_disk"]
        log = profile["log_disk"]
        lines = [
            profile["platform"],
            profile["cores"],
            profile["memory_gb"],
            profile["image_spec"],
            profile["core_fraction"],
            boot.get("type", "network-ssd"),
            boot.get("size_gb", 50),
            str(data.get("enabled", False)).lower(),
            data.get("type", "network-ssd"),
            data.get("size_gb", 0),
            data.get("mount_point", "/data"),
            str(log.get("enabled", False)).lower(),
            log.get("type", "network-ssd-io-m3"),
            log.get("size_gb", 0),
            log.get("mount_point", "/data/log1"),
        ]
        print("\n".join(str(x) for x in lines))


def cmd_image_spec(args: argparse.Namespace) -> None:
    cfg = load_config(Path(args.config))
    obs = resolve_profile(cfg, "observer")
    print(obs["image_spec"])


def cmd_validate(args: argparse.Namespace) -> None:
    cfg = load_config(Path(args.config))
    issues = validate_profiles(cfg)
    for item in issues:
        print(item, file=sys.stderr if item.startswith("ERROR") else sys.stdout)
    if any(i.startswith("ERROR") for i in issues):
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/deploy.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_resolve = sub.add_parser("resolve", help="Resolve VM profile for role")
    p_resolve.add_argument("role")
    p_resolve.add_argument("--format", choices=("lines", "json"), default="lines")
    p_resolve.add_argument("--config", default="config/deploy.yaml")
    p_resolve.set_defaults(func=cmd_resolve)

    p_image = sub.add_parser("image-spec", help="Print boot image spec for observer")
    p_image.add_argument("--config", default="config/deploy.yaml")
    p_image.set_defaults(func=cmd_image_spec)

    p_validate = sub.add_parser("validate", help="Validate profiles vs OceanBase recommendations")
    p_validate.add_argument("--config", default="config/deploy.yaml")
    p_validate.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
