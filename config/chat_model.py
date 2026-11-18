"""Explicit structured-output policy for OpenAI-compatible model endpoints."""
from typing import Any, Literal

from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field


StructuredOutputMethod = Literal["function_calling", "json_schema"]


def get_investigation_model():
    """Plain streaming client for the owned loop; no structured-output policy."""
    import httpx
    from . import settings
    return ChatOpenAI(model=settings.LLM_MODEL, api_key=settings.LLM_API_KEY,
        base_url=settings.LLM_BASE_URL or None, max_retries=0,
        timeout=httpx.Timeout(settings.INVESTIGATION_IDLE_TIMEOUT, connect=10),
        stream_usage=True)


def validate_output_method(value: str) -> StructuredOutputMethod:
    if value not in ("function_calling", "json_schema"):
        raise ValueError(
            "Structured output method must be 'function_calling' or 'json_schema'; "
            "set RCAGENT_LLM_STRUCTURED_OUTPUT_METHOD (or the advanced override)."
        )
    return value


class RCAChatOpenAI(ChatOpenAI):
    """Keep all agents on the configured submission protocol, not SDK defaults.

    There is deliberately no automatic protocol fallback: authentication,
    service failures and invalid answers must remain visible to the caller.
    """

    structured_output_method: StructuredOutputMethod = Field(
        default="function_calling", exclude=True
    )

    def with_structured_output(
        self, schema=None, *, method=None, include_raw=False, **kwargs: Any
    ):
        selected = validate_output_method(
            self.structured_output_method if method is None else method
        )
        runnable = super().with_structured_output(
            schema, method=selected, include_raw=include_raw, **kwargs
        )
        if include_raw:
            # Preserve LangChain's explicit raw-result/parse-error contract.
            return runnable

        def require_result(result):
            # A model that emits prose instead of the requested function call
            # otherwise parses to None; do not treat that as an empty diagnosis.
            if result is None:
                raise ValueError("Model did not submit a structured result")
            if isinstance(schema, type) and issubclass(schema, BaseModel):
                if not isinstance(result, schema):
                    raise ValueError("Model result does not match the requested schema")
            return result

        return runnable | RunnableLambda(require_result)
