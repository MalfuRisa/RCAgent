"""One model/tool step with paired replies, policy enforcement and response processing audit."""

import json
import asyncio

import jsonschema
from langchain_core.messages import HumanMessage

from context.request_budget import tool_schemas
from workspace import model_audit
from workspace.repository import WorkspaceError
from workspace.runtime import invoke_model
from . import tool_execution


async def take_step(
    binding, model, tools, messages, *, phase, empty_instruction, denied_reason
):
    from workspace.skill_knowledge import (
        enabled,
        hydrate,
        remaining,
        read_skill,
        credit_round,
    )

    await hydrate(binding)
    from workspace import investigation_phases as stages

    if stages.active(binding):
        await stages.refresh(binding)
    actual_phase = (
        "finalize_report"
        if phase == "finalize_report"
        else ("checkpoint_control" if binding.checkpoint_required else phase)
    )
    tools = list(tools)
    if enabled(binding) and remaining(binding):
        tools.append(read_skill)
    # Rebind before sending if the normal schema leaves no safe handoff space.
    # At most one alternate preparation; neither refused request reaches the model.
    for preparation in range(2):
        actual_phase = (
            "finalize_report"
            if phase == "finalize_report"
            else ("checkpoint_control" if binding.checkpoint_required else phase)
        )
        if stages.active(binding):
            await stages.refresh(binding)
            active = stages.tools_for(binding, tools)
            actual_phase = stages.view(binding)["phase"]
        else:
            active = (
                [
                    t
                    for t in tools
                    if t.name
                    in (
                        "commit_investigation_stage",
                        "complete_task",
                        "submit_report",
                        "read_skill",
                    )
                ]
                if binding.checkpoint_required
                else tools
            )
        try:
            answer = await invoke_model(
                model.bind_tools(active, parallel_tool_calls=False),
                messages,
                phase=actual_phase,
                request={"tools": tool_schemas(active), "parallel_tool_calls": False},
            )
            tools = active
            break
        except WorkspaceError as error:
            if (
                stages.active(binding)
                and str(error) == "phase_transition_required"
                and not preparation
            ):
                await stages.refresh(binding)
                continue
            if preparation or not binding.checkpoint_required:
                raise
    messages.append(answer)
    calls = getattr(answer, "tool_calls", [])
    if not calls:
        await model_audit.processed(
            binding,
            binding.role + "_response",
            {"status": "rejected", "reason": "tool call required"},
        )
        messages.append(HumanMessage(content=empty_instruction))
        await stages.settle(binding, [], [])
        return []

    lookup = {tool.name: tool for tool in tools}
    outcomes = []
    for call in calls:
        try:
            if len(calls) != 1 or call["name"] not in lookup:
                raise WorkspaceError(
                    ("phase_disallowed: " if stages.active(binding) else "")
                    + denied_reason
                )
            if (
                stages.active(binding)
                and getattr(binding, "phase_step_category", None) == "skill"
                and call["name"] != "read_skill"
            ):
                raise WorkspaceError(
                    "phase_disallowed: only remaining pure skill allowance is available"
                )
            arguments = dict(call["args"])
            if call["name"] in (
                "query_diagnostics",
                "read_evidence",
                "read_investigation",
                "read_artifact",
            ):
                from context.stage_budget import next_page_budget

                allowance = await next_page_budget(binding, [call])
                if allowance is None and not stages.active(binding):
                    raise WorkspaceError(
                        "checkpoint_required: commit this stage before another query/read"
                    )
                if allowance is None:
                    allowance = 512
                if call["name"].startswith("read_"):
                    requested = arguments.get("max_chars")
                    from config import settings

                    if requested is None or (
                        type(requested) is int
                        and 0 < requested <= settings.CONTEXT_READ_MAX_CHARS
                    ):
                        arguments["max_chars"] = min(
                            requested or settings.CONTEXT_READ_MAX_CHARS, int(allowance)
                        )
            from workspace.run_stops import persisted_reason

            stop_reason = await persisted_reason(binding)
            if stop_reason:
                raise WorkspaceError("execution_stopped: " + str(stop_reason))
            await stages.guard(binding, call["name"])
            binding.phase_skill_execution = (
                stages.active(binding)
                and getattr(binding, "phase_step_category", None) == "skill"
                and call["name"] == "read_skill"
            )
            try:
                result = await tool_execution.invoke_local(
                    lookup[call["name"]], arguments, call["id"]
                )
            finally:
                binding.phase_skill_execution = False
        except (WorkspaceError, ValueError, jsonschema.ValidationError) as error:
            from .submission_feedback import details

            result = {
                "status": "rejected",
                "reason": str(error)[:2200],
                "field_errors": details(error),
                "phase": actual_phase,
                "allowed_tools": list(lookup),
            }
        except BaseException as error:
            from workspace.model_requests import safe_message

            await asyncio.shield(
                model_audit.processed(
                    binding,
                    call["name"],
                    {
                        "status": "failed",
                        "type": type(error).__name__,
                        "reason": safe_message(error),
                    },
                    tool_call_id=call["id"],
                )
            )
            raise
        if result.get("status") in ("rejected", "error"):
            result = {
                **result,
                "phase": actual_phase,
                "allowed_tools": list(lookup),
            }
            from .submission_feedback import feedback

            guidance = feedback(call["name"], result, actual_phase)
            if not enabled(binding) or not remaining(binding):
                guidance.pop("skill_hint", None)
            result.update(guidance)
            if stages.active(binding):
                result["remaining_stages"] = stages.view(binding)["remaining"]
                if guidance.get("skill_hint"):
                    from workspace.skill_knowledge import activate

                    hint = guidance["skill_hint"]
                    await activate(
                        binding,
                        hint["name"],
                        "submission_rejected",
                        resource=hint["resource"],
                    )
            if enabled(binding) and not remaining(binding):
                result["guidance_status"] = (
                    "knowledge allowance exhausted; preserve remaining submission path"
                )
        await model_audit.processed(
            binding, call["name"], result, tool_call_id=call["id"]
        )
        messages.append(
            tool_execution.paired_reply(
                call,
                json.dumps(
                    result, ensure_ascii=False, default=lambda value: value.model_dump()
                ),
                status="error"
                if result.get("status") in ("error", "rejected")
                else "success",
            )
        )
        outcomes.append((call["name"], result))
    await credit_round(binding, calls)
    await stages.settle(binding, calls, outcomes)
    return outcomes
