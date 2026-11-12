"""The switch owner check: an app with a platform owner-proof key takes only a valid signed proof (wizard-os#133).

Host, Origin, Tailscale-User-Login and X-Wizard-Switch are client-set, so any process on the box that reaches the app
port could set them. With WIZARD_OWNER_PROOF_KEY_FILE named, only an hmac-v1 proof (wizard-os spec/OWNER_PROOF.md,
bound to app id, method and target, +-30 s, each nonce once) is accepted, and the headers are never a fallback.
"""
import hashlib
import hmac
import http.client
import json
import os
import secrets
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import server

KEY = bytes(range(32))
APP = 'wizard-connection-router'
FORGED = {'X-Wizard-Switch': '1', 'Host': 'wizard.example.com', 'Origin': 'https://wizard.example.com',
          'Tailscale-User-Login': 'owner@example.com'}


def sign(method, target, key=KEY, app=APP, ts=None, nonce=None):
    ts = str(int(time.time()) if ts is None else ts)
    nonce = nonce or secrets.token_hex(24)
    msg = '\n'.join(('wizard-app-owner-v1', app, ts, nonce, method, target)).encode()
    return f'{ts}.{nonce}.{hmac.new(key, msg, hashlib.sha256).hexdigest()}'


class Plain(BaseHTTPRequestHandler):
    def log_message(self, *args, **kwargs):
        pass

    def do_GET(self):
        self.send_error(404)


server.wizard_switches_install(Plain)


class SwitchOwnerProof(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(('WIZARD_OWNER', 'WIZARD_TRUSTED'))}
        env['WIZARD_SWITCHES_DIR'] = str(self.root)
        self.env = patch.dict(os.environ, env, clear=True)
        self.env.start()
        self.switches = patch.object(server, 'SWITCHES', [{'id': 'demo', 'name': 'Demo', 'default': False, 'issues': []}])
        self.switches.start()
        getattr(server, '_sw_nonces', {}).clear()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Plain)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.switches.stop()
        self.env.stop()
        self.dir.cleanup()

    def keyed(self, key=KEY):
        (self.root / 'proof').write_bytes(key)
        os.environ['WIZARD_OWNER_PROOF_KEY_FILE'] = str(self.root / 'proof')
        os.environ['WIZARD_OWNER_PROOF_APP'] = APP

    def call(self, method, path, headers, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.httpd.server_address[1], timeout=5)
        data = json.dumps(body).encode() if body is not None else None
        h = dict(headers)
        if data is not None:
            h['Content-Type'] = 'application/json'
        c.putrequest(method, path, skip_host='Host' in h)
        for k, v in h.items():
            c.putheader(k, v)
        if data is not None:
            c.putheader('Content-Length', str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_keyless_app_keeps_the_serve_login_check(self):
        self.assertEqual(self.call('GET', '/api/switches', FORGED), 200)
        self.assertEqual(self.call('GET', '/api/switches', {**FORGED, 'Host': '127.0.0.1:8080'}), 403)

    def test_keyed_app_refuses_the_forgeable_headers(self):
        self.keyed()
        self.assertEqual(self.call('GET', '/api/switches', FORGED), 403)
        self.assertEqual(self.call('POST', '/api/switches', FORGED, {'id': 'demo', 'on': True}), 403)
        self.assertIs(server.switch_on('demo'), False)

    def test_keyed_app_takes_a_valid_proof_once(self):
        self.keyed()
        h = {'X-Wizard-Switch': '1', 'X-Wizard-App-Owner-Proof': sign('POST', '/api/switches')}
        self.assertEqual(self.call('POST', '/api/switches', h, {'id': 'demo', 'on': True}), 200)
        self.assertIs(server.switch_on('demo'), True)
        self.assertEqual(self.call('POST', '/api/switches', h, {'id': 'demo', 'on': False}), 403)  # replay
        self.assertIs(server.switch_on('demo'), True)
        g = {'X-Wizard-Switch': '1', 'X-Wizard-App-Owner-Proof': sign('GET', '/router/api/switches?x=1')}
        self.assertEqual(self.call('GET', '/router/api/switches?x=1', g), 200)

    def test_keyed_app_refuses_a_bad_proof(self):
        self.keyed()
        for proof in (sign('GET', '/api/switches'),                          # wrong method
                      sign('POST', '/other/api/switches'),                   # wrong target
                      sign('POST', '/api/switches', app='wizard-notes'),     # wrong app
                      sign('POST', '/api/switches', key=bytes(32)),          # wrong key
                      sign('POST', '/api/switches', ts=int(time.time()) - 31),  # stale
                      'not-a-proof'):
            h = {**FORGED, 'X-Wizard-App-Owner-Proof': proof}
            self.assertEqual(self.call('POST', '/api/switches', h, {'id': 'demo', 'on': True}), 403, proof)
        self.assertIs(server.switch_on('demo'), False)

    def test_proof_needs_the_switch_header_and_no_bearer(self):
        self.keyed()
        self.assertEqual(self.call('GET', '/api/switches', {'X-Wizard-App-Owner-Proof': sign('GET', '/api/switches')}), 403)
        h = {'X-Wizard-Switch': '1', 'Authorization': 'Bearer x', 'X-Wizard-App-Owner-Proof': sign('GET', '/api/switches')}
        self.assertEqual(self.call('GET', '/api/switches', h), 403)

    def test_unreadable_key_refuses_and_never_falls_back(self):
        os.environ['WIZARD_OWNER_PROOF_KEY_FILE'] = str(self.root / 'missing')
        os.environ['WIZARD_OWNER_PROOF_APP'] = APP
        self.assertEqual(self.call('GET', '/api/switches', FORGED), 403)
        h = {'X-Wizard-Switch': '1', 'X-Wizard-App-Owner-Proof': sign('GET', '/api/switches')}
        self.assertEqual(self.call('GET', '/api/switches', h), 403)


if __name__ == '__main__':
    unittest.main()
