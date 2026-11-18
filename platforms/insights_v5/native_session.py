"""One run-owned MCP connection, unmodified readonly contracts and results."""

import asyncio
import json
import os
import time
from contextlib import AsyncExitStack
from datetime import timedelta

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from agents.investigation_budget import PhaseExpired
from agents.investigation_lifecycle import close_owned, initialization_guard
from agents.investigation_transport import controlled
from config import settings

from .diagnostic_policy import NATIVE_READS
from .native_task_scope import LocalRejection, TaskAuthority
from .session import gateway
from platforms.business_policy import BUSINESS_READS, DATA_READS, resolved_time


def authentication_error(exc):
    """Inspect actual transport status, never log text such as '403 forbidden'."""
    if getattr(getattr(exc, "response", None), "status_code", None) in (401, 403):
        return True
    return any(authentication_error(e) for e in getattr(exc, "exceptions", []))


class NativeMcpSession:
    def __init__(self, outputs, *, cancellation=None, deadline=None, profile=None):
        self.outputs = outputs
        self.budget = getattr(outputs, "budget", None)
        self.cancellation = cancellation or asyncio.Event()
        self.deadline = deadline
        self.stack = AsyncExitStack()
        self.tools = {}
        self.auth_failed = False
        self.authority = TaskAuthority()
        self.started_calls = set()
        self.profile = profile or os.getenv('RCAGENT_MCP_PROFILE', 'native')
        self.explicit_profile = profile is not None
        if self.profile not in {'native', 'business'}:
            raise ValueError('RCAGENT_MCP_PROFILE must be native or business')
        self.allowed = BUSINESS_READS if self.profile == 'business' else NATIVE_READS
        self.resolved_window = None
        self.source_context = {'family':self.profile,'status':'not_initialized'}

    async def __aenter__(self):
        try:
            async with initialization_guard(self.cancellation, self.deadline, self.budget):
                return await self._initialize()
        except BaseException:
            await close_owned(self.stack, self.outputs, self.budget, self.profile+"_initialization")
            raise

    async def _initialize(self):
        # This scope is entered and exited by the original resource owner.
        prefix = 'RCAGENT_BUSINESS_MCP' if self.profile == 'business' else 'RCAGENT_NATIVE_MCP'
        url = os.getenv(prefix + '_URL')
        token = os.getenv(prefix + '_TOKEN', '')
        if not url and self.profile == 'business':
            raise ValueError('Set RCAGENT_BUSINESS_MCP_URL; business profile never starts/falls back to native gateway')
        if not url:
            url, token = await self.stack.enter_async_context(
                gateway(self.outputs.root / "gateway", budget=self.budget)
            )
        self.outputs.secrets.append(token)
        streams = await self.stack.enter_async_context(
            streamablehttp_client(
                url,
                headers={"Authorization": "Bearer " + token} if token else {},
                timeout=10,
                sse_read_timeout=settings.NATIVE_MCP_TIMEOUT,
            )
        )
        self.client = await self.stack.enter_async_context(
            ClientSession(
                streams[0],
                streams[1],
                read_timeout_seconds=timedelta(seconds=settings.NATIVE_MCP_TIMEOUT),
            )
        )
        identity = await controlled(
            self.client.initialize(), self.cancellation, self.deadline, budget=self.budget
        )
        cursor = None
        while True:
            page = await controlled(
                self.client.list_tools(cursor=cursor),
                self.cancellation,
                self.deadline,
                budget=self.budget,
            )
            for tool in page.tools:
                if tool.name in self.allowed:
                    self.tools[tool.name] = {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description or "",
                            "parameters": tool.inputSchema,
                        },
                    }
            cursor = page.nextCursor
            if not cursor:
                break
        self.tools = dict(sorted(self.tools.items()))
        self.source_context = {'family':self.profile,'mcp_url':url,
            'server_identity':identity.model_dump(mode='json'),
            'configured_backend':os.getenv('RCAGENT_BUSINESS_BACKEND_URL') if self.profile=='business' else os.getenv('RCAGENT_V5_QUERIES_HTTP_URL'),
            'route_basis':'owner configuration, not proof of shared storage or cross-source ID equivalence'}
        self.outputs.save(
            'runtime-'+self.profile+'.json' if self.explicit_profile else 'runtime.json',
            {
                "runtime": "rcagent",
                "model": settings.LLM_MODEL,
                "endpoint": settings.LLM_BASE_URL,
                "mcp_url": url,
                "server_identity": identity.model_dump(mode="json"),
                "missing_tools": sorted(self.allowed - self.tools.keys()),
                "mcp_profile": self.profile,
                "tools": list(self.tools.values()),
                "application_budget": self.budget.card() if self.budget else None, "task_contract": "native_tasks_v2",
            },
        )
        return self

    async def __aexit__(self, *args):
        await close_owned(self.stack, self.outputs, self.budget, 'mcp:'+self.profile)

    async def call(
        self, call_id, name, arguments, role, *, execution=None, trace_ids=None
    ):
        outcome = {
            "call_id": call_id,
            "tool": name,
            "arguments": arguments,
            "role": role,
            "started_at": time.time(),
            "status": "ok",
            "raw_result": None,
            "mcp_call_started": False,
            'mcp_source':self.profile,
            'source_context':self.source_context,
        }
        if name not in self.allowed or name not in self.tools:
            return {
                **outcome,
                "finished_at": time.time(),
                "status": "tool_error",
                "error": {
                    "code": "tool_not_allowed",
                    "message": "Tool is outside discovered readonly set",
                    "retryable": False,
                    "source": "rcagent",
                    "stage": "validation",
                    "mcp_call_started": False,
                },
            }
        try:
            if self.budget:
                self.budget.check("investigation")
            if role not in {"worker", "planner"}:
                raise LocalRejection(
                    "execution_context_required",
                    "Only the assigned Worker or a single-use Planner direct scope can send remote MCP calls",
                )
            if not isinstance(arguments, dict):
                raise LocalRejection(
                    "invalid_argument", "Arguments must be a JSON object"
                )
            from agents.native_validation import validate

            if isinstance(execution, object) and hasattr(execution, "discovered_tools"):
                execution.discovered_tools = self.tools
            self.authority.verify(execution, name, arguments)
            validate(name, arguments, self.tools[name]["function"]["parameters"])
            self.authority.verify(execution, name, arguments, lock=True)
        except LocalRejection as exc:
            return {
                **outcome,
                "finished_at": time.time(),
                "status": "tool_error",
                "error": exc.error,
            }
        cache_key = (self.profile,name,json.dumps(arguments,sort_keys=True,ensure_ascii=False))
        cached = getattr(self.outputs,'preparation_cache',{}).get(cache_key)
        if cached and not self.auth_failed:
            return {**outcome,'cache_hit':True,'original_source_id':cached,'finished_at':time.time(),
                'raw_result':self.outputs.materials.raw[cached]}
        try:
            raw = await controlled(self._send(outcome, execution, trace_ids), self.cancellation,
                                   self.deadline, budget=self.budget)
            outcome["raw_result"] = raw.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            if self.profile == 'business' and not raw.isError:
                self._record_window(outcome, execution)
            if raw.isError:
                outcome.update(
                    status="tool_error",
                    error={
                        "code": "mcp_is_error",
                        "message": "MCP returned isError; inspect original result",
                        "retryable": False,
                    },
                )
        except PhaseExpired:
            raise
        except Exception as exc:  # noqa: BLE001 -- archive native transport failures as outcomes
            self.auth_failed = self.auth_failed or authentication_error(exc)
            outcome.update(
                status="transport_error",
                error={
                    "code": "authentication"
                    if self.auth_failed
                    else type(exc).__name__,
                    "message": self.outputs.redact(str(exc)),
                    "retryable": not self.auth_failed,
                },
            )
        outcome["finished_at"] = time.time()
        return outcome

    async def _send(self, outcome, execution, trace_ids):
        """Recheck after scheduling, immediately before entering the MCP SDK call."""
        name, arguments, call_id = outcome["tool"], outcome["arguments"], outcome["call_id"]
        if self.auth_failed:
            raise PermissionError("MCP connection authentication has failed")
        if self.cancellation.is_set() or self.outputs.sealed:
            raise asyncio.CancelledError()
        if self.deadline is not None and self.deadline <= time.time():
            raise TimeoutError("User deadline reached")
        if self.budget:
            self.budget.check("investigation")
        if self.profile == 'business' and self.resolved_window and not execution.spec.window:
            execution.spec.window = self.resolved_window
        self.authority.verify(execution, name, arguments)
        if name in execution.spec.target_tools and not (name == 'insights_generate_dashboard' and arguments.get('render') is False):
            execution.target_started += 1
        if name in execution.spec.business_tools:
            execution.business_started += 1
        self.outputs.event("mcp_call_started", outcome["role"], {"tool": name}, call_id=call_id,
            **{**(trace_ids or {}), "task_id": execution.task_id, "conversation_id": execution.conversation_id})
        outcome["mcp_call_started"] = True
        self.started_calls.add((execution.task_id, (trace_ids or {}).get("model_round_id"), call_id))
        return await self.client.call_tool(name, arguments)

    def _record_window(self, outcome, execution):
        value = resolved_time(outcome['raw_result'])
        if outcome['tool'] not in DATA_READS or not value:
            return
        from models.native_tasks import Window
        window = Window(start=value['start'], end=value['end'])
        window.seconds()
        outcome['resolved_time'] = value
        if execution.spec.window and execution.spec.window != window:
            outcome.update(status='tool_error', error={'code': 'resolved_window_mismatch',
                'message': 'Returned time differs from assigned window; original retained, do not equate scopes'})
            return
        main_window_result = execution.window_kind not in {'comparison', 'subwindow'}
        if main_window_result and self.resolved_window and self.resolved_window != window:
            outcome.update(status='tool_error', error={'code': 'resolved_window_mismatch',
                'message': 'Concurrent relative windows drifted; use previously resolved absolute window'})
            return
        execution.spec.window = window
        if main_window_result:
            execution.window_kind = 'main'
            self.resolved_window = self.resolved_window or window
        self.outputs.event('business_window_resolved', outcome['role'], {'requested': outcome['arguments'].get('time'), 'resolved_time': value})
