#!/usr/bin/env python3
"""Поля для `obd cluster export-to-ocp` / OCP takeOver.

OBD `plugins/ocp-server/4.2.1/takeover.py` читает только
`cluster_config.get_global_conf()`, не per-server. Если `mysql_port` есть
только у serverN, в POST /api/v2/ob/clusters/takeOver уходит `"port": null`
и OCP отвечает: «You must specify the value of the given parameter».

Патч вставляет mysql_port в `oceanbase-ce.global` текстово, не перезаписывая
весь YAML (OBD может хранить !encrypted пароли).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

DEFAULT_HOST_TYPE = "yandex-cloud"
OCEANBASE_KEYS = ("oceanbase-ce", "oceanbase")
_COMPONENT_RE = re.compile(r"^(oceanbase-ce|oceanbase):\s*(#.*)?$")
_GLOBAL_RE = re.compile(r"^(\s+)global:\s*(#.*)?$")
_TOP_KEY_RE = re.compile(r"^\S")


def _oceanbase_global(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    for key in OCEANBASE_KEYS:
        block = data.get(key)
        if isinstance(block, dict):
            global_conf = block.get("global")
            return global_conf if isinstance(global_conf, dict) else {}
    return None


def global_mysql_port(data: Any) -> int | None:
    global_conf = _oceanbase_global(data)
    if not global_conf:
        return None
    value = global_conf.get("mysql_port")
    if value in (None, ""):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if port > 0 else None


def needs_mysql_port(data: Any) -> bool:
    return global_mysql_port(data) is None and _oceanbase_global(data) is not None


def insert_global_mysql_port(text: str, mysql_port: int = 2881) -> str:
    """Вставить mysql_port в oceanbase-ce.global, не трогая остальные строки."""
    lines = text.splitlines(keepends=True)
    in_oceanbase = False
    for idx, line in enumerate(lines):
        stripped = line.split("\n", 1)[0]
        if _COMPONENT_RE.match(stripped):
            in_oceanbase = True
            continue
        if in_oceanbase and _TOP_KEY_RE.match(stripped) and not stripped.startswith("#"):
            in_oceanbase = False
        if not in_oceanbase:
            continue
        match = _GLOBAL_RE.match(stripped)
        if not match:
            continue
        indent = match.group(1) + "  "
        # Уже есть mysql_port сразу в этом global (не ищем по всему файлу —
        # per-server mysql_port не считается).
        for look in lines[idx + 1 :]:
            look_stripped = look.split("\n", 1)[0]
            if not look_stripped.strip() or look_stripped.lstrip().startswith("#"):
                continue
            if not look_stripped.startswith(indent):
                break
            if re.match(r"^\s+mysql_port\s*:", look_stripped):
                return text
        insertion = f"{indent}mysql_port: {int(mysql_port)}\n"
        # Сохранить исходный newline-стиль первой строки global.
        lines.insert(idx + 1, insertion)
        return "".join(lines)
    return text


def patch_obd_config_text(text: str, mysql_port: int = 2881) -> tuple[str, bool]:
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError:
            data = None
        if data is not None and not needs_mysql_port(data):
            return text, False
    patched = insert_global_mysql_port(text, mysql_port=mysql_port)
    return patched, patched != text


def patch_obd_cluster_dir(cluster_dir: Path, mysql_port: int = 2881) -> list[Path]:
    changed: list[Path] = []
    if not cluster_dir.is_dir():
        return changed
    for name in ("config.yaml", "config.yml"):
        path = cluster_dir / name
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8")
        patched, did = patch_obd_config_text(original, mysql_port=mysql_port)
        if did:
            path.write_text(patched, encoding="utf-8")
            changed.append(path)
    return changed


def export_to_ocp_log_ok(text: str) -> bool:
    """OBD may exit non-zero after utils RPM ERROR even when takeover was submitted."""
    lowered = text.lower()
    markers = (
        "takeover task successfully submitted",
        "successfully submitted to ocp",
        "already been taken over",
        "cluster has been taken over",
    )
    return any(marker in lowered for marker in markers)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("patch-config", "log-ok"))
    parser.add_argument("--cluster-dir", type=Path, default=None)
    parser.add_argument("--mysql-port", type=int, default=2881)
    parser.add_argument("--log-file", type=Path, default=None)
    args = parser.parse_args()
    if args.command == "patch-config":
        if args.cluster_dir is None or not args.cluster_dir.is_dir():
            print(f"WARN: нет каталога OBD {args.cluster_dir}", file=sys.stderr)
            return
        changed = patch_obd_cluster_dir(args.cluster_dir, mysql_port=args.mysql_port)
        if changed:
            for path in changed:
                print(f"patched {path}")
        else:
            print("ok mysql_port already in oceanbase-ce.global")
        return
    if args.command == "log-ok":
        text = args.log_file.read_text(encoding="utf-8") if args.log_file else sys.stdin.read()
        sys.exit(0 if export_to_ocp_log_ok(text) else 1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
