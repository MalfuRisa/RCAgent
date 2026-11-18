"""Stateless v5 MCP transport; access policy is supplied by composition."""
import asyncio
import json
import time
from contextlib import AsyncExitStack
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def decode_result(result):
    if result.isError:
        raise ValueError('MCP IsError: ' + ' '.join(getattr(c, 'text', '') for c in result.content)[:1000])
    raw = result.structuredContent
    if raw is None:
        raw = json.loads(next(c.text for c in result.content if getattr(c, 'type', None) == 'text'))
    if not isinstance(raw, dict) or 'data' not in raw or not isinstance(raw.get('meta'), dict):
        raise ValueError('v5 response requires data/meta')
    data = raw['data']
    if isinstance(data, dict) and (data.get('code', 200) not in (0, 200) or
            data.get('OPT_STATUS', 'SUCCESS') not in ('SUCCESS', 'success')):
        raise ValueError('v5 business failure')
    return raw


class V5Client:
    def __init__(self, url, token, *, policy):
        self.policy = policy
        self.url, self.token = url, token
        self.stack = AsyncExitStack()
        self.schemas = {}
        self.calls = []

    async def __aenter__(self):
        read, write, _ = await self.stack.enter_async_context(streamablehttp_client(
            self.url, headers={'Authorization': 'Bearer ' + self.token}))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        self.identity = (await self.session.initialize()).model_dump(mode='json')
        listed = await self.session.list_tools()
        self.published = {t.name for t in listed.tools}
        # Discovery policy is applied before anything can reach a model.
        self.schemas = {t.name: t.model_dump(mode='json') for t in listed.tools if t.name in self.policy.discovery}
        if not self.policy.discovery <= self.published:
            raise ValueError(self.policy.missing_prefix + repr(sorted(self.policy.discovery-self.published)))
        if len(self.published) != self.policy.published_count:
            raise ValueError('expected production observe toolset (54), refusing full/manage')
        return self

    async def __aexit__(self, *exc):
        return await self.stack.__aexit__(*exc)

    async def call(self, role, name, arguments):
        if name not in self.policy.role_tools.get(role, ()) or name not in self.schemas:
            raise ValueError(self.policy.denied_message)
        import jsonschema
        jsonschema.validate(arguments, self.schemas[name]['inputSchema'])
        self.policy.validate(name, arguments)
        started = time.monotonic()
        row = {'role': role, 'tool': name, 'arguments': arguments}
        self.calls.append(row)
        try:
            result = await asyncio.wait_for(self.session.call_tool(name, arguments), 40)
            row['raw_mcp_result'] = result.model_dump(mode='json')
            decoded = decode_result(result)
            row['status'] = 'ok'
            return decoded
        except (ValueError, TimeoutError) as exc:
            row.update(status='failed', error=str(exc)[:1000])
            if row.get('raw_mcp_result') is not None:
                from tools.diagnostic_catalog import DiagnosticResultError
                raise DiagnosticResultError(row['raw_mcp_result'],str(exc)) from exc
            raise
        finally:
            row['duration_ms'] = (time.monotonic()-started)*1000
