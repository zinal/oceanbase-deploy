#!/usr/bin/env python3
"""Генерация haproxy.cfg для runner-ВМ: backend obproxy по именам, не по IP."""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_IP_IN_SERVER = re.compile(
    r"^\s*server\s+\S+\s+(\S+?)(?::\d+)?(?:\s|$)",
    re.IGNORECASE,
)


def parse_inventory(path: Path) -> dict[str, str]:
    inv: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        inv[key] = value
    return inv


def _looks_like_ip(value: str) -> bool:
    host = value.split("%", 1)[0]
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def obproxy_names(inv: dict[str, str]) -> list[str]:
    """Имена obproxy из inventory (OBPROXY_N_NAME). IP не используем."""
    count = int(inv.get("OBPROXY_COUNT") or 0)
    names: list[str] = []
    for idx in range(1, count + 1):
        name = (inv.get(f"OBPROXY_{idx}_NAME") or "").strip()
        if not name:
            raise ValueError(
                f"Нет OBPROXY_{idx}_NAME в inventory — HAProxy должен смотреть на obproxy по имени"
            )
        if _looks_like_ip(name):
            raise ValueError(
                f"OBPROXY_{idx}_NAME={name} выглядит как IP — нужен hostname"
            )
        names.append(name)
    if not names:
        raise ValueError("OBPROXY_COUNT=0 — нет узлов obproxy для backend HAProxy")
    return names


def runner_names(inv: dict[str, str]) -> list[str]:
    count = int(inv.get("RUNNER_COUNT") or 0)
    names: list[str] = []
    for idx in range(1, count + 1):
        name = (inv.get(f"RUNNER_{idx}_NAME") or "").strip()
        if not name:
            raise ValueError(f"Нет RUNNER_{idx}_NAME в inventory")
        names.append(name)
    return names


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def obproxy_listen_port(cfg: dict[str, Any]) -> int:
    ports = (cfg.get("oceanbase") or {}).get("ports") or {}
    try:
        return int(ports.get("obproxy") or 2883)
    except (TypeError, ValueError):
        return 2883


def render_haproxy_cfg(proxy_names: list[str], port: int = 2883) -> str:
    """Конфиг по образцу bench/tpcc/haproxy.cfg: bind localhost, backend по hostname."""
    servers = "\n".join(
        f"    server obproxy{idx} {name}:{port} check"
        for idx, name in enumerate(proxy_names, start=1)
    )
    return f"""\
global
	log /dev/log	local0
	log /dev/log	local1 notice
	chroot /var/lib/haproxy
	stats socket /run/haproxy/admin.sock mode 660 level admin
	stats timeout 30s
	user haproxy
	group haproxy
	# systemd (haproxy -Ws): не daemon

	# Default SSL material locations
	ca-base /etc/ssl/certs
	crt-base /etc/ssl/private

	# See: https://ssl-config.mozilla.org/#server=haproxy&server-version=2.0.3&config=intermediate
        ssl-default-bind-ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384
        ssl-default-bind-ciphersuites TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256
        ssl-default-bind-options ssl-min-ver TLSv1.2 no-tls-tickets


defaults
    log     global
    mode    tcp
    option  tcplog
    option  dontlognull
    timeout connect 10s
    timeout client  24h
    timeout server  24h
    timeout tunnel  24h

# --- MySQL/OceanBase через OBProxy (порт {port}) ---

frontend obproxy_mysql
    bind 127.0.0.1:{port}
#    bind [::]:{port}
    default_backend obproxy_servers

backend obproxy_servers
    # Round-robin новых TCP-сессий (как bench/tpcc/haproxy.cfg)
    balance roundrobin

    # Пассивная TCP-проверка доступности порта obproxy
    option tcp-check
    default-server inter 5s fall 3 rise 2

    # Backend — hostname obproxy из inventory (не IP)
{servers}
"""


def backend_hosts_from_cfg(text: str) -> list[str]:
    hosts: list[str] = []
    for line in text.splitlines():
        match = _IP_IN_SERVER.match(line)
        if match:
            hosts.append(match.group(1))
    return hosts


def assert_backends_are_names(text: str) -> None:
    hosts = backend_hosts_from_cfg(text)
    if not hosts:
        raise ValueError("В haproxy.cfg нет server-строк backend")
    for host in hosts:
        if _looks_like_ip(host):
            raise ValueError(f"Backend HAProxy указывает на IP {host} — нужны имена obproxy")


def generate(inventory: Path, config: Path | None, output: Path | None) -> str:
    inv = parse_inventory(inventory)
    cfg = load_config(config)
    names = obproxy_names(inv)
    port = obproxy_listen_port(cfg)
    text = render_haproxy_cfg(names, port)
    assert_backends_are_names(text)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    return text


def cmd_generate(args: argparse.Namespace) -> None:
    text = generate(Path(args.inventory), Path(args.config) if args.config else None, Path(args.output) if args.output else None)
    if not args.output:
        sys.stdout.write(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="Сгенерировать haproxy.cfg из inventory")
    p_gen.add_argument("--inventory", required=True)
    p_gen.add_argument("--config", default="")
    p_gen.add_argument("--output", default="")
    p_gen.set_defaults(func=cmd_generate)

    args = parser.parse_args()
    if args.command == "generate":
        if not args.config:
            args.config = None
        if not args.output:
            args.output = None
    args.func(args)


if __name__ == "__main__":
    main()
