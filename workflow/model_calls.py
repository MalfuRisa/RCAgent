"""Shared structured-request description and the existing audited model sender."""
from context.request_budget import structured_request
from workspace import runtime


async def invoke_structured(configured_model, schema, messages, *, phase, invoke=None):
    """SDK binding and result parsing remain with their distinct caller contracts."""
    sender = invoke or runtime.invoke_model
    return await sender(configured_model, messages, phase=phase, request=structured_request(schema))
