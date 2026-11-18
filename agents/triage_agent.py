"""Compatibility node names and historical Gateway discovery readers.

Normal role execution delegates to the single investigation workflow below.
The data readers remain for archived Gateway callers; they do not choose roles.
"""

import json
import logging
from models import TriageAgentState
from config import settings as config_settings
from tools.incident_text import resolve_incident_scope
from tools.observability_adapter import collect_triage_evidence
from tools.triage_discovery import collect_discovery_evidence

logger = logging.getLogger(__name__)
GPT5_MINI = None  # Historical patch hook; normal execution uses the common model provider.
BLOCKING_TRIAGE_STATUSES = (
    "awaiting_confirmation",
    "insufficient",
    "not_found",
    "discovery_failed",
)


def _insufficient_evidence(scope) -> dict:
    """Explicitly say "not enough information" instead of guessing a target."""
    clarification = {
        "reason": "insufficient_information",
        "question": (
            "请提供服务名（例如 “payment 服务最近 15 分钟一直报 500”）"
            "或明确的目标 Pod（target_cluster + target_namespace + target_resource）。"
        ),
        "missing": list(scope.reasons) or ["service"],
        "next_step": (
            "Re-run with an incident_text containing a service name, or with a "
            "concrete Pod target. RCAgent will not assume a cluster, namespace or service."
        ),
    }
    skipped = {"info": "Triage could not identify a target; this lookup was skipped."}
    return {
        "problematic_pods": {
            "status": "insufficient_information",
            "missing": clarification["missing"],
            "reason": "no service or Pod target could be identified",
        },
        "problematic_metrics": {
            "status": "insufficient_information",
            "missing": clarification["missing"],
            "reason": "no target to query metrics for",
        },
        "slow_traces": skipped,
        "problematic_traces": skipped,
        "triage_status": "insufficient",
        "clarification": clarification,
        "discovery_record": {
            "status": "insufficient",
            "scope": scope.as_summary(),
            "tool_calls": [],
            "facts": [],
            "inferences": [],
            "data_gaps": [
                {
                    "source": "incident_input",
                    "reason": "insufficient_information",
                    "impact": (
                        "no target was identified, so no observation was made; "
                        "this is not evidence that anything is healthy"
                    ),
                }
            ],
        },
    }


async def get_triage_data(state: TriageAgentState) -> dict:
    """Gather focused summary evidence for whichever input path applies.

    Args:
        state: Current triage agent state

    Returns:
        Dictionary with problematic pods, traces, and metrics
    """
    incident_text = str(state.get("incident_text") or "").strip()

    # No natural-language input at all -> keep the original structured path
    # exactly as it was, including its error messages.
    if not incident_text:
        return await collect_triage_evidence(state)

    scope = resolve_incident_scope(state)

    # Structured fields carry explicit values -> they win, discovery is skipped.
    if scope.source == "structured":
        logger.info(
            "Triage: structured target supplied; keeping it and ignoring parsed text for scope."
        )
        return await collect_triage_evidence(state)

    if scope.is_insufficient:
        logger.warning("Triage: incident_text did not yield a usable target; asking the operator.")
        return _insufficient_evidence(scope)

    logger.info(
        "Triage: discovering target from incident_text via MCP Gateway (service=%s).",
        scope.service,
    )
    return await collect_discovery_evidence(state)


async def _triage_digest(state) -> dict:
    """Evidence references for the three summary views, when already archived."""
    from context.materials import evidence_digest
    from workspace.runtime import current

    binding = current.get()
    if binding is None:
        return {}
    lines, covered = await evidence_digest(
        binding,
        {
            "resolve_pod",
            "get_pod_runtime_status",
            "get_pod_network_summary",
            "get_pod_application_summary",
        },
    )
    if not lines:
        return {}
    rendered = json.dumps(
        {
            "archived_evidence": lines,
            "note": "Key observations repeat in the workbench; read_evidence returns detail.",
        },
        ensure_ascii=False,
    )
    digest = {}
    if {"get_pod_runtime_status", "resolve_pod"} & covered:
        digest["pods"] = rendered
    if {"get_pod_network_summary", "get_pod_application_summary"} & covered:
        digest["metrics"] = rendered
    return digest


async def triage_agent(state):
    """Deprecated node name; delegates to the common SRE role."""
    from workflow.role_compat import invoke_role

    return await invoke_role(state, "triage")


def triage_router(state: TriageAgentState) -> str:
    """Stop the workflow while the target is unresolved.

    Returning ``needs_clarification`` here is what makes "ambiguous -> ask,
    never assume" real: the Planner never receives an unresolved incident.
    """
    status = str(state.get("triage_status") or "")
    if (
        status in BLOCKING_TRIAGE_STATUSES
        or state.get("evidence_status") == "unavailable"
        or state.get("execution_status") == "failed"
    ):
        logger.info("Triage router: triage_status=%s -> ending investigation.", status)
        return "needs_clarification"
    return "investigate"


def build_triage_graph():
    """Deprecated public graph name; delegates to the one SRE graph."""
    from workflow.investigation import build_investigation_graph

    return build_investigation_graph()


triage_agent_graph = build_triage_graph()
