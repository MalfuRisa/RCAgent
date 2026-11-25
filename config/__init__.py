"""Configuration module.

``GPT5_MINI`` / ``GPT5_1`` are resolved lazily so that importing configuration
for the MCP Gateway (``MCP_CONFIG``, ``TOOLS_ALLOWED``) does not require LLM
credentials. ``from config import GPT5_MINI`` keeps working unchanged.
"""
from typing import Any

from .settings import (
    MAX_TOOL_CALLS,
    MCP_CONFIG,
    TOOLS_ALLOWED,
    KYLIN_TOOLS_ALLOWED,
    KYLIN_DISCOVERY_TOOLS_ALLOWED,
    KYLIN_ALL_TOOLS_ALLOWED,
    RCA_TASKS_PER_ITERATION,
    MAX_DAILY_OPENAI_TOKEN_LIMIT,
    TRACE_SERVICE_STARTING_POINT,
    apply_config_overrides,
    get_mcp_config,
    get_chat_model,
)


def __getattr__(name: str) -> Any:
    if name in ("GPT5_MINI", "GPT5_1"):
        from . import settings

        return getattr(settings, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    'GPT5_MINI',
    'GPT5_1',
    'MAX_TOOL_CALLS',
    'MCP_CONFIG',
    'TOOLS_ALLOWED',
    'KYLIN_TOOLS_ALLOWED',
    'KYLIN_DISCOVERY_TOOLS_ALLOWED',
    'KYLIN_ALL_TOOLS_ALLOWED',
    'RCA_TASKS_PER_ITERATION',
    'TRACE_SERVICE_STARTING_POINT',
    'MAX_DAILY_OPENAI_TOKEN_LIMIT',
    'apply_config_overrides',
    'get_mcp_config',
    'get_chat_model'
]
