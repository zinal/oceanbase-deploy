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

    def log_message(self, fmt: str, *args: object) -> None:
        return


def test_apply_sets_mode_one() -> None:
    Handler.store = {
        "ocp.host.check.clock-diff.mode": "0",
        "ocp.host.check.clock-diff.enable": "true",
    }
    Handler.puts = []
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        rc = ocp_clockdiff.apply_clockdiff_workaround(
            f"http://127.0.0.1:{port}", "admin", "secret", mode="1"
        )
        assert rc == 0
        assert Handler.store["ocp.host.check.clock-diff.mode"] == "1"
        assert Handler.puts
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_prepare_script_has_setcap() -> None:
    text = (ROOT / "scripts" / "lib" / "prepare-ocp-host.sh").read_text(encoding="utf-8")
    assert "setcap cap_net_raw,cap_sys_nice+ep" in text
    assert 'CLOCKDIFF_ONLY:-' in text or 'CLOCKDIFF_ONLY:-}' in text or 'CLOCKDIFF_ONLY' in text
    assert "/usr/bin/clockdiff" in text
    register = (ROOT / "scripts" / "09-ocp-register.sh").read_text(encoding="utf-8")
    assert "ocp_clockdiff.py" in register
    assert "--clockdiff-only" in register
    deploy = (ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    assert "ocp-clockdiff" in deploy


def main() -> None:
    test_apply_sets_mode_one()
    print("OK test_apply_sets_mode_one")
    test_prepare_script_has_setcap()
    print("OK test_prepare_script_has_setcap")
    print("OK: 2 tests")


if __name__ == "__main__":
    main()
