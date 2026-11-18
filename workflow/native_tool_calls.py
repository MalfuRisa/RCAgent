"""Validate in wire order, share run concurrency, settle every call in order."""

import asyncio
import json
import time

from langchain_core.messages import AIMessage, ToolMessage

from agents.investigation_transport import controlled
from agents.native_validation import validate
from platforms.business_policy import assigned_time
from platforms.insights_v5.native_task_scope import LocalRejection


def parse_tool_arguments(raw):
    """Recover an arguments value the SDK could not parse into an object."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None
    value = raw
    for _ in range(2):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return None
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            return None
    return None


def calls_from(message):
    calls = list(message.tool_calls) + [
        {**c, "args": parse_tool_arguments(c.get("args")), "raw_args": c.get("args"),
         "invalid": parse_tool_arguments(c.get("args")) is None}
        for c in message.invalid_tool_calls
    ]
    raw = message.additional_kwargs.get("tool_calls", [])
    order = {c.get("id"): i for i, c in enumerate(raw)}
    if raw:
        calls.sort(key=lambda c: order.get(c.get("id"), len(order)))
    return calls


def wire_message(response, calls):
    """One tool_call per real ID; unparsed raw arguments stay only in the audit event."""
    if response is None or not getattr(response, "invalid_tool_calls", None):
        return response
    return AIMessage(
        content=response.content,
        tool_calls=[
            {"name": c.get("name"), "args": c.get("args") or {}, "id": c.get("id")}
            for c in calls
            if c.get("id")
        ],
        id=response.id,
        response_metadata=response.response_metadata,
    )


def valid_ids(calls):
    ids = [c.get("id") for c in calls]
    return all(isinstance(i, str) and i for i in ids) and len(set(ids)) == len(ids)


def prepare(call, runner):
    name, arguments = call.get("name"), call.get("args")
    from workspace.native_materials import BUSINESS_TOOLS

    if (
        runner.execution
        and name in BUSINESS_TOOLS
        and name not in runner.execution.spec.business_tools
    ):
        raise LocalRejection(
            "scope_change_required",
            "Business tool is outside this task's assigned family",
        )
    if runner.execution:
        runner.execution.discovered_tools = runner.owner.native.tools
    if name not in runner.definitions:
        raise LocalRejection("tool_not_allowed", "Tool is not bound for this role/task")
    if call.get("invalid") or not isinstance(arguments, dict):
        raise LocalRejection(
            "invalid_argument", "Tool arguments must be complete JSON object"
        )
    json.dumps(arguments, allow_nan=False)
    hint = validate(name, arguments, runner.definitions[name]["function"]["parameters"])
    if name not in runner.local_handlers:
        runner.owner.native.authority.verify(
            runner.execution, name, arguments, lock=True
        )
    return hint


async def execute_batch(calls, runner, role=None):
    role = role or runner.role
    calls = [dict(call) for call in calls]
    messages, settled, errors, hints = [None] * len(calls), [None] * len(calls), {}, {}
    for index, call in enumerate(calls):
        try:
            definition = runner.definitions.get(call.get('name'))
            original = call.get('args')
            if definition and isinstance(original, dict) and not call.get('invalid'):
                effective = assigned_time(runner.execution, call['name'], original, definition['function']['parameters'])
                if effective is not original:
                    call['args'] = effective
                    runner.outputs.event('tool_window_defaulted', role,
                        {'tool': call['name'], 'original_arguments': original, 'time': effective['time']}, call_id=call['id'])
            previous = runner.execution.dataset if runner.execution else None
            hints[index] = prepare(call, runner)
            if runner.execution and runner.execution.dataset and not previous:
                runner.outputs.event(
                    "task_scope_locked",
                    role,
                    {"dataset": runner.execution.dataset.model_dump()},
                )
        except Exception as exc:  # noqa: BLE001 -- each malformed call must settle independently
            errors[index] = (
                exc.error
                if isinstance(exc, LocalRejection)
                else {
                    "code": type(exc).__name__,
                    "message": str(exc),
                    "source": "rcagent",
                    "stage": "validation",
                    "mcp_call_started": False,
                }
            )
            if errors[index]["code"] == "scope_change_required" and runner.execution:
                runner.execution.scope_changed = True

    async def one(index, call):
        outcome = dict(
            call_id=call["id"],
            tool=call.get("name"),
            arguments=call.get("args"),
            role=role,
            started_at=time.time(),
            raw_result=None,
            **runner.ids,
        )
        source_id = None
        try:
            if index in errors:
                outcome.update(status="tool_error", error=errors[index])
                runner.outputs.event(
                    "tool_rejected", role, errors[index], call_id=call["id"]
                )
                return
            async with runner.owner.tool_semaphore:
                runner.owner.check_active()
                name, args = call["name"], call["args"]
                runner.outputs.event(
                    "tool_started",
                    role,
                    {"tool": name, "arguments": args},
                    call_id=call["id"],
                )
                if name in runner.local_handlers:
                    outcome.update(
                        status="ok",
                        raw_result={
                            "structuredContent": runner.local_handlers[name](**args)
                        },
                    )
                else:
                    remote = await runner.owner.native.call(
                        call["id"],
                        name,
                        args,
                        role,
                        execution=runner.execution,
                        trace_ids=runner.ids,
                    )
                    outcome.update(remote)
        except asyncio.CancelledError:
            outcome.update(
                status="cancelled",
                error={
                    "code": "cancelled",
                    "message": "Cancelled; remote completion may be unknown",
                },
            )
            raise
        except Exception as exc:  # noqa: BLE001 -- partial batch success survives one call failure
            outcome.update(
                status="tool_error",
                error={
                    "code": type(exc).__name__,
                    "message": runner.outputs.redact(str(exc)),
                },
            )
        finally:
            outcome["mcp_call_started"] = (runner.ids.get("task_id"), runner.ids.get("model_round_id"), call["id"]) in runner.owner.native.started_calls
            if outcome.get("error"):
                outcome["error"]["mcp_call_started"] = outcome["mcp_call_started"]
            outcome["finished_at"] = time.time()
            if hints.get(index):
                outcome["rcagent_hint"] = hints[index]
            if call.get("name") in runner.local_handlers:
                result = json.dumps(
                    outcome.get("raw_result") or {"error": outcome.get("error")},
                    ensure_ascii=False,
                )
                runner.outputs.event("tool_result", role, outcome, call_id=call["id"])
            else:
                result = runner.owner.outputs.tool_result(outcome)
                source_id = json.loads(result)["source_id"]
                if runner.execution:
                    runner.execution.source_ids.append(source_id)
            settled[index] = outcome
            messages[index] = ToolMessage(
                result,
                tool_call_id=call["id"],
                name=call.get("name"),
                additional_kwargs={"source_id": source_id} if source_id else {},
            )

    try:
        await controlled(
            asyncio.gather(
                *(one(i, c) for i, c in enumerate(calls)), return_exceptions=True
            ),
            runner.owner.cancellation,
            runner.owner.deadline,
            budget=runner.owner.budget,
        )
    finally:
        for index, call in enumerate(calls):
            if messages[index] is None:
                started = (runner.ids.get("task_id"), runner.ids.get("model_round_id"), call["id"]) in runner.owner.native.started_calls
                messages[index] = ToolMessage(
                    json.dumps({"status": "cancelled", "mcp_call_started": started,
                                "remote_completion": "unknown" if started else "not_sent"}),
                    tool_call_id=call["id"],
                )
        runner.conversation.messages.extend(messages)
    runner.owner.check_active()
    return settled
