"""Configured Gateway protocol adapter for the single diagnostic graph."""

from tools.diagnostic_catalog import Capability, CapabilityCatalog, QueryResult
from tools.contracts import VERSION, HAS_WINDOW, unsupported_scope
from tools.observability_adapter import _call_gateway_raw
from workspace.repository import WorkspaceError
from workspace.resources import (
    scope,
    check_query,
    refresh_for_query,
    query_kind,
    candidates,
    accept_inventory,
    register,
)
from workspace.evidence_targets import evidence_targets
from workspace.windows import request_window, returned_window
from workspace.queries import metadata


async def make_catalog(state):
    from tools.mcp_tools import load_tools
    from config import MCP_CONFIG
    from urllib.parse import urlsplit, urlunsplit

    capabilities = []
    for native in load_tools():
        name = native.name
        schema = (
            native.args_schema
            if isinstance(native.args_schema, dict)
            else native.args_schema.model_json_schema()
        )

        async def normalize(b, args, name=name):
            args = await request_window(b, name, args)
            if unsupported_scope(name, args, await scope(b)):
                raise WorkspaceError("unsupported scope: do not drop incident constraints")
            await refresh_for_query(b, name, args)
            await check_query(b, name, args)
            return args

        async def invoke(b, args, name=name):
            raw = await _call_gateway_raw(name, args)
            meta = metadata(raw)
            resources = await b.repository.all(b.incident_id, "resource")
            refs = evidence_targets(
                resources, name, args, query_kind(name, args), candidates(raw), await scope(b)
            )
            return QueryResult(
                raw,
                refs,
                returned_window(raw),
                meta.get("status") or "unknown",
                bool(meta.get("truncated")),
                meta.get("warnings") or [],
                meta.get("data_gaps") or [],
            )

        capabilities.append(
            Capability(
                name, native.description, schema, normalize, invoke, VERSION, name in HAS_WINDOW
            )
        )
    endpoints = []
    for server, configuration in MCP_CONFIG.items():
        url = configuration.get("url")
        if url:
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if parsed.port:
                host += ":" + str(parsed.port)
            endpoints.append(
                {
                    "server": server,
                    "url": urlunsplit((parsed.scheme, host, parsed.path, "", "")),
                    "transport": configuration.get("transport"),
                }
            )
    catalog = CapabilityCatalog(
        capabilities,
        service_identity={
            "platform": "configured_gateway",
            "contract": VERSION,
            "endpoints": endpoints,
        },
    )

    async def bootstrap(b, state):
        if not state.get("target_resource") or not state.get("target_cluster"):
            return
        from workspace.query_execution import execute_query

        args = {
            "query": state["target_resource"],
            "cluster": state["target_cluster"],
            "kind": "auto",
        }
        if state.get("target_namespace"):
            args["namespace"] = state["target_namespace"]
        if "search_k8s_targets" not in catalog.capabilities:
            return
        raw = await execute_query(b, catalog, "search_k8s_targets", args)
        for item in candidates(raw):
            if item.get("name") and item.get("namespace"):
                await register(b, item.get("kind", "pod"), item["name"], item["namespace"])
        await accept_inventory(b, raw, args, raw["_rcagent_query"]["evidence_id"])

    catalog.bootstrap = bootstrap
    return catalog
