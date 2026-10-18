"""Read-only tools: agents cannot approve substitutions or reorder providers."""
TOOLS = [{"name": "connection_needs",
          "description": "This calling app's model and service readiness, with owner fix links. No secrets.",
          "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]
