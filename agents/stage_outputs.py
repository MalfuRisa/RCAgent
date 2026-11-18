"""Capture SDK raw structured outputs without regenerating valid outer payloads."""
import inspect
import json
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from config import settings
from context.request_budget import structured_request
from workspace.artifacts import write_artifact
from workspace.repository import WorkspaceError
from workspace.runtime import current, invoke_model
from workflow import model_calls


def raw_structured(model, schema):
    params = inspect.signature(model.with_structured_output).parameters
    return model.with_structured_output(schema, include_raw=True) if 'include_raw' in params else model.with_structured_output(schema)


def extract(result, schema):
    if not isinstance(result, dict) or 'raw' not in result:
        return result
    raw = result['raw']
    if result.get('parsed') is not None:
        return result['parsed']
    if not isinstance(raw, AIMessage):
        raise WorkspaceError('stage_repair_not_applicable: no reliable raw response')
    if len(raw.tool_calls) == 1 and not raw.invalid_tool_calls and raw.tool_calls[0]['name'] == schema.__name__:
        return raw.tool_calls[0]['args']
    if settings.LLM_STRUCTURED_OUTPUT_METHOD == 'json_schema' and not raw.tool_calls and isinstance(raw.content, str):
        try:
            value = json.loads(raw.content)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    raise WorkspaceError('stage_repair_not_applicable: unparseable, wrong tool or ambiguous structured response')


async def preserve_outer(b, result, schema):
    try:
        value = extract(result, schema)
        if hasattr(value, 'model_dump'):
            value = value.model_dump(exclude_unset=True)
        try:
            return schema.model_validate(value)
        except ValidationError as exc:
            if not isinstance(value, dict) or any(r['loc'][0] != 'stage_proposal' for r in exc.errors()):
                raise
            outer = schema.model_validate({**value, 'stage_proposal': None})
            # Only the stage is deferred to the shared validator. The business
            # output was independently validated and is never regenerated.
            artifact = await write_artifact(b, {'kind': 'role_stage_parse_failure', 'schema': schema.__name__,
                'response': value, 'errors': exc.errors(include_input=False, include_url=False)})
            await b.repository.save(b.incident_id, 'artifact', artifact)
            return outer.model_copy(update={'stage_proposal': value['stage_proposal']})
    except (ValidationError, WorkspaceError) as exc:
        if b:
            raw = result.model_dump(mode='json') if hasattr(result, 'model_dump') else result
            artifact = await write_artifact(b, {'kind': 'role_output_rejected', 'schema': schema.__name__,
                                               'response': raw, 'error': str(exc)})
            await b.repository.save(b.incident_id, 'artifact', artifact)
        raise


async def invoke_role(model, schema, messages, *, phase):
    result = await model_calls.invoke_structured(raw_structured(model, schema), schema, messages, phase=phase, invoke=invoke_model)
    return await preserve_outer(current.get(), result, schema)
