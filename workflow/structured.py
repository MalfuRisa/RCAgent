"""Domain-neutral structured calls with raw-response audit and explicit parsing."""

import inspect
import json
from pydantic import ValidationError
from langchain_core.messages import AIMessage
from config import settings
from context.request_budget import structured_request
from workspace.runtime import invoke_model, current
from workspace import model_audit
from workspace.repository import WorkspaceError


async def ask_structured(model, schema, messages, *, phase):
    """One local parsing correction, with the rejected public response retained."""
    working = list(messages)
    for attempt in range(2):
        try:
            return await parse_once(model, schema, working, phase=phase)
        except WorkspaceError as error:
            if attempt or not str(error).startswith("structured_output_invalid:"):
                raise
            from langchain_core.messages import HumanMessage

            working.append(
                HumanMessage(
                    content="Correct only the structured submission error; preserve the existing task and scope: "
                    + str(error)[:2000]
                )
            )


async def parse_once(model, schema, messages, *, phase):
    parameters = inspect.signature(model.with_structured_output).parameters
    kwargs = {}
    if "include_raw" in parameters or any(
        p.kind == p.VAR_KEYWORD for p in parameters.values()
    ):
        kwargs["include_raw"] = True
    if "method" in parameters or any(
        p.kind == p.VAR_KEYWORD for p in parameters.values()
    ):
        kwargs["method"] = settings.LLM_STRUCTURED_OUTPUT_METHOD
    result = await invoke_model(
        model.with_structured_output(schema, **kwargs),
        messages,
        phase=phase,
        request=structured_request(schema),
    )
    b = current.get()
    try:
        value = result
        if isinstance(result, dict) and "raw" in result:
            value = result.get("parsed")
            raw = result.get("raw")
            if (
                value is None
                and isinstance(raw, AIMessage)
                and len(raw.tool_calls) == 1
                and not raw.invalid_tool_calls
            ):
                if raw.tool_calls[0]["name"] == schema.__name__:
                    value = raw.tool_calls[0]["args"]
            if (
                value is None
                and isinstance(raw, AIMessage)
                and not raw.tool_calls
                and settings.LLM_STRUCTURED_OUTPUT_METHOD == "json_schema"
            ):
                value = json.loads(raw.content)
        parsed = schema.model_validate(value)
    except (ValueError, TypeError, ValidationError) as exc:
        if b:
            from workspace.investigation_phases import settle

            await settle(
                b,
                [],
                [(schema.__name__, {"status": "rejected", "reason": str(exc)[:2000]})],
            )
            await model_audit.processed(
                b, schema.__name__, {"status": "rejected", "reason": str(exc)[:2000]}
            )
        raise WorkspaceError("structured_output_invalid: " + str(exc)[:2000]) from exc
    if b:
        await model_audit.processed(
            b, schema.__name__, {"status": "accepted", "parsed": parsed}
        )
    return parsed
