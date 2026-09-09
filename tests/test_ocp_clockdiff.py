#!/usr/bin/env python3
"""OCP clock-diff system parameter helper tests."""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import ocp_clockdiff  # noqa: E402

SPA_HTML = "<!DOCTYPE html><html><head><title>OCP</title></head><body>login</body></html>"


class Handler(BaseHTTPRequestHandler):
    store = {
        "ocp.host.check.clock-diff.mode": "0",
        "ocp.host.check.clock-diff.enable": "true",
    }
    puts: list[dict] = []

    def _auth_ok(self) -> bool:
        return self.headers.get("Authorization", "").startswith("Basic ")

    def _send(self, code: int, payload: object) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if not self._auth_ok():
            self._send(401, {"error": "auth"})
            return
        if self.path.rstrip("/") != "/api/v2/compute/computeParameters":
            self._send(404, {"error": self.path})
            return
        items = [{"key": key, "value": value} for key, value in self.store.items()]
        self._send(200, {"data": items})

    def do_PUT(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        Handler.puts.append(body)
        key = str(body.get("key") or body.get("name") or "")
        value = str(body.get("value") or "")
        if key:
            Handler.store[key] = value
        self._send(200, {"data": body})

    def do_POST(self) -> None:  # noqa: N802
        self._send(404, {"error": self.path})

    def log_message(self, fmt: str, *args: object) -> None:
        return


class HtmlHandler(BaseHTTPRequestHandler):
    """Unknown API paths return the OCP SPA (HTTP 200 HTML), not JSON."""

    def _send_html(self) -> None:
        raw = SPA_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._send_html()

    def do_PUT(self) -> None:  # noqa: N802
        self._send_html()

    def do_POST(self) -> None:  # noqa: N802
        self._send_html()

    def log_message(self, fmt: str, *args: object) -> None:
        return


class LoginHandler(BaseHTTPRequestHandler):
    store = {
        "ocp.host.check.clock-diff.mode": "0",
        "ocp.host.check.clock-diff.enable": "true",
    }
    puts: list[dict] = []

    def _send_html(self) -> None:
        raw = SPA_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_json(self, code: int, payload: object, extra_headers: list[tuple[str, str]] | None = None) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        return "SESSION=ocp-ok" in cookie

    def do_GET(self) -> None:  # noqa: N802
        if not self._authed():
            self._send_html()
            return
        if self.path.rstrip("/") != "/api/v2/compute/computeParameters":
            self._send_json(404, {"error": self.path})
            return
        items = [{"key": key, "value": value} for key, value in self.store.items()]
        self._send_json(200, {"data": items})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if self.path.rstrip("/") == "/api/v2/iam/login":
            if body.get("username") == "admin" and body.get("password") == "secret":
                self._send_json(
                    200,
                    {"successful": True, "data": {"username": "admin"}},
                    extra_headers=[("Set-Cookie", "SESSION=ocp-ok; Path=/")],
                )
                return
            self._send_json(401, {"successful": False})
            return
        if not self._authed():
            self._send_html()
            return
        LoginHandler.puts.append(body)
        key = str(body.get("key") or body.get("name") or "")
        value = str(body.get("value") or "")
        if key:
            LoginHandler.store[key] = value
        self._send_json(200, {"data": body})

    def do_PUT(self) -> None:  # noqa: N802
        if not self._authed():
            self._send_html()
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        LoginHandler.puts.append(body)
        key = str(body.get("key") or body.get("name") or "")
        value = str(body.get("value") or "")
        if key:
            LoginHandler.store[key] = value
        self._send_json(200, {"data": body})

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[HTTPServer, str]:
    httpd = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    return httpd, f"http://127.0.0.1:{port}"


def test_apply_sets_mode_one() -> None:
    Handler.store = {
        "ocp.host.check.clock-diff.mode": "0",
        "ocp.host.check.clock-diff.enable": "true",
    }
    Handler.puts = []
    httpd, url = _serve(Handler)
    try:
        rc = ocp_clockdiff.apply_clockdiff_workaround(url, "admin", "secret", mode="1")
        assert rc == 0
        assert Handler.store["ocp.host.check.clock-diff.mode"] == "1"
        assert Handler.puts
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_html_spa_does_not_raise() -> None:
    httpd, url = _serve(HtmlHandler)
    try:
        rc = ocp_clockdiff.apply_clockdiff_workaround(url, "admin", "secret", mode="1")
        assert rc == 1
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_login_cookie_then_put() -> None:
    LoginHandler.store = {
        "ocp.host.check.clock-diff.mode": "0",
        "ocp.host.check.clock-diff.enable": "true",
    }
    LoginHandler.puts = []
    httpd, url = _serve(LoginHandler)
    try:
        rc = ocp_clockdiff.apply_clockdiff_workaround(url, "admin", "secret", mode="1")
        assert rc == 0
        assert LoginHandler.store["ocp.host.check.clock-diff.mode"] == "1"
        assert LoginHandler.puts
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_parse_html_is_none() -> None:
    assert ocp_clockdiff._parse_body(SPA_HTML) is None
    assert ocp_clockdiff._parse_body("") is None
    assert ocp_clockdiff._parse_body("{not json") is None
    assert ocp_clockdiff._parse_body('{"data":[]}') == {"data": []}
    assert ocp_clockdiff._is_html(SPA_HTML)


def test_prepare_script_has_setcap() -> None:
    text = (ROOT / "scripts" / "lib" / "prepare-ocp-host.sh").read_text(encoding="utf-8")
    assert "setcap cap_net_raw,cap_sys_nice+ep" in text
    assert "CLOCKDIFF_ONLY" in text
    assert "/usr/bin/clockdiff" in text
    assert "/usr/lib/oceanbase/clockdiff.real" in text
    assert 'exec "$REAL" -o "$@"' in text
    register = (ROOT / "scripts" / "09-ocp-register.sh").read_text(encoding="utf-8")
    assert "ocp_clockdiff.py" in register
    assert "--clockdiff-only" in register
    assert "exit 0" in register
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "ocp-clockdiff" in deploy
    deploy_case = deploy.split("\n  deploy)")[1].split("\n  tenant)")[0]
    assert "run_ocp_clockdiff_if_enabled" in deploy_case
    all_case = deploy.split("\n  all)")[1].split("\n  destroy)")[0]
    assert "run_ocp_clockdiff_if_enabled" in all_case
    cluster = (ROOT / "scripts" / "04-deploy-cluster.sh").read_text(encoding="utf-8")
    assert "--clockdiff-only" in cluster


def main() -> None:
    test_parse_html_is_none()
    print("OK test_parse_html_is_none")
    test_apply_sets_mode_one()
    print("OK test_apply_sets_mode_one")
    test_html_spa_does_not_raise()
    print("OK test_html_spa_does_not_raise")
    test_login_cookie_then_put()
    print("OK test_login_cookie_then_put")
    test_prepare_script_has_setcap()
    print("OK test_prepare_script_has_setcap")
    print("OK: 5 tests")


if __name__ == "__main__":
    main()
