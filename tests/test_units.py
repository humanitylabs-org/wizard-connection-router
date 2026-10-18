import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from projection import mirror_routes
from server import Fault, Router, atomic_json, endpoint, events


class UnitTests(unittest.TestCase):
    def test_multiline_sse_and_comments(self):
        raw = b': comment\r\nevent: done\r\ndata: {"model": "exact",\r\ndata: "usage": {}}\r\n\r\n'
        self.assertEqual(list(events(io.BytesIO(raw))), [{"type": "done", "model": "exact", "usage": {}}])

    def test_sse_conflicting_type_and_incomplete_frame(self):
        with self.assertRaises(ValueError):
            list(events(io.BytesIO(b'event: done\ndata: {"type":"delta"}\n\n')))
        with self.assertRaises(Fault):
            list(events(io.BytesIO(b'data: {"type":"done"}\n')))

    def test_endpoint_never_embeds_userinfo_or_non_http(self):
        for url in ("file:///etc/passwd", "http://user:password@example.test/", "ftp://example.test"):
            with self.assertRaises(Fault):
                endpoint({"api": {"url": url}})
        self.assertEqual(endpoint({"mcp": {"internal": "http://provider:8080/mcp"}}), "http://provider:8080")

    def test_atomic_write_failure_preserves_prior_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            atomic_json(root / "apps.json", {"apps": []})
            router = Router(root / "apps.json", root / "data")
            router.order({"providers": ["claude"]})
            before = router.state_file.read_bytes()
            with patch("server.os.replace", side_effect=OSError("fixture full disk")):
                with self.assertRaises(OSError):
                    router.order({"providers": ["chatgpt"]})
            self.assertEqual(router.routes()["provider_order"], ["claude"])
            self.assertEqual(router.state_file.read_bytes(), before)
            self.assertEqual([p.name for p in router.state_file.parent.iterdir()], ["routes.json"])

    def test_registry_projection_preserves_fields_strips_noncontract_data(self):
        registry = {"schema": 1, "apps": [{"id": "reader", "unknown": "preserved"}], "grants": []}
        route = {"app": "reader", "feature": "ask", "provider": "claude", "model": "sonnet",
                 "tested_on": "sonnet", "chosen": "match", "at": 1, "token": "must-not-copy"}
        projected = mirror_routes(registry, {"routes": [route, {**route, "app": "uninstalled"}]})
        self.assertNotIn("routes", registry)
        self.assertEqual(projected["apps"], registry["apps"])
        self.assertEqual(len(projected["routes"]), 1)
        self.assertNotIn("token", json.dumps(projected))
