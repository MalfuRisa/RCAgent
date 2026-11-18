"""One cancellable send controller per account/endpoint and event loop."""

import asyncio
import hashlib
import math
import random
import time
import weakref
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from langchain_core.callbacks import AsyncCallbackHandler
from config import settings
from . import run_stops
from .repository import new_id, utcnow

_loops = weakref.WeakKeyDictionary()


class ModelRequestError(RuntimeError):
    def __init__(self, code, message, http_status=None):
        self.detail = {"code": code, "message": message or code, "http_status": http_status}
        super().__init__(code + ": " + (message or code))


def safe_message(exc):
    message = str(exc) or type(exc).__name__
    for secret in (settings.LLM_API_KEY, __import__("os").environ.get("OPENAI_API_KEY")):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:4000]


def classify(exc):
    status = getattr(exc, "status_code", None)
    message = safe_message(exc)
    lowered = message.lower()
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or type(exc).__name__ in (
        "APITimeoutError",
        "ReadTimeout",
    ):
        code = "model_timeout"
    elif status == 429:
        code = "model_rate_limited"
    elif status in (401, 403):
        code = "model_authentication_failed"
    elif status in (400, 413, 422) and any(
        word in lowered for word in ("context", "token", "too long", "maximum length")
    ):
        code = "provider_context_limit"
    elif status in (502, 503, 504):
        code = "model_service_unavailable"
    elif isinstance(exc, ConnectionError) or type(exc).__name__ in (
        "APIConnectionError",
        "ConnectError",
        "RemoteProtocolError",
    ):
        code = "model_connection_error"
    elif isinstance(exc, ValueError):
        code = "model_output_invalid"
    else:
        code = "model_request_failed"
    return ModelRequestError(code, message, status)


def retry_delay(exc, attempt):
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = 2**attempt + random.uniform(0, 0.2)
    return (
        seconds if math.isfinite(seconds) and seconds >= 0 else 2**attempt + random.uniform(0, 0.2)
    )


class Usage(AsyncCallbackHandler):
    def __init__(self):
        self.tokens = None
        self.request_id = None

    async def on_llm_end(self, response, **kwargs):
        output = response.llm_output or {}
        self.tokens = output.get("token_usage") or output.get("usage")
        for generations in response.generations:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is not None:
                    self.tokens = getattr(message, "usage_metadata", None) or self.tokens
                    self.request_id = (getattr(message, "response_metadata", {}) or {}).get(
                        "request_id"
                    )


class Controller:
    def __init__(self, maximum, interval, clock=time.monotonic, sleep=asyncio.sleep):
        self.semaphore = asyncio.Semaphore(maximum)
        self.pace = asyncio.Lock()
        self.interval, self.clock, self.sleep = interval, clock, sleep
        self.last_start = float("-inf")

    async def acquire(self, remaining):
        await asyncio.wait_for(self.semaphore.acquire(), remaining())
        try:
            async with asyncio.timeout(remaining()):
                async with self.pace:
                    delay = self.last_start + self.interval - self.clock()
                    if delay > 0:
                        await self.sleep(delay)
                    remaining()
                    self.last_start = self.clock()
        except BaseException:
            self.semaphore.release()
            raise


def controller():
    # Account key stays in memory; no endpoint authentication value in audit.
    key = (settings.LLM_BASE_URL, hashlib.sha256(settings.LLM_API_KEY.encode()).digest())
    registry = _loops.setdefault(asyncio.get_running_loop(), {})
    if key not in registry:
        registry[key] = Controller(
            settings.MODEL_MAX_IN_FLIGHT, settings.MODEL_MIN_INTERVAL_SECONDS
        )
    return registry[key]


async def send(model, messages, binding=None, manifest=None, preparation_ms=0):
    logical_id = (manifest or {}).get("model_call_reference") or new_id("model_call")
    deadline = binding.deadline if binding else time.time() + settings.INVESTIGATION_TIMEOUT_SECONDS
    planner = (
        binding is not None
        and binding.role == "planner"
        and (manifest or {}).get("phase") != "stage_repair"
    )
    request_timeout = (
        settings.PLANNER_MODEL_TIMEOUT_SECONDS if planner else settings.MODEL_TIMEOUT_SECONDS
    )
    if (
        binding is not None
        and binding.role == "supervisor"
        and (manifest or {}).get("phase") != "stage_repair"
    ):
        request_timeout = settings.SUPERVISOR_MODEL_TIMEOUT_SECONDS

    def remaining():
        seconds = deadline - time.time()
        if seconds <= 0:
            raise ModelRequestError(
                "investigation_deadline", "total investigation deadline exceeded"
            )
        return seconds

    async def save(kind, row):
        if binding:
            await binding.repository.save(binding.incident_id, kind, row)

    logical = {
        "id": logical_id,
        "run_id": binding.run_id if binding else None,
        "role": binding.role if binding else None,
        "task_id": binding.task_id if binding else None,
        "context_id": (manifest or {}).get("id"),
        "preparation_ms": preparation_ms,
        "prepared_at": utcnow(),
        "status": "queued",
        "rate_limit_policy": settings.RATE_LIMIT_POLICY,
        "request_timeout_seconds": request_timeout,
    }
    await save("model_call", logical)
    control = controller()
    # The unified role controller owns its single changed-view recovery. Legacy
    # callers keep their configured sender retries; the two never stack.
    retry_limit = (
        0 if (manifest or {}).get("context_policy") == "unified_sre" else settings.MODEL_MAX_RETRIES
    )
    for index in range(retry_limit + 1):
        usage = Usage()
        row = {
            "id": new_id("model_attempt"),
            "logical_call_id": logical_id,
            "attempt_number": index + 1,
            "queued_at": utcnow(),
            "send_started_at": None,
            "finished_at": None,
            "queue_ms": None,
            "request_ms": None,
            "token_usage": None,
            "usage_status": "unavailable",
            "retry_wait_seconds": None,
            "http_status": None,
            "error_type": None,
            "provider_request_id": None,
        }
        queued = time.monotonic()
        acquired, started, error, original = False, None, None, None
        try:
            # A stopped run sends nothing new: check before queueing and again
            # after the queue, so a request still waiting is terminated unsent.
            stopped = await run_stops.persisted_reason(binding)
            if stopped:
                raise ModelRequestError(stopped, "run already stopped; no further request is sent")
            await control.acquire(remaining)
            acquired = True
            remaining()
            stopped = await run_stops.persisted_reason(binding)
            if stopped:
                raise ModelRequestError(stopped, "run stopped while queued; request was not sent")
            row.update(send_started_at=utcnow(), queue_ms=(time.monotonic() - queued) * 1000)
            started = time.monotonic()
            result = await asyncio.wait_for(
                model.ainvoke(messages, config={"callbacks": [usage]}),
                min(request_timeout, remaining()),
            )
            row.update(
                status="completed",
                token_usage=usage.tokens or getattr(result, "usage_metadata", None),
                provider_request_id=usage.request_id,
            )
        except asyncio.CancelledError:
            row.update(
                status="cancelled",
                error_type="model_cancelled",
                message="model queue or send cancelled",
            )
            logical.update(
                status="cancelled",
                error={"code": "model_cancelled", "message": "model queue or send cancelled"},
            )
            await asyncio.shield(save("model_call", logical))
            raise
        except Exception as exc:
            original = exc
            error = exc if isinstance(exc, ModelRequestError) else classify(exc)
            if isinstance(exc, TimeoutError) and started is None or time.time() >= deadline:
                error = ModelRequestError(
                    "investigation_deadline", "deadline while queued or sending"
                )
            row.update(
                status="failed",
                error_type=error.detail["code"],
                message=error.detail["message"],
                http_status=error.detail["http_status"],
            )
            row["provider_request_id"] = getattr(exc, "request_id", None)
            if (
                error.detail["code"] == "model_rate_limited"
                and settings.RATE_LIMIT_POLICY == "stop_run"
                and binding is not None
            ):
                # Mark the run before this request releases the queue slot, so a
                # request that was waiting cannot slip through after the 429.
                await run_stops.stop_run(
                    binding,
                    "model_rate_limited",
                    {
                        "http_status": row.get("http_status"),
                        "attempt_id": row["id"],
                        "message": row.get("message"),
                    },
                )
                logical["stopped_run"] = True
        finally:
            if acquired:
                control.semaphore.release()
            row.update(
                finished_at=utcnow(),
                request_ms=(time.monotonic() - started) * 1000 if started is not None else None,
            )
            if row["queue_ms"] is None:
                row["queue_ms"] = (time.monotonic() - queued) * 1000
            row["usage_status"] = "reported" if row["token_usage"] is not None else "unavailable"
            await asyncio.shield(save("model_attempt", row))
        if error is None:
            logical.update(status="completed")
            await save("model_call", logical)
            return result
        rate_limited = error.detail["code"] == "model_rate_limited"
        retryable = error.detail["code"] in ("model_connection_error", "model_service_unavailable")
        if rate_limited and settings.RATE_LIMIT_POLICY == "legacy":
            retryable = True  # explicit legacy comparison only
        if retryable and index < retry_limit:
            delay = retry_delay(original, index + 1)
            row["retry_wait_seconds"] = delay
            await save("model_attempt", row)
            remaining_seconds = deadline - time.time()
            if delay < remaining_seconds:
                try:
                    await asyncio.wait_for(control.sleep(delay), remaining_seconds)
                    continue
                except asyncio.CancelledError:
                    logical.update(
                        status="cancelled",
                        error={"code": "model_cancelled", "message": "retry wait cancelled"},
                    )
                    await asyncio.shield(save("model_call", logical))
                    raise
                except TimeoutError:
                    error = ModelRequestError(
                        "investigation_deadline", "deadline during retry wait"
                    )
            error = ModelRequestError(
                "investigation_deadline", "Retry-After exceeds remaining investigation deadline"
            )
        logical.update(status="failed", error=error.detail)
        await save("model_call", logical)
        if error.detail["code"] == "model_output_invalid":
            raise original
        raise error
