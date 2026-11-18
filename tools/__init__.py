"""Tools module exports.

The re-exports are resolved lazily (PEP 562) on purpose:

* ``.mcp_tools`` connects to the MCP Gateway and ``config`` builds LLM clients
  at import time. Importing this package must therefore not happen just because
  a caller wants :mod:`tools.incident_text` or :mod:`tools.triage_discovery`.
* Side-effect-free modules stay usable in unit tests without credentials and
  without a running Gateway.

``from tools import TOOLS`` and ``from tools import observability_adapter`` keep
working exactly as before.
"""

from typing import Any

__all__ = [
    "TOOLS",
    "get_mcp_tools",
    "submit_final_diagnosis",
]


def __getattr__(name: str) -> Any:
    if name in ("TOOLS", "get_mcp_tools"):
        from . import mcp_tools

        return getattr(mcp_tools, name)
    if name == "submit_final_diagnosis":
        from . import rca_tools

        return getattr(rca_tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)