"""Deprecated node names execute the same authoritative role implementation."""

from config import settings
from context.investigation import common_preparers
from workspace.runtime import current
from .scopes import role_scope, investigation_binding


async def invoke_role(state, role):
    from .routing import triage_blocked, clarification_result

    if role == "triage" and triage_blocked(state):
        return clarification_result(state)
    from agents.investigation_planning import triage, planner
    from .workers import run_attempt
    from agents.investigation_supervisor import supervisor
    from platforms.gateway import make_catalog

    action = {
        "triage": triage,
        "planner": planner,
        "worker": run_attempt,
        "supervisor": supervisor,
    }[role]
    binding = current.get()
    if binding is not None:
        from workspace.run_stops import persisted_reason

        stopped = await persisted_reason(binding)
        if stopped:
            from workspace.reports import conservative_report

            return {
                "execution_status": "failed",
                "execution_error": {"code": stopped, "message": "run already stopped"},
                "stop_reason": stopped,
                "tasks_to_execute_ids": [],
                "final_report": await conservative_report(
                    binding, stopped, state.get("symptoms", [])
                ),
            }
        catalog = getattr(binding, "capability_catalog", None) or await make_catalog(state)
        with investigation_binding(binding, catalog):
            return await action(binding, settings.get_chat_model(), state, catalog)
    if not state.get("incident_id"):
        from .legacy_investigation import build_investigation_graph

        return await build_investigation_graph().ainvoke(state)
    with role_scope(state, role, context_preparers=common_preparers()) as binding:
        return await action(binding, settings.get_chat_model(), state, await make_catalog(state))
