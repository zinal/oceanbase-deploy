#!/usr/bin/env python3
"""Подпись запросов к obshell (X-OCS-Header) и GET последнего DAG агента.

Голый curl к /api/v1/task/dag/maintain/agent даёт 400 Request.Header.NotFound:
нужен гибридный заголовок (RSA PKCS1 поверх JSON auth/ts/uri/keys).
Алгоритм — публичная документация OceanBase «API 混合加密».
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URI = "/api/v1/task/dag/maintain/agent"
DEFAULT_URI_DETAILS = "/api/v1/task/dag/maintain/agent?show_details=true"


def _b64decode_key(pk: str) -> bytes:
    raw = pk.strip()
    if raw.startswith("-----"):
        return raw.encode("ascii")
    try:
        return base64.b64decode(raw)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"public_key не base64: {exc}") from exc


def _load_public_key_cryptography(key_bytes: bytes):
    from cryptography.hazmat.primitives import serialization

    if key_bytes.startswith(b"-----"):
        return serialization.load_pem_public_key(key_bytes)
    try:
        return serialization.load_der_public_key(key_bytes)
    except ValueError:
        pem = (
            b"-----BEGIN RSA PUBLIC KEY-----\n"
            + base64.encodebytes(key_bytes)
            + b"-----END RSA PUBLIC KEY-----\n"
        )
        return serialization.load_pem_public_key(pem)


def rsa_encrypt_pkcs1(plaintext: bytes, public_key_b64: str) -> str:
    """Сегментное RSA PKCS1v15, как в официальном примере (блок 512/8-11)."""
    key_bytes = _b64decode_key(public_key_b64)
    try:
        from cryptography.hazmat.primitives.asymmetric import padding as asy_padding

        pub = _load_public_key_cryptography(key_bytes)
    except ImportError:
        return _rsa_encrypt_openssl(plaintext, key_bytes)

    chunk = pub.key_size // 8 - 11
    blocks = [plaintext[i : i + chunk] for i in range(0, len(plaintext), chunk)] or [b""]
    out = b"".join(pub.encrypt(block, asy_padding.PKCS1v15()) for block in blocks)
    return base64.b64encode(out).decode("ascii")


def _rsa_encrypt_openssl(plaintext: bytes, key_bytes: bytes) -> str:
    if key_bytes.startswith(b"-----"):
        pem = key_bytes
    else:
        pem = (
            b"-----BEGIN RSA PUBLIC KEY-----\n"
            + base64.encodebytes(key_bytes)
            + b"-----END RSA PUBLIC KEY-----\n"
        )
    with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as fh:
        fh.write(pem)
        pem_path = fh.name
    try:
        meta = _run(
            ["openssl", "rsa", "-pubin", "-in", pem_path, "-text", "-noout"],
            check=False,
        )
        bits = 512
        for line in meta.splitlines():
            if "Public-Key:" in line or "RSA Public-Key:" in line:
                digits = "".join(ch for ch in line if ch.isdigit())
                if digits:
                    bits = int(digits)
                    break
        chunk = bits // 8 - 11
        if chunk < 1:
            chunk = 53
        ciphertext = b""
        blocks = [plaintext[i : i + chunk] for i in range(0, len(plaintext), chunk)] or [b""]
        for block in blocks:
            ciphertext += _run_bin(
                [
                    "openssl",
                    "pkeyutl",
                    "-encrypt",
                    "-pubin",
                    "-inkey",
                    pem_path,
                    "-pkeyopt",
                    "rsa_padding_mode:pkcs1",
                ],
                stdin=block,
            )
        return base64.b64encode(ciphertext).decode("ascii")
    finally:
        try:
            os.unlink(pem_path)
        except OSError:
            pass


def _run(cmd: list[str], check: bool = True) -> str:
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or str(cmd))
    return proc.stdout


def _run_bin(cmd: list[str], stdin: bytes) -> bytes:
    import subprocess

    proc = subprocess.run(cmd, input=stdin, capture_output=True, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        raise RuntimeError(err or str(cmd))
    return proc.stdout


def header_payload(auth: str, uri: str, keys: bytes, ts: str | None = None) -> bytes:
    if ts is None:
        ts = str(int(time.time()) + 100000)
    body = {
        "auth": auth,
        "ts": ts,
        "uri": uri,
        "keys": base64.b64encode(keys).decode("ascii"),
    }
    return json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def build_ocs_header(public_key_b64: str, auth: str, uri: str, keys: bytes | None = None) -> str:
    if keys is None:
        keys = os.urandom(32)
    return rsa_encrypt_pkcs1(header_payload(auth, uri, keys), public_key_b64)


def http_get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 8) -> tuple[int, Any]:
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    ctx = ssl._create_unverified_context()  # noqa: S323 — obshell на стенде часто без доверенного TLS
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            parsed: Any = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"raw": raw}
        return exc.code, parsed


def fetch_public_key(host: str, port: int, timeout: int = 8) -> str:
    for scheme in ("http", "https"):
        status, body = http_get_json(f"{scheme}://{host}:{port}/api/v1/secret", timeout=timeout)
        if status == 200:
            pk = ((body.get("data") or {}) if isinstance(body, dict) else {}).get("public_key")
            if pk:
                return str(pk)
    raise RuntimeError(f"не удалось получить /api/v1/secret с {host}:{port}")


def fetch_agent_dag(
    host: str,
    port: int,
    passwords: list[str],
    timeout: int = 8,
) -> tuple[int, Any, str]:
    """GET last maintain DAG. Возвращает (http_status, json, used_uri)."""
    pk = fetch_public_key(host, port, timeout=timeout)
    last: tuple[int, Any, str] = (0, {}, "")
    uris = [DEFAULT_URI_DETAILS, DEFAULT_URI]
    seen_pwd: set[str] = set()
    for pwd in passwords:
        if pwd in seen_pwd:
            continue
        seen_pwd.add(pwd)
        for uri in uris:
            header = build_ocs_header(pk, pwd, uri)
            for scheme in ("http", "https"):
                url = f"{scheme}://{host}:{port}{uri}"
                status, body = http_get_json(
                    url,
                    headers={"X-OCS-Header": header, "Accept": "application/json"},
                    timeout=timeout,
                )
                last = (status, body, uri)
                if status == 200 and isinstance(body, dict) and body.get("data"):
                    return last
                if status == 200:
                    return last
    return last


def _print_dag_summary(payload: Any) -> None:
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not data:
        err = (payload or {}).get("error") if isinstance(payload, dict) else None
        print("нет data:", (err or {}).get("message") if isinstance(err, dict) else payload)
        print("DAG_STATE=MISSING")
        return
    states = {0: "PENDING", 1: "READY", 2: "RUNNING", 3: "FAILED", 4: "SUCCEED"}

    def st(val: Any) -> str:
        if isinstance(val, int):
            return states.get(val, str(val))
        return str(val or "?")

    state = st(data.get("state"))
    print(f"DAG_STATE={state}")
    print(
        "name=%s  state=%s  stage=%s/%s  operator=%s"
        % (data.get("name"), state, data.get("stage"), data.get("max_stage"), data.get("operator"))
    )
    print("id=%s  start=%s  end=%s" % (data.get("id"), data.get("start_time"), data.get("end_time")))
    for node in data.get("nodes") or []:
        print("  node %s  state=%s  stage=%s" % (node.get("name"), st(node.get("state")), node.get("stage")))
        for sub in node.get("sub_nodes") or node.get("subNodes") or []:
            print("    sub %s  state=%s" % (sub.get("name"), st(sub.get("state"))))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="obshell DAG с X-OCS-Header")
    sub = parser.add_subparsers(dest="cmd", required=True)

    dag = sub.add_parser("dag", help="GET /api/v1/task/dag/maintain/agent")
    dag.add_argument("--host", required=True)
    dag.add_argument("--port", type=int, default=2886)
    dag.add_argument(
        "--password",
        action="append",
        default=[],
        help="пароль root@sys; можно повторить (пустой и ocp.root_password)",
    )
    dag.add_argument("--timeout", type=int, default=8)
    dag.add_argument("--raw", action="store_true", help="печатать JSON как есть")

    args = parser.parse_args(argv)
    passwords = list(args.password) if args.password else [""]
    try:
        status, body, uri = fetch_agent_dag(args.host, args.port, passwords, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"DAG_STATE=ERROR\n{exc}", file=sys.stderr)
        return 2
    if args.raw or not isinstance(body, dict):
        print(json.dumps(body, ensure_ascii=False, indent=2) if not isinstance(body, str) else body)
    else:
        print(f"http={status} uri={uri}")
        _print_dag_summary(body)
        if status != 200:
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                print("error:", err.get("errCode") or err.get("code"), err.get("message"))
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
