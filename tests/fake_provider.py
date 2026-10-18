"""Deterministic loopback-only provider fixture. No real credentials or model calls."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeProvider:
    def __init__(self, name, models):
        self.name, self.models = name, models
        self.status = "connected"
        self.error = None
        self.requests = []
        self.effective_model = None
        self.infer_status = 200
        self.mode = "normal"
        self.grant = None
        self.started = threading.Event()
        self.release = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def respond(self, code, body, content="application/json"):
                raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", content)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(raw)

            def handle_call(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                fixture.requests.append({"method": self.command, "path": self.path,
                                         "headers": dict(self.headers), "body": body})
                if fixture.grant and self.headers.get("X-Wizard-Grant") != fixture.grant:
                    return self.respond(403, {})
                if self.path == "/api/connection":
                    return self.respond(200, {"contract": "wizard.ai-connection/2", "provider": fixture.name,
                                             "status": fixture.status, "error": fixture.error,
                                             "models": fixture.models})
                if self.path == "/api/infer":
                    if fixture.infer_status != 200:
                        return self.respond(fixture.infer_status, {})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    model = json.loads(body)["model"]
                    self.wfile.write(b': heartbeat\n\nevent: delta\ndata: {"text":"hello"}\n\n')
                    self.wfile.flush()
                    fixture.started.set()
                    if fixture.mode == "pause":
                        fixture.release.wait(3)
                    if fixture.mode == "missing":
                        return
                    if fixture.mode == "malformed":
                        self.wfile.write(b'data: broken\n\n')
                        return
                    if fixture.mode == "error":
                        self.wfile.write(b'data: {"type":"error","code":"limit"}\n\n')
                        return
                    if fixture.mode == "oversize":
                        self.wfile.write(b'data: ' + b'x' * (1024 * 1024 + 1) + b'\n\n')
                        return
                    done = {"type": "done", "model": fixture.effective_model or model,
                            "usage": {"input": 3, "output": 1}}
                    if fixture.mode == "no-model":
                        del done["model"]
                    self.wfile.write(("data: " + json.dumps(done) + "\n\n").encode())
                    return
                return self.respond(200, {"method": self.command, "path": self.path, "body": body.decode(),
                                          "grant": self.headers.get("X-Wizard-Grant"),
                                          "authorization": self.headers.get("Authorization")})

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_call

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def app(self, private=False):
        return {"id": self.name, "status": "running", "provides": ["ai-connection"],
                "private": private, "view": {"upstream": self.url + "/"}}


def model(ident, tier="standard", abilities=None):
    return {"id": ident, "label": ident, "tier": tier, "abilities": abilities or []}
