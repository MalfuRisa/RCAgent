"""Run planned attempts and preserve their terminal states on failure or cancellation."""

import asyncio

from context.investigation import common_preparers
from workspace.model_requests import ModelRequestError
from workspace.recovery import record_issue
from workspace.repository import WorkspaceError
from workspace.run_stops import persisted_reason
from workspace.tasks import finish, start
from .errors import execution_error
from .scopes import role_scope


def unresolved_submission(message):
    return {
        "diagnosis": "Unresolved",
        "gaps": [message],
        "supporting_evidence_ids": [],
        "opposing_evidence_ids": [],
    }


async def run_attempt(binding, model, state, catalog):
    from agents.investigation_worker import worker

    try:
        return await worker(binding, model, state, catalog)
    except asyncio.CancelledError:
        await asyncio.shield(
            finish(
                binding,
                unresolved_submission("Worker cancelled; outcome unknown"),
                status="cancelled",
                stop_reason="execution_cancelled",
            )
        )
        raise
    except (WorkspaceError, ModelRequestError, TimeoutError) as error:
        detail = execution_error(error, "worker")
        await finish(
            binding,
            unresolved_submission(str(error)[:2000]),
            status="failed",
            stop_reason=detail["code"],
        )
        if await persisted_reason(binding) or detail["code"] in (
            "model_authentication_failed",
            "investigation_deadline",
        ):
            raise
        await record_issue(
            binding, detail["code"], "preserve_evidence_and_finalize", detail=str(error)[:2000]
        )
        return {"status": "failed", "execution_error": detail}


async def run_workers(binding, model, state, catalog):
    analyses = []
    for task in state.get("rca_tasks", []):
        attempt = await start(binding, task.task_id)
        task_state = dict(
            state, task_id=task.task_id, attempt_id=attempt["id"], resource_id=task.resource_id
        )
        with role_scope(task_state, "worker", context_preparers=common_preparers()) as owner:
            owner.capability_catalog = catalog
            result = await run_attempt(owner, model, state, catalog)
        analyses.append(
            {
                "task_id": task.task_id,
                "attempt_id": attempt["id"],
                "run_id": binding.run_id,
                "execution_status": "finished" if result.get("status") == "accepted" else "failed",
                **result,
            }
        )
    return {"rca_analyses_list": analyses}
