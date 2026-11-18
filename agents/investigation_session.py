"""Run owner: shared MCP lifecycle and limiters, isolated role runners."""

import asyncio
import os
from contextlib import AsyncExitStack

from config import settings
from config.chat_model import get_investigation_model
from platforms.insights_v5.native_session import NativeMcpSession

from .investigation_lifecycle import close_owned, initialization_guard
from .investigation_tools import local_tools
from .investigation_transport import RunCancellation, interruption_reason
from .native_role_runner import RoleRunner


class InvestigationSession:
    def __init__(
        self,
        outputs,
        *,
        task,
        cancellation=None,
        deadline=None,
        progress=None,
        model=None,
        native=None,
    ):
        self.outputs, self.task = outputs, task
        self.budget = getattr(outputs, "budget", None)
        self.closing = False
        self.cancellation = (
            cancellation if cancellation is not None else RunCancellation()
        )
        self.deadline = deadline
        self.progress = progress or (lambda event: None)
        self.model = model
        from platforms.integrated_session import IntegratedMcpSession
        factory = IntegratedMcpSession if os.getenv('RCAGENT_MCP_PROFILE','native')=='integrated' else NativeMcpSession
        self.native = native or factory(
            outputs, cancellation=self.cancellation, deadline=deadline
        )
        self.stack = AsyncExitStack()
        self.concurrency_limits = {
            "tools": max(1, settings.NATIVE_CONCURRENCY),
            "models": max(1, settings.NATIVE_MODEL_CONCURRENCY),
            "workers": max(1, settings.NATIVE_WORKER_CONCURRENCY),
        }
        self.tool_semaphore = asyncio.Semaphore(self.concurrency_limits["tools"])
        self.model_semaphore = asyncio.Semaphore(self.concurrency_limits["models"])
        self.worker_semaphore = asyncio.Semaphore(self.concurrency_limits["workers"])
        self.runners = {}

    async def __aenter__(self):
        try:
            async with initialization_guard(self.cancellation, self.deadline, self.budget):
                self.check_active()
                await self.stack.enter_async_context(self.native)
                self.check_active()
                self.model = self.model or get_investigation_model()
                self.outputs.secrets.append(settings.LLM_API_KEY)
                self.local_definitions, self.local_handlers = local_tools(self.outputs)
                self.outputs.event("concurrency_limits", "system", self.concurrency_limits)
                return self
        except BaseException:
            await close_owned(self.stack, self.outputs, self.budget, "investigation_initialization")
            raise

    async def __aexit__(self, *args):
        await close_owned(self.stack, self.outputs, self.budget, "investigation")

    def check_active(self):
        reason = interruption_reason(self.cancellation, self.deadline)
        if reason == "cancelled":
            raise asyncio.CancelledError()
        if reason == "deadline":
            raise TimeoutError("User deadline reached")
        if self.budget:
            self.budget.check("generation" if self.closing else "investigation")
        if self.native.auth_failed:
            raise PermissionError(
                "MCP transport authentication failed; stopped new requests"
            )

    def runner(self, role, *, execution=None, definitions=None):
        runner = RoleRunner(self, role, execution=execution, definitions=definitions)
        self.runners[runner.conversation_id] = runner
        return runner
