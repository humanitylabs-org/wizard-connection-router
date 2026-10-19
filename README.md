# Wizard Connection Router

A small Python-standard-library service for Wizard OS decision #65. Discovers
installed app requirements, saves explicit owner choices, and forwards isolated
inference to connection apps. Engine images are released through the GitHub-hosted
`humanitylabs-org/wizard-app-store` workflows. This does not modify Hermes or install
provider plugins; the shell approval UI and automatic registry projection remain
separate integration work.

## Try it

Python 3.12+; no dependencies or installation required:

```sh
python3 -m unittest discover -s tests -v
```

Tests start the actual HTTP router and deterministic fake Claude/ChatGPT providers
on disposable loopback ports. Every registry, grant file and routing table is a
fixture. The four boxes (Claude only, ChatGPT only, both, neither) are simulated
in-process, **not** four deployed VMs or evidence of real model inference.

To run against a registry and grants file you control:

```sh
WIZARD_APPS_JSON="$PWD/fixtures/apps.json" \
WIZARD_GRANTS_FILE="$PWD/fixtures/grants.json" \
WIZARD_ROUTER_DATA_DIR="$PWD/data" \
WIZARD_ROUTER_BIND=127.0.0.1 WIZARD_ROUTER_PORT=8802 \
python3 server.py
```

Create those files first. A minimal registry looks like this (the provider must
actually serve the contract on the configured address):

```json
{"schema":1,"apps":[
  {"id":"reader","requires":{"models":{"ask":{
    "tier":"standard","tested_on":"claude-sonnet","abilities":["web_search"]
  }},"services":[]}},
  {"id":"claude","status":"running","provides":["ai-connection"],
   "view":{"upstream":"http://127.0.0.1:8901/"},"private":false}
],"slots":{}}
```

For a disposable report-only fixture, `grants.json` can be
`{"enforce":false,"incoming":[],"outgoing":{}}`. Such callers identify themselves
with `X-Wizard-App: reader`; this is deliberately **not authenticated**. Production
integration must supply App Manager's grants; never disable enforcement as a fix.

```sh
curl -H 'X-Wizard-App: reader' http://127.0.0.1:8802/v1/needs
curl -N -H 'X-Wizard-App: reader' -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Explain this passage."}],"max_tokens":100}' \
  http://127.0.0.1:8802/v1/models/ask/run
```

## App API

All app endpoints (including `/mcp`) check `X-Wizard-Grant`. Only `/health` and the
static informational view `/` are public. Grant hashes map the caller to its app;
requests cannot choose another app in the query/body. Files are reread each request
so revocation takes effect without restart. A configured missing/malformed grants
file fails closed. Omitting `WIZARD_GRANTS_FILE` means report-only development mode.

| Endpoint | Result |
|---|---|
| `GET /v1/needs` | Calling app's `models`, `services`, and unchanged `agent_binding`; each need has ready / needs_owner / blocked and `fix_url`. |
| `POST /v1/models/<feature>/run` | Body: `system`, `messages`, `max_tokens`, `stream`, `abilities`. Provider/model are router-owned; unknown fields are rejected. SSE is always used in this slice. |
| `ANY /v1/services/<slot>/<path>` | Standard HTTP methods, path/query/body forwarded to the selected slot app. Does not forward caller grants, authorization or cookies. Uses the router's outgoing grant. |
| `POST /mcp` | Stateless streamable-HTTP MCP with the read-only `connection_needs` tool; no approval or inference tool. |

Provider discovery calls `GET /api/connection` on each running `ai-connection` app.
Both `/1` and additive `/2` contracts are accepted; absent `models` cannot satisfy
inference. Every model needs an id, label, valid tier and abilities list. Provider
requests never use environment HTTP proxies or follow redirects. Only
`POST /api/infer` receives the app's prompt; no agent, memory, persona or tools are
added. Abilities must be declared by the app and supported by the selected model.

The provider's SSE `delta` / `text` events are passed through as provisional text.
A final `done` must report **exactly the routed model**; missing, unknown or
mismatched effective models, malformed streams and missing completion fail closed.
The router generates the final provenance: `provider`, `model`, `tier`, `tested_on`,
`chosen`, `usage`. Callers **must discard provisional output unless a valid done
arrives**. This is provider-reported provenance, not cryptographic attestation.

Errors are JSON before streaming starts, SSE `error` afterwards. Every router
error includes `fix_url`; codes are `signed_out`, `expired`, `limit`, `unreachable`,
`provider_error`, `needs_owner`, `blocked`. Service HTTP responses retain their own
status/body. Inference providers cannot be accessed through the service proxy,
which would bypass model approval. Generic service grants are not path-scoped.

## Owner API (trusted shell backend only)

- `GET /v1/owner/routes`: saved routes, pinned services and provider order.
- `POST /v1/owner/approve`: `{app, feature, provider, model, scope}`.
  - `scope: install` saves one route; repeated runs reuse it.
  - `scope: run` (default) returns an `override` capability, valid for 300 seconds.
    Pass it on the named call as `X-Wizard-Override`. It is bound to app, feature
    and declaration, consumed once under a lock, never written to disk, and lost
    on restart. Failed provider calls also consume it; parallel requests cannot
    spend it twice.
- `POST /v1/owner/provider-order`: `{"providers":["chatgpt","claude"]}`.
  These are installed **app ids**, not self-reported vendor names. Setting order
  never changes an existing route.

Requests bearing `tailscale-user-login`, `x-forwarded-for`, `forwarded`,
`x-real-ip`, or any `cf-*` header are refused, per AI_CONNECTIONS. Additionally,
owner endpoints refuse `Origin`, `X-Wizard-App` and `X-Wizard-Grant`. There is no
CORS allowance or browser approval form. **These header refusals are not owner
session authentication**: a direct box process can omit the headers. The shell
backend and deployment network must provide the trusted owner boundary. Do not
expose this source-only service on an untrusted network. App grants do not grant
owner authority. No agent-facing tool can approve a swap.

## Routing and persistence

1. Route an exact tested model reachable through an eligible connected provider.
2. Otherwise `swap: ask` (default) produces `needs_owner` with an offer; it does not
   run until explicit approval. Prefer the requested tier, then the strongest
   available tier. Within a tier use owner provider order, then stable app/model
   ids. A different-tier offer is explicitly marked `standing_in`; actual model
   tier remains in completion provenance.
3. `swap: never` has no offer and blocks until its tested model is connected.
4. `private_data: true` only allows provider apps marked `private: true` in the
   owner-controlled registry. Approval never bypasses privacy or abilities.
5. Once selected, nothing automatically switches provider/model, including when
   a preferred provider reconnects. A `tested_on` change invalidates only that
   feature and redoes matching. Other declaration changes are revalidated at run
   time. Changing an existing match by hand records `owner-pick`; substituting
   for an unavailable tested model records `owner-swap`.

`$WIZARD_ROUTER_DATA_DIR/routes.json` contains schema 1, `routes` keyed by
`app/feature`, `services`, and `provider_order`. Route entries contain exactly
`app`, `feature`, `provider`, `model`, `tested_on`, `chosen`, `at` (Unix seconds).
Writes are mode 0600, temp + fsync + rename, with process-local locking. Failed
pre-rename writes restore in-memory choices; corrupt state refuses startup rather
than erasing approvals. Use one router process per data directory. Back up this
file with whole-box data. There are no provider credentials or prompts in state.

Services select the installed registry's `slots[slot].default`, then pin that
selection. They never select an arbitrary candidate or auto-failover. Optional
needs remain visible as blocked without disabling other features. `recommend`
and `purpose` are returned for a later install sheet; this service installs nothing.

## Proposed fields / integration decisions

The merged spec fixes the direction but labels its field/API details proposed.
This slice makes the following explicit, locally tested choices:

- **Identity before grants land:** unknown/missing grants log a constant warning
  and may use `X-Wizard-App` only while `enforce: false`; known grants always win.
  With `enforce: true`, an unknown grant gets 403 regardless of identity hints.
- **Private marker:** registry app `private: true` is owner-controlled. Never trust
  a provider's self-asserted privacy flag. Reconcile with the final PR #49 shape
  before shipping real private-data traffic.
- **Provider address:** use optional `api.internal` / `api.url`, otherwise the
  origin of `view.upstream`, `mcp.internal`, or `mcp.url`, in that order. Fixed
  `/api/connection` and `/api/infer` are served on that origin. No provider-specific
  hostnames or assumptions are hardcoded.
- **Route mirroring:** APPS_JSON names App Manager as the sole registry writer.
  Router therefore does **not** race it by writing `apps.json`. `projection.py`
  provides tested `mirror_routes(registry, owner_response)` for App Manager's
  atomic write transaction: it copies only contract fields into top-level
  `routes`, without tokens or routes for uninstalled consumers. Actual App Manager
  wiring is not in this repo and is still required for automatic mirroring.
- **Read/install timing:** `/v1/needs` lazily resolves/persists matches; first run
  does the same if discovery wasn't requested. Shell should call needs at install.
  Owner routes list only already resolved choices; pending needs come from needs.
- **One-off API / SSE:** single-use override header and five-minute lifetime are
  slice-local proposed transport details. `stream: false` still yields SSE, since
  the router contract specifies a streamed response; there is no buffered JSON
  mode. Output is provisional until done rather than buffered until attestation.
- **Services:** this slice pins existing slot defaults and supports standard HTTP
  verbs; WebSocket upgrades, credential-bearing service authorization and service
  reassignment UI are not implemented. No inference-through-service escape hatch.

## Package and verification boundary

`wizard.md`, `runtipi/`, `Dockerfile`, the integrated `mcp/` front and `SKILL.md`
follow the stdlib provider-app pattern. The static view is informational, not a
finished shell settings UI. The recipe pins the published multi-architecture image
by index digest, exposes only a loopback host port, and mounts App Manager's
credential-free shared registry read-only. Supply scoped grants in the app's
`wizard/grants.json` directory and a UID 1000-writable `data/` directory before
use. An initial closed grants file is `{"enforce":true,"incoming":[],"outgoing":{}}`;
missing grants fail closed. Do not relax enforcement to bypass integration.
No privileged mounts, runtime sockets or provider login volumes are requested.

Tests cover four-box discovery, exact pin/abilities/privacy, approval persistence,
run-only/concurrent consumption, grants/revocation, runtime failures/no failover,
unknown effective models, live SSE flushing, malformed registries/streams, owner
header refusal, service forwarding/bypass refusal, MCP, atomic failure and the
registry projection helper. Hosted CI also builds and smoke-tests the image before
publication. These fixtures do not claim signed-in owner inference, shell approval
UI, App Manager route-projection wiring, or independent security review.

Spec baseline: `wizard-os` main `73e6a76e7e2ace775c3c9204376c2285efe55d8f`
(CONNECTION_ROUTER, AI_CONNECTIONS, APP_WORKFLOWS, WIZARD_MD, APPS_JSON);
grants draft `468dc5ecf1cce312f2de11ba19edf1ee389b4c09`.
Template: ChatGPT connection `f87d5ddc449535418ee92ebeb24aaec36cc58058`.
