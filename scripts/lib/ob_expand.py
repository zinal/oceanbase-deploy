#!/usr/bin/env python3
"""Установка и расширение кластера OceanBase без OCP и OBD.

Первые три узла запускаются своим бинарём и собираются через
ALTER SYSTEM BOOTSTRAP. Следующий observer входит через ADD SERVER.
Список Root Service для IPv6 пишется в скобках. OBProxy — отдельный
процесс без членства в DBA_OB_SERVERS.
"""

from __future__ import annotations

import argparse
import ipaddress
import shlex
import sys
from dataclasses import dataclass


class ExpandError(ValueError):
    """Некорректный адрес, зона или параметр запуска."""


def _strip_ip(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    if "%" in text:
        raise ExpandError(
            f"адрес {value!r} содержит зону интерфейса (%…); для кластера нужен адрес без неё"
        )
    return text


def parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    text = _strip_ip(value)
    try:
        addr = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ExpandError(f"не IP-адрес: {value!r}") from exc
    if addr.is_unspecified or addr.is_loopback or addr.is_multicast or addr.is_link_local:
        raise ExpandError(
            f"адрес {addr} для узла кластера не подходит "
            "(нужен глобальный IPv6, ULA или обычный IPv4)"
        )
    return addr


def is_ipv6(value: str) -> bool:
    return isinstance(parse_ip(value), ipaddress.IPv6Address)


def same_ip(left: str, right: str) -> bool:
    return parse_ip(left) == parse_ip(right)


def server_endpoint(ip: str, port: int) -> str:
    """Адрес в SQL: IPv4 как a.b.c.d:port, IPv6 как [addr]:port."""
    addr = parse_ip(ip)
    check_port(port)
    if isinstance(addr, ipaddress.IPv6Address):
        return f"[{addr}]:{port}"
    return f"{addr}:{port}"


def check_port(port: int) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        raise ExpandError(f"порт вне 1..65535: {port!r}")
    return port


def check_zone(zone: str) -> str:
    text = (zone or "").strip()
    if not text or not text.replace("_", "").isalnum() or text[0].isdigit():
        raise ExpandError(f"имя zone должно быть идентификатором, получено {zone!r}")
    return text


def check_name(value: str, label: str) -> str:
    text = (value or "").strip()
    if not text or any(ch.isspace() for ch in text) or "'" in text or ";" in text:
        raise ExpandError(f"{label} содержит недопустимые символы: {value!r}")
    return text


def parse_ip_list(raw: str) -> list[str]:
    parts = [item.strip() for item in (raw or "").split(",") if item.strip()]
    if not parts:
        raise ExpandError("список Root Service пуст")
    return [str(parse_ip(item)) for item in parts]


def rootservice_list(members: list[str], rpc_port: int, mysql_port: int) -> str:
    """Список для -r observer: [ipv6]:rpc:sql или ipv4:rpc:sql."""
    check_port(rpc_port)
    check_port(mysql_port)
    items: list[str] = []
    for ip in members:
        items.append(f"{server_endpoint(ip, rpc_port)}:{mysql_port}")
    return ";".join(items)


def obproxy_rs_list(members: list[str], mysql_port: int) -> str:
    """Список для OBProxy: один SQL-порт, IPv6 в скобках."""
    check_port(mysql_port)
    return ";".join(server_endpoint(ip, mysql_port) for ip in members)


def quote_opt(value: str) -> str:
    text = check_name(value, "параметр")
    if any(ch in text for ch in ",'\"\\"):
        raise ExpandError(f"значение параметра нельзя безопасно передать в -o: {value!r}")
    return text


@dataclass(frozen=True)
class ObserverStart:
    """Команда запуска пустого observer, который ещё не член кластера."""

    ip: str
    zone: str
    roots: tuple[str, ...]
    appname: str
    cluster_id: int
    home_path: str
    data_dir: str
    redo_dir: str
    mysql_port: int = 2881
    rpc_port: int = 2882
    memory_limit: str = "64G"
    system_memory: str = "16G"
    datafile_size: str = "192G"
    log_disk_size: str = "192G"
    wipe: bool = False
    bootstrap: bool = False

    def argv(self) -> list[str]:
        if self.cluster_id < 1:
            raise ExpandError(f"cluster_id должен быть >= 1, получено {self.cluster_id}")
        ip = str(parse_ip(self.ip))
        zone = check_zone(self.zone)
        if self.bootstrap:
            roots = [str(parse_ip(item)) for item in self.roots]
            unique = {parse_ip(item) for item in roots}
            if len(unique) != 3:
                raise ExpandError("bootstrap запускает ровно 3 разных узла")
            if not any(same_ip(item, ip) for item in roots):
                raise ExpandError("список -r при bootstrap должен включать этот узел")
            if len({type(parse_ip(item)) for item in roots}) != 1:
                raise ExpandError("смешивать IPv4 и IPv6 в одном bootstrap нельзя")
        else:
            roots = [item for item in self.roots if not same_ip(item, ip)]
            if not roots:
                raise ExpandError("в --rs нужен хотя бы один уже работающий observer, не новый узел")
        ipv6 = is_ipv6(ip)
        opt = ",".join(
            [
                f"memory_limit={quote_opt(self.memory_limit)}",
                f"system_memory={quote_opt(self.system_memory)}",
                f"datafile_size={quote_opt(self.datafile_size)}",
                f"log_disk_size={quote_opt(self.log_disk_size)}",
            ]
        )
        if ipv6:
            opt += ",use_ipv6=true"
        binary = f"{self.home_path.rstrip('/')}/bin/observer"
        cmd = [binary]
        if ipv6:
            cmd.append("-6")
        cmd.extend(
            [
                "-I",
                ip,
                "-p",
                str(check_port(self.mysql_port)),
                "-P",
                str(check_port(self.rpc_port)),
                "-z",
                zone,
                "-n",
                check_name(self.appname, "appname"),
                "-c",
                str(self.cluster_id),
                "-d",
                self.data_dir,
                "-r",
                rootservice_list(roots, self.rpc_port, self.mysql_port),
                "-o",
                opt,
            ]
        )
        return cmd

    def add_server_sql(self, timeout_us: int = 3_600_000_000) -> str:
        if timeout_us < 1:
            raise ExpandError("timeout_us должен быть положительным")
        endpoint = server_endpoint(self.ip, self.rpc_port)
        zone = check_zone(self.zone)
        return (
            f"SET SESSION ob_query_timeout = {timeout_us}; "
            f"ALTER SYSTEM ADD SERVER '{endpoint}' ZONE '{zone}'"
        )


def bootstrap_sql(
    nodes: list[tuple[str, str]],
    rpc_port: int = 2882,
    timeout_us: int = 3_600_000_000,
) -> str:
    """nodes — пары (zone, ip). Ровно три разные zone и три разных адреса одной семьи."""
    if timeout_us < 1:
        raise ExpandError("timeout_us должен быть положительным")
    check_port(rpc_port)
    if len(nodes) != 3:
        raise ExpandError("начальная установка — ровно 3 узла, по одному на zone")
    zones: list[str] = []
    addrs: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    parts: list[str] = []
    for zone, ip in nodes:
        zone_name = check_zone(zone)
        addr = parse_ip(ip)
        zones.append(zone_name)
        addrs.append(addr)
        parts.append(f"ZONE '{zone_name}' SERVER '{server_endpoint(str(addr), rpc_port)}'")
    if len(set(zones)) != 3:
        raise ExpandError("три узла должны лежать в трёх разных zone")
    if len(set(addrs)) != 3:
        raise ExpandError("адреса трёх узлов должны различаться")
    if len({type(addr) for addr in addrs}) != 1:
        raise ExpandError("смешивать IPv4 и IPv6 в одном bootstrap нельзя")
    body = ", ".join(parts)
    return f"SET SESSION ob_query_timeout = {timeout_us}; ALTER SYSTEM BOOTSTRAP {body}"


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def remote_observer_script(spec: ObserverStart) -> str:
    """Bash, который на новом хосте поднимает пустой observer. Чужие хосты не трогает."""
    cmd = spec.argv()
    binary = cmd[0]
    home = spec.home_path.rstrip("/")
    data = spec.data_dir.rstrip("/")
    redo = spec.redo_dir.rstrip("/")
    if not home or home == "/" or not data or data == "/" or not redo or redo == "/":
        raise ExpandError("home, data_dir и redo_dir не могут быть пустыми или /")
    if data == redo:
        raise ExpandError("data_dir и redo_dir должны быть разными каталогами")
    wipe = "1" if spec.wipe else "0"
    if spec.bootstrap:
        clog_hint = "Повторный bootstrap по непустому clog невозможен. Запустите с --wipe"
    else:
        clog_hint = "Повторный ADD SERVER даст ERROR 4179. Запустите с --wipe"
    rendered = " ".join(shell_quote(part) for part in cmd)
    return f"""#!/bin/bash
set -euo pipefail
HOME_PATH={shell_quote(home)}
DATA_DIR={shell_quote(data)}
REDO_DIR={shell_quote(redo)}
WIPE={wipe}
BINARY={shell_quote(binary)}
if [[ ! -x "$BINARY" ]]; then
  echo "нет бинаря $BINARY (положите RPM/распакованный observer в home_path)" >&2
  exit 1
fi
mkdir -p "$HOME_PATH/run" "$HOME_PATH/log" "$DATA_DIR" "$REDO_DIR"
pidfile="$HOME_PATH/run/observer.pid"
if [[ -f "$pidfile" ]]; then
  old="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$old" && -d "/proc/$old" ]] && tr '\\0' ' ' < "/proc/$old/cmdline" | grep -F -q -- "$BINARY"; then
    if [[ "$WIPE" != "1" ]]; then
      echo "observer уже запущен, pid $old. Для чистого входа нужен --wipe" >&2
      exit 1
    fi
    kill "$old" || true
    sleep 2
    if [[ -d "/proc/$old" ]]; then
      kill -9 "$old" || true
    fi
  fi
fi
clog="$DATA_DIR/clog"
if [[ -d "$clog" ]] && [[ -n "$(find "$clog" -mindepth 1 -print -quit 2>/dev/null)" ]]; then
  if [[ "$WIPE" != "1" ]]; then
    echo "clog в $clog не пуст. {clog_hint}" >&2
    exit 2
  fi
fi
if [[ "$WIPE" == "1" ]]; then
  find "$DATA_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +
  find "$REDO_DIR" -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +
fi
mkdir -p "$DATA_DIR" "$REDO_DIR"
if [[ ! -e "$DATA_DIR/clog" ]]; then
  ln -s "$REDO_DIR" "$DATA_DIR/clog"
fi
cd "$HOME_PATH"
nohup {rendered} >>"$HOME_PATH/log/expand-start.log" 2>&1 &
echo $! >"$pidfile"
echo "observer pid $(cat "$pidfile")"
"""


@dataclass(frozen=True)
class ObproxyStart:
    ip: str
    roots: tuple[str, ...]
    appname: str
    home_path: str
    listen_port: int = 2883
    mysql_port: int = 2881
    prometheus_port: int = 2884

    def argv(self) -> list[str]:
        ip = str(parse_ip(self.ip))
        roots = [item for item in self.roots if not same_ip(item, ip)]
        if not roots:
            raise ExpandError("в --rs нужен хотя бы один observer")
        binary = f"{self.home_path.rstrip('/')}/bin/obproxy"
        rs = obproxy_rs_list(roots, self.mysql_port)
        cmd = [
            binary,
            "--listen_port",
            str(check_port(self.listen_port)),
            "--prometheus_listen_port",
            str(check_port(self.prometheus_port)),
            "--rs_list",
            rs,
            "--cluster_name",
            check_name(self.appname, "appname"),
        ]
        if is_ipv6(ip):
            cmd.extend(["-o", f"local_bound_ipv6_ip={ip}"])
        else:
            cmd.extend(["-o", f"local_bound_ip={ip}"])
        return cmd

    def haproxy_server_line(self, name: str) -> str:
        endpoint = server_endpoint(self.ip, self.listen_port)
        ident = check_zone(name)
        return f"server {ident} {endpoint} check"


def remote_obproxy_script(spec: ObproxyStart) -> str:
    cmd = spec.argv()
    binary = cmd[0]
    home = spec.home_path.rstrip("/")
    if not home or home == "/":
        raise ExpandError("home_path obproxy не может быть пустым или /")
    rendered = " ".join(shell_quote(part) for part in cmd)
    return f"""#!/bin/bash
set -euo pipefail
HOME_PATH={shell_quote(home)}
BINARY={shell_quote(binary)}
if [[ ! -x "$BINARY" ]]; then
  echo "нет бинаря $BINARY" >&2
  exit 1
fi
mkdir -p "$HOME_PATH/run" "$HOME_PATH/log"
pidfile="$HOME_PATH/run/obproxy.pid"
if [[ -f "$pidfile" ]]; then
  old="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$old" && -d "/proc/$old" ]] && tr '\\0' ' ' < "/proc/$old/cmdline" | grep -F -q -- "$BINARY"; then
    echo "obproxy уже запущен, pid $old" >&2
    exit 1
  fi
fi
cd "$HOME_PATH"
nohup {rendered} >>"$HOME_PATH/log/expand-start.log" 2>&1 &
echo $! >"$pidfile"
echo "obproxy pid $(cat "$pidfile")"
"""


def status_is_active(rows: str, ip: str) -> bool:
    """rows — вывод `SELECT SVR_IP, STATUS FROM oceanbase.DBA_OB_SERVERS` (TSV)."""
    want = parse_ip(ip)
    for line in rows.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            got = parse_ip(parts[0])
        except ExpandError:
            continue
        if got == want and parts[1].upper() == "ACTIVE":
            return True
    return False


def _observer_from_args(ns: argparse.Namespace) -> ObserverStart:
    return ObserverStart(
        ip=ns.ip,
        zone=ns.zone,
        roots=tuple(parse_ip_list(ns.rs)),
        appname=ns.appname,
        cluster_id=ns.cluster_id,
        home_path=ns.home,
        data_dir=ns.data_dir,
        redo_dir=ns.redo_dir,
        mysql_port=ns.mysql_port,
        rpc_port=ns.rpc_port,
        memory_limit=ns.memory_limit,
        system_memory=ns.system_memory,
        datafile_size=ns.datafile_size,
        log_disk_size=ns.log_disk_size,
        wipe=ns.wipe,
        bootstrap=bool(getattr(ns, "bootstrap", False)),
    )


def _add_observer_flags(parser: argparse.ArgumentParser, *, require_zone: bool) -> None:
    parser.add_argument("--ip", required=True)
    parser.add_argument("--zone", required=require_zone)
    parser.add_argument("--rs", required=True, help="уже работающие observer, через запятую")
    parser.add_argument("--appname", default="obcluster")
    parser.add_argument("--cluster-id", type=int, default=1)
    parser.add_argument("--home", default="/home/obadmin/observer")
    parser.add_argument("--data-dir", default="/data/1")
    parser.add_argument("--redo-dir", default="/data/log1")
    parser.add_argument("--mysql-port", type=int, default=2881)
    parser.add_argument("--rpc-port", type=int, default=2882)
    parser.add_argument("--memory-limit", default="64G")
    parser.add_argument("--system-memory", default="16G")
    parser.add_argument("--datafile-size", default="192G")
    parser.add_argument("--log-disk-size", default="192G")
    parser.add_argument("--wipe", action="store_true")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Команды расширения кластера без OCP и OBD")
    sub = parser.add_subparsers(dest="cmd", required=True)

    observer = sub.add_parser("observer-remote")
    _add_observer_flags(observer, require_zone=True)
    observer.add_argument("--bootstrap", action="store_true")

    sql = sub.add_parser("add-server-sql")
    _add_observer_flags(sql, require_zone=True)

    proxy = sub.add_parser("obproxy-remote")
    proxy.add_argument("--ip", required=True)
    proxy.add_argument("--rs", required=True)
    proxy.add_argument("--appname", default="obcluster")
    proxy.add_argument("--home", default="/home/obadmin/obproxy")
    proxy.add_argument("--listen-port", type=int, default=2883)
    proxy.add_argument("--mysql-port", type=int, default=2881)
    proxy.add_argument("--prometheus-port", type=int, default=2884)

    hap = sub.add_parser("haproxy-line")
    hap.add_argument("--ip", required=True)
    hap.add_argument("--name", required=True)
    hap.add_argument("--listen-port", type=int, default=2883)

    match = sub.add_parser("status-active")
    match.add_argument("--ip", required=True)

    boot = sub.add_parser("bootstrap-sql")
    boot.add_argument("--zone1", required=True)
    boot.add_argument("--zone2", required=True)
    boot.add_argument("--zone3", required=True)
    boot.add_argument("--rpc-port", type=int, default=2882)

    ns = parser.parse_args(argv)
    try:
        if ns.cmd == "observer-remote":
            sys.stdout.write(remote_observer_script(_observer_from_args(ns)))
        elif ns.cmd == "add-server-sql":
            print(_observer_from_args(ns).add_server_sql())
        elif ns.cmd == "obproxy-remote":
            spec = ObproxyStart(
                ip=ns.ip,
                roots=tuple(parse_ip_list(ns.rs)),
                appname=ns.appname,
                home_path=ns.home,
                listen_port=ns.listen_port,
                mysql_port=ns.mysql_port,
                prometheus_port=ns.prometheus_port,
            )
            sys.stdout.write(remote_obproxy_script(spec))
        elif ns.cmd == "haproxy-line":
            print(
                ObproxyStart(
                    ip=ns.ip,
                    roots=("203.0.113.10",),
                    appname="obcluster",
                    home_path="/tmp/obproxy",
                    listen_port=ns.listen_port,
                ).haproxy_server_line(ns.name)
            )
        elif ns.cmd == "status-active":
            rows = sys.stdin.read()
            if not status_is_active(rows, ns.ip):
                return 1
        elif ns.cmd == "bootstrap-sql":
            print(
                bootstrap_sql(
                    [("zone1", ns.zone1), ("zone2", ns.zone2), ("zone3", ns.zone3)],
                    rpc_port=ns.rpc_port,
                )
            )
        return 0
    except ExpandError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
