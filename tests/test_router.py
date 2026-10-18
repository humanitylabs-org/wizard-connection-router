"""HTTP integration tests use disposable registries, grants and fake providers."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import Fault, Router, Server, atomic_json
from tests.fake_provider import FakeProvider, model


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.apps_file, self.grants_file = self.root / "apps.json", self.root / "grants.json"
        self.claude = self.provider("claude", [model("claude-sonnet", abilities=["web_search"])])
        self.chatgpt = self.provider("chatgpt", [model("gpt-standard", abilities=["web_search"])])
        self.need = {"tier": "standard", "tested_on": "claude-sonnet", "abilities": ["web_search"]}
        self.app = {"id": "reader", "requires": {"models": {"ask": self.need}, "services": []}}
        self.write_apps([self.claude.app()])
        self.write_grants(False, [{"from": "reader", "sha256": hashlib.sha256(b"default-fixture").hexdigest()}])
        self.router = Router(self.apps_file, self.root / "data", self.grants_file, timeout=1)
        self.server = Server(("127.0.0.1", 0), self.router)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def provider(self, name, models):
        p = FakeProvider(name, models)
        self.addCleanup(p.close)
        return p

    def write_apps(self, providers, extra=None, slots=None):
        self.registry = {"schema": 1, "apps": [self.app, *providers, *(extra or [])], "slots": slots or {}}
        atomic_json(self.apps_file, self.registry)

    def write_grants(self, enforce, incoming=None, outgoing=None):
        atomic_json(self.grants_file, {"enforce": enforce, "incoming": incoming or [], "outgoing": outgoing or {}})

    def request(self, path, body=None, method=None, headers=None, owner=False) -> tuple[int, Any]:
        hdr = {} if owner else {"X-Wizard-App": "reader", "X-Wizard-Grant": "default-fixture"}
        hdr.update(headers or {})
        if body is not None:
            hdr["Content-Type"] = "application/json"
        req = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                      headers=hdr, method=method or ("POST" if body is not None else "GET"))
        try:
            res = urlopen(req, timeout=4)
        except HTTPError as e:
            res = e
        with res:
            raw = res.read()
            if res.headers.get_content_type() == "text/event-stream":
                data = [json.loads(line[6:]) for line in raw.decode().splitlines() if line.startswith("data: ")]
            elif res.headers.get_content_type() == "application/json":
                data = json.loads(raw) if raw else None
            else:
                data = raw
            return res.status, data

    def needs(self):
        status, data = self.request("/v1/needs")
        self.assertEqual(status, 200, data)
        return data["models"][0]

    def infer(self, headers=None, body=None):
        return self.request("/v1/models/ask/run", body or {"messages": [{"role": "user", "content": "question"}]}, headers=headers)

    def approve(self, provider="chatgpt", selected="gpt-standard", scope="install"):
        return self.request("/v1/owner/approve", {"app": "reader", "feature": "ask", "provider": provider,
                                                "model": selected, "scope": scope}, owner=True)

    def test_claude_only_exact_match_and_provenance(self):
        self.assertEqual(self.needs()["status"], "ready")
        status, events = self.infer()
        self.assertEqual(status, 200)
        self.assertEqual(events[-1], {"type": "done", "provider": "claude", "model": "claude-sonnet",
                                      "tier": "standard", "tested_on": "claude-sonnet", "chosen": "match",
                                      "usage": {"input": 3, "output": 1}})
        sent = json.loads(self.claude.requests[-1]["body"])
        self.assertEqual(sent["abilities"], ["web_search"])
        self.assertTrue(sent["stream"])

    def test_chatgpt_only_needs_owner(self):
        self.write_apps([self.chatgpt.app()])
        need = self.needs()
        self.assertEqual(need["status"], "needs_owner")
        self.assertEqual(need["offered"]["model"], "gpt-standard")
        self.assertEqual(self.infer()[1]["code"], "needs_owner")
        self.assertFalse(any(r["path"] == "/api/infer" for r in self.chatgpt.requests))

    def test_both_exact_tested_model_beats_provider_order(self):
        self.write_apps([self.chatgpt.app(), self.claude.app()])
        self.request("/v1/owner/provider-order", {"providers": ["chatgpt", "claude"]}, owner=True)
        self.assertEqual(self.needs()["route"]["provider"], "claude")

    def test_neither_box(self):
        self.write_apps([])
        need = self.needs()
        self.assertEqual(need["status"], "needs_owner")
        self.assertNotIn("offered", need)
        self.assertIn("fix_url", self.infer()[1])

    def test_swap_never_waits_until_tested_connected(self):
        self.need["swap"] = "never"
        self.write_apps([self.chatgpt.app()])
        self.assertEqual(self.needs()["status"], "blocked")
        self.assertNotIn("offered", self.needs())
        self.assertEqual(self.approve()[0], 409)
        self.write_apps([self.chatgpt.app(), self.claude.app()])
        self.assertEqual(self.needs()["status"], "ready")

    def test_install_swap_recorded_once_reused_and_survives_restart(self):
        self.write_apps([self.chatgpt.app()])
        self.assertEqual(self.needs()["status"], "needs_owner")
        self.assertEqual(self.approve()[0], 200)
        before = self.router.state_file.read_bytes()
        for _ in range(2):
            self.assertEqual(self.infer()[1][-1]["chosen"], "owner-swap")
        self.assertEqual(before, self.router.state_file.read_bytes())
        reloaded = Router(self.apps_file, self.root / "data", self.grants_file)
        self.assertEqual(reloaded.needs("reader")["models"][0]["route"]["model"], "gpt-standard")
        self.assertEqual(len(reloaded.routes()["routes"]), 1)

    def test_one_run_override_not_persisted_and_consumed(self):
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        self.needs()
        before = self.router.state_file.read_bytes()
        status, approval = self.approve(scope="run")
        self.assertEqual(status, 200)
        headers = {"X-Wizard-Override": approval["override"]}
        self.assertEqual(self.infer(headers)[1][-1]["provider"], "chatgpt")
        self.assertEqual(self.infer(headers)[0], 403)
        self.assertEqual(self.infer()[1][-1]["provider"], "claude")
        self.assertEqual(before, self.router.state_file.read_bytes())
        self.assertEqual(Router(self.apps_file, self.root / "data", self.grants_file).overrides, {})

    def test_run_override_bound_to_app_feature_and_expiry(self):
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        token = self.approve(scope="run")[1]["override"]
        self.router.overrides[token]["expires"] = 0
        self.assertEqual(self.infer({"X-Wizard-Override": token})[0], 403)

    def test_run_override_not_reusable_under_concurrency(self):
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        token = self.approve(scope="run")[1]["override"]
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.infer({"X-Wizard-Override": token})[0])) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(results), [200, 403])

    def test_effective_unknown_model_fails_closed(self):
        self.claude.effective_model = "unknown-model"
        status, events = self.infer()
        self.assertEqual(status, 200)
        self.assertEqual(events[-1]["code"], "provider_error")
        self.assertFalse(any(e["type"] == "done" for e in events))

    def test_missing_effective_model_and_bad_stream_fail_closed(self):
        for mode in ("no-model", "missing", "malformed", "oversize"):
            with self.subTest(mode=mode):
                self.claude.mode = mode
                events = self.infer()[1]
                self.assertEqual(events[-1]["code"], "provider_error")
                self.assertFalse(any(e["type"] == "done" for e in events))

    def test_sse_text_arrives_before_provider_done(self):
        self.claude.mode = "pause"
        req = Request(self.url + "/v1/models/ask/run", data=b'{"messages":[]}', headers={"X-Wizard-App": "reader"})
        with urlopen(req, timeout=2) as res:
            self.assertEqual(res.readline(), b"event: delta\n")
            self.assertIn(b"hello", res.readline())
            self.assertFalse(self.claude.release.is_set())
            self.claude.release.set()
            self.assertIn(b'"type": "done"', res.read())

    def test_no_runtime_failover_signed_out_expired_limit(self):
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        self.needs()
        for state, code in (("not_connected", "signed_out"), ("expired", "expired"), ("error", "limit")):
            with self.subTest(state=state):
                self.claude.status, self.claude.error = state, {"code": "limit"}
                self.assertEqual(self.infer()[1]["code"], code)
                self.assertEqual(self.needs()["route"]["provider"], "claude")
        self.assertFalse(any(r["path"] == "/api/infer" for r in self.chatgpt.requests))

    def test_http_provider_errors_and_error_event(self):
        for code, expected in ((401, "signed_out"), (403, "expired"), (429, "limit"), (500, "provider_error")):
            self.claude.infer_status = code
            self.assertEqual(self.infer()[1][-1]["code"], expected)
        self.claude.infer_status, self.claude.mode = 200, "error"
        self.assertEqual(self.infer()[1][-1]["code"], "limit")

    def test_unreachable_routed_provider_does_not_switch(self):
        self.needs()
        self.write_apps([self.chatgpt.app()])
        self.assertEqual(self.infer()[1]["code"], "unreachable")

    def test_grants_report_only_logs_unknown_without_token(self):
        with self.assertLogs("router", level="WARNING") as logs:
            status, _ = self.request("/v1/needs", headers={"X-Wizard-Grant": "fixture-secret"})
        self.assertEqual(status, 200)
        self.assertNotIn("fixture-secret", "".join(logs.output))
        self.assertIn("report-only", "".join(logs.output))

    def test_grants_enforced_identity_ignores_spoofed_app_and_reloads(self):
        row = {"from": "reader", "sha256": hashlib.sha256(b"fixture-token").hexdigest()}
        self.write_grants(True, [row])
        self.assertEqual(self.request("/v1/needs")[0], 403)
        headers = {"X-Wizard-Grant": "fixture-token", "X-Wizard-App": "other-app"}
        self.assertEqual(self.request("/v1/needs", headers=headers)[1]["app"], "reader")
        self.write_grants(True)
        self.assertEqual(self.request("/v1/needs", headers=headers)[0], 403)

    def test_invalid_grants_do_not_disable_enforcement(self):
        self.grants_file.write_text("{")
        self.assertEqual(self.request("/v1/needs")[0], 503)
        self.grants_file.unlink()
        self.assertEqual(self.request("/v1/needs")[0], 503)

    def test_outgoing_grant_is_router_grant_not_caller(self):
        self.claude.grant = "provider-fixture"
        self.write_grants(False, outgoing={"claude": "provider-fixture"})
        self.assertEqual(self.infer({"X-Wizard-Grant": "caller-fixture"})[1][-1]["type"], "done")
        self.assertEqual(self.claude.requests[-1]["headers"]["X-Wizard-Grant"], "provider-fixture")
        self.write_grants(False, outgoing={"slot:ai-connection": "provider-fixture"})
        self.assertEqual(self.infer()[1][-1]["type"], "done")

    def test_malformed_apps_json_is_safe_error_not_empty_registry(self):
        for data in ("broken", "[]", '{"apps":{}}', '{"apps":[{"id":"reader","requires":{"models":[]}}]}',
                     '{"apps":[{"id":"reader","requires":{"models":{"ask":{"tier":"unknown"}}}}]}'):
            with self.subTest(data=data):
                self.apps_file.write_text(data)
                status, result = self.request("/v1/needs")
                self.assertEqual(status, 503)
                self.assertEqual(result["code"], "blocked")
        self.assertEqual(self.request("/health", owner=True)[0], 200)

    def test_private_data_only_owner_marked_providers_even_after_approval(self):
        self.need["private_data"] = True
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        self.assertEqual(self.needs()["status"], "needs_owner")
        self.assertNotIn("offered", self.needs())
        self.assertEqual(self.approve()[0], 409)
        self.write_apps([self.claude.app(), self.chatgpt.app(private=True)])
        self.assertEqual(self.needs()["offered"]["provider"], "chatgpt")
        self.assertEqual(self.approve()[0], 200)
        self.write_apps([self.claude.app(), self.chatgpt.app(private=False)])
        self.assertEqual(self.infer()[1]["code"], "blocked")

    def test_missing_abilities_excludes_offers_and_blocks_existing(self):
        self.claude.models[0]["abilities"] = []
        self.assertEqual(self.needs()["status"], "needs_owner")
        self.claude.models[0]["abilities"] = ["web_search"]
        self.needs()
        self.assertEqual(self.infer(body={"messages": [], "abilities": ["shell"]})[0], 400)
        self.claude.models[0]["abilities"] = []
        self.assertEqual(self.infer()[1]["code"], "blocked")

    def test_provider_order_and_standing_in_tier(self):
        self.need["tested_on"] = "missing-model"
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        self.request("/v1/owner/provider-order", {"providers": ["chatgpt", "claude"]}, owner=True)
        self.assertEqual(self.needs()["offered"]["provider"], "chatgpt")
        self.need["tier"] = "frontier"
        self.write_apps([self.chatgpt.app()])
        self.assertTrue(self.needs()["offered"]["standing_in"])
        self.assertEqual(self.needs()["offered"]["tier"], "standard")
        self.assertEqual(self.approve()[0], 200)
        self.assertEqual(self.infer()[1][-1]["tier"], "standard")

    def test_tested_on_update_invalidates_only_affected_feature(self):
        self.app["requires"]["models"]["other"] = dict(self.need)
        self.write_apps([self.claude.app()])
        self.needs()
        original = self.router.routes()["routes"]
        self.need["tested_on"] = "new-model"
        self.write_apps([self.claude.app()])
        self.assertEqual(self.needs()["status"], "needs_owner")
        remaining = self.router.routes()["routes"]
        self.assertEqual(remaining, [r for r in original if r["feature"] == "other"])

    def test_owner_routes_refuse_proxy_and_app_headers(self):
        for header in ("tailscale-user-login", "X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Ray", "Origin", "X-Wizard-Grant", "X-Wizard-App"):
            with self.subTest(header=header):
                self.assertEqual(self.request("/v1/owner/routes", headers={header: "fixture"}, owner=True)[0], 403)
                self.assertEqual(self.request("/v1/owner/approve", {}, headers={header: "fixture"}, owner=True)[0], 403)
        self.assertEqual(self.request("/v1/owner/routes", owner=True)[0], 200)

    def test_owner_approval_invalid_payload_and_order(self):
        for body in ({}, {"app": []}, {"app": "reader", "feature": "ask", "provider": "chatgpt", "model": "gpt", "scope": "forever"}):
            self.assertEqual(self.request("/v1/owner/approve", body, owner=True)[0], 400)
        self.assertEqual(self.request("/v1/owner/provider-order", {"providers": ["chatgpt", "chatgpt"]}, owner=True)[0], 400)

    def test_service_forwards_method_body_query_and_router_grant(self):
        service = {"id": "scraper", "status": "running", "provides": ["web-scraping"], "mcp": {"url": self.chatgpt.url + "/mcp"}}
        self.app["requires"]["services"] = [{"slot": "web-scraping", "optional": True, "recommend": "scraper"}]
        self.write_apps([self.claude.app()], [service], {"web-scraping": {"default": "scraper"}})
        self.write_grants(False, outgoing={"scraper": "service-fixture"})
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            status, result = self.request("/v1/services/web-scraping/fetch?q=one", {"input": "hello"}, method=method,
                                          headers={"Authorization": "do-not-forward"})
            self.assertEqual(status, 200)
            self.assertEqual(result["method"], method)
            self.assertEqual(result["path"], "/fetch?q=one")
            self.assertEqual(result["grant"], "service-fixture")
            self.assertIsNone(result["authorization"])
        self.assertEqual(self.request("/v1/services/other/fetch")[0], 403)
        self.assertEqual(self.request("/v1/services/web-scraping/%2e%2e/evil")[0], 400)

    def test_service_proxy_cannot_bypass_model_approval(self):
        self.app["requires"]["services"] = [{"slot": "ai-connection"}]
        self.write_apps([self.claude.app()], slots={"ai-connection": {"default": "claude"}})
        status, result = self.request("/v1/services/ai-connection/api/infer", {"model": "unapproved"})
        self.assertEqual(status, 403)
        self.assertEqual(result["code"], "blocked")
        self.assertFalse(any(r["path"] == "/api/infer" for r in self.claude.requests))

    def test_service_route_does_not_switch_silently(self):
        service = {"id": "scraper", "status": "running", "provides": ["web-scraping"], "mcp": {"url": self.chatgpt.url + "/mcp"}}
        self.app["requires"]["services"] = [{"slot": "web-scraping"}]
        self.write_apps([], [service], {"web-scraping": {"default": "scraper"}})
        self.request("/v1/needs")
        self.write_apps([], [], {})
        self.assertEqual(self.request("/v1/services/web-scraping/fetch")[1]["code"], "blocked")

    def test_mcp_has_only_readonly_needs_tool(self):
        status, result = self.request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(status, 200)
        self.assertEqual([t["name"] for t in result["result"]["tools"]], ["connection_needs"])
        _, result = self.request("/mcp", {"id": 2, "method": "tools/call", "params": {"name": "connection_needs"}})
        self.assertEqual(json.loads(result["result"]["content"][0]["text"])["app"], "reader")

    def test_request_cannot_override_model_or_provider(self):
        for key in ("model", "provider", "chosen", "tools"):
            self.assertEqual(self.infer(body={"messages": [], key: "injected"})[0], 400)

    def test_invalid_provider_model_metadata_is_not_matched(self):
        self.claude.models = [{"id": "claude-sonnet", "tier": "frontier"}]
        self.assertEqual(self.needs()["status"], "needs_owner")
        self.assertEqual(self.approve("claude", "claude-sonnet")[0], 409)

    def test_atomic_state_has_no_temporary_files_or_tokens(self):
        self.needs()
        self.assertEqual(sorted(p.name for p in self.router.state_file.parent.iterdir()), ["routes.json"])
        state = json.loads(self.router.state_file.read_text())
        self.assertEqual(state["schema"], 1)
        self.assertNotIn("token", self.router.state_file.read_text())
        self.assertEqual(self.router.state_file.stat().st_mode & 0o777, 0o600)

    def test_owner_pick_changes_existing_match(self):
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        self.needs()
        self.assertEqual(self.approve()[1]["route"]["chosen"], "owner-pick")
        self.assertEqual(self.infer()[1][-1]["chosen"], "owner-pick")

    def test_install_swap_does_not_return_to_newly_connected_tested_model(self):
        self.write_apps([self.chatgpt.app()])
        self.approve()
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        self.assertEqual(self.needs()["route"]["provider"], "chatgpt")
        self.assertEqual(self.infer()[1][-1]["provider"], "chatgpt")

    def test_one_run_override_is_app_and_feature_bound(self):
        other = {"id": "other-app", "requires": {"models": {"ask": dict(self.need)}}}
        self.app["requires"]["models"]["second"] = dict(self.need)
        self.write_apps([self.claude.app(), self.chatgpt.app()], [other])
        token = self.approve(scope="run")[1]["override"]
        with self.assertRaises(Fault) as raised:
            self.router.prepare_run("other-app", "ask", {"messages": []}, token)
        self.assertEqual(raised.exception.status, 403)
        with self.assertRaises(Fault):
            self.router.prepare_run("reader", "second", {"messages": []}, token)
        self.assertEqual(self.infer({"X-Wizard-Override": token})[1][-1]["type"], "done")

    def test_tested_on_update_rematches_exact_model(self):
        self.needs()
        self.need["tested_on"] = "gpt-standard"
        self.write_apps([self.claude.app(), self.chatgpt.app()])
        self.assertEqual(self.infer()[1][-1]["provider"], "chatgpt")
        self.assertEqual(self.needs()["route"]["chosen"], "match")

    def test_unknown_app_and_undeclared_feature_are_blocked(self):
        self.write_grants(False)
        self.assertEqual(self.request("/v1/needs", headers={"X-Wizard-App": "not-installed"})[0], 403)
        self.assertEqual(self.request("/v1/models/other/run", {"messages": []})[0], 404)

    def test_agent_binding_passed_through_without_agent_dependency(self):
        self.app["agent_binding"] = {"required": True, "purpose": "Where recaps go"}
        self.write_apps([self.claude.app()])
        self.assertEqual(self.request("/v1/needs")[1]["agent_binding"], self.app["agent_binding"])
        self.assertEqual(self.infer()[1][-1]["type"], "done")

    def test_mcp_initialize_and_notifications(self):
        status, response = self.request("/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(status, 200)
        self.assertEqual(response["result"]["serverInfo"]["name"], "wizard-connection-router")
        self.assertEqual(self.request("/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})[0], 202)

    def test_invalid_persisted_state_refuses_start(self):
        self.router.state_file.parent.mkdir()
        self.router.state_file.write_text("{\"schema\":1}")
        with self.assertRaises(ValueError):
            Router(self.apps_file, self.root / "data", self.grants_file)


if __name__ == "__main__":
    unittest.main()
