"""One owner, two explicit readonly sources, one family per task."""
import asyncio
from contextlib import AsyncExitStack

from agents.investigation_budget import PhaseExpired
from agents.investigation_lifecycle import close_owned
from models.native_capabilities import task_source

from .insights_v5.native_session import NativeMcpSession
from .insights_v5.native_task_scope import LocalRejection, TaskAuthority
from .resource_identity import validate_resource_ids


class IntegratedMcpSession:
    profile = 'integrated'

    def __init__(self, outputs, *, cancellation=None, deadline=None, factory=NativeMcpSession):
        self.outputs, self.budget = outputs, outputs.budget
        self.cancellation, self.deadline = cancellation or asyncio.Event(), deadline
        self.factory, self.stack = factory, AsyncExitStack()
        self.tools, self.routes, self.clients, self.availability = {}, {}, {}, {}
        self.authority = TaskAuthority()
        self.resolved_window = None

    @property
    def started_calls(self):
        return set().union(*(s.started_calls for s in self.clients.values()))

    @property
    def auth_failed(self):
        return bool(self.clients) and all(s.auth_failed for s in self.clients.values())

    @auth_failed.setter
    def auth_failed(self, value):
        # A global caller stop may disable all; source-specific failures stay local.
        if value:
            for client in self.clients.values():
                client.auth_failed = True

    async def __aenter__(self):
        try:
            for family in ('business','native'):
                try:
                    if self.budget:
                        self.budget.check('investigation')
                    client = self.factory(self.outputs, cancellation=self.cancellation, deadline=self.deadline, profile=family)
                    client.authority = self.authority
                    await self.stack.enter_async_context(client)
                    self.clients[family] = client
                    self.availability[family] = {'status':'available','source':client.source_context}
                except (asyncio.CancelledError, PhaseExpired):
                    raise
                except Exception as exc:  # source failure never becomes an empty successful result
                    self.availability[family] = {'status':'unavailable','error':self.outputs.redact(str(exc))}
                    self.outputs.event('mcp_source_unavailable','system', {'family':family,**self.availability[family]})
            for family, client in self.clients.items():
                for name, definition in client.tools.items():
                    if name in self.routes:
                        raise ValueError('MCP tool source conflict: '+name)
                    self.routes[name] = family
                    self.tools[name] = definition
            self.outputs.save('runtime.json', {'runtime':'rcagent','mcp_profile':self.profile,
                'sources':self.availability,'tool_sources':self.routes,'tools':list(self.tools.values()),
                'application_budget':self.budget.card() if self.budget else None})
            if not self.tools:
                raise ConnectionError('Both MCP sources unavailable; inspect source errors')
            return self
        except BaseException:
            await close_owned(self.stack,self.outputs,self.budget,'integrated_initialization')
            raise

    async def __aexit__(self,*args):
        await close_owned(self.stack,self.outputs,self.budget,'integrated')

    async def call(self, call_id, name, arguments, role, *, execution=None, trace_ids=None):
        import time
        try:
            if not execution:
                raise LocalRejection('execution_context_required','Assigned Worker required')
            execution.discovered_tools = self.tools
            self.authority.verify(execution,name,arguments)
            family = self.routes.get(name)
            if not family or task_source(execution.spec.tools+execution.spec.legacy_default,self.tools) != family:
                raise LocalRejection('source_change_required','Planner must issue a separate task for source change')
            if family == 'native':
                validate_resource_ids(self.outputs,execution,name,arguments)
            client = self.clients[family]
            client.resolved_window = self.resolved_window or client.resolved_window
            outcome = await client.call(call_id,name,arguments,role,execution=execution,trace_ids=trace_ids)
            self.resolved_window = self.resolved_window or client.resolved_window
            return outcome
        except LocalRejection as exc:
            return {'call_id':call_id,'tool':name,'arguments':arguments,'role':role,'started_at':time.time(),
                'finished_at':time.time(),'status':'tool_error','error':exc.error,'raw_result':None,'mcp_call_started':False,
                'mcp_source':self.routes.get(name)}
