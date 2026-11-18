"""One same-role recovery, sharing the original step budget and total deadline."""

import json
import time
from langchain_core.messages import HumanMessage
from config import settings
from .model_requests import ModelRequestError
from .repository import WorkspaceError, new_id
from .recovery import record_issue
from .run_stops import persisted_reason

RECOVERABLE = {
    "model_timeout",
    "model_connection_error",
    "model_service_unavailable",
    "provider_context_limit",
}


def recovery_messages(messages):
    """Remove only role input copies already present in the same workbench."""
    result = []
    for message in messages:
        if message.type != "human":
            result.append(message)
            continue
        try:
            body = json.loads(message.content)
        except (ValueError, TypeError):
            result.append(message)
            continue
        if not isinstance(body, dict) or body.get("role") not in (
            "Triage",
            "Planner",
            "Worker",
            "Supervisor",
        ):
            result.append(message)
            continue
        body = dict(body)
        for key in ("resources", "verified_resources", "questions"):
            body.pop(key, None)
        if isinstance(body.get("task"), dict):
            body["task_id"] = body.pop("task")["id"]
        body["recovery"] = (
            "Continue this same role/run from the current workbench. The previous request failed; "
            "its missing response is not evidence. Duplicated resources/questions/task bodies were removed here "
            "and remain in the workbench. Preserve observations, opposing evidence and unresolved gaps."
        )
        result.append(HumanMessage(content=json.dumps(body, ensure_ascii=False)))
    return result


def step_available(b):
    from .investigation_phases import active, can_send

    if active(b):
        can_send(b)
        return
    from .skill_knowledge import business_steps

    limits = [b.local_model_limit] if b.local_model_limit is not None else []
    if limits and b.model_steps >= min(limits):
        raise WorkspaceError("role model step budget exhausted")
    if b.role == "worker" and business_steps(b) >= settings.WORKER_MAX_MODEL_STEPS:
        raise WorkspaceError("role model step budget exhausted")


async def invoke_with_recovery(b, model, messages, *, phase, request):
    from context.builder import prepare
    from .runtime import audited_send

    for _ in range(2):
        from .skill_knowledge import hydrate

        await hydrate(b)
        from .investigation_phases import active as phased, refresh, remaining

        if phased(b):
            await refresh(b)
            if (
                (request or {}).get("response_schema")
                or (request or {}).get("tool_choice")
            ) and not remaining(b):
                raise WorkspaceError(
                    "phase_budget_exhausted: structured submission cannot use pure skill allowance"
                )
        step_available(b)
        started = time.monotonic()
        active = recovery_messages(messages) if b.recovery_view else messages
        prepared, manifest = await prepare(b, active, phase, request=request)
        from .investigation_phases import reserve

        await reserve(b)
        b.model_steps += 1
        manifest.update(
            model_call_reference=new_id("model_call"),
            model_phase=phase,
            total_role_model_steps=b.model_steps,
            knowledge_rounds_used=b.knowledge_rounds,
        )
        await b.repository.save(b.incident_id, "context", manifest)
        b.last_context_id = manifest["id"]
        try:
            return await audited_send(
                model, prepared, b, manifest, (time.monotonic() - started) * 1000
            )
        except ModelRequestError as error:
            previous = [
                row
                for row in await b.repository.all(b.incident_id, "recovery_issue")
                if (
                    row.get("run_id"),
                    row.get("role"),
                    row.get("task_id"),
                    row.get("attempt_id"),
                )
                == (b.run_id, b.role, b.task_id, b.attempt_id)
                and row.get("action") == "same_role_smaller_view_retry"
            ]
            if (
                manifest.get("context_policy") != "unified_sre"
                or b.recovery_used
                or previous
                or error.detail["code"] not in RECOVERABLE
                or await persisted_reason(b)
                or b.deadline - time.time() <= 10
            ):
                raise
            # One flag for the entire bound role/attempt, not one retry per call
            # or segment. The sender disables its nested retries for this policy.
            step_available(b)
            b.recovery_used = b.recovery_view = True
            await record_issue(
                b,
                error.detail["code"],
                "same_role_smaller_view_retry",
                detail={
                    "context_id": b.last_context_id,
                    "response_id": b.last_response_id,
                    "steps_used": b.model_steps,
                    "deadline_unchanged": b.deadline,
                },
            )
    raise WorkspaceError("bounded model recovery exhausted")
