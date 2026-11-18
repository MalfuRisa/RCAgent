"""Compatibility node names and historical submission adapters."""

from workflow.legacy_tool_calls import execute_calls


async def worker_tools():
    from tools.mcp_tools import _mcp_client, get_mcp_tools, _TOOLS

    return _TOOLS if _TOOLS is not None else await get_mcp_tools(_mcp_client)


async def rcaAgent(state):
    """Deprecated node name; delegates to the common SRE role."""
    from workflow.role_compat import invoke_role

    return await invoke_role(state, "worker")


async def explain_analysis(state):
    return {
        "insights": [state.get("rca_output", {}).get("diagnosis", "Unresolved")],
        "prev_steps": [],
    }


async def format_response(state):
    return {"rca_analyses_list": [state.get("rca_output", {})]}


def build_rca_graph():
    """Deprecated public graph name; delegates to the one SRE graph."""
    from workflow.investigation import build_investigation_graph

    return build_investigation_graph()


rca_agent_graph = build_rca_graph()
