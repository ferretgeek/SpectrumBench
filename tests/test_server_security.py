from __future__ import annotations

import json
import unittest

from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from stress_tool import server


class _FakeWebSocket:
    def __init__(self, origin: str, host: str = "127.0.0.1:18976") -> None:
        self.headers = {"origin": origin, "host": host}


class ServerSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        server._report_exports.clear()

    def tearDown(self) -> None:
        server._report_exports.clear()

    def test_same_origin_websocket_policy(self) -> None:
        self.assertTrue(server._websocket_origin_allowed(_FakeWebSocket("http://127.0.0.1:18976")))
        self.assertFalse(server._websocket_origin_allowed(_FakeWebSocket("https://attacker.example")))

    def test_security_headers_and_health_marker(self) -> None:
        client = TestClient(server.app, base_url="http://testserver")
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["app"], "SpectrumBench")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

        index = client.get("/")
        csp = index.headers["content-security-policy"]
        self.assertRegex(csp, r"script-src 'nonce-[A-Za-z0-9_-]+'")
        self.assertRegex(index.text, r'<script nonce="[A-Za-z0-9_-]+">')

    def test_websocket_accepts_same_origin_and_rejects_cross_origin(self) -> None:
        client = TestClient(server.app, base_url="http://testserver")
        with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as socket:
            message = socket.receive_json()
            self.assertEqual(message["type"], "init")
            self.assertNotIn("api_key", message["data"].get("startup_draft") or {})
            self.assertNotIn("base_url", message["data"].get("startup_draft") or {})
        with (
            self.assertRaises(WebSocketDisconnect) as rejected,
            client.websocket_connect(
                "/ws",
                headers={"origin": "https://attacker.example"},
            ),
        ):
            pass
        self.assertEqual(rejected.exception.code, 1008)

    def test_report_download_is_one_time_and_not_cached(self) -> None:
        server._report_exports["once"] = {
            "filename": "report.json",
            "content": json.dumps({"ok": True}),
        }
        client = TestClient(server.app, base_url="http://testserver")
        first = client.get("/download-report/once")
        second = client.get("/download-report/once")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(second.status_code, 404)


if __name__ == "__main__":
    unittest.main()
