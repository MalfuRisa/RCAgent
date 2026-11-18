"""Bind the restricted control model before assembling its measured request."""
from langchain_core.messages import HumanMessage
from context.request_budget import tool_schemas
from context.stage_budget import CONTROL_PROMPT
from workspace.runtime import invoke_model
from workspace.repository import WorkspaceError


async def stage_request(binding, client, messages, tools, control_tools, *, phase, parallel=None, regular_model=None):
    """Retry preparation once with control tools; never send an oversized summary."""
    for attempt in range(2):
        controlled = binding.checkpoint_required
        bound = control_tools if controlled else tools
        options = {} if parallel is None else {'parallel_tool_calls': parallel}
        model = regular_model if not controlled and regular_model is not None else client.bind_tools(bound, **options)
        request = {'tools': tool_schemas(bound), **options}
        visible = list(messages) + ([HumanMessage(content=CONTROL_PROMPT)] if controlled else [])
        try:
            return await invoke_model(model, visible, phase='checkpoint_control' if controlled else phase, request=request)
        except WorkspaceError as exc:
            if controlled or attempt or not str(exc).startswith('context_unsatisfiable'):
                raise
            binding.checkpoint_required = True
    raise WorkspaceError('context_unsatisfiable: no bounded handoff request fits')
