"""Canonical request size. This is engineering metering, never a token estimate."""
import json
from langchain_core.utils.function_calling import convert_to_openai_tool


def serialized_chars(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))


def tool_schemas(tools):
    return [convert_to_openai_tool(tool) for tool in tools]


def canonical_request(messages, request=None):
    # Use the installed transport's conversion, including escaped JSON arguments
    # and tool_call_id/name, rather than LangChain's internal ToolCall shape.
    from langchain_openai.chat_models.base import _convert_message_to_dict
    rows = [_convert_message_to_dict(message) for message in messages]
    return {'messages': rows, **{key: value for key, value in (request or {}).items() if value is not None}}


def measure(messages, request=None):
    canonical = canonical_request(messages, request)
    # Include keys and separators in the envelope so component totals add up.
    costs = {key: serialized_chars(value) for key, value in canonical.items()}
    total = serialized_chars(canonical)
    costs['envelope'] = total - sum(costs.values())
    return total, costs


def structured_request(schema):
    from config import settings
    if getattr(settings, 'LLM_STRUCTURED_OUTPUT_METHOD', 'function_calling') == 'json_schema':
        return {'response_schema': schema.model_json_schema()}
    tool = convert_to_openai_tool(schema)
    return {'tools': [tool], 'tool_choice': {'type': 'function', 'function': {'name': tool['function']['name']}}}
