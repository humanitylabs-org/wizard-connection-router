---
wizard: 0.2
id: wizard-connection-router
name: Connection Router
type: service
category: system
version: 0.1.3
summary: Connect installed apps to their tested models and services, with explicit owner-approved replacements.
license: MIT
source: https://github.com/humanitylabs-org/wizard-connection-router
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
mcp:
  path: /mcp
  port: 8802
  transport: streamable-http
skill: SKILL.md
view:
  path: /
  port: 8802
provides: [connection-router]
uses: [ai-connection]
health: /health
---

# Connection Router

A credential-free router for isolated app inference and service connections.
Read README.md for the API, draft-field decisions, and security boundary.
The static view describes this source-only slice; shell settings come later.
