"""App Manager integration helper; the router never writes the installed registry.

Pass GET /v1/owner/routes to mirror_routes inside the registry writer's own atomic
write transaction. This avoids competing writers and copies no grants or tokens.
"""
import copy

ROUTE_FIELDS = ("app", "feature", "provider", "model", "tested_on", "chosen", "at")


def mirror_routes(registry, owner_response):
    result = copy.deepcopy(registry)
    installed = {app["id"] for app in result["apps"]}
    result["routes"] = [{key: route[key] for key in ROUTE_FIELDS}
                        for route in owner_response["routes"] if route["app"] in installed]
    return result
