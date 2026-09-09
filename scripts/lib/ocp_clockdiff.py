#!/usr/bin/env python3
"""OCP system parameters for host clock-diff (takeover Pre check for create host).

Default mode 0 runs `clockdiff <ip>` (ICMP TIMESTAMP). Yandex Cloud and
unprivileged OCP JVM often make that exit 1. Mode 1 is `clockdiff -o`
(IP timestamps). Mode 2 is `clockdiff -o1`.

Unknown `/api/v2/...` paths on ocp-server-ce often return the SPA HTML
(HTTP 200). Never treat that as JSON.
"""

from __future__ import annotations

import argparse
import base64
import http.cookiejar
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

PARAMETER_GET_PATHS = (
    "/api/v2/compute/computeParameters",
    "/api/v2/compute/parameters",
    "/api/v2/system/parameters",
    "/api/v2/profiles/parameters",
    "/api/v2/config/properties",
    "/api/v2/compute/configProperties",
)

LOGIN_PATHS = (
    "/api/v2/iam/login",
    "/api/v2/login",
)

UI_HINT = (
    "В UI OCP: Системные параметры → "
    f"{CLOCK_DIFF_MODE_KEY}=1 или {CLOCK_DIFF_ENABLE_KEY}=false"
)


def _looks_like_json(raw: str) -> bool:
    text = (raw or "").lstrip()
    return bool(text) and text[0] in "{["


def _parse_body(raw: str) -> Any:
    if not _looks_like_json(raw):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _is_html(raw: str) -> bool:
    text = (raw or "").lstrip().lower()
    return text.startswith("<!doctype") or text.startswith("<html") or "<head" in text[:800]


class OcpClient:
    def __init__(self, base: str, user: str, password: str, timeout: int = 15) -> None:
        self.base = base.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=ctx),
        )
        self.bearer = ""
        self.logged_in = False

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.bearer:
            headers["Authorization"] = f"Bearer {self.bearer}"
        else:
            creds = f"{self.user}:{self.password}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(creds).decode("ascii")
        csrf = self._csrf_token()
        if csrf:
            headers["X-XSRF-TOKEN"] = csrf
            headers["X-CSRF-TOKEN"] = csrf
        return headers

    def _csrf_token(self) -> str:
        for cookie in self.jar:
            if cookie.name.lower() in ("xsrf-token", "csrf-token", "x-xsrf-token"):
                return cookie.value
        return ""

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | list[Any] | None = None,
    ) -> tuple[int, Any, str]:
        """Return (status, parsed_json_or_None, raw). HTML/empty → parsed None."""
        url = path if path.startswith("http") else self.base + path
        headers = self._auth_headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return int(resp.status), _parse_body(raw), raw
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            return int(exc.code), _parse_body(raw), raw
        except urllib.error.URLError as exc:
            return 0, {"error": str(exc.reason)}, str(exc.reason)

    def login(self) -> bool:
        if self.logged_in:
            return True
        bodies = (
            {"username": self.user, "password": self.password},
            {"user": self.user, "password": self.password},
        )
        for path in LOGIN_PATHS:
            for body in bodies:
                status, payload, raw = self.request(path, method="POST", body=body)
                if status < 200 or status >= 300:
                    continue
                if payload is None:
                    continue
                data = payload.get("data") if isinstance(payload, dict) else payload
                token = ""
                if isinstance(data, dict):
                    for key in ("token", "accessToken", "access_token", "idToken"):
                        value = data.get(key)
                        if value:
                            token = str(value)
                            break
                if token:
                    self.bearer = token
                ok = bool(token) or bool(list(self.jar))
                if not ok and isinstance(payload, dict) and payload.get("successful") is True:
                    ok = True
                if ok:
                    self.logged_in = True
                    print(f"OCP login {path} ok")
                    return True
        return False


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


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


def _as_items(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap(payload)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for field in ("contents", "list", "items"):
            block = data.get(field)
            if isinstance(block, list):
                return [item for item in block if isinstance(item, dict)]
        if _item_key(data):
            return [data]
    return []


def list_parameters(client: OcpClient) -> list[dict[str, Any]]:
    last = ""
    for path in PARAMETER_GET_PATHS:
        status, payload, raw = client.request(path)
        if payload is None:
            kind = "HTML" if _is_html(raw) else "empty/non-JSON"
            last = f"{path} -> {status} {kind}"
            continue
        if status >= 400:
            last = f"{path} -> {status}"
            continue
        items = _as_items(payload)
        if items:
            return items
        last = f"{path} -> {status} no items"
    if last:
        print(f"WARN: OCP parameters API: {last}", file=sys.stderr)
    return []


def put_parameter(
    client: OcpClient,
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
        ("/api/v2/config/properties", "PUT"),
        (f"/api/v2/compute/computeParameters/{quote(key, safe='')}", "PUT"),
    )
    last: tuple[int, Any] = (0, None)
    for path, method in paths_methods:
        status, payload, raw = client.request(path, method=method, body=body)
        if payload is None:
            last = (status, {"error": "HTML" if _is_html(raw) else "non-JSON"})
            continue
        last = (status, payload)
        if 200 <= status < 300:
            return last
    return last


def apply_clockdiff_workaround(base: str, user: str, password: str, mode: str = DEFAULT_MODE) -> int:
    """Set clock-diff.mode (and disable the check if mode cannot be written). Return 0 on success.

    Never raises: HTML login/SPA pages must not abort `ocp-clockdiff` after setcap.
    """
    try:
        client = OcpClient(base, user, password)
        client.login()
        items = list_parameters(client)
        by_key = {_item_key(item): item for item in items if _item_key(item)}
        mode_item = by_key.get(CLOCK_DIFF_MODE_KEY)
        enable_item = by_key.get(CLOCK_DIFF_ENABLE_KEY)

        if mode_item is not None:
            current = _item_value(mode_item)
            print(f"{CLOCK_DIFF_MODE_KEY}={current}")
            if current == mode:
                print(f"ok {CLOCK_DIFF_MODE_KEY} already {mode}")
                return 0
            status, payload = put_parameter(client, CLOCK_DIFF_MODE_KEY, mode, mode_item)
            if 200 <= status < 300:
                print(f"set {CLOCK_DIFF_MODE_KEY}={mode}")
                return 0
            print(f"WARN: PUT {CLOCK_DIFF_MODE_KEY} -> {status} {payload}", file=sys.stderr)

        if enable_item is not None:
            status, payload = put_parameter(client, CLOCK_DIFF_ENABLE_KEY, "false", enable_item)
            if 200 <= status < 300:
                print(f"set {CLOCK_DIFF_ENABLE_KEY}=false (ICMP clockdiff unavailable)")
                return 0
            print(f"WARN: PUT {CLOCK_DIFF_ENABLE_KEY} -> {status} {payload}", file=sys.stderr)

        print(
            "WARN: OCP API не отдала JSON системных параметров (часто SPA/login HTML). "
            "clockdiff на ОС уже настроен — это не сбой setcap. " + UI_HINT,
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # pragma: no cover
        print(f"WARN: OCP parameters API: {exc}", file=sys.stderr)
        print(UI_HINT, file=sys.stderr)
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
        client = OcpClient(args.url, args.user, args.password)
        client.login()
        for item in list_parameters(client):
            key = _item_key(item)
            if "clock" in key.lower() or "clock" in json.dumps(item).lower():
                print(json.dumps(item, ensure_ascii=False))
        return
    sys.exit(apply_clockdiff_workaround(args.url, args.user, args.password, args.mode))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        print(f"WARN: {exc}", file=sys.stderr)
        sys.exit(1)
