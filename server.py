"""Credential-free connection router. Python stdlib; no agent runtime imports."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import yaml
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

VERSION = "0.1.7"
APP_ID = "wizard-connection-router"
TIERS = ("fast", "standard", "frontier")
CODES = {"signed_out", "expired", "limit", "unreachable", "provider_error", "needs_owner", "blocked"}
FIX = "/wizard-os-home/?open=wizard-connection-router"
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_BODY = 1024 * 1024
LOG = logging.getLogger("router")


class Fault(Exception):
    def __init__(self, code, message, fix_url=FIX, status=409):
        super().__init__(message)
        self.code, self.fix_url, self.status = code, fix_url, status

    def payload(self):
        return {"type": "error", "code": self.code, "message": str(self), "fix_url": self.fix_url}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # Never send app content/grants to a redirected destination.


HTTP = build_opener(ProxyHandler({}), NoRedirect())


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".router-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_json(path):
    with Path(path).open() as file:
        return json.load(file)


def identifier(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def strings(value):
    return isinstance(value, list) and all(isinstance(x, str) for x in value)


def endpoint(app):
    api, mcp, view = (app.get(k) or {} for k in ("api", "mcp", "view"))
    raw = api.get("internal") or api.get("url") or view.get("upstream") or mcp.get("internal") or mcp.get("url")
    try:
        if not isinstance(raw, str):
            raise ValueError()
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError()
        _ = parsed.port  # Validate malformed/out-of-range ports before opening a socket.
    except ValueError:
        raise Fault("unreachable", "Provider has no usable address.", status=503) from None
    # API paths are fixed by the provider contract; metadata may name a view or MCP path.
    return f"{parsed.scheme}://{parsed.netloc}"


def fix_for(app):
    return f"/wizard-os-home/?open={app['id']}"


class Router:
    def __init__(self, apps_file, data_dir, grants_file=None, timeout=10):
        self.apps_file = Path(apps_file)
        self.state_file = Path(data_dir) / "routes.json"
        self.grants_file = Path(grants_file) if grants_file else None
        self.timeout = timeout
        self.lock = threading.RLock()
        self.overrides = {}
        if self.state_file.exists():
            self.state = load_json(self.state_file)
            if not isinstance(self.state, dict) or self.state.get("schema") != 1:
                raise ValueError("Invalid routing table; refusing to reset owner choices")
            if not isinstance(self.state.get("routes"), dict) or not isinstance(self.state.get("services"), dict) or not strings(self.state.get("provider_order")):
                raise ValueError("Invalid routing table")
            required = {"app", "feature", "provider", "model", "tested_on", "chosen", "at"}
            if any(not isinstance(r, dict) or not required <= r.keys() for r in self.state["routes"].values()):
                raise ValueError("Invalid route record")
        else:
            self.state = {"schema": 1, "routes": {}, "services": {}, "provider_order": []}

        self.saved_state = copy.deepcopy(self.state)

    def save(self):
        try:
            atomic_json(self.state_file, self.state)
        except OSError:
            self.state = copy.deepcopy(self.saved_state)
            raise
        self.saved_state = copy.deepcopy(self.state)

    def grants(self):
        if self.grants_file is None:
            return {"enforce": False, "incoming": [], "outgoing": {}}
        try:
            data = load_json(self.grants_file)
            if not isinstance(data, dict) or type(data.get("enforce")) is not bool or not isinstance(data.get("incoming"), list) or not isinstance(data.get("outgoing", {}), dict):
                raise ValueError()
            for row in data["incoming"]:
                # spec/PERMISSIONS.md layer 2: the gateway's row is `host:agent-service` (outside the app-id namespace).
                frm = row.get("from") if isinstance(row, dict) else None
                host = isinstance(frm, str) and frm.startswith("host:") and identifier(frm[5:])
                if not isinstance(row, dict) or not (identifier(frm) or host) or not re.fullmatch(r"[0-9a-f]{64}", row.get("sha256", "")):
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError):
            raise Fault("blocked", "Grants configuration is unavailable or invalid.", status=503) from None

    def caller(self, headers, anonymous=False):
        """The calling app id (or a `host:` row such as the Agent Service gateway's).

        `anonymous=True` (MCP only): in report-only mode a caller that names no app gets None instead of a 400, so
        data-free MCP methods (initialize, tools/list) work for the agent gateway, which sends no X-Wizard-App and,
        until the App Manager writes its key, no grant. Enforcement is unchanged: an unknown grant is still 403."""
        grants = self.grants()
        digest = hashlib.sha256(headers.get("X-Wizard-Grant", "").encode()).hexdigest()
        for row in grants["incoming"]:
            if headers.get("X-Wizard-Grant") and hmac.compare_digest(digest, row["sha256"]):
                return row["from"]
        if grants["enforce"]:
            raise Fault("blocked", "Unknown app grant.", status=403)
        LOG.warning("Unknown app grant (report-only); caller identity is unverified")
        # Transitional identity hint only. Never honored when enforcement is enabled.
        app = headers.get("X-Wizard-App", "")
        if not identifier(app):
            if anonymous and not app:
                return None
            raise Fault("blocked", "Report-only callers must identify X-Wizard-App.", status=400)
        return app

    def outgoing(self, app, slot=None):
        grants = self.grants()
        token = grants.get("outgoing", {}).get(app)
        if token is None and slot:
            token = grants.get("outgoing", {}).get("slot:" + slot)
        if token is not None and not isinstance(token, str):
            raise Fault("blocked", "Invalid outgoing grant.", status=503)
        return {"X-Wizard-Grant": token} if token else {}

    def registry(self):
        try:
            data = load_json(self.apps_file)
            if not isinstance(data, dict) or not isinstance(data.get("apps"), list) or not isinstance(data.get("slots", {}), dict):
                raise ValueError()
            apps = {}
            for app in data["apps"]:
                if not isinstance(app, dict) or not identifier(app.get("id")) or app["id"] in apps:
                    raise ValueError()
                if not strings(app.get("provides", [])):
                    raise ValueError()
                for field in ("api", "mcp", "view"):
                    if app.get(field) is not None and not isinstance(app[field], dict):
                        raise ValueError()
                app = copy.deepcopy(app)
                recipes = os.environ.get("WIZARD_RECIPES_DIR")
                if recipes and "requires" not in app:
                    app_id, _, store = app.get("urn", "").partition(":")
                    if app_id == app["id"] and identifier(store):
                        root = Path(recipes) / store / "apps" / app_id
                        try:
                            # Ignore a staged recipe until WAM records that exact release.
                            if (root / ".wizard-commit").read_text().strip() == app.get("commit"):
                                source = (root / "wizard.md").read_text()
                                if len(source) > 262144 or not source.startswith("---\n"):
                                    raise ValueError()
                                manifest = yaml.safe_load(source.split("---", 2)[1])
                                # Legacy agent/service manifests can omit version and
                                # have no declarations. Do not let those unrelated apps
                                # block every feature; fence any actual declaration.
                                if "requires" in manifest:
                                    if manifest.get("id") != app_id or str(manifest.get("version")) != app.get("version"):
                                        raise ValueError()
                                    app["requires"] = manifest["requires"]
                        except FileNotFoundError:
                            pass
                # Owner deployment addresses belong in server-side config, not source
                # or the credential-free registry. They never override uninstalled apps.
                address = self.grants().get("provider_endpoints", {}).get(app["id"])
                if address:
                    app["api"] = {"internal": address}
                    endpoint(app)  # Fail closed on malformed owner addresses.
                req = app.get("requires", {})
                if not isinstance(req, dict) or not isinstance(req.get("models", {}), dict) or not isinstance(req.get("services", []), list):
                    raise ValueError()
                for feature, need in req.get("models", {}).items():
                    if not identifier(feature) or not isinstance(need, dict) or need.get("tier") not in TIERS or not isinstance(need.get("tested_on"), str) or not need["tested_on"] or need.get("swap", "ask") not in ("ask", "never") or type(need.get("private_data", False)) is not bool or not strings(need.get("abilities", [])):
                        raise ValueError()
                for need in req.get("services", []):
                    if not isinstance(need, dict) or not identifier(need.get("slot")):
                        raise ValueError()
                apps[app["id"]] = app
            return data, apps
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            raise Fault("blocked", "Installed apps registry is unavailable or malformed.", status=503) from None

    def providers(self, apps):
        result = {}
        for app_id, app in apps.items():
            if "ai-connection" not in app.get("provides", []):
                continue
            p = {"app": app, "models": [], "status": "error", "error": "unreachable"}
            result[app_id] = p
            if app.get("status") != "running":
                continue
            try:
                request = Request(endpoint(app) + "/api/connection", headers=self.outgoing(app_id, "ai-connection"))
                with HTTP.open(request, timeout=self.timeout) as res:
                    raw = res.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ValueError()
                data = json.loads(raw)
                if not isinstance(data, dict) or data.get("contract") not in ("wizard.ai-connection/1", "wizard.ai-connection/2"):
                    raise ValueError()
                models = data.get("models", [])
                if not isinstance(models, list):
                    raise ValueError()
                seen = set()
                for model in models:
                    if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not model["id"] or model["id"] in seen or not isinstance(model.get("label"), str) or model.get("tier") not in TIERS or not strings(model.get("abilities", [])):
                        raise ValueError()
                    seen.add(model["id"])
                status = data.get("status")
                if status not in ("connected", "not_connected", "expired", "error"):
                    raise ValueError()
                error = data.get("error") or {}
                code = error.get("code", "provider_error")
                p.update(models=models, status=status, error=code if code in CODES else "provider_error")
            except HTTPError as e:
                p["error"] = {401: "signed_out", 403: "expired", 429: "limit"}.get(e.code, "provider_error")
                e.close()
            except (OSError, URLError, Fault):
                pass
            except (ValueError, TypeError, AttributeError):
                p["error"] = "provider_error"
        return result

    def eligible(self, need, p, model):
        return (p["status"] == "connected" and
                (not need.get("private_data") or p["app"].get("private") is True) and
                set(need.get("abilities", [])) <= set(model.get("abilities", [])))

    def candidates(self, need, providers):
        order = self.state["provider_order"]
        candidates = [(pid, m) for pid, p in providers.items() for m in p["models"] if self.eligible(need, p, m)]
        # Same-tier first, then strongest available; provider order breaks ties.
        return sorted(candidates, key=lambda c: (c[1]["tier"] != need["tier"],
                      0 if c[1]["tier"] == need["tier"] else -TIERS.index(c[1]["tier"]),
                      order.index(c[0]) if c[0] in order else len(order), c[0], c[1]["id"]))

    def record(self, app, feature, need, provider, model, chosen):
        return dict(app=app, feature=feature, provider=provider, model=model,
                    tested_on=need["tested_on"], chosen=chosen, at=int(time.time()))

    def check_route(self, route, need, providers):
        p = providers.get(route["provider"])
        if not p:
            raise Fault("unreachable", "Routed provider is not installed.")
        fix = fix_for(p["app"])
        status = p["status"]
        if status != "connected":
            code = {"not_connected": "signed_out", "expired": "expired"}.get(status, p["error"])
            raise Fault(code, "Reconnect or repair the routed provider; no automatic switch.", fix)
        m = next((m for m in p["models"] if m["id"] == route["model"]), None)
        if not m or not self.eligible(need, p, m):
            raise Fault("blocked", "Routed model no longer satisfies this feature.", fix)
        if need.get("swap") == "never" and route["model"] != need["tested_on"]:
            raise Fault("blocked", "This feature requires its tested model.")
        return p, m

    def context(self, app):
        data, apps = self.registry()
        if app not in apps:
            raise Fault("blocked", "Calling app is not installed.", status=403)
        return data, apps, apps[app].get("requires", {}), self.providers(apps)

    def model_need(self, app, feature, need, providers):
        key = app + "/" + feature
        route = self.state["routes"].get(key)
        if route and route["tested_on"] != need["tested_on"]:
            del self.state["routes"][key]
            route = None
            self.save()
        if route is None:
            match = next(((pid, m) for pid, m in self.candidates(need, providers) if m["id"] == need["tested_on"]), None)
            if match:
                route = self.record(app, feature, need, match[0], match[1]["id"], "match")
                self.state["routes"][key] = route
                self.save()
        info = {"feature": feature, **need, "route": route, "fix_url": FIX}
        if route:
            try:
                _, model = self.check_route(route, need, providers)
                info.update(status="ready", tier=model["tier"])
            except Fault as e:
                info.update(status="blocked", error=e.payload(), fix_url=e.fix_url)
        else:
            strict = need.get("swap", "ask") == "never"
            info["status"] = "blocked" if strict else "needs_owner"
            options = self.candidates(need, providers)
            if options and not strict:
                pid, m = options[0]
                info["offered"] = {"provider": pid, "model": m["id"], "tier": m["tier"],
                                   "standing_in": m["tier"] != need["tier"],
                                   "warning": "Results may differ from what the developer tested."}
        return info

    def service_need(self, app, need, data, apps):
        key, slot = app + "/" + need["slot"], need["slot"]
        chosen = self.state["services"].get(key)
        if chosen is None:
            selection = data.get("slots", {}).get(slot, {})
            if not isinstance(selection, dict):
                raise Fault("blocked", "Malformed slot declaration.", status=503)
            chosen = selection.get("default")
            if chosen in apps and slot in apps[chosen].get("provides", []) and apps[chosen].get("status") == "running":
                self.state["services"][key] = chosen
                self.save()
            else:
                chosen = None
        target = apps.get(chosen, {})
        ready = target.get("status") == "running" and slot in target.get("provides", [])
        return {**need, "provider": chosen, "status": "ready" if ready else "blocked", "fix_url": fix_for(target) if target else FIX}

    def needs(self, app):
        with self.lock:
            data, apps, req, providers = self.context(app)
            return {"app": app, "agent_binding": apps[app].get("agent_binding"),
                    "models": [self.model_need(app, f, n, providers) for f, n in req.get("models", {}).items()],
                    "services": [self.service_need(app, n, data, apps) for n in req.get("services", [])]}

    def provider_summary(self, providers):
        # Names, states and model lists only: enough for the owner sheet to say "Connect X".
        return [{"id": pid, "name": str(p["app"].get("name") or pid)[:80], "status": p["status"],
                 "error": p["error"] if p["status"] != "connected" else None, "fix_url": fix_for(p["app"]),
                 "models": [{"id": m["id"], "label": m["label"], "tier": m["tier"]} for m in p["models"]]}
                for pid, p in sorted(providers.items())]

    def owner_needs(self, app):
        """The shell's view of an installed app's needs: same rules as /v1/needs, plus providers."""
        with self.lock:
            result = self.needs(app)
            _, apps = self.registry()
            result["providers"] = self.provider_summary(self.providers(apps))
            return result

    def approve(self, body):
        app, feature, provider, model = (body.get(k) for k in ("app", "feature", "provider", "model"))
        scope = body.get("scope", "run")
        if not all(isinstance(x, str) for x in (app, feature, provider, model)) or scope not in ("run", "install"):
            raise Fault("blocked", "Name app, feature, provider, model and valid scope.", status=400)
        with self.lock:
            _, _, req, providers = self.context(app)
            need = req.get("models", {}).get(feature)
            if not need:
                raise Fault("blocked", "Unknown feature.", status=404)
            previous = self.state["routes"].get(app + "/" + feature)
            hand_pick = previous and previous["tested_on"] == need["tested_on"] and previous["chosen"] in ("match", "owner-pick")
            chosen = "owner-pick" if hand_pick or model == need["tested_on"] else "owner-swap"
            route = self.record(app, feature, need, provider, model, chosen)
            self.check_route(route, need, providers)
            if scope == "install":
                self.state["routes"][app + "/" + feature] = route
                self.save()
                return {"scope": scope, "route": route}
            # Single-use capability, bound to app + feature + declaration; never persisted.
            now = time.monotonic()
            self.overrides = {k: v for k, v in self.overrides.items() if v["expires"] > now}
            token = secrets.token_urlsafe(32)
            self.overrides[token] = {"route": route, "need": need, "expires": now + 300}
            return {"scope": scope, "route": route, "override": token, "expires_in": 300}

    def order(self, body):
        order = body.get("providers")
        if not strings(order) or any(not identifier(x) for x in order) or len(set(order)) != len(order):
            raise Fault("blocked", "providers must be unique provider app ids.", status=400)
        with self.lock:
            self.state["provider_order"] = order
            self.save()
            return {"providers": order}

    def routes(self):
        with self.lock:
            return {"routes": list(self.state["routes"].values()), "services": dict(self.state["services"]),
                    "provider_order": list(self.state["provider_order"])}

    def prepare_run(self, app, feature, body, override=None):
        allowed = {"system", "messages", "max_tokens", "stream", "abilities"}
        if set(body) - allowed or not isinstance(body.get("messages"), list) or not strings(body.get("abilities", [])):
            raise Fault("blocked", "Invalid run body; model and provider are router-owned.", status=400)
        with self.lock:
            _, _, req, providers = self.context(app)
            need = req.get("models", {}).get(feature)
            if need is None:
                raise Fault("blocked", "Unknown feature.", status=404)
            if override:
                entry = self.overrides.get(override)
                if not entry or entry["expires"] <= time.monotonic() or entry["route"]["app"] != app or entry["route"]["feature"] != feature or entry["need"] != need:
                    raise Fault("blocked", "Invalid or expired one-run approval.", status=403)
                del self.overrides[override]  # One admission, including failed provider calls.
                route = entry["route"]
            else:
                info = self.model_need(app, feature, need, providers)
                if info["status"] != "ready":
                    if info.get("error"):
                        err = info["error"]
                        raise Fault(err["code"], err["message"], err["fix_url"])
                    raise Fault(info["status"], "Owner action is required before this feature can run.")
                route = dict(info["route"])
            p, model = self.check_route(route, need, providers)
            abilities = body.get("abilities", need.get("abilities", []))
            if not set(abilities) <= set(need.get("abilities", [])) or not set(abilities) <= set(model.get("abilities", [])):
                raise Fault("blocked", "Undeclared or unsupported abilities.", status=400)
            payload = {**body, "model": route["model"], "stream": True, "abilities": abilities}
            headers = {"Content-Type": "application/json", **self.outgoing(p["app"]["id"], "ai-connection")}
            return Request(endpoint(p["app"]) + "/api/infer", data=json.dumps(payload).encode(), headers=headers), route, model, fix_for(p["app"])

    def service(self, app, slot, path):
        decoded = unquote(path)
        if "\\" in decoded or any(part in (".", "..") for part in decoded.split("?", 1)[0].split("/")) or decoded.startswith("/"):
            raise Fault("blocked", "Invalid service path.", status=400)
        with self.lock:
            data, apps = self.registry()
            req = apps.get(app, {}).get("requires", {})
            need = next((n for n in req.get("services", []) if n["slot"] == slot), None)
            if not need:
                raise Fault("blocked", "Service not declared by caller.", status=403)
            info = self.service_need(app, need, data, apps)
            if info["status"] != "ready":
                raise Fault("blocked", "Service is not connected.", info["fix_url"])
            target = apps[info["provider"]]
            if slot in ("ai-connection", "connection-router") or "ai-connection" in target.get("provides", []):
                raise Fault("blocked", "Inference providers must use the model route, not the service proxy.", status=403)
            return endpoint(target) + "/" + path, self.outgoing(target["id"], slot), fix_for(target)


def events(response):
    """Bound each SSE frame; multiline data and comments follow the SSE grammar."""
    data, event, size = [], None, 0
    while True:
        raw = response.readline(MAX_BODY + 1)
        if not raw:
            if data:
                raise Fault("provider_error", "Provider ended an incomplete SSE frame.")
            return
        size += len(raw)
        if size > MAX_BODY:
            raise Fault("provider_error", "Provider SSE frame is too large.")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                obj = json.loads("\n".join(data))
                if not isinstance(obj, dict):
                    raise ValueError("Invalid SSE payload")
                if event and obj.get("type", event) != event:
                    raise ValueError("Conflicting SSE type")
                yield {**obj, "type": obj.get("type", event)}
            data, event, size = [], None, 0
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            event = line[6:].strip()


class Handler(BaseHTTPRequestHandler):
    server: Server
    server_version = "WizardRouter/" + VERSION

    @property
    def router(self):
        return self.server.router

    def log_message(self, format, *args):
        pass  # No paths, prompts, request headers or tokens in access logs.

    def send_response(self, code, message=None):
        self.response_started = True
        super().send_response(code, message)

    def send_json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def body(self):
        if self.headers.get("Transfer-Encoding"):
            raise Fault("blocked", "Chunked request bodies are unsupported.", status=400)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size < 0 or size > MAX_BODY:
                raise ValueError()
        except ValueError:
            raise Fault("blocked", "Invalid request size.", status=413) from None
        return self.rfile.read(size)

    def json_body(self):
        try:
            value = json.loads(self.body())
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise Fault("blocked", "Expected a JSON object.", status=400) from None

    def owner(self):
        # These are network refusal rules, not an owner session authentication system.
        refused = {"tailscale-user-login", "x-forwarded-for", "forwarded", "x-real-ip", "origin", "x-wizard-grant", "x-wizard-app"}
        if any(k.lower() in refused or k.lower().startswith("cf-") for k in self.headers):
            raise Fault("blocked", "Owner API is box-only; call through the trusted shell backend.", status=403)

    def app_needs(self, app):
        # connection_needs answers for one calling APP. The agent gateway (a `host:` row, or an unnamed report-only
        # caller) is not an app: it gets a tool error, never another app's needs and never the owner view.
        if app is None or not identifier(app):
            raise Fault("blocked", "connection_needs answers for a calling app; the agent gateway has no app needs.", status=403)
        return self.router.needs(app)

    def dispatch(self):
        path = urlsplit(self.path).path
        if path == "/health" and self.command == "GET":
            return self.send_json(200, {"ok": True, "version": VERSION})
        static = {"/": ("index.html", "text/html"), "/appearance.js": ("appearance.js", "text/javascript"),
                  "/appearance.css": ("appearance.css", "text/css")}
        if path in static and self.command == "GET":
            name, content_type = static[path]
            raw = (Path(__file__).parent / "static" / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'self'")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        if path.startswith("/v1/owner/"):
            self.owner()
            if path == "/v1/owner/routes" and self.command == "GET":
                return self.send_json(200, self.router.routes())
            if path == "/v1/owner/approve" and self.command == "POST":
                return self.send_json(200, self.router.approve(self.json_body()))
            if path == "/v1/owner/provider-order" and self.command == "POST":
                return self.send_json(200, self.router.order(self.json_body()))
            match = re.fullmatch(r"/v1/owner/needs/([a-z0-9][a-z0-9-]{0,63})", path)
            if match and self.command == "GET":
                return self.send_json(200, self.router.owner_needs(match.group(1)))
        if path == "/mcp" and self.command == "POST":
            from mcp.front import rpc
            app = self.router.caller(self.headers, anonymous=True)
            message = self.json_body()
            if "id" not in message:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                return self.end_headers()
            return self.send_json(200, rpc(message, lambda: self.app_needs(app)))
        app = self.router.caller(self.headers)
        if path == "/v1/needs" and self.command == "GET":
            return self.send_json(200, self.router.needs(app))
        match = re.fullmatch(r"/v1/models/([a-z0-9-]+)/run", path)
        if match and self.command == "POST":
            prepared = self.router.prepare_run(app, match[1], self.json_body(), self.headers.get("X-Wizard-Override"))
            return self.infer(*prepared)
        match = re.match(r"/v1/services/([a-z0-9-]+)/(.*)", self.path)
        if match:
            return self.proxy(*self.router.service(app, match[1], match[2]))
        raise Fault("blocked", "Unknown route or method.", status=404)

    def sse(self, obj):
        self.wfile.write(("event: " + obj["type"] + "\ndata: " + json.dumps(obj) + "\n\n").encode())
        self.wfile.flush()

    def infer(self, request, route, model, fix):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            with HTTP.open(request, timeout=self.router.timeout) as response:
                if response.headers.get_content_type() != "text/event-stream":
                    raise Fault("provider_error", "Provider did not return SSE.", fix)
                for event in events(response):
                    if event["type"] == "done":
                        if event.get("model") != route["model"]:
                            raise Fault("provider_error", "Effective model differs from the approved route.", fix)
                        usage = event.get("usage", {})
                        if not isinstance(usage, dict):
                            raise Fault("provider_error", "Invalid provider usage.", fix)
                        self.sse({"type": "done", **{k: route[k] for k in ("provider", "model", "tested_on", "chosen")}, "tier": model["tier"], "usage": usage})
                        return
                    if event["type"] == "error":
                        code = event.get("code", "provider_error")
                        raise Fault(code if code in CODES else "provider_error", "Provider inference failed.", fix)
                    # Provisional text only; never forward provider provenance or arbitrary event types.
                    if event["type"] in ("delta", "text") and isinstance(event.get("text"), str):
                        self.sse({"type": event["type"], "text": event["text"]})
                raise Fault("provider_error", "Provider ended without an effective-model done event.", fix)
        except HTTPError as e:
            code = {401: "signed_out", 403: "expired", 429: "limit"}.get(e.code, "provider_error")
            e.close()
            self.sse(Fault(code, "Provider rejected inference.", fix).payload())
        except Fault as e:
            e.fix_url = fix
            self.sse(e.payload())
        except (OSError, URLError):
            self.sse(Fault("unreachable", "Provider connection failed.", fix).payload())
        except (ValueError, TypeError, UnicodeError):
            self.sse(Fault("provider_error", "Malformed provider stream.", fix).payload())

    def proxy(self, url, headers, fix):
        body = self.body()
        # Do not leak caller identity, cookies, authorization or forwarding headers.
        for key in ("Content-Type", "Accept", "Range"):
            if self.headers.get(key):
                headers[key] = self.headers[key]
        request = Request(url, data=body if body else None, method=self.command, headers=headers)
        try:
            try:
                response = HTTP.open(request, timeout=self.router.timeout)
            except HTTPError as e:
                response = e
            with response:
                self.send_response(int(response.status))
                for key in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    if response.headers.get(key):
                        self.send_header(key, response.headers[key])
                self.end_headers()
                if self.command != "HEAD":
                    while chunk := response.read(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
        except (URLError, OSError):
            raise Fault("unreachable", "Service connection failed.", fix, 502) from None

    def handle_request(self):
        self.connection.settimeout(30)
        self.response_started = False
        try:
            self.dispatch()
        except Fault as e:
            if not self.response_started:
                self.send_json(e.status, e.payload())
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except OSError:
            if not self.response_started:
                self.send_json(503, Fault("blocked", "Router state is unavailable.").payload())

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_request


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, router):
        self.router = router
        super().__init__(address, Handler)


def main():
    logging.basicConfig(level=logging.INFO)
    router = Router(os.environ["WIZARD_APPS_JSON"], os.environ.get("WIZARD_ROUTER_DATA_DIR", "/data"),
                    os.environ.get("WIZARD_GRANTS_FILE"))
    with Server((os.environ.get("WIZARD_ROUTER_BIND", "0.0.0.0"), int(os.environ.get("WIZARD_ROUTER_PORT", "8802"))), router) as server:
        server.serve_forever()

# ---- wizard.app-switches/1 ------------------------------------------------------------------------------------------
# Owner feature switches for the Tracker's Try it (wizard-os docs/APP_SWITCHES.md). Shared block: the same text ships in
# every Wizard app (template: wizard-app-manager templates/hello-wizard). Every new feature ships behind a switch, off by
# default: append it to SWITCHES with the issue it delivers, e.g.
#   {'id': 'compact-header', 'name': 'Compact top bar', 'description': '...', 'default': False,
#    'issues': ['humanitylabs-org/<repo>#20']}
# and gate the feature on switch_on('compact-header'). State lives on the box (DATA_DIR/switches.json), so the app's
# page, the Tracker (through the App Manager) and the agent see one state. GET/POST /api/switches, owner only, with
# X-Wizard-Switch: 1 and never a bearer or agent grant. An app with a platform owner-proof key (owner_proof in wizard.md,
# wizard-os spec/OWNER_PROOF.md) accepts only a valid signed proof: any process on the box can set the other headers.
# An app without one accepts the box's Tailscale Serve owner login, never a box-local host.
import hashlib as _sw_hashlib
import hmac as _sw_hmac
import json as _sw_json
import os as _sw_os
import re as _sw_re
import threading as _sw_threading
import time as _sw_time
from pathlib import Path as _SwPath
from urllib.parse import urlsplit as _sw_urlsplit

SWITCHES = []
_SW_CONTRACT = 'wizard.app-switches/1'
_SW_ROUTE = _sw_re.compile(r'(?:/[a-z0-9][a-z0-9-]{0,62})?/api/switches/?')
_SW_LOCAL = {'localhost', '127.0.0.1', '::1', '[::1]', '0.0.0.0'}
_sw_lock = _sw_threading.Lock()
_sw_nonces = {}
_SW_PROOF = _sw_re.compile(r'(\d{1,12})\.([0-9a-f]{48})\.([0-9a-f]{64})')


def _sw_file():
    return _SwPath(_sw_os.environ.get('WIZARD_SWITCHES_DIR') or _sw_os.environ.get('DATA_DIR') or '/data') / 'switches.json'


def _sw_saved():
    try:
        saved = _sw_json.loads(_sw_file().read_text())
    except (OSError, ValueError):
        return {}
    return saved if isinstance(saved, dict) else {}


def switches_doc():
    saved = _sw_saved()
    return {'contract': _SW_CONTRACT, 'switches': [
        dict(s, on=saved[s['id']] if isinstance(saved.get(s['id']), bool) else bool(s.get('default'))) for s in SWITCHES]}


def switch_on(switch_id):
    return next((s['on'] for s in switches_doc()['switches'] if s['id'] == switch_id), False)


def _sw_set(switch_id, on):
    with _sw_lock:
        ids = {s['id'] for s in SWITCHES}
        saved = {k: v for k, v in _sw_saved().items() if k in ids and isinstance(v, bool)}
        saved[switch_id] = on
        f = _sw_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + '.tmp')
        tmp.write_text(_sw_json.dumps(saved))
        _sw_os.replace(tmp, f)
    return switches_doc()


def _sw_proof(h):
    """None when this app has no platform owner-proof key, else whether the request carries a valid, unused proof."""
    key_file = _sw_os.environ.get('WIZARD_OWNER_PROOF_KEY_FILE')
    if not key_file:
        return None
    try:
        with open(key_file, 'rb') as f:
            key = f.read(33)
    except OSError:
        return False                    # a key is named but unreadable: refuse, never fall back to headers
    app = _sw_os.environ.get('WIZARD_OWNER_PROOF_APP') or ''
    values = h.headers.get_all('X-Wizard-App-Owner-Proof') or []
    m = _SW_PROOF.fullmatch(values[0].strip()) if len(values) == 1 else None
    if len(key) != 32 or not app or not m or abs(_sw_time.time() - int(m.group(1))) > 30:
        return False
    msg = '\n'.join(('wizard-app-owner-v1', app, m.group(1), m.group(2), h.command, h.path)).encode()
    if not _sw_hmac.compare_digest(_sw_hmac.new(key, msg, _sw_hashlib.sha256).hexdigest(), m.group(3)):
        return False
    now = _sw_time.monotonic()
    with _sw_lock:
        for n in [n for n, until in _sw_nonces.items() if until < now]:
            del _sw_nonces[n]
        if m.group(2) in _sw_nonces:
            return False
        _sw_nonces[m.group(2)] = now + 60
    return True


def _sw_owner(h, write):
    hd = h.headers
    if hd.get('Authorization') or hd.get('X-Wizard-Switch') != '1':
        return False
    proof = _sw_proof(h)
    if proof is not None:
        return proof
    if (hd.get('Sec-Fetch-Site') or 'same-origin').lower() not in ('same-origin', 'none'):
        return False
    host = (hd.get('Host') or '').strip().lower()
    name = host.split(']')[0] + ']' if host.startswith('[') else host.rsplit(':', 1)[0]
    if not name or name in _SW_LOCAL or '.' not in name:      # box-local and container hosts never prove the owner
        return False
    origin = hd.get('Origin')
    trusted = {o.strip() for o in (_sw_os.environ.get('WIZARD_TRUSTED_ORIGINS') or '').split(',') if o.strip()}
    if origin and _sw_urlsplit(origin).netloc.lower() != host and origin not in trusted:
        return False
    if write and not origin:
        return False
    logins = [x.strip() for x in (hd.get_all('Tailscale-User-Login') or [])]
    owners = {x.strip().lower() for x in (_sw_os.environ.get('WIZARD_OWNER_LOGINS') or '').split(',') if x.strip()}
    return len(logins) == 1 and '@' in logins[0] and (not owners or logins[0].lower() in owners)


def _sw_send(h, code, doc):
    body = _sw_json.dumps(doc).encode()
    h.send_response(code)
    h.send_header('Content-Type', 'application/json')
    h.send_header('Cache-Control', 'no-store')
    h.send_header('Content-Length', str(len(body)))
    h.end_headers()
    if h.command != 'HEAD':
        h.wfile.write(body)


def _sw_handle(h):
    """Serve /api/switches (or /<app>/api/switches) and return True; False for every other request."""
    if not _SW_ROUTE.fullmatch(_sw_urlsplit(h.path).path):
        return False
    if h.command in ('GET', 'HEAD'):
        if not _sw_owner(h, False):
            _sw_send(h, 403, {'error': "Only this Wizard's owner can see these switches.", 'code': 'owner_only'})
        else:
            _sw_send(h, 200, switches_doc())
        return True
    if not _sw_owner(h, True):
        _sw_send(h, 403, {'error': "Only this Wizard's owner can change these switches.", 'code': 'owner_only'})
        return True
    try:
        if (h.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            raise ValueError
        size = int(h.headers.get('Content-Length') or '0')
        if size <= 0 or size > 4096:
            raise ValueError
        body = _sw_json.loads(h.rfile.read(size))
        if not isinstance(body, dict) or not isinstance(body.get('id'), str) or not isinstance(body.get('on'), bool) \
                or set(body) - {'id', 'on'}:
            raise ValueError
    except (ValueError, TypeError):
        _sw_send(h, 400, {'error': 'Send JSON {"id": "...", "on": true|false}', 'code': 'invalid_input'})
        return True
    if body['id'] not in {s['id'] for s in SWITCHES}:
        _sw_send(h, 404, {'error': 'No such switch', 'code': 'unknown_switch'})
        return True
    try:
        _sw_send(h, 200, _sw_set(body['id'], body['on']))
    except OSError:
        _sw_send(h, 500, {'error': 'storage_failure'})
    return True


def wizard_switches_install(handler_cls):
    """Route the switch endpoint ahead of the app's own GET/HEAD/POST handlers."""
    for method in ('do_GET', 'do_HEAD', 'do_POST'):
        orig = getattr(handler_cls, method, None)
        if getattr(orig, '_wizard_switches', False):
            continue

        def wrapped(self, _orig=orig):
            if _sw_handle(self):
                return None
            if _orig is None:
                return self.send_error(501)
            return _orig(self)
        wrapped._wizard_switches = True
        setattr(handler_cls, method, wrapped)
    return handler_cls
# ---- end wizard.app-switches/1 ---------------------------------------------------------------------------------------

wizard_switches_install(Handler)


if __name__ == "__main__":
    main()
