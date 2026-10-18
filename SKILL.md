---
name: wizard-connection-router
description: Use to inspect installed app model and service readiness through the Connection Router.
---

# Connection Router

1. Load the calling app's shipped workflow and model pin before inference.
2. Send that app's `X-Wizard-Grant` on `GET /v1/needs` (or MCP
   `connection_needs`). Never impersonate an app or invent an approval.
3. Run `POST /v1/models/<feature>/run` with only system, messages,
   max_tokens, stream and declared abilities. Never specify a provider/model.
4. Treat streamed text as provisional. Admit an answer only after the router's
   validated `done`, displaying provider, model, tier, tested_on, chosen and usage.
   Discard provisional output after an error, disconnect, or missing done.
5. On needs_owner, blocked, signed_out, expired, limit, unreachable or
   provider_error, retain input and show the supplied fix_url. Never retry with
   another provider, read credentials, call Hermes, or manufacture an override.
6. Only the trusted owner-facing shell backend may call owner approvals. A
   run-scoped approval yields an expiring, single-use X-Wizard-Override; installation
   scope is persistent only when explicitly selected. Do not expose approval as an
   agent tool. Private features never use ordinary providers, even with approval.

This package routes app-owned workflows; it generates no content of its own and
has no model pin, prompts, persona or memory. See README.md for integration limits.
