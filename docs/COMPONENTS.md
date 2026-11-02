# Components

| Capability | Entry point | Responsibility |
| --- | --- | --- |
| Router caller grants and MCP admission | `Router.grants`, `Router.caller`, `Handler.app_needs`, `mcp.front.rpc` | Reuse the existing grants reader, matched-key identity and MCP front. A verified gateway key may list tools but does not impersonate an app; unnamed callers are admitted only to data-free MCP methods in report-only mode. Enforced mode still rejects unknown grants. |
