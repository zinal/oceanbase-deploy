#!/usr/bin/env python3
"""RSA PKCS1 header used for obshell DAG API."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

from obshell_ocs import build_ocs_header, header_payload, rsa_encrypt_pkcs1  # noqa: E402


def test_header_payload_json() -> None:
    raw = header_payload("pwd", "/api/v1/task/dag/maintain/agent", b"\x00" * 32, ts="1")
    data = json.loads(raw)
    assert data["auth"] == "pwd"
    assert data["uri"] == "/api/v1/task/dag/maintain/agent"
    assert data["ts"] == "1"
    assert base64.b64decode(data["keys"]) == b"\x00" * 32


def test_rsa_pkcs1_roundtrip() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=512)
    pub_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.PKCS1,
    )
    pk_b64 = base64.b64encode(pub_der).decode("ascii")
    plain = header_payload("secret", "/api/v1/task/dag/maintain/agent?show_details=true", b"k" * 32, ts="99")
    token = rsa_encrypt_pkcs1(plain, pk_b64)
    blob = base64.b64decode(token)
    klen = key.key_size // 8
    out = b""
    for i in range(0, len(blob), klen):
        out += key.decrypt(blob[i : i + klen], padding.PKCS1v15())
    assert out == plain
    header = build_ocs_header(pk_b64, "secret", "/x", keys=b"z" * 32)
    assert isinstance(header, str) and len(header) > 20


def main() -> None:
    tests = [test_header_payload_json, test_rsa_pkcs1_roundtrip]
    for fn in tests:
        fn()
        print(f"OK {fn.__name__}")
    print(f"OK: {len(tests)} tests")


if __name__ == "__main__":
    main()
