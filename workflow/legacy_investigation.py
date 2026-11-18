"""One authoritative SRE graph; platforms supply tools rather than role policies."""

from langgraph.graph import START, END, StateGraph

from agents.investigation_planning import triage, planner
from agents.investigation_supervisor import supervisor
from config import settings
from context.investigation import common_preparers
from models.states import SreParentState
from workspace.runtime import initialize_incident
from .gate import evidence_gate
from .scopes import investigation_node, role_scope
from .workers import run_workers
from .routing import triage_blocked


def after_triage(state):
    if triage_blocked(state):
        return "evidence_gate"
    return "planner_agent"


def after_supervisor(state):
    if state.get("tasks_to_execute_ids") and not state.get("execution_error"):
        return "rca_agent"
    return "evidence_gate"


def continue_or_gate(state, next_node):
    return "evidence_gate" if state.get("execution_error") else next_node


def build_investigation_graph(*, catalog=None, model=None):
    async def catalog_for(state):
        if catalog is not None:
            return catalog
        from platforms.gateway import make_catalog

        return await make_catalog(state)

    def model_for():
        return model if model is not None else settings.get_chat_model()

    async def initialize(state):
        result = await initialize_incident(state)
        initialized = dict(state, **result)
        if triage_blocked(state):
            return result
        with role_scope(initialized, "triage", context_preparers=common_preparers()) as binding:
            try:
                directory = await catalog_for(initialized)
                binding.capability_catalog = directory
                if directory.bootstrap:
                    await directory.bootstrap(binding, state)
            except BaseException as error:
                import asyncio
                from workspace.runtime import record_execution
                from workspace.model_requests import safe_message

                await asyncio.shield(
                    record_execution(
                        binding,
                        "cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                        {"code": "initialization_failed", "message": safe_message(error)},
                    )
                )
                raise
        return result

    builder = StateGraph(SreParentState)
    builder.add_node("initialize_workspace", initialize)
    for name, role, action in (
        ("triage_agent", "triage", triage),
        ("planner_agent", "planner", planner),
        ("rca_agent", "worker", run_workers),
        ("supervisor_agent", "supervisor", supervisor),
    ):
        builder.add_node(name, investigation_node(role, action, catalog_for, model_for))
    builder.add_node("evidence_gate", evidence_gate)
    builder.add_edge(START, "initialize_workspace")
    builder.add_edge("initialize_workspace", "triage_agent")
    builder.add_conditional_edges("triage_agent", after_triage, ["planner_agent", "evidence_gate"])
    builder.add_conditional_edges(
        "planner_agent", lambda s: continue_or_gate(s, "rca_agent"), ["rca_agent", "evidence_gate"]
    )
    builder.add_conditional_edges(
        "rca_agent",
        lambda s: continue_or_gate(s, "supervisor_agent"),
        ["supervisor_agent", "evidence_gate"],
    )
    builder.add_conditional_edges(
        "supervisor_agent", after_supervisor, ["rca_agent", "evidence_gate"]
    )
    builder.add_edge("evidence_gate", END)
    return builder.compile()
