"""Public model responses and processing outcomes, including the terminal step."""

import os
from config import settings
from .artifacts import write_artifact
from .repository import new_id


def public_value(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            k: public_value(v)
            for k, v in value.items()
            if k.lower()
            not in (
                "reasoning_content",
                "internal_reasoning",
                "authorization",
                "api_key",
            )
        }
    if isinstance(value, (list, tuple)):
        return [public_value(v) for v in value]
    if isinstance(value, str):
        for secret in (settings.LLM_API_KEY, os.environ.get("OPENAI_API_KEY")):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


async def response(b, manifest, result=None, error=None):
    payload = {
        "response": public_value(result),
        "error": public_value(error),
        "response_available": result is not None,
        "provider_raw_available": getattr(result, "type", None) == "ai"
        or isinstance(result, dict)
        and result.get("raw") is not None,
    }
    artifact = await write_artifact(b, payload)
    await b.repository.save(b.incident_id, "artifact", artifact)
    row = {
        "id": new_id("model_response"),
        "run_id": b.run_id,
        "role": b.role,
        "task_id": b.task_id,
        "attempt_id": b.attempt_id,
        "context_id": manifest["id"],
        "model_call_id": manifest["model_call_reference"],
        "artifact_id": artifact["id"],
        "response_available": result is not None,
        "provider_raw_available": payload["provider_raw_available"],
    }
    saved = await b.repository.save(
        b.incident_id, "model_response", row, action="model_response_recorded"
    )
    b.last_response_id = saved["id"]
    attempts = await b.repository.all(b.incident_id, "model_attempt")
    if manifest.get("context_policy") == "unified_sre" and any(
        r.get("logical_call_id") == manifest["model_call_reference"]
        and r.get("send_started_at")
        for r in attempts
    ):
        from .skill_knowledge import record_delivery

        await record_delivery(b, manifest)
    if result is not None and manifest.get("context_policy") == "unified_sre":
        from .evidence_references import capture_context

        await capture_context(b, manifest)


async def processed(b, operation, outcome, *, tool_call_id=None):
    return await b.repository.save(
        b.incident_id,
        "model_action",
        {
            "id": new_id("model_action"),
            "run_id": b.run_id,
            "role": b.role,
            "task_id": b.task_id,
            "attempt_id": b.attempt_id,
            "context_id": b.last_context_id,
            "response_id": getattr(b, "last_response_id", None),
            "tool_call_id": tool_call_id,
            "operation": operation,
            "outcome": public_value(outcome),
        },
        action="model_action_recorded",
    )
