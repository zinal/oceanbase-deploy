#!/usr/bin/env python3
"""Профили ВМ по ролям OceanBase и валидация ресурсов."""

from __future__ import annotations

import argparse
import json
import math
import re
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

# Официальные ориентиры:
# https://oceanbase.github.io/docs/user_manual/quick_starts/en-US/chapter_02_deploy_oceanbase_database/preparation_before_deployment
# https://en.oceanbase.com/blog/2614861312
# https://www.oceanbase.com/docs/common-obd-cn-1000000003892315
# https://github.com/oceanbase/oceanbase-doc/blob/V4.3.5/en-US/800.FAQ/300.deployment-faq.md
OB_MEMORY_LIMIT_PCT_LT_512 = 80
OB_MEMORY_LIMIT_PCT_GE_512 = 90
OB_LOG_DISK_FACTOR = 3
OB_DATA_PLUS_LOG_FACTOR = 6
OB_PROD_CPU_MIN = 4
OB_PROD_CPU_RECOMMENDED = 32
OB_PROD_MEMORY_MIN_GB = 16
OB_PROD_MEMORY_LONG_TERM_GB = 32
OB_OCP_SERVER_MEMORY_MIN_GB = 8
# datafile/log prealloc: OBD max occupancy ~85–90% диска, в production log ≥ 48G, data ≥ 20G
OB_DATAFILE_DISK_PCT = 90
OB_LOGFILE_DISK_PCT = 90
OB_PROD_DATAFILE_MIN_GB = 20
OB_PROD_LOG_MIN_GB = 48
_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([KMGT]I?B?)?$", re.IGNORECASE)

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


def parse_size_to_gb(value: Any) -> float | None:
    """Разбор размеров OceanBase/OBD: 28G, 28GB, 28672M, 2.0, 8."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().upper().replace(" ", "")
    match = _SIZE_RE.fullmatch(text)
    if not match:
        return None
    amount = float(match.group(1))
    unit = (match.group(2) or "G").replace("IB", "I").replace("B", "")
    factors = {
        "K": 1 / (1024 * 1024),
        "M": 1 / 1024,
        "G": 1.0,
        "T": 1024.0,
        "KI": 1 / (1024 * 1024),
        "MI": 1 / 1024,
        "GI": 1.0,
        "TI": 1024.0,
    }
    if unit not in factors:
        return None
    return amount * factors[unit]


def fmt_gb(n: float) -> str:
    if abs(n - round(n)) < 1e-6:
        return f"{int(round(n))}G"
    return f"{n:.1f}G"


def recommended_memory_limit_pct(memory_gb: int) -> int:
    """memory_limit_percentage: 80% при RAM < 512 GB, иначе 90%."""
    return OB_MEMORY_LIMIT_PCT_GE_512 if memory_gb >= 512 else OB_MEMORY_LIMIT_PCT_LT_512


def recommended_system_memory_range(memory_limit_gb: float) -> tuple[float, float]:
    """Официальный диапазон system_memory от memory_limit (preparations before deployment)."""
    if memory_limit_gb > 64:
        rec = 3 * (math.sqrt(memory_limit_gb) - 3)
        rec = max(10.0, rec)
        return rec * 0.8, rec * 1.2
    if memory_limit_gb >= 32:
        return 5.0, 10.0
    if memory_limit_gb >= 16:
        return 3.0, 5.0
    if memory_limit_gb >= 8:
        return 3.0, 3.0
    return 2.0, 2.0


def effective_oceanbase_resources(cfg: dict[str, Any]) -> dict[str, Any]:
    """Параметры, которые уйдут в OBD: auto_tune перекрывает секцию oceanbase."""
    ob = dict(cfg.get("oceanbase") or {})
    if ob.get("auto_tune", True):
        ob.update(observer_auto_tune(cfg))
    return ob


def _issue_cpu_vs_vm(
    label: str,
    cpu_count: float | None,
    cores: int,
    *,
    production: bool,
    kinds: str = "all",
) -> list[str]:
    issues: list[str] = []

    def emit(msg: str) -> None:
        if kinds == "all" or (kinds == "error" and msg.startswith("ERROR")) or (
            kinds == "warn" and msg.startswith("WARN")
        ):
            issues.append(msg)

    if cpu_count is None:
        return issues
    if cpu_count < 0:
        emit(f"ERROR: {label} cpu_count={cpu_count} — значение должно быть ≥ 0")
        return issues
    if cpu_count > cores:
        emit(
            f"ERROR: {label} cpu_count={cpu_count:g} превышает observer.cores={cores} "
            "(ядра, выделенные OceanBase, не могут быть больше ВМ)"
        )
        return issues
    if cpu_count == 0:
        emit(
            f"WARN: {label} cpu_count=0 — автодетект; процесс observer может занять все "
            f"{cores} vCPU ВМ, без запаса на OS/obagent"
        )
        return issues
    if production and cpu_count < OB_PROD_CPU_MIN:
        emit(
            f"WARN: {label} cpu_count={cpu_count:g} < {OB_PROD_CPU_MIN} — "
            "для production нужно ≥ 4 ядра процессу observer"
        )
    if cores >= OB_PROD_CPU_RECOMMENDED and cpu_count < OB_PROD_CPU_RECOMMENDED:
        emit(
            f"WARN: {label} cpu_count={cpu_count:g} при observer.cores={cores} — "
            f"для production рекомендуется ≥ {OB_PROD_CPU_RECOMMENDED} ядер OceanBase"
        )
    # OBD max occupancy: cpu_count = max(16, CPU-2); оставляем запас OS.
    if cores >= 16 and cpu_count > cores - 2:
        emit(
            f"WARN: {label} cpu_count={cpu_count:g} почти равен observer.cores={cores} — "
            "рекомендуется оставить ≥ 2 vCPU OS/irq (OBD: CPU−2)"
        )
    return issues


def _issue_memory_vs_vm(
    label: str,
    memory_limit_gb: float | None,
    memory_gb: int,
    *,
    production: bool,
    kinds: str = "all",
) -> list[str]:
    issues: list[str] = []

    def emit(msg: str) -> None:
        if kinds == "all" or (kinds == "error" and msg.startswith("ERROR")) or (
            kinds == "warn" and msg.startswith("WARN")
        ):
            issues.append(msg)

    if memory_limit_gb is None:
        return issues
    if memory_limit_gb > memory_gb + 1e-6:
        emit(
            f"ERROR: {label} memory_limit={fmt_gb(memory_limit_gb)} превышает "
            f"observer.memory_gb={memory_gb} (память процесса > RAM ВМ)"
        )
        return issues
    pct = recommended_memory_limit_pct(memory_gb)
    max_rec = memory_gb * pct / 100.0
    if memory_limit_gb > max_rec + 0.5:
        emit(
            f"WARN: {label} memory_limit={fmt_gb(memory_limit_gb)} > {pct}% RAM ВМ "
            f"({fmt_gb(max_rec)}) — запас OS/obagent; при RAM < 512G рекомендуется 80%, "
            "при ≥ 512G — 90%"
        )
    if production and memory_limit_gb < OB_PROD_MEMORY_MIN_GB:
        emit(
            f"WARN: {label} memory_limit={fmt_gb(memory_limit_gb)} < {OB_PROD_MEMORY_MIN_GB}G — "
            "минимум для production, выделяемый OceanBase (не total RAM сервера)"
        )
    elif production and memory_limit_gb < OB_PROD_MEMORY_LONG_TERM_GB:
        emit(
            f"WARN: {label} memory_limit={fmt_gb(memory_limit_gb)} < {OB_PROD_MEMORY_LONG_TERM_GB}G — "
            "для длительной эксплуатации рекомендуется ≥ 32G процессу observer"
        )
    if memory_limit_gb > 1024:
        emit(
            f"WARN: {label} memory_limit={fmt_gb(memory_limit_gb)} > 1T — "
            "официальный потолок настройки observer — 1024 GB"
        )
    return issues


def _observer_provisioned_disks(obs: dict[str, Any]) -> dict[str, Any]:
    """Фактические размеры дисков ВМ после округления YC (шаг 93 GB)."""
    data = obs.get("data_disk") or {}
    log = obs.get("log_disk") or {}
    boot = obs.get("boot_disk") or {}
    data_enabled = bool(data.get("enabled"))
    log_enabled = bool(log.get("enabled"))
    return {
        "data_enabled": data_enabled,
        "log_enabled": log_enabled,
        "data_gb": float(int(data.get("size_gb", 0) or 0)) if data_enabled else 0.0,
        "log_gb": float(int(log.get("size_gb", 0) or 0)) if log_enabled else 0.0,
        "boot_gb": float(int(boot.get("size_gb", 0) or 0)),
    }


def _issue_prealloc_vs_disk(
    param: str,
    size_gb: float | None,
    disk_gb: float,
    disk_label: str,
    *,
    fill_pct: int,
    kinds: str = "all",
) -> list[str]:
    issues: list[str] = []

    def emit(msg: str) -> None:
        if kinds == "all" or (kinds == "error" and msg.startswith("ERROR")) or (
            kinds == "warn" and msg.startswith("WARN")
        ):
            issues.append(msg)

    if size_gb is None:
        return issues
    if disk_gb <= 0:
        emit(
            f"ERROR: oceanbase.{param}={fmt_gb(size_gb)}, но {disk_label} не задан "
            "(enabled=false или size_gb=0)"
        )
        return issues
    if size_gb > disk_gb + 1e-6:
        emit(
            f"ERROR: oceanbase.{param}={fmt_gb(size_gb)} превышает {disk_label}={fmt_gb(disk_gb)} "
            "(prealloc больше диска ВМ)"
        )
        return issues
    max_fill = disk_gb * fill_pct / 100.0
    if size_gb > max_fill + 0.5:
        emit(
            f"WARN: oceanbase.{param}={fmt_gb(size_gb)} > {fill_pct}% {disk_label} "
            f"({fmt_gb(max_fill)}) — prealloc; оставьте запас файловой системы"
        )
    return issues


def _check_oceanbase_disks(
    *,
    datafile: float | None,
    log_size: float | None,
    disks: dict[str, Any],
    memory_limit_gb: float | None,
    production: bool,
    kinds: str,
) -> list[str]:
    """Сверка datafile_size / log_disk_size с реальными дисками observer."""
    issues: list[str] = []
    data_gb = disks["data_gb"]
    log_gb = disks["log_gb"]
    log_enabled = disks["log_enabled"]
    data_enabled = disks["data_enabled"]

    if log_enabled:
        issues.extend(
            _issue_prealloc_vs_disk(
                "datafile_size",
                datafile,
                data_gb,
                "observer data_disk",
                fill_pct=OB_DATAFILE_DISK_PCT,
                kinds=kinds,
            )
        )
        issues.extend(
            _issue_prealloc_vs_disk(
                "log_disk_size",
                log_size,
                log_gb,
                "observer log_disk",
                fill_pct=OB_LOGFILE_DISK_PCT,
                kinds=kinds,
            )
        )
    else:
        # clog на том же диске, что и data (или на boot, если data выключен)
        shared_gb = data_gb if data_enabled else disks["boot_gb"]
        shared_label = "observer data_disk" if data_enabled else "observer boot_disk"
        occupied = None
        if datafile is not None or log_size is not None:
            occupied = (datafile or 0.0) + (log_size or 0.0)
        issues.extend(
            _issue_prealloc_vs_disk(
                "datafile_size+log_disk_size",
                occupied,
                shared_gb,
                shared_label,
                fill_pct=OB_DATAFILE_DISK_PCT,
                kinds=kinds,
            )
        )
        if kinds in ("all", "warn") and production:
            issues.append(
                "WARN: observer log_disk.enabled=false — в production data и clog должны быть "
                "на разных дисках"
            )

    if kinds in ("all", "warn"):
        if production and datafile is not None and datafile < OB_PROD_DATAFILE_MIN_GB:
            issues.append(
                f"WARN: oceanbase.datafile_size={fmt_gb(datafile)} < {OB_PROD_DATAFILE_MIN_GB}G — "
                "минимум data directory в доке"
            )
        if production and log_size is not None and log_size < OB_PROD_LOG_MIN_GB:
            issues.append(
                f"WARN: oceanbase.log_disk_size={fmt_gb(log_size)} < {OB_PROD_LOG_MIN_GB}G — "
                "в production log disk ≥ 48G"
            )
        if memory_limit_gb is not None:
            if log_size is not None and log_size + 1e-6 < memory_limit_gb * OB_LOG_DISK_FACTOR:
                issues.append(
                    f"WARN: oceanbase.log_disk_size={fmt_gb(log_size)} < "
                    f"{OB_LOG_DISK_FACTOR}× memory_limit ({fmt_gb(memory_limit_gb * OB_LOG_DISK_FACTOR)}) — "
                    "официально log_disk_size ≥ memory_limit × 3"
                )
            elif log_enabled and log_gb + 1e-6 < memory_limit_gb * OB_LOG_DISK_FACTOR:
                issues.append(
                    f"WARN: observer log_disk={fmt_gb(log_gb)} < "
                    f"{OB_LOG_DISK_FACTOR}× memory_limit ({fmt_gb(memory_limit_gb * OB_LOG_DISK_FACTOR)})"
                )
            total_disk = data_gb + (log_gb if log_enabled else 0.0)
            if total_disk > 0 and total_disk + 1e-6 < memory_limit_gb * OB_DATA_PLUS_LOG_FACTOR:
                issues.append(
                    f"WARN: data_disk+log_disk={fmt_gb(total_disk)} < "
                    f"{OB_DATA_PLUS_LOG_FACTOR}× memory_limit "
                    f"({fmt_gb(memory_limit_gb * OB_DATA_PLUS_LOG_FACTOR)}) — "
                    "суммарно диск должен быть > 6× памяти OceanBase"
                )
    return issues


def validate_oceanbase_against_vms(cfg: dict[str, Any]) -> list[str]:
    """Сверка секции oceanbase (и ocp) с профилями ВМ.

    ERROR — параметры OceanBase больше ресурсов ВМ.
    WARN — отклонение от публичных рекомендаций OceanBase/OBD.
    """
    issues: list[str] = []
    ob = cfg.get("oceanbase") or {}
    if not ob:
        issues.append("WARN: секция oceanbase отсутствует — сверка с ВМ пропущена")
        return issues

    try:
        obs = resolve_profile(cfg, "observer")
    except KeyError:
        issues.append("WARN: нет vm_profiles.observer — сверка oceanbase с ВМ пропущена")
        return issues

    cores = int(obs["cores"])
    memory_gb = int(obs["memory_gb"])
    production = int(obs.get("count", 0)) >= 3
    auto_tune = bool(ob.get("auto_tune", True))
    yaml_cpu = parse_size_to_gb(ob.get("cpu_count"))
    yaml_mem = parse_size_to_gb(ob.get("memory_limit"))
    yaml_sys = parse_size_to_gb(ob.get("system_memory"))
    yaml_data = parse_size_to_gb(ob.get("datafile_size"))
    yaml_log = parse_size_to_gb(ob.get("log_disk_size"))

    effective = effective_oceanbase_resources(cfg)
    eff_cpu = parse_size_to_gb(effective.get("cpu_count"))
    eff_mem = parse_size_to_gb(effective.get("memory_limit"))
    eff_sys = parse_size_to_gb(effective.get("system_memory"))
    eff_data = parse_size_to_gb(effective.get("datafile_size"))
    eff_log = parse_size_to_gb(effective.get("log_disk_size"))

    issues.append(
        f"INFO: oceanbase vs observer: yaml cpu_count={ob.get('cpu_count')} "
        f"memory_limit={ob.get('memory_limit')}; auto_tune={str(auto_tune).lower()} → "
        f"OBD cpu_count={effective.get('cpu_count')} memory_limit={effective.get('memory_limit')}; "
        f"ВМ {cores} vCPU / {memory_gb} GB"
    )
    disks = _observer_provisioned_disks(obs)
    issues.append(
        f"INFO: oceanbase disks: yaml datafile_size={ob.get('datafile_size')} "
        f"log_disk_size={ob.get('log_disk_size')}; auto_tune → "
        f"OBD datafile_size={effective.get('datafile_size')} "
        f"log_disk_size={effective.get('log_disk_size')}; "
        f"ВМ data_disk={fmt_gb(disks['data_gb']) if disks['data_enabled'] else 'off'} "
        f"log_disk={fmt_gb(disks['log_gb']) if disks['log_enabled'] else 'off'}"
    )

    # (а) превышение ресурсов ВМ — YAML всегда, auto_tune — если отличается
    issues.extend(_issue_cpu_vs_vm("oceanbase", yaml_cpu, cores, production=production, kinds="error"))
    issues.extend(_issue_memory_vs_vm("oceanbase", yaml_mem, memory_gb, production=production, kinds="error"))
    issues.extend(
        _check_oceanbase_disks(
            datafile=yaml_data,
            log_size=yaml_log,
            disks=disks,
            memory_limit_gb=yaml_mem,
            production=production,
            kinds="error",
        )
    )
    if auto_tune:
        if eff_cpu != yaml_cpu:
            issues.extend(
                _issue_cpu_vs_vm("auto_tune/OBD", eff_cpu, cores, production=production, kinds="error")
            )
        if eff_mem != yaml_mem:
            issues.extend(
                _issue_memory_vs_vm(
                    "auto_tune/OBD", eff_mem, memory_gb, production=production, kinds="error"
                )
            )
        if eff_data != yaml_data or eff_log != yaml_log:
            issues.extend(
                _check_oceanbase_disks(
                    datafile=eff_data,
                    log_size=eff_log,
                    disks=disks,
                    memory_limit_gb=eff_mem,
                    production=production,
                    kinds="error",
                )
            )
        yaml_sig = (
            None if yaml_cpu is None else f"{yaml_cpu:g}",
            None if yaml_mem is None else fmt_gb(yaml_mem),
            None if yaml_data is None else fmt_gb(yaml_data),
            None if yaml_log is None else fmt_gb(yaml_log),
        )
        eff_sig = (
            None if eff_cpu is None else f"{eff_cpu:g}",
            None if eff_mem is None else fmt_gb(eff_mem),
            None if eff_data is None else fmt_gb(eff_data),
            None if eff_log is None else fmt_gb(eff_log),
        )
        if yaml_sig != eff_sig:
            issues.append(
                "WARN: oceanbase.auto_tune=true — cpu_count/memory_limit/datafile_size/log_disk_size "
                "из YAML будут заменены при генерации OBD "
                f"(yaml {ob.get('cpu_count')}/{ob.get('memory_limit')}/"
                f"{ob.get('datafile_size')}/{ob.get('log_disk_size')} → "
                f"{effective.get('cpu_count')}/{effective.get('memory_limit')}/"
                f"{effective.get('datafile_size')}/{effective.get('log_disk_size')})"
            )

    # (б) рекомендации — по эффективным значениям (то, что реально попадёт в OBD)
    warn_cpu = eff_cpu if auto_tune else yaml_cpu
    warn_mem = eff_mem if auto_tune else yaml_mem
    warn_data = eff_data if auto_tune else yaml_data
    warn_log = eff_log if auto_tune else yaml_log
    issues.extend(_issue_cpu_vs_vm("oceanbase", warn_cpu, cores, production=production, kinds="warn"))
    issues.extend(
        _issue_memory_vs_vm("oceanbase", warn_mem, memory_gb, production=production, kinds="warn")
    )

    mem_for_sys = yaml_mem if not auto_tune else eff_mem
    sys_for_check = yaml_sys if not auto_tune else eff_sys
    if mem_for_sys is not None and sys_for_check is not None:
        if sys_for_check >= mem_for_sys:
            issues.append(
                f"ERROR: oceanbase.system_memory={fmt_gb(sys_for_check)} ≥ "
                f"memory_limit={fmt_gb(mem_for_sys)} — system_memory — часть memory_limit "
                "(tenant 500)"
            )
        else:
            low, high = recommended_system_memory_range(mem_for_sys)
            if sys_for_check < low - 0.25 or sys_for_check > high + 0.25:
                issues.append(
                    f"WARN: oceanbase.system_memory={fmt_gb(sys_for_check)} вне диапазона "
                    f"{fmt_gb(low)}–{fmt_gb(high)} для memory_limit={fmt_gb(mem_for_sys)} "
                    "(формула доки: [16G,32G]→3–5G, [32G,64G]→5–10G, >64G → "
                    "3×(√memory_limit−3G))"
                )

    issues.extend(
        _check_oceanbase_disks(
            datafile=warn_data,
            log_size=warn_log,
            disks=disks,
            memory_limit_gb=warn_mem,
            production=production,
            kinds="warn",
        )
    )

    issues.extend(_validate_ocp_against_vms(cfg, cores, mem_for_sys, sys_for_check, production))
    return issues


def _validate_ocp_against_vms(
    cfg: dict[str, Any],
    observer_cores: int,
    memory_limit_gb: float | None,
    system_memory_gb: float | None,
    production: bool,
) -> list[str]:
    issues: list[str] = []
    ocp = cfg.get("ocp") or {}
    profiles = cfg.get("vm_profiles", {})
    ocp_vm = profiles.get("ocp") or {}
    enabled = bool(ocp.get("enabled")) and bool(ocp_vm.get("enabled", False))
    if not enabled:
        return issues

    try:
        ocp_profile = resolve_profile(cfg, "ocp")
    except KeyError:
        return issues

    ocp_mem_gb = int(ocp_profile["memory_gb"])
    ocp_cores = int(ocp_profile["cores"])
    heap = parse_size_to_gb(ocp.get("memory_size"))
    if heap is not None and heap > ocp_mem_gb + 1e-6:
        issues.append(
            f"ERROR: ocp.memory_size={fmt_gb(heap)} превышает vm_profiles.ocp.memory_gb={ocp_mem_gb}"
        )
    elif heap is not None and heap < OB_OCP_SERVER_MEMORY_MIN_GB:
        issues.append(
            f"WARN: ocp.memory_size={fmt_gb(heap)} < {OB_OCP_SERVER_MEMORY_MIN_GB}G — "
            "OCP-Server официально минимум 8 GB (4 vCPU / 8 GB)"
        )
    if ocp_cores < 4:
        issues.append(
            f"WARN: vm_profiles.ocp.cores={ocp_cores} < 4 — OCP-Server рекомендуется ≥ 4 vCPU"
        )

    tenant_cpu = 0.0
    tenant_mem = 0.0
    for key in ("meta_tenant", "monitor_tenant"):
        tenant = ocp.get(key) or {}
        max_cpu = parse_size_to_gb(tenant.get("max_cpu"))
        tmem = parse_size_to_gb(tenant.get("memory_size"))
        if max_cpu is not None:
            tenant_cpu += max_cpu
            if max_cpu > observer_cores:
                issues.append(
                    f"ERROR: ocp.{key}.max_cpu={max_cpu:g} превышает observer.cores={observer_cores}"
                )
        if tmem is not None:
            tenant_mem += tmem

    if tenant_cpu > observer_cores:
        issues.append(
            f"ERROR: сумма ocp meta+monitor max_cpu={tenant_cpu:g} превышает "
            f"observer.cores={observer_cores}"
        )
    if memory_limit_gb is not None and tenant_mem > 0:
        available = memory_limit_gb - (system_memory_gb or 0)
        if tenant_mem > available + 1e-6:
            issues.append(
                f"ERROR: сумма ocp meta+monitor memory_size={fmt_gb(tenant_mem)} превышает "
                f"доступную память observer (memory_limit−system_memory={fmt_gb(available)})"
            )
        elif production and tenant_mem > available * 0.5:
            issues.append(
                f"WARN: тенанты OCP meta+monitor занимают {fmt_gb(tenant_mem)} из "
                f"{fmt_gb(available)} доступных на observer — оставьте запас user tenant"
            )
    return issues


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
                if ltype != "network-ssd-nonreplicated":
                    issues.append(
                        f"WARN: observer log_disk.type={ltype} — для clog рекомендуется "
                        "network-ssd-nonreplicated (репликация Paxos, как у data)"
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
            log.get("type", "network-ssd-nonreplicated"),
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
    issues.extend(validate_oceanbase_against_vms(cfg))
    has_error = False
    for item in issues:
        if item.startswith("ERROR"):
            print(item, file=sys.stderr)
            has_error = True
        else:
            print(item)
    if has_error:
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

    p_validate = sub.add_parser(
        "validate",
        help="Validate VM profiles and oceanbase/ocp resources vs VMs",
    )
    p_validate.add_argument("--config", default="config/deploy.yaml")
    p_validate.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
