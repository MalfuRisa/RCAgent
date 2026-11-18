"""Temporary native runtime configuration; credentials never enter run state."""
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from config import settings
from platforms.insights_v5.diagnostic_policy import NATIVE_READS
from platforms.insights_v5.session import gateway


def cli_path():
    return os.environ.get("RCAGENT_OPENCODE_BINARY", "/root/.opencode/bin/opencode")


@asynccontextmanager
async def connection(output):
    external = os.environ.get("RCAGENT_NATIVE_MCP_URL")
    if external:
        token = os.environ.get("RCAGENT_NATIVE_MCP_TOKEN", "")
        yield external, token, await discover(external, token)
    else:
        async with gateway(output / "gateway") as (url, token):
            yield url, token, await discover(url, token)


async def discover(url, token):
    headers = {"Authorization": "Bearer " + token} if token else {}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
    selected = [t.name for t in tools if t.name in NATIVE_READS
                and t.annotations and t.annotations.readOnlyHint is True]
    if not selected:
        raise RuntimeError("No configured native read-only MCP tools are available")
    # Tool-count changes are informational, never an exact-54 startup gate.
    return selected


def configuration(url, token, work, names):
    provider = "rcagent-native"
    model = provider + "/" + settings.LLM_MODEL
    output_roots = [Path.home() / ".local/share/opencode/tool-output", Path("/tmp/opencode/tool-output")]
    read_paths = [str(p / "*") for p in output_roots]
    permission = {"*": "deny", "read": {"*": "deny", str(work / "*"): "allow",
        **{p: "allow" for p in read_paths}}, "grep": "allow", "glob": "allow",
        "external_directory": {"*": "deny", **{p: "allow" for p in read_paths}}, "todowrite": "allow"}
    tools = {"*": False, "read": True, "grep": True, "glob": True, "todowrite": True}
    for name in names:
        permission["insights_" + name] = "allow"
        tools["insights_" + name] = True
    config = {"model": model, "small_model": model, "enabled_providers": [provider],
        "autoupdate": False, "share": "disabled", "snapshot": False, "instructions": [], "plugin": [],
        "provider": {provider: {"npm": "@ai-sdk/openai-compatible", "name": "RCAgent native investigation",
            "options": {"baseURL": settings.LLM_BASE_URL, "apiKey": settings.LLM_API_KEY},
            "models": {settings.LLM_MODEL: {"id": settings.LLM_MODEL, "name": settings.LLM_MODEL}}}},
        "mcp": {"insights": {"type": "remote", "url": url, "enabled": True, "oauth": False,
            "headers": {"Authorization": "Bearer " + token} if token else {}}},
        "permission": permission, "tools": tools, "agent": {"build": {"permission": permission}}}
    return config, {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(config)}
