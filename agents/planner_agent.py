"""Compatibility node names and historical Gateway dependency readers."""

from models import PlannerAgentState, Symptom
from config import settings as config_settings
from tools.observability_adapter import collect_resource_context


async def get_resource_dependencies(symptom: Symptom, state: PlannerAgentState) -> dict:
    """Get Gateway-derived context for a symptom's affected resource.

    Args:
        symptom: Symptom with affected resource information

    Returns:
        Dictionary with data and infrastructure dependencies
    """
    return await collect_resource_context(symptom, state)


async def _dependency_references():
    """Evidence ids for dependency facts this run already archived."""
    from context.materials import evidence_digest
    from workspace.runtime import current

    binding = current.get()
    if binding is None:
        return []
    lines, _ = await evidence_digest(
        binding,
        {"search_k8s_targets", "get_service_backends", "query_metrics", "compare_pod_with_peers"},
    )
    return lines


async def planner_agent(state):
    """Deprecated node name; delegates to the common SRE role."""
    from workflow.role_compat import invoke_role

    return await invoke_role(state, "planner")


def build_planner_graph():
    """Deprecated public graph name; delegates to the one SRE graph."""
    from workflow.investigation import build_investigation_graph

    return build_investigation_graph()


planner_agent_graph = build_planner_graph()
