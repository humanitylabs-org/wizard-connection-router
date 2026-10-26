"""Real HTTP/WS boundary tests; deterministic in-process provider, stdlib only."""
import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import Router, Server, atomic_json


# Deliberately synthetic test capabilities, never owner/provider credentials.
CALLER = "test-reader-grant"
OUTGOING = "test-router-to-provider-grant"
KEY = base64.b64encode(b"0123456789abcdef").decode()


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            raise EOFError()
        data.extend(part)
    return bytes(data)


def frame(payload, opcode=2, masked=False):
    size = len(payload)
    marker = 0x80 if masked else 0
    if size < 126:
        head = bytes([0x80 | opcode, marker | size])
    elif size < 65536:
        head = bytes([0x80 | opcode, marker | 126]) + struct.pack("!H", size)
    else:
        head = bytes([0x80 | opcode, marker | 127]) + struct.pack("!Q", size)
    if not masked:
        return head + payload
    mask = b"test"
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def read_frame(stream):
    first, second = read_exact(stream, 2)
    size = second & 127
    if size == 126:
        size = struct.unpack("!H", read_exact(stream, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", read_exact(stream, 8))[0]
    mask = read_exact(stream, 4) if second & 128 else None
    data = read_exact(stream, size)
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return first & 15, data


class EchoServer(ThreadingHTTPServer):
    state: dict[str, Any]


class EchoHandler(BaseHTTPRequestHandler):
    server: EchoServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        state = self.server.state
        if self.path == "/api/connection":
            data = json.dumps({"contract": "wizard.ai-connection/2", "status": state.get("connection", "connected"), "models": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        state["requests"].append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        if not self.headers.get("Upgrade"):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if state["status"] != 101:
            self.close_connection = True
            self.send_response(state["status"])
            self.send_header("Content-Length", "18")
            self.send_header("Location", "http://must-not-follow.invalid/")
            self.end_headers()
            self.wfile.write(b"private error body")
            return
        if state["mode"] == "silent":
            state["release"].wait(2)
            return
        accept = base64.b64encode(hashlib.sha1((self.headers["Sec-WebSocket-Key"] +
                 "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if state["mode"] == "bad-accept":
            accept = "invalid"
        headers = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: recorder.v1\r\n"
                   "Set-Cookie: must-not-relay=1\r\n\r\n").encode()
        if state["mode"] == "oversize":
            headers = b"HTTP/1.1 101 Switching Protocols\r\nX-Huge: " + b"x" * 70000 + b"\r\n\r\n"
        self.close_connection = True
        try:
            # Send the first provider frame together with the handshake, before any client frame.
            self.connection.sendall(headers + frame(b'{"type":"ready"}', opcode=1))
            while True:
                opcode, payload = read_frame(self.rfile)
                self.connection.sendall(frame(payload, opcode))
                if opcode == 8:
                    break
        except (EOFError, OSError):
            pass
        finally:
            state["closed"].set()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state: dict[str, Any] = {"requests": [], "status": 101, "mode": "echo", "closed": threading.Event(),
                      "release": threading.Event()}
        self.provider = EchoServer(("127.0.0.1", 0), EchoHandler)
        self.provider.daemon_threads = True
        self.provider.state = self.state
        self.start_server(self.provider)
        self.target: dict[str, Any] = {"id": "transcriber", "provides": ["live-transcription"], "private": True,
                       "status": "running", "api": {"internal": f"http://127.0.0.1:{self.provider.server_port}"}}
        self.need: dict[str, Any] = {"slot": "live-transcription", "private_data": True, "recommend": "wizard-tinfoil",
                     "purpose": "Words appear as you speak"}
        self.app: dict[str, Any] = {"id": "reader", "requires": {"services": [self.need]}}
        self.write_apps()
        self.grants = self.root / "grants.json"
        self.write_grants({"transcriber": OUTGOING})
        self.router = Router(self.root / "apps.json", self.root / "data", self.grants, timeout=.5)
        self.server = Server(("127.0.0.1", 0), self.router)
        self.start_server(self.server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        def close():
            self.state["release"].set()
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(close)

    def write_apps(self, target=True):
        atomic_json(self.root / "apps.json", {"apps": [self.app] + ([self.target] if target else []),
                    "slots": {"live-transcription": {"default": "transcriber"}}})

    def write_grants(self, outgoing):
        atomic_json(self.grants, {"enforce": True,
                    "incoming": [{"from": "reader", "sha256": hashlib.sha256(CALLER.encode()).hexdigest()}],
                    "outgoing": outgoing})

    def request(self, path="/v1/needs", owner=False) -> tuple[int, Any]:
        req = Request(self.url + path, headers={} if owner else {"X-Wizard-Grant": CALLER})
        try:
            response = urlopen(req, timeout=2)
        except HTTPError as e:
            response = e
        with response:
            body = response.read()
            return response.status, json.loads(body) if response.headers.get_content_type() == "application/json" else body

    def connect(self, path="/v1/services/live-transcription/v1/live", grant=CALLER, extra=None, early=b"", method="GET"):
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=2)
        self.addCleanup(sock.close)
        stream = sock.makefile("rb")
        self.addCleanup(stream.close)
        headers = {"Host": "router.invalid", "Connection": "keep-alive, Upgrade", "Upgrade": "websocket",
                   "Sec-WebSocket-Key": KEY, "Sec-WebSocket-Version": "13", "Sec-WebSocket-Protocol": "recorder.v1",
                   "Sec-WebSocket-Extensions": "test-extension", "X-Wizard-App": "spoofed-app"}
        if grant is not None:
            headers["X-Wizard-Grant"] = grant
        headers.update(extra or {})
        sock.sendall((f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n").encode() + early)
        status = int(stream.readline().split()[1])
        response = {}
        while line := stream.readline():
            if line == b"\r\n":
                break
            k, v = line.decode().split(":", 1)
            response[k.lower()] = v.strip()
        body = json.loads(read_exact(stream, int(response["content-length"]))) if status != 101 else None
        return sock, stream, status, response, body

    def assert_upgraded(self, result):
        sock, stream, status, headers, body = result
        self.assertEqual(status, 101, body)
        self.assertEqual(headers["sec-websocket-protocol"], "recorder.v1")
        self.assertNotIn("set-cookie", headers)
        self.assertEqual(read_frame(stream), (1, b'{"type":"ready"}'))
        return sock, stream

    def test_needs_and_owner_needs_additive_fields(self):
        for path, owner in (("/v1/needs", False), ("/v1/owner/needs/reader", True)):
            with self.subTest(path=path):
                status, result = self.request(path, owner)
                self.assertEqual(status, 200)
                self.assertTrue(result["ready_to_open"])
                n = result["services"][0]
                self.assertEqual({k: n[k] for k in ("required", "purpose", "private_data", "recommend")},
                                 {"required": True, "purpose": self.need["purpose"], "private_data": True, "recommend": "wizard-tinfoil"})
                self.assertIn("fix_url", n)

    def test_required_model_blocks_and_optional_model_does_not(self):
        model: dict[str, Any] = {"tier": "fast", "tested_on": "missing", "purpose": "Live outline"}
        self.app["requires"]["models"] = {"outline": model}
        self.write_apps()
        result = self.request()[1]
        self.assertFalse(result["ready_to_open"])
        self.assertTrue(result["models"][0]["required"])
        model["optional"] = True
        self.write_apps()
        result = self.request()[1]
        self.assertTrue(result["ready_to_open"])
        self.assertFalse(result["models"][0]["required"])
        self.assertEqual(result["models"][0]["purpose"], "Live outline")

    def test_optional_service_retains_blocked_status_but_allows_opening(self):
        self.write_apps(target=False)
        self.assertFalse(self.request()[1]["ready_to_open"])
        self.need["optional"] = True
        self.write_apps(target=False)
        result = self.request()[1]
        self.assertTrue(result["ready_to_open"])
        self.assertEqual(result["services"][0]["status"], "blocked")
        self.assertFalse(result["services"][0]["required"])
        self.assertEqual(self.connect()[2], 409)  # Optional never bypasses call admission.

    def test_empty_needs_are_ready_and_agent_binding_is_unchanged(self):
        self.app["requires"] = {}
        self.app["agent_binding"] = {"required": True, "purpose": "Send recaps"}
        self.write_apps()
        result = self.request()[1]
        self.assertTrue(result["ready_to_open"])
        self.assertEqual(result["agent_binding"], self.app["agent_binding"])

    def test_missing_fields_have_explicit_defaults(self):
        self.need.clear()
        self.need["slot"] = "live-transcription"
        self.app["requires"]["models"] = {"outline": {"tier": "fast", "tested_on": "missing"}}
        self.write_apps()
        result = self.request()[1]
        n = result["services"][0]
        self.assertEqual((n["required"], n["purpose"], n["private_data"], n["recommend"]), (True, None, False, None))
        self.assertEqual((result["models"][0]["required"], result["models"][0]["purpose"]), (True, None))

    def test_private_service_blocks_http_and_ws_then_ready_with_private_provider(self):
        for marker in (False, None, "true", 1):
            with self.subTest(marker=marker):
                self.target["private"] = marker
                self.write_apps()
                result = self.request()[1]
                self.assertFalse(result["ready_to_open"])
                self.assertEqual(result["services"][0]["status"], "blocked")
                self.assertIn("private: true", result["services"][0]["error"]["message"])
                self.assertEqual(self.request("/v1/services/live-transcription/v1/live")[0], 409)
                self.assertEqual(self.connect()[2], 409)
                self.assertEqual(self.state["requests"], [])
        self.target["private"] = True
        self.write_apps()
        self.assertTrue(self.request()[1]["ready_to_open"])
        self.assertEqual(self.request("/v1/services/live-transcription/v1/live"), (200, b"ok"))
        self.assert_upgraded(self.connect())

    def test_privacy_is_rechecked_on_pinned_service(self):
        self.assertTrue(self.request()[1]["ready_to_open"])
        self.target["private"] = False
        self.write_apps()
        self.assertFalse(self.request()[1]["ready_to_open"])
        self.assertEqual(self.connect()[2], 409)
        self.assertEqual(self.router.routes()["services"]["reader/live-transcription"], "transcriber")

    def test_declaration_validation_rejects_invalid_additive_fields(self):
        for name, value in (("optional", "true"), ("private_data", 1), ("purpose", "x" * 81),
                            ("purpose", "two\nlines"), ("purpose", 5)):
            for kind in ("service", "model"):
                with self.subTest(name=name, value=value, kind=kind):
                    self.need.clear()
                    self.need["slot"] = "live-transcription"
                    self.app["requires"].pop("models", None)
                    need = self.need
                    if kind == "model":
                        need = {"tier": "fast", "tested_on": "model"}
                        self.app["requires"]["models"] = {"ask": need}
                    need[name] = value
                    self.write_apps()
                    self.assertEqual(self.request()[0], 503)

    def test_websocket_echo_both_directions_header_isolation_and_concurrency(self):
        sock, stream = self.assert_upgraded(self.connect(path="/v1/services/live-transcription/v1/live?language=en", extra={
            "Cookie": "do-not-forward", "Authorization": "do-not-forward", "Origin": "https://caller.invalid",
            "X-Forwarded-For": "192.0.2.1", "X-Forwarded-Host": "do-not-forward", "Forwarded": "do-not-forward"}))
        for payload in (b'{"type":"start"}', bytes(range(256)), b"a" * 65536):
            opcode = 1 if payload.startswith(b"{") else 2
            sock.sendall(frame(payload, opcode, masked=True))
            self.assertEqual(read_frame(stream), (opcode, payload))
        self.assertEqual(self.request("/health")[0], 200)  # Open tunnel never blocks the server.
        path, sent = self.state["requests"][0]
        self.assertEqual(path, "/v1/live?language=en")
        self.assertEqual(sent["x-wizard-grant"], OUTGOING)
        self.assertEqual(sent["sec-websocket-key"], KEY)
        self.assertEqual(sent["sec-websocket-version"], "13")
        self.assertEqual(sent["sec-websocket-protocol"], "recorder.v1")
        self.assertEqual(sent["sec-websocket-extensions"], "test-extension")
        self.assertEqual(set(sent), {"host", "x-wizard-grant", "upgrade", "connection", "sec-websocket-key",
                                    "sec-websocket-version", "sec-websocket-protocol", "sec-websocket-extensions"})
        sock.sendall(frame(b"\x03\xe8", opcode=8, masked=True))
        self.assertEqual(read_frame(stream), (8, b"\x03\xe8"))
        self.assertEqual(stream.read(1), b"")

    def test_client_frame_coalesced_with_upgrade_is_not_lost(self):
        _, stream = self.assert_upgraded(self.connect(early=frame(b"early audio", masked=True)))
        self.assertEqual(read_frame(stream), (2, b"early audio"))

    def test_slot_outgoing_grant_is_reused(self):
        self.write_grants({"slot:live-transcription": OUTGOING})
        self.assert_upgraded(self.connect())
        self.assertEqual(self.state["requests"][0][1]["x-wizard-grant"], OUTGOING)

    def test_websocket_missing_or_unknown_grant_refused_before_provider_contact(self):
        for grant in (None, "unknown-test-grant"):
            result = self.connect(grant=grant)
            self.assertEqual(result[2], 403)
            self.assertEqual(result[4]["code"], "blocked")
        self.assertEqual(self.state["requests"], [])

    def test_websocket_undeclared_service_and_traversal_refused(self):
        for path, expected in (("/v1/services/other/v1/live", 403),
                               ("/v1/services/live-transcription/%2e%2e/api/infer", 400)):
            self.assertEqual(self.connect(path=path)[2], expected)
        self.assertEqual(self.state["requests"], [])

    def test_signed_out_mixed_provider_needs_owner_until_connected(self):
        self.target["provides"].append("ai-connection")
        self.write_apps()
        self.state["connection"] = "not_connected"
        status, result = self.request("/v1/owner/needs/reader", True)
        self.assertEqual(status, 200)
        self.assertEqual(result["services"][0]["status"], "needs_owner")
        self.assertFalse(result["ready_to_open"])
        self.state["connection"] = "connected"
        status, result = self.request("/v1/owner/needs/reader", True)
        self.assertEqual(result["services"][0]["status"], "ready")
        self.assertTrue(result["ready_to_open"])

    def test_mixed_provider_only_allows_live_contract_not_inference(self):
        self.target["provides"].append("ai-connection")
        self.write_apps()
        self.assert_upgraded(self.connect())
        before = len(self.state["requests"])
        for path in ("api/infer", "api/connection", "v1/live/else", "v1/live%3f/../api/infer"):
            route = "/v1/services/live-transcription/" + path
            self.assertEqual(self.connect(path=route)[2], 403)
            self.assertEqual(self.request(route)[0], 403)
        self.assertEqual(len(self.state["requests"]), before)

    def test_non101_is_fault_json_and_never_redirects_or_relays_private_body(self):
        for status, code in ((200, "provider_error"), (302, "provider_error"), (401, "signed_out"),
                             (403, "expired"), (429, "limit"), (503, "provider_error")):
            with self.subTest(status=status):
                self.state["status"] = status
                result = self.connect()
                self.assertEqual(result[2], 502)
                self.assertEqual(set(result[4]), {"type", "code", "message", "fix_url"})
                self.assertEqual(result[4]["code"], code)
                self.assertNotIn("private error body", json.dumps(result[4]))
                self.assertNotIn("location", result[3])

    def test_invalid_client_handshake_refused_without_provider_contact(self):
        for extra in ({"Sec-WebSocket-Key": "bad"}, {"Sec-WebSocket-Version": "12"},
                      {"Connection": "close"}, {"Content-Length": "1"}, {"Transfer-Encoding": "chunked"}):
            self.assertEqual(self.connect(extra=extra)[2], 400)
        self.assertEqual(self.connect(method="POST")[2], 400)
        self.assertEqual(self.state["requests"], [])

    def test_bad_or_oversized_provider_handshake_is_fault_json(self):
        for mode in ("bad-accept", "oversize"):
            self.state["mode"] = mode
            result = self.connect()
            self.assertEqual(result[2], 502)
            self.assertEqual(result[4]["code"], "provider_error")

    def test_provider_handshake_has_a_deadline(self):
        self.state["mode"] = "silent"
        self.router.timeout = .05
        result = self.connect()
        self.assertEqual(result[2], 502)
        self.assertEqual(result[4]["code"], "unreachable")

    def test_idle_timeout_closes_both_sides(self):
        with patch("server.WS_IDLE_TIMEOUT", .08):
            _, stream = self.assert_upgraded(self.connect())
            self.assertEqual(stream.read(1), b"")
            self.assertTrue(self.state["closed"].wait(1))

    def test_max_session_closes_even_with_ongoing_bytes(self):
        with patch("server.WS_MAX_SESSION", .15), patch("server.WS_IDLE_TIMEOUT", 2):
            sock, stream = self.assert_upgraded(self.connect())
            count = 0
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    sock.sendall(frame(b"active", masked=True))
                    self.assertEqual(read_frame(stream), (2, b"active"))
                    count += 1
                    time.sleep(.02)
                except (EOFError, OSError):
                    break
            else:
                self.fail("maximum session did not close the active tunnel")
            self.assertGreater(count, 0)
            self.assertTrue(self.state["closed"].wait(1))

    def test_client_disconnect_closes_provider(self):
        sock, stream = self.assert_upgraded(self.connect())
        stream.close()
        sock.close()
        self.assertTrue(self.state["closed"].wait(1))


if __name__ == "__main__":
    unittest.main()
