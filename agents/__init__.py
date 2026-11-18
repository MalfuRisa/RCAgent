"""Agents module exports.

Re-exports stay lazy so data readers remain usable without loading a workflow.
Historical role graph names now delegate to the same investigation factory;
they no longer select separate role or domain graphs.
"""

from typing import Any

__all__ = [
    # Triage
    "triage_agent_graph",
    "triage_agent",
    "get_triage_data",
    "triage_router",
    # Planner
    "planner_agent_graph",
    "planner_agent",
    "get_resource_dependencies",
    # RCA
    "rca_agent_graph",
    "rcaAgent",
    "explain_analysis",
    "format_response",
    # Supervisor
    "supervisor_agent_graph",
    "supervisor_agent",
]

_MODULE_BY_NAME = {
    "triage_agent_graph": "triage_agent",
    "triage_agent": "triage_agent",
    "get_triage_data": "triage_agent",
    "triage_router": "triage_agent",
    "planner_agent_graph": "planner_agent",
    "planner_agent": "planner_agent",
    "get_resource_dependencies": "planner_agent",
    "rca_agent_graph": "rca_agent",
    "rcaAgent": "rca_agent",
    "explain_analysis": "rca_agent",
    "format_response": "rca_agent",
    "supervisor_agent_graph": "supervisor_agent",
    "supervisor_agent": "supervisor_agent",
}


def __getattr__(name: str) -> Any:
    module_name = _MODULE_BY_NAME.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    import importlib

    module = importlib.import_module(f".{module_name}", __name__)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(__all__)
