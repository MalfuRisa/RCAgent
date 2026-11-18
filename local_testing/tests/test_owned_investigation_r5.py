"""Short behavioral checks for PRD24; no real model replay."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents.investigation_session import InvestigationSession
from context.investigation_conversation import Conversation
from graph import build_parent_graph
from local_testing.tests.test_task_convergence_r7 import (
    DEFINITIONS,
    Model,
    Native,
    chunk,
    dispatch,
    query,
    spec,
)
from models.native_tasks import TaskSpec
from platforms.insights_v5.native_task_scope import TaskExecution
from workspace.investigation_outputs import RunOutputs

QUERY = DEFINITIONS["insights_query_list"]


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, model, native):
        def create(out, **kw):
            native.outputs = out
            native.cancellation = kw["cancellation"]
            native.deadline = kw["deadline"]
            return InvestigationSession(out, model=model, native=native, **kw)

        return create

    async def product(self, model, native, name="run"):
        return await build_parent_graph(
            runtime_factory=self.factory(model, native)
        ).ainvoke(
            {
                "incident_text": "调查目标现象",
                "target_namespace": "kylin-insights",
                "query_window": {"start_time": 1793681255, "end_time": 1793682155},
                "output_directory": str(self.root / name),
            }
        )

    async def test_roles_batch_schema_order_and_plain_review(self):
        model = Model(
            [
                dispatch(spec()),
                chunk(
                    calls=[
                        ("a", "insights_query_list", query()),
                        ("b", "insights_query_list", query(select="error")),
                        ("c", "insights_query_list", {"body": {}}),
                    ]
                ),
                chunk("Worker阶段已有原文，归属host。"),
                chunk("完整草稿"),
                chunk("完整修订报告"),
            ]
        )
        native = Native(None)
        with patch(
            "agents.opencode_session.OpenCodeSession.__aenter__",
            side_effect=AssertionError("External delegation"),
        ):
            result = await self.product(model, native)
        report = result["final_report"]
        self.assertEqual(report["review_status"], "completed")
        self.assertEqual(report["evidence_status"], "unchecked")
        self.assertEqual(len(native.sent), 2)
        self.assertTrue(native.closed)
        tools = [m for m in model.requests[2]["messages"] if isinstance(m, ToolMessage)]
        self.assertEqual([m.tool_call_id for m in tools], ["a", "b", "c"])
        self.assertIn("1234567890123456789", tools[0].content)
        self.assertIn("ValidationError", tools[2].content)
        self.assertEqual(model.requests[-1]["tools"], [])
        self.assertTrue((self.root / "run" / "worker-answer.md").exists())

    async def test_duplicate_ids_not_executed_and_review_failure_keeps_worker(self):
        model = Model(
            [
                dispatch(spec()),
                chunk(
                    calls=[
                        ("dup", "insights_query_list", query()),
                        ("dup", "insights_query_list", query()),
                    ]
                ),
                chunk("已有有限结论"),
                RuntimeError("review unavailable"),
            ]
        )
        native = Native(None)
        result = await self.product(model, native)
        self.assertEqual(native.sent, [])
        self.assertEqual(result["final_report"]["body"], "已有有限结论")
        self.assertEqual(result["final_report"]["review_status"], "unavailable")
        self.assertFalse(
            any(
                isinstance(m, AIMessage) and any(c["id"] == "dup" for c in m.tool_calls)
                for m in model.requests[2]["messages"]
            )
        )

    async def test_material_paging_precision_and_write_failure(self):
        out = RunOutputs(self.root / "materials", "r")
        raw = {
            "structuredContent": {
                "data": {
                    "list": [{"v": 1234567890123456789, "log": "中" * 70000}, {"v": 2}],
                    "total": 0,
                }
            }
        }
        out.materials.store("s", raw)
        view = out.materials.model_view("s", raw)
        self.assertIn("truncated", view)
        self.assertLessEqual(len(view.encode("utf-8")), 50 * 1024)
        page = out.materials.read(
            "s", "/structuredContent/data/list", limit=1, max_chars=200
        )
        self.assertEqual(page["next_offset"], 1)
        fragment = out.materials.read(
            "s",
            "/structuredContent/data/list/0/log",
            offset=100,
            limit=200,
            max_chars=200,
        )
        self.assertEqual(fragment["next_offset"], 300)
        self.assertEqual(len(fragment["items_or_text"]), 200)
        with self.assertRaises(ValueError):
            out.materials.read("../../secret")
        with patch("pathlib.Path.write_text", side_effect=OSError("disk")):
            out.save_answer("worker", "仍可返回答案")
            report = out.publish("execution_error")
        self.assertEqual(report["body"], "仍可返回答案")
        self.assertIsNone(report["worker_answer_path"])
        self.assertTrue(report["limitations"])
        # Oversized metadata must expose later field names, not just initial rows.
        metadata = {
            "structuredContent": {
                "data": {
                    "list": [
                        {
                            "list": {
                                "early": {"name": "early", "description": "x" * 70000},
                                "actual_field": {
                                    "name": "generic",
                                    "client_name": "generic_0",
                                    "server_name": "generic_1",
                                },
                            }
                        }
                    ]
                }
            }
        }
        out.materials.store("metadata", metadata)
        preview = out.materials.model_view("metadata", metadata)
        self.assertIn("generic_0", preview)
        self.assertIn("structure_index", preview)

    async def test_context_capacity_summary_pairs_and_service_error(self):
        conv = Conversation("原始问题", [])
        for i in range(7):
            conv.messages += [
                AIMessage(
                    content="", tool_calls=[{"id": str(i), "name": "x", "args": {}}]
                ),
                ToolMessage(
                    "big" * 1000,
                    tool_call_id=str(i),
                    additional_kwargs={"source_id": "s" + str(i)},
                ),
            ]
        conv.active_role = "worker"
        conv.messages.append(
            HumanMessage("当前任务", additional_kwargs={"role_task": "worker"})
        )

        async def summarize(messages):
            return "关键值90，source_id=s0，未决"

        out = RunOutputs(self.root / "compact", "r")
        self.assertTrue(await conv.compact(summarize, out))
        self.assertEqual(conv.messages[1].content, "原始问题")
        for i, m in enumerate(conv.messages):
            if isinstance(m, AIMessage) and m.tool_calls:
                self.assertEqual(
                    conv.messages[i + 1].tool_call_id, m.tool_calls[0]["id"]
                )
        self.assertIn("source_id=s0", str(conv.messages))

    async def test_service_context_rejection_compacts_and_continues(self):
        out = RunOutputs(self.root / "capacity-service", "r")
        native = Native(out)
        model = Model(
            [
                RuntimeError("context_length_exceeded"),
                chunk("摘要：关键值90，原文source_old"),
                chunk("接续后的有限结论"),
            ]
        )
        async with InvestigationSession(
            out, task="原任务", model=model, native=native
        ) as session:
            task_spec = TaskSpec(**spec(business_tools=[])).normalize(
                seed=False, window=None, tools=native.tools, source_ids=[]
            )
            context = TaskExecution("task", "conversation", task_spec, status="running")
            native.authority.register(context)
            runner = session.runner("worker", execution=context)
            for i in range(7):
                runner.conversation.messages += [
                    AIMessage("旧资料" * 1000),
                    HumanMessage("继续"),
                ]
            text, _reason = await runner.work()
            self.assertEqual(text, "接续后的有限结论")
            self.assertEqual(len(model.requests), 3)
            self.assertEqual(
                model.requests[-1]["messages"][0].content,
                model.requests[0]["messages"][0].content,
            )
            self.assertIn("source_old", str(model.requests[-1]))

    async def test_transient_retry_and_partial_stream_not_final(self):
        from agents.investigation_transport import stream_response

        class Failure(Exception):
            status_code = 503

        model = Model([Failure("temporary"), chunk("恢复")])
        out = RunOutputs(self.root / "retry", "r")
        with patch("agents.investigation_transport.retry_delay", return_value=0):
            response = await stream_response(
                model, [HumanMessage("q")], out, "worker", 1
            )
        self.assertEqual(response.content, "恢复")
        self.assertEqual(len(model.requests), 2)

        class Interrupted:
            async def astream(self, messages):
                yield chunk("未完成正文")
                raise RuntimeError("stream interrupted")

        with self.assertRaises(RuntimeError):
            await stream_response(Interrupted(), [HumanMessage("q")], out, "worker", 1)
        self.assertEqual(out.answer("worker"), "")
        self.assertIn("未完成正文", (out.root / "worker-partial.md").read_text())

    async def test_native_pagination_missing_tools_and_actual_sdk_schema(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        from config.chat_model import get_investigation_model
        from platforms.insights_v5.native_session import NativeMcpSession

        @asynccontextmanager
        async def streams(*args, **kwargs):
            yield None, None, None

        class Client:
            def __init__(self, *args, **kwargs):
                self.cursors = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def initialize(self):
                return AIMessage("identity")

            async def list_tools(self, cursor=None):
                self.cursors.append(cursor)
                name = (
                    "insights_query_list"
                    if cursor is None
                    else "insights_system_version"
                )
                tool = SimpleNamespace(
                    name=name,
                    description="原描述",
                    inputSchema=QUERY["function"]["parameters"],
                )
                return SimpleNamespace(
                    tools=[tool], nextCursor="next" if cursor is None else None
                )

        out = RunOutputs(self.root / "native-contract", "r")
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
                self.assertEqual(native.client.cursors, [None, "next"])
                self.assertEqual(len(native.tools), 2)
                defs = list(native.tools.values())
                bound = get_investigation_model().bind_tools(defs, tool_choice="auto")
                self.assertEqual(bound.kwargs["tools"], defs)
                self.assertNotIn("parallel_tool_calls", bound.kwargs)


if __name__ == "__main__":
    unittest.main()
