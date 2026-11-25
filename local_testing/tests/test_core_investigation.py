"""Offline regression checks for the shipped investigation graph."""
import json
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import AIMessageChunk
from mcp.types import CallToolResult

from agents.investigation_session import InvestigationSession
from graph import build_parent_graph
from platforms.insights_v5.native_session import NativeMcpSession


CLIENT = "client-a"
FRONT = "frontend"
OTHER = "storage"
METRIC = "server_response_time"
HISTORY = "insights_metric_history"
START = 1767225600  # 08:00 Asia/Shanghai
END = START + 3600
QUESTION = (
    "请调查北京时间 2026 年 1 月 1 日 08:00–09:00，client-a 作为客户端访问 frontend "
    "和 storage 的两条路径的普通响应时延。分别给出峰值分钟、峰前一分钟、峰值和峰后一分钟，"
    "并说明每条路径的形态。frontend 达到最高值时，另一条路径比上一分钟升了还是降了？"
)


def reply(text="", *, call=None):
    calls = []
    if call:
        call_id, name, args = call
        calls.append({"id": call_id, "name": name, "args": json.dumps(args), "index": 0})
    return AIMessageChunk(
        content=text, tool_call_chunks=calls,
        response_metadata={"finish_reason": "tool_calls" if call else "stop"},
    )


def dispatch(target):
    return reply(call=(f"dispatch-{target}", "dispatch_business", {
        "question": f"查 {CLIENT} 作为客户端访问 {target} 的普通响应时延分钟时序",
        "tools": [HISTORY], "return_mode": "first_result",
    }))


def query(target):
    return reply(call=(f"history-{target}", HISTORY, {
        "datasetId": "app-path", "client": CLIENT, "server": target, "metrics": [METRIC],
        "time": {"start": START, "end": END},
    }))


class ScriptedModel:
    def __init__(self, script):
        self.script = iter(script)

    def bind_tools(self, definitions, **_):
        model = self

        class WithTools:
            async def astream(self, messages):
                yield model.respond(messages, definitions)

        return WithTools()

    async def astream(self, messages):
        yield self.respond(messages, [])

    def respond(self, messages, definitions):
        item = next(self.script, None)
        if item is None:
            raise AssertionError("Unexpected model request")
        return item(messages, definitions) if callable(item) else item


class HistoryMcp(NativeMcpSession):
    def __init__(self, outputs, *, missing, cancellation):
        super().__init__(outputs, cancellation=cancellation, profile="business")
        text = {"type": "string"}
        self.tools = {HISTORY: {"type": "function", "function": {
            "name": HISTORY, "description": "Read minute history",
            "parameters": {"type": "object", "properties": {
                "datasetId": text, "client": text, "server": text,
                "metrics": {"type": "array", "items": text}, "time": {"type": "object"},
            }, "required": ["datasetId", "client", "server", "metrics", "time"]},
        }}}
        self.source_context = {
            "mcp_url": "http://history.example.test/mcp",
            "server_identity": {"serverInfo": {"name": "test-history", "version": "1"}},
        }
        self.missing = missing
        self.sent = []

    async def __aenter__(self):
        self.client = self
        return self

    async def __aexit__(self, *_):
        pass

    async def call_tool(self, name, arguments):
        assert name == HISTORY and arguments["client"] == CLIENT
        target = arguments["server"]
        self.sent.append(target)
        window = {key: arguments["time"][key] for key in ("start", "end")}
        filters = [
            {"field": "auto_service_0", "op": "eq", "values": [CLIENT]},
            {"field": "auto_service_1", "op": "eq", "values": [target]},
        ]
        values = [100] * 61 if target == FRONT else [900] * 61
        if target == FRONT:
            values[19:22] = [120, 7500, 110]
        else:
            values[8], values[19], values[20] = 1234, 940, 910
        if self.missing and target == OTHER:
            values = [None] * len(values)
        finite = [value for value in values if value is not None]
        return CallToolResult(content=[], structuredContent={"data": {
            "time": window,
            "metrics": [{"key": METRIC, "unit": "µs", "stats": {"max": max(finite, default=None)}}],
            "queryMeta": {
                "requested": {"datasetId": "app-path", "time": window, "filters": filters},
                "constructed": {"appliedFilters": filters, "requests": [{
                    "interval": 60, "groupBy": ["toi"], "dataSource": "1m",
                    "source": {"database": "metrics", "table": "paths"},
                }]},
            },
            "charts": [{"metricKey": METRIC, "unit": "µs", "series": [{
                "points": [[(START + 60 * index) * 1000, value] for index, value in enumerate(values)],
            }]}],
        }})


class InvestigationTests(unittest.IsolatedAsyncioTestCase):
    async def run_scenario(self, script, *, missing=False):
        native_sessions = []

        def runtime(outputs, **kwargs):
            native = HistoryMcp(outputs, missing=missing, cancellation=kwargs["cancellation"])
            native_sessions.append(native)
            return InvestigationSession(outputs, model=ScriptedModel(script), native=native, **kwargs)

        with tempfile.TemporaryDirectory() as folder:
            archive = Path(folder) / "run"
            result = await build_parent_graph(runtime_factory=runtime).ainvoke({
                "incident_text": QUESTION, "query_window": {"start_time": START, "end_time": END},
                "output_directory": str(archive), "review_mode": "program",
            })
            self.assertTrue((archive / "report.md").is_file())
            self.assertEqual(len(list((archive / "sources").glob("*.json"))), 2)
            self.assertEqual(native_sessions[0].sent, [FRONT, OTHER])
            return result["final_report"]

    def final_reply(self, _messages, definitions):
        self.assertEqual(definitions, [])
        return reply("以下为已核实的分钟证据。")

    async def test_missing_requirement_defers_closeout_until_second_path(self):
        def after_rejection(messages, _definitions):
            self.assertTrue(any("evidence_incomplete" in str(message.content) for message in messages))
            return dispatch(OTHER)

        result = await self.run_scenario([
            dispatch(FRONT), query(FRONT), reply(call=("early", "close_investigation", {})),
            after_rejection, query(OTHER), self.final_reply,
        ])
        self.assertEqual(result["answer_status"], "completed")
        self.assertEqual(result["stop_reason"], "evidence_ready_closeout")
        for value in ("08:20", "7,500", "08:08", "1,234", "940", "910", "下降"):
            self.assertIn(value, result["body"])

    async def test_empty_second_path_delivers_verified_partial_result(self):
        result = await self.run_scenario([
            dispatch(FRONT), query(FRONT), dispatch(OTHER), query(OTHER),
            reply(call=("close", "close_investigation", {"reason": "第二条路径无有效样本"})),
            self.final_reply,
        ], missing=True)
        self.assertEqual(result["answer_status"], "partial")
        self.assertIn("7,500", result["body"])
        self.assertIn(OTHER, result["body"])
        self.assertRegex(result["body"], r"缺|无有效|无法判断")
