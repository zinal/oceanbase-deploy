#!/usr/bin/env python3
"""OCP system parameters for host clock-diff (takeover Pre check for create host).

Default mode 0 runs `clockdiff <ip>` (ICMP TIMESTAMP). Yandex Cloud and
unprivileged OCP JVM often make that exit 1. Mode 1 is `clockdiff -o`
(IP timestamps). Mode 2 is `clockdiff -o1`.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

CLOCK_DIFF_MODE_KEY = "ocp.host.check.clock-diff.mode"
CLOCK_DIFF_ENABLE_KEY = "ocp.host.check.clock-diff.enable"
# 1 = clockdiff -o (IP timestamp), community workaround when ICMP TIMESTAMP is blocked.
DEFAULT_MODE = "1"


def _request(
    url: str,
    *,
    user: str,
    password: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: int = 15,
) -> tuple[int, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    creds = f"{user}:{password}".encode("utf-8")
    req.add_header("Authorization", "Basic " + __import__("base64").b64encode(creds).decode("ascii"))
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            parsed: Any = json.loads(raw) if raw.strip() else None
            return resp.status, parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {"error": raw}
        except json.JSONDecodeError:
            parsed = {"error": raw}
        return exc.code, parsed


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def list_parameters(base: str, user: str, password: str) -> list[dict[str, Any]]:
    candidates = (
        "/api/v2/compute/computeParameters",
        "/api/v2/compute/parameters",
        "/api/v2/system/parameters",
        "/api/v2/profiles/parameters",
    )
    last_err = ""
    for path in candidates:
        status, payload = _request(base.rstrip("/") + path, user=user, password=password)
        if status >= 400:
            last_err = f"{path} -> {status} {payload}"
            continue
        data = _unwrap(payload)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict) and isinstance(data.get("contents"), list):
            return [item for item in data["contents"] if isinstance(item, dict)]
        if isinstance(data, dict):
            return [data]
    raise RuntimeError(last_err or "no computeParameters endpoint accepted GET")


def _item_key(item: dict[str, Any]) -> str:
    for field in ("key", "name", "paramName", "parameterName"):
        value = item.get(field)
        if value:
            return str(value)
    return ""


def _item_value(item: dict[str, Any]) -> str:
    for field in ("value", "currentValue", "paramValue"):
        value = item.get(field)
        if value is not None:
            return str(value)
    return ""


def put_parameter(
    base: str,
    user: str,
    password: str,
    key: str,
    value: str,
    template: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    body: dict[str, Any] = dict(template or {})
    if "name" in body and "key" not in body:
        body["name"] = key
        body["value"] = value
    else:
        body["key"] = key
        body["name"] = body.get("name", key)
        body["value"] = value
    paths_methods = (
        ("/api/v2/compute/computeParameters", "PUT"),
        ("/api/v2/compute/computeParameters", "POST"),
        ("/api/v2/compute/parameters", "PUT"),
        ("/api/v2/system/parameters", "PUT"),
        (f"/api/v2/compute/computeParameters/{quote(key, safe='')}", "PUT"),
    )
    last = (0, None)
    for path, method in paths_methods:
        status, payload = _request(
            base.rstrip("/") + path,
            user=user,
            password=password,
            method=method,
            body=body,
        )
        last = (status, payload)
        if 200 <= status < 300:
            return last
    return last


def apply_clockdiff_workaround(base: str, user: str, password: str, mode: str = DEFAULT_MODE) -> int:
    """Set clock-diff.mode (and disable the check if mode cannot be written). Return 0 on success."""
    items = list_parameters(base, user, password)
    by_key = {_item_key(item): item for item in items if _item_key(item)}
    mode_item = by_key.get(CLOCK_DIFF_MODE_KEY)
    enable_item = by_key.get(CLOCK_DIFF_ENABLE_KEY)

    if mode_item is not None:
        current = _item_value(mode_item)
        print(f"{CLOCK_DIFF_MODE_KEY}={current}")
        if current == mode:
            print(f"ok {CLOCK_DIFF_MODE_KEY} already {mode}")
            return 0
        status, payload = put_parameter(base, user, password, CLOCK_DIFF_MODE_KEY, mode, mode_item)
        if 200 <= status < 300:
            print(f"set {CLOCK_DIFF_MODE_KEY}={mode}")
            return 0
        print(f"WARN: PUT {CLOCK_DIFF_MODE_KEY} -> {status} {payload}", file=sys.stderr)

    if enable_item is not None:
        status, payload = put_parameter(
            base, user, password, CLOCK_DIFF_ENABLE_KEY, "false", enable_item
        )
        if 200 <= status < 300:
            print(f"set {CLOCK_DIFF_ENABLE_KEY}=false (ICMP clockdiff unavailable)")
            return 0
        print(f"WARN: PUT {CLOCK_DIFF_ENABLE_KEY} -> {status} {payload}", file=sys.stderr)

    print(
        "Не удалось сменить параметр через API. В UI OCP: "
        "Системные параметры → "
        f"{CLOCK_DIFF_MODE_KEY}={mode} или {CLOCK_DIFF_ENABLE_KEY}=false",
        file=sys.stderr,
    )
    return 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "list"))
    parser.add_argument("--url", required=True, help="http://<OCP_IP>:8080")
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", required=True)
    parser.add_argument("--mode", default=DEFAULT_MODE, help="clock-diff.mode: 0 ICMP, 1 -o, 2 -o1")
    args = parser.parse_args()
    if args.command == "list":
        for item in list_parameters(args.url, args.user, args.password):
            key = _item_key(item)
            if "clock" in key.lower() or "clock" in json.dumps(item).lower():
                print(json.dumps(item, ensure_ascii=False))
        return
    sys.exit(apply_clockdiff_workaround(args.url, args.user, args.password, args.mode))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
