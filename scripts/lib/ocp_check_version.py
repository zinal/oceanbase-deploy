#!/usr/bin/env python3
"""Версия OCP для `obd cluster check4ocp -V`.

OBD по умолчанию передаёт в плагин `-V 3.1.1` (`_cmd.py`, ClusterCheckForOCPChange).
`plugins/oceanbase/3.1.0/ocp_check.py` тогда требует `user.username == admin`,
если версия OCP < 4.2.0. Это OS-пользователь SSH, не admin консоли OCP.

В этом репозитории SSH — `obadmin` (`oceanbase.deploy_user` / `yandex_cloud.ssh_user`).
Менять его на `admin` нельзя: сломается доступ к ВМ. Для OCP ≥ 4.2.0 (включая
текущий ocp-server-ce 4.4.x) проверка admin-пользователя не нужна — достаточно
передать реальную версию через `-V`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

DEFAULT_OCP_CHECK_VERSION = "4.4.2"
OCP_COMPONENT_KEYS = ("ocp-server-ce", "ocp-server")
_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)")


def normalize_ocp_version(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() == "null":
        return ""
    match = _VERSION_RE.match(text)
    return match.group(1) if match else text


def ocp_version_from_mapping(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in OCP_COMPONENT_KEYS:
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        for candidate in (
            block.get("version"),
            (block.get("repository") or {}).get("version") if isinstance(block.get("repository"), dict) else None,
            (block.get("pkg") or {}).get("version") if isinstance(block.get("pkg"), dict) else None,
        ):
            version = normalize_ocp_version(candidate)
            if version:
                return version
    for value in data.values():
        if isinstance(value, dict):
            found = ocp_version_from_mapping(value)
            if found:
                return found
    return ""


def ocp_version_from_yaml_file(path: Path) -> str:
    if yaml is None or not path.is_file():
        return ""
    try:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return ""
    return ocp_version_from_mapping(data)


def ocp_version_from_api_payload(payload: Any) -> str:
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return normalize_ocp_version(text)
    if not isinstance(payload, dict):
        return ""
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    for key in ("buildVersion", "build_version", "version"):
        version = normalize_ocp_version(data.get(key))
        if version:
            return version
    return ""


def resolve_ocp_check_version(
    config_version: str = "",
    yaml_paths: list[Path] | None = None,
    api_payload: str = "",
    default: str = DEFAULT_OCP_CHECK_VERSION,
) -> str:
    version = normalize_ocp_version(config_version)
    if version:
        return version
    for path in yaml_paths or []:
        version = ocp_version_from_yaml_file(Path(path))
        if version:
            return version
    version = ocp_version_from_api_payload(api_payload)
    if version:
        return version
    return default


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-version",
        default="",
        help="ocp.version из config/deploy.yaml",
    )
    parser.add_argument(
        "--yaml",
        action="append",
        default=[],
        dest="yaml_paths",
        help="OBD YAML (generated/obd-cluster.yaml или ~/.obd/cluster/<name>/*.yaml)",
    )
    parser.add_argument(
        "--api-payload",
        default="",
        help="Тело JSON ответа OCP /api/v2/info",
    )
    parser.add_argument(
        "--default",
        default=DEFAULT_OCP_CHECK_VERSION,
        help="Fallback, если версия нигде не задана",
    )
    args = parser.parse_args()
    print(
        resolve_ocp_check_version(
            config_version=args.config_version,
            yaml_paths=[Path(p) for p in args.yaml_paths],
            api_payload=args.api_payload,
            default=args.default,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
