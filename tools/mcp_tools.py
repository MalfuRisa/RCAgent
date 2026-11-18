"""MCP tools setup and configuration.

``TOOLS`` is resolved lazily (PEP 562) instead of being built by ``asyncio.run``
at import time. The eager version raised ``RuntimeError: asyncio.run() cannot be
called from a running event loop`` whenever this module happened to be imported
for the first time from inside async code -- which is exactly what the Triage
discovery path and the acceptance runner do. Callers are unaffected:
``from tools.mcp_tools import TOOLS`` and ``from tools import TOOLS`` both still
return the filtered allow-list.
"""
import asyncio
import concurrent.futures
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient

from config import MCP_CONFIG, TOOLS_ALLOWED


async def get_mcp_tools(mcp_client: MultiServerMCPClient) -> list:
    """Get and filter MCP tools based on allowed list.
    
    Args:
        mcp_client: Initialized MCP client
        
    Returns:
        List of filtered tool objects
    """
    mcp_tools = await mcp_client.get_tools()
    
    tools = []
    for tool in mcp_tools:
        if tool.name in TOOLS_ALLOWED:
            from .contracts import validate_schema, fingerprint, SCHEMA_FINGERPRINTS
            schema = tool.args_schema if isinstance(tool.args_schema, dict) else tool.args_schema.model_json_schema()
            error = validate_schema(tool.name, schema)
            if error:
                raise ValueError(error)
            SCHEMA_FINGERPRINTS[tool.name] = fingerprint(schema)
            tools.append(tool)
    
    return tools


def _run_sync(coro):
    """Run ``coro`` to completion whether or not a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # Inside a running loop: hand the coroutine to a worker thread so the
    # caller's loop is never nested or blocked on itself.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


_mcp_client = MultiServerMCPClient(MCP_CONFIG)
_TOOLS: list | None = None


def load_tools(force: bool = False) -> list:
    """Return the allow-listed Gateway tools, fetching them once."""
    global _TOOLS
    if _TOOLS is None or force:
        _TOOLS = _run_sync(get_mcp_tools(_mcp_client))
    return _TOOLS


def __getattr__(name: str) -> Any:
    if name == "TOOLS":
        return load_tools()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["TOOLS", "get_mcp_tools", "load_tools"]
