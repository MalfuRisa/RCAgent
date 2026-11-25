"""Streaming aggregation and bounded service retries, independent of task budget."""

import asyncio
import time
from email.utils import parsedate_to_datetime

from langchain_core.messages import message_chunk_to_message


class RunCancellation(asyncio.Event):
    """Latch the first known run stop cause before asynchronous cleanup starts."""

    def __init__(self):
        super().__init__()
        self.stop_reason = None

    def set(self):
        self.stop_reason = self.stop_reason or "cancelled"
        super().set()


def interruption_reason(cancellation, deadline=None):
    reason = getattr(cancellation, "stop_reason", None)
    if reason is None:
        if cancellation.is_set():
            reason = "cancelled"
        elif deadline is not None and deadline <= time.time():
            reason = "deadline"
        if isinstance(cancellation, RunCancellation):
            cancellation.stop_reason = reason
    return reason


def content_text(content):
    if isinstance(content, str):
        return content
    return "\n".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    )


def capacity_error(exc):
    code = str(getattr(exc, "code", "")) + str(exc)
    return any(
        s in code.lower()
        for s in (
            "context_length",
            "context window",
            "maximum context",
            "too many tokens",
        )
    )


async def controlled(awaitable, cancellation, deadline=None, *, budget=None, phase="investigation"):
    """Cancel only our task; caller owns resource cleanup outside this race."""
    from .investigation_budget import PhaseExpired
    if isinstance(awaitable, asyncio.Future):
        awaitable.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
    reason = interruption_reason(cancellation, deadline)
    if budget is not None and budget.remaining(phase) <= 0 and not reason:
        close = getattr(awaitable, "close", None)
        if close: close()
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
            await asyncio.wait([awaitable], timeout=0)
        raise PhaseExpired(phase, budget.phase_reason(phase))
    if reason:
        if isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
            await asyncio.wait([awaitable], timeout=0)
        close = getattr(awaitable, "close", None)
        if close:
            close()
        if reason == "cancelled":
            raise asyncio.CancelledError()
        raise TimeoutError("User deadline reached")
    task = asyncio.ensure_future(awaitable)
    # A cancelled parent may leave a settled gather future whose exception still
    # needs retrieving. This does not suppress exceptions from awaiting it.
    task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
    signal = asyncio.create_task(cancellation.wait())
    try:
        timeout = max(0, deadline - time.time()) if deadline is not None else None
        if budget is not None:
            timeout = min(timeout, budget.remaining(phase)) if timeout is not None else budget.remaining(phase)
        done, _ = await asyncio.wait(
            [task, signal], timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if signal in done:
            interruption_reason(cancellation, deadline)
            raise asyncio.CancelledError()
        if task not in done:
            if budget is not None and budget.remaining(phase) <= 0:
                raise PhaseExpired(phase, budget.phase_reason(phase))
            interruption_reason(cancellation, deadline)
            raise TimeoutError("User deadline reached")
        return await task
    finally:
        for pending in (task, signal):
            if not pending.done():
                pending.cancel()
        await asyncio.wait([task, signal], timeout=min(2, budget.remaining("delivery")) if budget else 2)


def retry_delay(exc, attempt):
    value = getattr(getattr(exc, "response", None), "headers", {}).get("retry-after")
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(value).timestamp() - time.time())
            except (ValueError, TypeError):
                pass
    return 2**attempt


def exception_types(exc):
    types = []
    while exc is not None and len(types) < 5:
        types.append(type(exc).__name__)
        exc = exc.__cause__
    return types


async def consume_stream(model, messages, idle_timeout, on_first, on_first_text, state):
    """Aggregate one transport attempt; the caller owns slot acquisition.

    ``state['combined']`` keeps the last received chunk visible to the
    cancellation handler even when the attempt never returns.
    """
    combined = None
    first = True
    iterator = model.astream(messages).__aiter__()
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(anext(iterator), idle_timeout)
            except StopAsyncIteration:
                break
            if first:
                state["first_chunk_received"] = True
                on_first()
                first = False
            combined = chunk if combined is None else combined + chunk
            state["combined"] = combined
            if not state.get("first_text") and content_text(chunk.content).strip():
                state["first_text"] = True
                on_first_text()
    finally:
        close = getattr(iterator, "aclose", None)
        if close:
            await close()
    return combined


async def stream_response(
    model, messages, outputs, role, idle_timeout, on_partial=None, budget=None,
    phase="investigation", slot=None, on_first_text=None,
):
    for attempt in range(3):
        combined = None
        state = {}
        if budget is not None:
            budget.check(phase)
        base = getattr(outputs, "base", outputs)
        base.model_attempts = getattr(base, "model_attempts", 0) + 1
        outputs.event("model_started", role, {"transport_attempt": attempt + 1})

        def first_chunk():
            outputs.event(
                "model_first_chunk",
                role,
                {
                    "transport_attempt": attempt + 1,
                    "meaning": "首个流式片段；不是首个正文token或思考结束",
                },
            )

        def first_text():
            if on_first_text is not None:
                on_first_text()
            outputs.event(
                "model_first_text",
                role,
                {
                    "transport_attempt": attempt + 1,
                    "meaning": "首个非空可展示正文片段",
                },
            )

        async def send():
            state["sent_at"] = time.monotonic()
            outputs.event("model_send_started", role, {"transport_attempt": attempt + 1})
            return await consume_stream(
                model, messages, idle_timeout, first_chunk, first_text, state
            )

        try:
            if slot is None:
                combined = await send()
            else:
                async with slot():
                    combined = await send()
            if combined is None:
                return None
            return message_chunk_to_message(combined)
        except BaseException as exc:
            combined = combined or state.get("combined")
            if combined is not None:
                if on_partial:
                    on_partial(content_text(combined.content))
                # Preserve public partial output; never promote it to final prose.
                outputs.event(
                    "model_response",
                    role,
                    {
                        "partial": True,
                        "text": content_text(combined.content),
                        "tool_call_chunks": combined.tool_call_chunks,
                    },
                )
                outputs.save(role + "-partial.md", content_text(combined.content))
            if isinstance(exc, asyncio.CancelledError):
                raise
            status = getattr(exc, "status_code", None)
            transient = (
                status == 429
                or (status and status >= 500)
                or type(exc).__name__
                in {
                    "APIConnectionError",
                    "APITimeoutError",
                    "RemoteProtocolError",
                    "ReadTimeout",
                    "TimeoutError",
                }
            )
            sent_at = state.get("sent_at")
            outputs.event(
                "model_transport_failed", role,
                {
                    "transport_attempt": attempt + 1,
                    "phase": phase,
                    "exception_types": exception_types(exc),
                    "status_code": status,
                    "send_seconds": time.monotonic() - sent_at if sent_at is not None else None,
                    "first_chunk_received": state.get("first_chunk_received", False),
                    "public_text_received": state.get("first_text", False),
                },
            )
            if capacity_error(exc) or not transient or attempt == 2:
                raise
            outputs.event(
                "execution_error",
                role,
                {
                    "transient": True,
                    "error": outputs.redact(str(exc)),
                    "retry": attempt + 1,
                    "slot_released": True,
                },
            )
            delay = retry_delay(exc, attempt)
            if budget is not None and delay >= budget.remaining(phase):
                raise
            base.model_backoffs.append(delay)
            outputs.event(
                "model_backoff",
                role,
                {
                    "transport_attempt": attempt + 1,
                    "delay_seconds": delay,
                    "slot_released": True,
                    "meaning": "模型槽已释放；退避结束后下一次尝试重新排队",
                },
            )
            await asyncio.sleep(delay)
