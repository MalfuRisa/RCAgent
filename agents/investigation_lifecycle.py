"""Initialization interruption and bounded cleanup on the resource owner task."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar

from .investigation_budget import PhaseExpired
from .investigation_transport import interruption_reason

_INITIALIZING = ContextVar("rcagent_initializing_owner", default=None)


@asynccontextmanager
async def initialization_guard(cancellation, deadline, budget):
    owner = asyncio.current_task()
    inherited = _INITIALIZING.get()
    if inherited and inherited["owner"] is owner:
        yield
        return
    reason = interruption_reason(cancellation, deadline)
    if reason == "cancelled":
        raise asyncio.CancelledError()
    if reason == "deadline":
        raise TimeoutError("User deadline reached")
    if budget:
        budget.check("investigation")
    state = {"owner": owner, "cleaning": False, "cause":None, "timer":None}
    token = _INITIALIZING.set(state)
    cause, timer = None, None
    loop = asyncio.get_running_loop()

    def poll():
        nonlocal cause, timer
        state['timer'] = None
        if state["cleaning"]:
            return
        cause = interruption_reason(cancellation, deadline)
        if not cause and budget and budget.remaining("investigation") <= 0:
            cause = "budget_investigation"
        if cause:
            state['cause'] = cause
            owner.cancel()
        else:
            timer = loop.call_later(.02, poll)
            state['timer'] = timer

    def resume():
        nonlocal timer
        if state['timer'] is None and state['cause'] is None:
            timer = loop.call_later(.02,poll)
            state['timer'] = timer
    state['resume'] = resume

    poll()
    try:
        yield
    except asyncio.CancelledError:
        if cause == "budget_investigation":
            raise PhaseExpired("investigation", budget.phase_reason("investigation")) from None
        if cause == "deadline":
            raise TimeoutError("User deadline reached") from None
        raise
    finally:
        if state['timer']:
            state['timer'].cancel()
        _INITIALIZING.reset(token)


async def close_owned(stack, outputs, budget, layer):
    """No helper task enters/exits the MCP/anyio scopes owned by our caller."""
    initializing = _INITIALIZING.get()
    was_cleaning = None
    if initializing and initializing["owner"] is asyncio.current_task():
        was_cleaning = initializing['cleaning']
        initializing["cleaning"] = True
    failures = getattr(outputs, "cleanup_failures", [])
    outputs.cleanup_failures = failures
    outputs.event("cleanup_started", "system", {"layer": layer})
    try:
        limit = min(10, budget.remaining("delivery")) if budget else 10
        async with asyncio.timeout(max(.01, limit)):
            await stack.aclose()
    except Exception as exc:  # noqa: BLE001 -- preserve the original failure and report cleanup separately
        error = layer + " cleanup unconfirmed: " + (str(exc) or type(exc).__name__)
        failures.append(error)
        outputs.errors.append(error)
    finally:
        if was_cleaning is not None:
            initializing['cleaning'] = was_cleaning
            if not was_cleaning:
                initializing['resume']()
    success = not failures
    outputs.event("cleanup_finished", "system", {"layer": layer, "success": success,
                                                   "unconfirmed": list(failures)})
    return success
