"""Reserve handoff space before fetching another page or remote result."""

import json
from config import settings
from .request_budget import serialized_chars, tool_schemas
from .budget_policy import hard_limit

CONTROL_PROMPT = (
    "checkpoint_required: hand off this stage alone, prepare a final packet from verified observations, "
    "or submit an explicitly unresolved final result, using only bound tools. "
    "No new query or read is allowed until a valid handoff. Preserve active and unread material."
)
BOUNDED_ERROR = {
    "status": "rejected",
    "reason": "checkpoint_invalid",
    "field_errors": [
        {
            "field": "observation_proposals[0]",
            "reason": "not_shown_to_model: read this exact field before claiming it",
        }
    ],
}


def reserve():
    from workspace.stage_tools import commit_investigation_stage

    return {
        "commit_schema": serialized_chars(tool_schemas([commit_investigation_stage])),
        "control_prompt": serialized_chars(CONTROL_PROMPT),
        "error_receipt": serialized_chars(BOUNDED_ERROR),
        # One tool-call envelope plus a useful page must fit after the
        # current request; reserving only 512 chars can leave no room even
        # to issue the first read (before there is anything to hand off).
        "next_minimum_page": 4096,
    }


async def next_page_budget(binding, calls, returned_chars=0):
    from .lifecycle import effective_mode

    if effective_mode(binding) != "active":
        return settings.CONTEXT_READ_MAX_CHARS
    if binding.checkpoint_required:
        return None
    manifest = await binding.repository.read(
        binding.incident_id, binding.last_context_id, "context"
    )
    fixed = reserve()
    remaining = hard_limit() - (manifest.get("request_chars_v2") or 0)
    # The commit schema is already present in a normal bound tool set. Count it
    # in the audit, but reserve only its uncounted cost at the send boundary.
    extra = sum(fixed.values()) - (
        fixed["commit_schema"]
        if "commit_investigation_stage" in manifest.get("bound_tool_names", [])
        else 0
    )
    # The minimum page is a target for preparation, not an additional cost:
    # it is the very space returned here. Already returned replies, however,
    # consume this batch's shared allowance.
    remaining -= extra - fixed["next_minimum_page"] + serialized_chars(calls) + returned_chars
    allowed = min(8000, settings.CONTEXT_READ_MAX_CHARS, remaining)
    audit = {
        "request_context_id": manifest["id"],
        "reserve": fixed,
        "remaining_page_chars": allowed,
        "checkpoint_required": allowed < 512,
    }
    await binding.repository.transact(
        lambda db: binding.repository.event(
            db, binding.incident_id, binding.task_id, "stage_capacity_checked", audit
        )
    )
    if allowed < 512:
        binding.checkpoint_required = True
        return None
    return allowed


def remote_result_card(result):
    """The original has already been persisted; unseen fields stay unread."""
    query = result.get("_rcagent_query") or {}
    if not query.get("evidence_id"):
        return result
    return {
        "status": result.get("status"),
        "evidence_id": query["evidence_id"],
        "mode": query.get("mode"),
        "_rcagent_query": {"evidence_id": query["evidence_id"], "mode": query.get("mode")},
        "raw_shown": False,
        "coverage": "unread",
        "read": "read_evidence(evidence_id=<id>, field=<path>, max_chars=512)",
        "note": "Original persisted. Only concrete values in the workbench or a read page have been shown.",
    }


def worker_tool_set(tools, resource_type):
    if settings.CONTEXT_LIFECYCLE_MODE != "active":
        return tools
    names = (
        {
            "get_pod_runtime_status",
            "get_pod_network_summary",
            "get_pod_application_summary",
            "get_abnormal_requests",
            "trace_request",
            "compare_pod_with_peers",
            "diagnose_pod",
        }
        if resource_type == "pod"
        else {
            "search_k8s_targets",
            "get_service_backends",
            "query_metrics",
            "search_traces",
            "get_trace",
        }
    )
    return [tool for tool in tools if tool.name in names]
