"""Shared tool invocation scope and protocol pairing, without loop/error policy."""

from langchain_core.messages import ToolMessage
from workspace.read_tools import CURRENT_TOOL_CALL
from langchain_core.tools import StructuredTool


class ContractTool(StructuredTool):
    """Keep a strict public DTO intact; it has no injected model arguments."""

    @property
    def tool_call_schema(self):
        return self.args_schema


def contract_tool(*, args_schema):
    return lambda fn: ContractTool.from_function(
        coroutine=fn, name=fn.__name__, description=fn.__doc__, args_schema=args_schema
    )


async def invoke_local(tool, arguments, call_id):
    """Restore the caller's tool-call context even on errors or cancellation."""
    token = CURRENT_TOOL_CALL.set(call_id)
    try:
        return await tool.ainvoke(arguments)
    finally:
        CURRENT_TOOL_CALL.reset(token)


def paired_reply(call, content, *, status="success"):
    """Serialization and rejection semantics belong to the calling policy."""
    return ToolMessage(
        content=content, tool_call_id=call["id"], name=call["name"], status=status
    )
