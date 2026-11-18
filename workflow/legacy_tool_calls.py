"""Historical StageProposal tool-call adapter; the unified Worker uses tool_execution."""

import asyncio
import json
import sqlite3
from config import settings
from tools import observability_adapter as gateway
from tools.rca_tools import submit_final_diagnosis
from workspace import investigations
from workspace.stage_tools import COMMIT_TOOL, commit_investigation_stage, mixed_batch
from workspace.read_tools import READ_TOOLS
from workspace.repository import WorkspaceError
from workspace.tasks import finish
from workspace.queries import execute
from workflow import tool_execution


async def execute_calls(binding, calls, tools):
    """Sequential reservation gives every proposed call its matching ToolMessage."""
    messages, submission, fatal, checkpoint = [], None, None, None
    returned_chars = 0
    tools_by_name = {t.name: t for t in tools}
    local = {t.name: t for t in READ_TOOLS}
    local[COMMIT_TOOL] = commit_investigation_stage
    mixed = mixed_batch(calls)
    for call in calls:
        name, args, call_id = call["name"], call["args"], call["id"]
        try:
            if fatal:
                raise WorkspaceError("attempt stopped after storage failure: " + fatal)
            if name == "submit_final_diagnosis":
                if submission is not None:
                    raise WorkspaceError("multiple submissions in one model step")
                # Validate against the existing submission tool schema, then commit atomically.
                submission_args = submit_final_diagnosis.args_schema.model_validate(
                    {**args, "stage_proposal": None, "tool_call_id": call_id}
                )
                response = submission_args.model_dump(exclude={"tool_call_id"})
                if mixed:
                    raise WorkspaceError(
                        "stage commit and final submission must travel in separate batches"
                    )
                # A handoff carried with the submission is validated and saved first;
                # the assessment keeps its documented fields.
                from agents.stage_handoff import accept_role_stage

                handoff = await accept_role_stage(
                    binding,
                    getattr(binding, "active_role_model", None) or settings.get_chat_model(),
                    args.get("stage_proposal"),
                    owner="worker",
                    phase="final_submission",
                )
                if handoff and not handoff.get("accepted"):
                    response["gaps"] = list(response.get("gaps") or []) + [
                        "Optional final stage was not accepted: " + str(handoff.get("reason"))
                    ]
                checkpoint_id = response.get("checkpoint_id") or (handoff or {}).get(
                    "checkpoint_id"
                )
                if checkpoint_id:
                    checkpoint = await investigations.active_checkpoint(binding, binding.role)
                    if not checkpoint or checkpoint["id"] != checkpoint_id:
                        raise WorkspaceError(
                            "final checkpoint is not this task/attempt active head"
                        )
                observation_ids = (
                    response.get("observation_ids") or (handoff or {}).get("observation_ids") or []
                )
                observations = await investigations.observations_for(binding, observation_ids)
                if len({row["id"] for row in observations}) != len(set(observation_ids)):
                    raise WorkspaceError("unknown final observation reference")
                for observation in observations:
                    if observation.get("run_id") != binding.run_id:
                        raise WorkspaceError("final observation belongs to another run")
                    evidence_id = (observation.get("source_ref") or {}).get("evidence_id")
                    if evidence_id:
                        response["supporting_evidence_ids"] = list(
                            dict.fromkeys(
                                response.get("supporting_evidence_ids", []) + [evidence_id]
                            )
                        )
                response.pop("stage_proposal", None)
                assessment = await finish(binding, response)
                submission = assessment
                result = {"status": "submitted", "answer_status": assessment["answer_status"]}
            elif submission is not None:
                raise WorkspaceError("attempt already submitted")
            elif name in local:
                if name != COMMIT_TOOL:
                    from context.stage_budget import next_page_budget

                    cap = await next_page_budget(binding, calls, returned_chars)
                    if cap is None:
                        raise WorkspaceError(
                            "checkpoint_required: commit this stage before another read"
                        )
                    if (
                        settings.CONTEXT_LIFECYCLE_MODE == "active"
                        and "max_chars" in local[name].args
                    ):
                        args = {**args, "max_chars": min(args.get("max_chars") or cap, cap)}
                if name == COMMIT_TOOL and mixed:
                    raise WorkspaceError(
                        "a stage handoff must not share its model step with queries, reads or "
                        "the final submission; submit it alone"
                    )
                result = await tool_execution.invoke_local(local[name], args, call_id)
                if name == COMMIT_TOOL and result.get("status") == "accepted":
                    checkpoint = result["checkpoint_id"]
                    binding.checkpoint_required = False
            elif name in tools_by_name:
                from context.stage_budget import next_page_budget, remote_result_card

                if await next_page_budget(binding, calls, returned_chars) is None:
                    raise WorkspaceError(
                        "checkpoint_required: commit this stage before another query"
                    )
                tool = tools_by_name[name]

                async def raw_call(effective):
                    result = await asyncio.wait_for(
                        tool.ainvoke(effective), gateway.tool_timeout_seconds()
                    )
                    return gateway._decode(result)

                result = await execute(binding, name, args, raw_call, call_id)
                if settings.CONTEXT_LIFECYCLE_MODE == "active":
                    result = remote_result_card(result)
            else:
                raise WorkspaceError("unknown tool")
            messages.append(
                tool_execution.paired_reply(
                    call,
                    json.dumps(result, ensure_ascii=False, default=str),
                    status="error" if result.get("status") in ("error", "rejected") else "success",
                )
            )
        except Exception as exc:
            if isinstance(exc, (OSError, sqlite3.DatabaseError)):
                fatal = type(exc).__name__ + ": " + str(exc)
            messages.append(
                tool_execution.paired_reply(
                    call, json.dumps({"status": "rejected", "reason": str(exc)}), status="error"
                )
            )
        from context.request_budget import serialized_chars

        returned_chars += serialized_chars(messages[-1].model_dump(mode="json"))
    return messages, submission, fatal, checkpoint
