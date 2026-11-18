"""PRD26 Phase5 contract, delivery and readonly boundary checks; no paid model."""

import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.investigation_session import InvestigationSession
from graph import build_parent_graph
from local_testing.tests.test_task_convergence_r7 import (
    Model,
    Native,
    chunk,
    dispatch,
    query,
    spec,
)
from models.native_tasks import TaskSpec
from platforms.insights_v5.diagnostic_policy import NATIVE_READS
from platforms.insights_v5.native_session import NativeMcpSession
from platforms.insights_v5.native_task_scope import TaskExecution
from workspace.investigation_outputs import RunOutputs
from workspace.native_materials import page_counts
from workspace.skill_knowledge import load


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def test_discovery_binding_and_direct_send_share_allowlist(self):
        sent = []

        @asynccontextmanager
        async def streams(*args, **kwargs):
            yield None, None, None

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def initialize(self):
                return AIMessage("identity")

            async def list_tools(self, cursor=None):
                names = sorted(
                    NATIVE_READS | {"insights_query_dimensions", "write_operation"}
                )
                return SimpleNamespace(
                    tools=[
                        SimpleNamespace(
                            name=n,
                            description="actual description " + n,
                            inputSchema={
                                "type": "object",
                                "properties": {"n": {"type": "integer"}},
                            },
                        )
                        for n in names
                    ],
                    nextCursor=None,
                )

            async def call_tool(self, name, args):
                sent.append(name)
                return SimpleNamespace(
                    isError=False,
                    model_dump=lambda **kw: {"content": [], "isError": False},
                )

        out = RunOutputs(self.root / "discovery", "r")
        with (
            patch.dict(
                "os.environ", {"RCAGENT_NATIVE_MCP_URL": "http://localhost/mcp"}
            ),
            patch(
                "platforms.insights_v5.native_session.streamablehttp_client", streams
            ),
            patch("platforms.insights_v5.native_session.ClientSession", Client),
        ):
            async with NativeMcpSession(out) as native:
                self.assertEqual(len(native.tools), 14)
                self.assertIn("insights_describe_query", native.tools)
                self.assertIn("insights_meta_tag_values", native.tools)
                for name in (
                    "insights_query_dimensions",
                    "write_operation",
                    "hidden_unknown",
                ):
                    result = await native.call(name, name, {}, "worker")
                    self.assertEqual(result["status"], "tool_error")
                task_spec = TaskSpec(**spec(business_tools=[])).normalize(
                    seed=False, window=None, tools=native.tools, source_ids=[]
                )
                execution = TaskExecution("t", "c", task_spec, status="running")
                native.authority.register(execution)
                result = await native.call(
                    "ok", "insights_describe_query", {}, "worker", execution=execution
                )
                self.assertEqual(result["status"], "ok")
                self.assertEqual(sent, ["insights_describe_query"])
                self.assertEqual(
                    native.tools["insights_describe_query"]["function"]["parameters"],
                    {"type": "object", "properties": {"n": {"type": "integer"}}},
                )

    async def test_worker_first_request_guidance_once_and_batch_rejection(self):
        native = None
        model = Model(
            [
                dispatch(spec()),
                chunk(
                    calls=[
                        ("ok", "insights_query_list", query()),
                        ("dimensions", "insights_query_dimensions", {}),
                        ("write", "write_operation", {}),
                    ]
                ),
                chunk("已有阶段事实"),
                chunk("完整草稿"),
                chunk("完整复核"),
            ]
        )

        def factory(out, **kw):
            nonlocal native
            native = Native(
                out, cancellation=kw["cancellation"], deadline=kw["deadline"]
            )
            return InvestigationSession(out, model=model, native=native, **kw)

        result = await build_parent_graph(runtime_factory=factory).ainvoke(
            {
                "incident_text": "观察对象",
                "target_namespace": "kylin-insights",
                "query_window": {"start_time": 1793681255, "end_time": 1793682155},
                "output_directory": str(self.root / "worker"),
            }
        )
        text, _ = load("submitting-observations", "SKILL.md")
        occurrences = lambda messages: sum(
            isinstance(m, HumanMessage)
            and m.content == "项目方法指导（非证据）：\n" + text
            for m in messages
        )
        self.assertEqual(
            [occurrences(r["messages"]) for r in model.requests], [0, 1, 1, 0, 0]
        )
        self.assertEqual([name for name, args in native.sent], ["insights_query_list"])
        self.assertEqual(
            [
                m.tool_call_id
                for m in model.requests[2]["messages"]
                if isinstance(m, ToolMessage)
            ],
            ["ok", "dimensions", "write"],
        )
        self.assertTrue(
            all(
                r["messages"][0].content == model.requests[0]["messages"][0].content
                for r in model.requests
            )
        )
        self.assertEqual(result["final_report"]["review_status"], "completed")

    async def test_phase5_scope_error_projection_and_precision_visible(self):
        out = RunOutputs(self.root / "views", "r")
        value = 9876543210123456789
        scope = {
            "requested": {"database": "db", "table": "table", "data_source": None},
            "backend_reported": {"data_source": "1m", "interval": 60},
            "source_contract": {"runtime_match": "unverified"},
            "unknown": ["effective filters"],
        }
        pagination = {
            "count_kind": "business_rows",
            "returned_rows": 1,
            "backend_total": 0,
            "total_status": "not_requested",
            "has_more": None,
            "truncated": None,
            "effective_page_size": None,
        }
        raw = {
            "structuredContent": {
                "data": {"list": [{"id": value, "body": "中" * 70000}], "total": 0},
                "meta": {
                    "query_scope": scope,
                    "pagination": pagination,
                    "warnings": ["total not requested"],
                },
            },
            "isError": False,
        }
        outcome = {
            "call_id": "v",
            "tool": "insights_query_list",
            "arguments": {},
            "role": "worker",
            "started_at": 1,
            "finished_at": 2,
            "status": "ok",
            "raw_result": raw,
        }
        view = json.loads(out.tool_result(outcome))
        sid = view["source_id"]
        self.assertEqual(out.materials.raw[sid], raw)
        self.assertEqual(
            out.materials.read(sid, "/structuredContent/data/list/0/id")[
                "items_or_text"
            ],
            value,
        )
        self.assertIn("not_requested", json.dumps(view))
        self.assertIn("unverified", json.dumps(view))
        self.assertEqual(out.sources[sid]["returned_rows"], 1)
        self.assertEqual(out.sources[sid]["backend_total"], 0)
        self.assertLessEqual(
            len(json.dumps(view, ensure_ascii=False).encode()), 50 * 1024
        )
        details = {
            "stage": "business_status",
            "reason": "backend_business_failure",
            "request_id": "r",
            "http_status": 200,
            "backend_code": 500,
            "retry_advice": "unknown",
        }
        error = {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": json.dumps({"code": "backend_error", "details": details}),
                }
            ],
        }
        ev = out.tool_result(
            {**outcome, "call_id": "e", "status": "tool_error", "raw_result": error}
        )
        self.assertIn("business_status", ev)
        self.assertIn("backend_code", ev)
        self.assertNotIn(
            "structuredContent", out.materials.raw[next(reversed(out.sources))]
        )
        metric = {
            "structuredContent": {
                "data": {"list": [{"v": value}], "data_source": "1m"},
                "meta": {"query_scope": scope},
            }
        }
        mv = out.tool_result(
            {
                **outcome,
                "call_id": "m",
                "tool": "insights_query_metric",
                "raw_result": metric,
            }
        )
        self.assertIn(str(value), mv)
        self.assertIn("backend_reported", mv)

    async def test_helper_metadata_is_not_business_rows_and_projection_is_readable(
        self,
    ):
        out = RunOutputs(self.root / "helper", "r")
        data = {
            "endpoint": "list",
            "section": "fields",
            "items": [
                {
                    "kind": "tag_field",
                    "key": "pod",
                    "definition": {
                        "name": "pod",
                        "client_name": "pod_0",
                        "server_name": "pod_1",
                        "unit": "",
                        "description": "x" * 70000,
                    },
                }
            ],
            "selection": {
                "source_items": 164,
                "returned_items": 1,
                "complete_for": "observed_metadata_response",
            },
            "provenance": [
                {"kind": "backend_metadata", "source_ref": "/data/list/3/list/pod"}
            ],
            "unknowns": ["runtime availability"],
            "warnings": ["projection only"],
            "pagination_consistency": "independent_reads",
        }
        raw = {
            "structuredContent": {
                "data": data,
                "meta": {"backend": "gateway", "request_id": "help-r"},
            }
        }
        outcome = {
            "call_id": "h",
            "tool": "insights_describe_query",
            "arguments": {},
            "role": "worker",
            "started_at": 1,
            "finished_at": 2,
            "status": "ok",
            "raw_result": raw,
        }
        view = out.tool_result(outcome)
        self.assertIn("independent_reads", view)
        self.assertIn("observed_metadata_response", view)
        self.assertIn("source_ref", view)
        self.assertIn("pod_0", view)
        self.assertNotIn("returned_rows", out.sources[next(reversed(out.sources))])
        metadata = {"structuredContent": {"data": {"list": [{"name": "tag"}]}}}
        self.assertEqual(page_counts(metadata, "insights_get_tags"), {})
        self.assertEqual(out.materials.raw[next(reversed(out.sources))], raw)


if __name__ == "__main__":
    unittest.main()
