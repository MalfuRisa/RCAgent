"""Common visible-input metering/publication; no domain or role selection."""

import json
from langchain_core.messages import SystemMessage
from config import settings
from workspace.artifacts import write_artifact
from workspace.repository import WorkspaceError, new_id
from .selection import message_chars
from . import budget_policy


WORKBENCH_PREFIX = "Incident workbench (scope bound by program):"


def workbench_message(payload):
    return WORKBENCH_PREFIX + "\n" + payload


def visible_chars(payload, messages):
    return message_chars([SystemMessage(content=workbench_message(payload))] + list(messages))


def component_chars(payload_text, messages, request):
    """Same metering basis as the request, split by where the characters come from."""
    import json as _json

    payload = {}
    try:
        payload = _json.loads(payload_text)
    except ValueError:
        payload = {}
    parts = {"workbench": len(payload_text), "messages": message_chars(messages)}
    for key in (
        "evidence",
        "evidence_directory",
        "tasks",
        "resources",
        "assessments",
        "investigation",
        "verified_read_details",
        "coverage",
        "windows",
    ):
        if key in payload:
            parts["workbench:" + key] = len(_json.dumps(payload[key], ensure_ascii=False))
    if request:
        if request.get("tools"):
            parts["tool_definitions"] = len(
                _json.dumps(request["tools"], ensure_ascii=False, default=str)
            )
        if request.get("response_schema"):
            parts["response_schema"] = len(
                _json.dumps(request["response_schema"], ensure_ascii=False, default=str)
            )
    return parts


def budget_basis(payload_text, messages, request):
    """The measured basis for one request, per the configured policy.

    v2 counts the bound tool schemas and the structured-output schema as well;
    v1 keeps the previous message-only measure. Both are always recorded, so a
    switch is auditable instead of silent (PRD §19.8.1).
    """
    v1_chars, v2_chars = request_chars(payload_text, messages, request)
    use_v2 = settings.CONTEXT_LIFECYCLE_MODE == "active" or settings.REQUEST_BUDGET_BASIS == "v2"
    basis = v2_chars if use_v2 else v1_chars
    return basis, v1_chars, v2_chars, ("v2" if use_v2 else "v1")


def request_chars(payload_text, messages, request):
    """``(v1, v2)`` for one call: v1 keeps the old message measure, v2 the canonical request.

    v2 serializes the messages plus the schemas the caller says it binds; when the
    caller cannot describe them the manifest records that the part is unavailable
    instead of pretending the number is exact.
    """
    import json as _json

    v1 = message_chars([SystemMessage(content=workbench_message(payload_text))] + list(messages))
    from .request_budget import measure

    v2, _ = measure(
        [SystemMessage(content=workbench_message(payload_text))] + list(messages), request
    )
    return v1, v2


def base_manifest(binding, snapshot, phase):
    return {
        "id": new_id("context"),
        "run_id": binding.run_id,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "role": binding.role,
        "phase": phase,
        "mode": settings.CONTEXT_VIEW_MODE,
        "workspace_revision": snapshot["workspace_revision"],
        "resource_revisions": {r["id"]: r["revision"] for r in snapshot["resource"]},
        "injected_evidence_ids": [],
        "omitted_references_and_reasons": [],
        "read_events": [],
    }


async def publish(binding, manifest, visible):
    audit = await write_artifact(
        binding,
        [
            {
                "type": m.type,
                "content": m.content,
                "tool_calls": getattr(m, "tool_calls", []),
                "tool_call_id": getattr(m, "tool_call_id", None),
            }
            for m in visible
        ],
    )
    await binding.repository.save(binding.incident_id, "artifact", audit)
    manifest["visible_messages_artifact_id"] = audit["id"]
    await binding.repository.save(binding.incident_id, "context", manifest)
    return visible, manifest


async def refuse_context(
    binding, manifest, payload, messages, phase, *, reason, visible=None, protected=None, extra=None
):
    """Refuse before sending and keep the candidate input for explanation (§10.3)."""
    candidate = (
        visible
        if visible is not None
        else [SystemMessage(content=workbench_message(json.dumps(payload, ensure_ascii=False)))]
        + list(messages)
    )
    artifact = await write_artifact(
        binding,
        [
            {
                "type": m.type,
                "content": m.content,
                "tool_calls": getattr(m, "tool_calls", []),
                "tool_call_id": getattr(m, "tool_call_id", None),
            }
            for m in candidate
        ],
    )
    await binding.repository.save(binding.incident_id, "artifact", artifact)
    manifest.update(
        context_incomplete=True,
        disposition="rejected_before_send",
        candidate_messages_artifact_id=artifact["id"],
        rejection_reason=reason,
        input_chars=message_chars(candidate),
        phase=phase,
    )
    from .request_budget import measure

    # The caller's complete measurement takes precedence when schemas exist.
    manifest.update(
        budget_policy.audit((extra or {}).get("request_chars_v2") or measure(candidate)[0])
    )
    if protected is not None:
        manifest["protected_parts_exceed_budget"] = True
        manifest["protected_parts"] = protected
    if extra:
        manifest.update(extra)
    await binding.repository.save(
        binding.incident_id, "context", manifest, action="context_incomplete"
    )
    from .lifecycle import effective_mode

    code = "context_unsatisfiable" if effective_mode(binding) == "active" else "context_incomplete"
    raise WorkspaceError(code + ": " + reason)
