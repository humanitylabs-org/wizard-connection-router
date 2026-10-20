"""Small stateless streamable-HTTP MCP front, served by server.py."""
from .tools import TOOLS


def rpc(message, needs):
    ident, method = message.get("id"), message.get("method")
    params = message.get("params") or {}
    result = None
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "wizard-connection-router", "version": "0.1.4"}}
    elif method == "notifications/initialized":
        result = {}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call" and isinstance(params, dict) and params.get("name") == "connection_needs":
        import json
        result = {"content": [{"type": "text", "text": json.dumps(needs())}]}
    else:
        return {"jsonrpc": "2.0", "id": ident, "error": {"code": -32601, "message": "Unknown method or tool"}}
    return {"jsonrpc": "2.0", "id": ident, "result": result}
