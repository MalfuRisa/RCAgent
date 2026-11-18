"""PRD44 program fetch handoff, directed read, closeout and retry slot."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import ToolMessage

from agents.investigation_budget import RunBudget
from agents.investigation_session import InvestigationSession
from graph import build_parent_graph
from workflow.direct_query import DIRECT_TOOL
from local_testing.tests.test_prd42_short_dispatch import (
    END,
    NODE,
    START,
    Model,
    Native,
    all_receipts,
    business_args,
    chunk,
    first_source_id,
    planners,
)


def history_args(server="frontend"):
    return {
        "datasetId": "app-path",
        "client": NODE,
        "server": server,
        "metrics": ["server_response_time", "percentile95"],
        "time": {"start": START, "end": END},
        "chartFormat": "auto",
    }


def direct_args(metrics=("server_response_time", "request_rate"), object_name=NODE):
    return {
        "question": "取原题窗口内该客户端的响应时延和请求率",
        "object": object_name,
        "metrics": list(metrics),
    }


def evidence_receipts(model):
    """Direct, closeout and dispatch receipts in first-seen call order."""
    seen, sequence = set(), []
    for request in model.requests:
        for message in request["messages"]:
            if not isinstance(message, ToolMessage) or not message.name:
                continue
            if not (
                message.name.startswith("dispatch_")
                or message.name in {DIRECT_TOOL, "close_investigation"}
            ):
                continue
            if message.tool_call_id in seen:
                continue
            seen.add(message.tool_call_id)
            sequence.append(json.loads(message.content))
    return sequence


class EmptyNative:
    tools, profile, auth_failed = {}, "native", False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def closeout_text():
    return (
        "frontend 在 10:46 出现短时尖峰，响应时延从 266 微秒升至 100732 微秒，"
        "下一分钟回落到 242 微秒。现有对照样本不足以判断多条路径是否同步变慢。"
    )


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions, self.natives = [], []

    def tearDown(self):
        self.tmp.cleanup()

    async def product(self, model, *, name="run", **state):
        def factory(out, **kw):
            native = Native(out, cancellation=kw.get("cancellation"), deadline=kw.get("deadline"))
            session = InvestigationSession(out, model=model, native=native, **kw)
            self.sessions.append(session)
            self.natives.append(native)
            return session

        return await build_parent_graph(runtime_factory=factory).ainvoke(
            {
                "incident_text": (
                    f"请调查 {NODE} 在北京时间 2026年11月16日 09:47–10:47 作为客户端访问其他服务时的"
                    "响应时延和请求率。"
                ),
                "query_window": {"start_time": START, "end_time": END},
                "output_directory": str(self.root / name),
                **state,
            }
        )

    def events(self, name="run"):
        return [
            json.loads(line)
            for line in (self.root / name / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    async def test_first_result_batch_hands_back_facts_without_worker_summary(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(
                        calls=[
                            (
                                "seed",
                                "dispatch_business",
                                {
                                    "question": "取延迟与分钟P95",
                                    "tools": [
                                        "insights_query_metrics",
                                        "insights_metric_history",
                                    ],
                                },
                            )
                        ]
                    )
                return chunk("结论引用 " + str(first_source_id(request)))
            if not request["tools"]:
                return chunk("阶段")
            return chunk(
                calls=[
                    ("q", "insights_query_metrics", business_args()),
                    ("h", "insights_metric_history", history_args()),
                ]
            )

        model = Model(script)
        result = await self.product(model)
        report = result["final_report"]
        task = report["task_results"][0]
        self.assertEqual(task["end_reason"], "first_result_settled")
        self.assertEqual(task["handoff_mode"], "facts")
        self.assertEqual(task["handoff_reason"], "known_structured_results")
        self.assertEqual(report["execution_counts"]["fact_handoffs"], 1)
        self.assertEqual(report["execution_counts"]["worker_summary_rounds"], 0)
        self.assertEqual(report["execution_counts"]["worker_model_rounds"], 1)
        self.assertEqual(len(self.natives[0].sent), 2)
        self.assertEqual(len(task["source_ids"]), 2)
        worker_requests = [
            request
            for request in model.requests
            if not request["planner"] and request["task"]
        ]
        self.assertEqual(len(worker_requests), 1, "facts handoff must not add a summary request")
        self.assertIn("source_", report["body"])

    async def test_direct_query_returns_planner_source_with_zero_workers(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(calls=[("direct", DIRECT_TOOL, direct_args())])
                return chunk("结论引用 " + str(first_source_id(request)))
            raise AssertionError("directed read must not start a Worker")

        model = Model(script)
        result = await self.product(model)
        report = result["final_report"]
        self.assertEqual(report["task_results"], [])
        self.assertEqual(report["execution_counts"]["direct_queries"], 1)
        self.assertEqual(report["execution_counts"]["zero_worker"], True)
        self.assertEqual(report["dispatch_status"], "direct")
        self.assertEqual(report["execution_counts"]["worker_model_rounds"], 0)
        self.assertEqual(len(report["sources"]), 1)
        source = report["sources"][0]
        self.assertEqual(source["role"], "planner")
        self.assertEqual(source["tool_name"], "insights_query_metrics")
        self.assertEqual(source["status"], "ok")
        self.assertEqual(source["arguments"]["time"], {"start": START, "end": END})
        self.assertEqual(
            source["arguments"]["plans"][0]["filters"],
            [{"field": "auto_service_0", "op": "eq", "values": [NODE]}],
        )
        self.assertEqual([name for name, _ in self.natives[0].sent], ["insights_query_metrics"])
        self.assertIn("source_", report["body"])

    async def test_direct_query_rejects_unconfirmed_object_then_legal_read_runs(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(
                        calls=[
                            (
                                "bad",
                                DIRECT_TOOL,
                                direct_args(object_name="invented-service"),
                            )
                        ]
                    )
                if count["planner"] == 2:
                    return chunk(calls=[("good", DIRECT_TOOL, direct_args())])
                return chunk("正文 " + str(first_source_id(request)))
            raise AssertionError("directed read must not start a Worker")

        model = Model(script)
        result = await self.product(model, name="reject")
        report = result["final_report"]
        self.assertEqual(report["execution_counts"]["direct_queries"], 1)
        self.assertEqual(len(report["sources"]), 1)
        self.assertEqual([name for name, _ in self.natives[0].sent], ["insights_query_metrics"])
        sequence = evidence_receipts(model)
        self.assertEqual(sequence[0]["status"], "rejected")
        error = sequence[0]["rejected"][0]["error"]
        self.assertEqual(error["code"], "unconfirmed_object")
        self.assertEqual(error["path"], "object")
        self.assertFalse(error["mcp_call_started"])
        self.assertEqual(sequence[1]["status"], "executed")

    async def test_planner_closeout_uses_one_bounded_generation_round(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(calls=[("direct", DIRECT_TOOL, direct_args())])
                if count["planner"] == 2:
                    return chunk(
                        calls=[
                            (
                                "close",
                                "close_investigation",
                                {"reason": "已取得足够的窗口原值"},
                            )
                        ]
                    )
                return chunk(closeout_text())
            raise AssertionError("directed read must not start a Worker")

        model = Model(script)
        result = await self.product(model, name="closeout")
        report = result["final_report"]
        self.assertEqual(report["execution_counts"]["closeout_rounds"], 1)
        self.assertEqual(report["execution_counts"]["closeout_trigger"], "planner")
        self.assertEqual(report["stop_reason"], "planner_closeout")
        self.assertEqual(report["stop_reasons"], ["planner_closeout"])
        self.assertNotIn("\n\n", report["body"].strip())
        self.assertIn("100732", report["body"])
        closeout_request = [
            request
            for request in planners(model)
            if not request["tools"]
            and any("停止取证" in str(message.content) for message in request["messages"])
        ][-1]
        text = " ".join(str(message.content) for message in closeout_request["messages"])
        self.assertNotIn("本run实际发现的MCP工具目录", text)
        self.assertIn("当前取证与待答项", text)
        self.assertEqual(report["review_status"], "program_checked")

    async def test_margin_closeout_starts_before_the_next_investigation_round(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(
                        calls=[
                            (
                                "seed",
                                "dispatch_business",
                                {
                                    "question": "取延迟和请求率",
                                    "tools": ["insights_query_metrics"],
                                },
                            )
                        ]
                    )
                return chunk(closeout_text())
            if not request["tools"]:
                return chunk("阶段")
            return chunk(calls=[("q", "insights_query_metrics", business_args())])

        model = Model(script)
        real_budget = RunBudget
        with patch(
            "workflow.investigation.RunBudget",
            side_effect=lambda deadline=None: real_budget(deadline, total=15),
        ):
            result = await self.product(model, name="margin")
        report = result["final_report"]
        self.assertEqual(report["execution_counts"]["closeout_trigger"], "program")
        self.assertEqual(report["stop_reason"], "investigation_margin")
        self.assertEqual(report["execution_counts"]["fact_handoffs"], 1)
        self.assertEqual(len(planners(model)), 2, report)
        self.assertIn("100732", report["body"])

    async def test_closeout_conflict_keeps_the_legal_direct_read(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(
                        calls=[
                            ("direct", DIRECT_TOOL, direct_args()),
                            ("close", "close_investigation", {"reason": "enough"}),
                        ]
                    )
                return chunk("正文 " + str(first_source_id(request)))
            raise AssertionError("directed read must not start a Worker")

        model = Model(script)
        result = await self.product(model, name="conflict")
        report = result["final_report"]
        self.assertEqual(report["execution_counts"]["direct_queries"], 1)
        self.assertEqual(report["execution_counts"]["closeout_rounds"], 0)
        self.assertEqual(len(report["sources"]), 1)
        sequence = evidence_receipts(model)
        self.assertEqual(sequence[0]["status"], "executed")
        self.assertEqual(sequence[1]["status"], "rejected")
        self.assertEqual(
            sequence[1]["rejected"][0]["error"]["code"], "closeout_conflict"
        )

    async def test_backoff_releases_model_slot_for_another_request(self):
        class Transient(Exception):
            status_code = 503

        class Flaky:
            def __init__(self):
                self.calls = 0

            async def astream(self, messages):
                self.calls += 1
                if self.calls == 1:
                    raise Transient("503 service unavailable")
                yield chunk("ok")

        out = self._outputs("slot")
        model = Flaky()
        session = InvestigationSession(out, task="q", model=model, native=EmptyNative())
        async with session:
            first = session.runner("review", definitions={})
            second = session.runner("review", definitions={})
            with patch(
                "agents.investigation_transport.retry_delay", return_value=0.2
            ):
                job = asyncio.create_task(first.request(tools=False))
                await asyncio.sleep(0.05)
                other = await second.request(tools=False)
                waited = await job
        self.assertEqual(other.content, "ok")
        self.assertEqual(waited.content, "ok")
        events = [
            json.loads(line)
            for line in (out.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        backoff = next(
            event
            for event in events
            if event["type"] == "model_backoff"
            and event.get("conversation_id") == first.conversation_id
        )
        self.assertTrue(backoff["payload"]["slot_released"])
        second_slot = next(
            event
            for event in events
            if event["type"] == "model_slot_acquired"
            and event.get("conversation_id") == second.conversation_id
        )
        first_retry = next(
            event
            for event in events
            if event["type"] == "model_started"
            and event.get("conversation_id") == first.conversation_id
            and event["payload"]["transport_attempt"] == 2
        )
        self.assertLess(backoff["sequence"], second_slot["sequence"])
        self.assertLess(second_slot["sequence"], first_retry["sequence"])
        self.assertEqual(out.model_attempts, 3)
        self.assertEqual(out.model_backoffs, [0.2])

    def _outputs(self, name):
        from workspace.investigation_outputs import RunOutputs

        return RunOutputs(self.root / name, name)

    async def test_existing_budget_and_cancel_paths_still_work(self):
        # The retry refactor must not bypass the phase budget or cancellation.
        from agents.investigation_budget import PhaseExpired
        from workspace.investigation_outputs import RunOutputs

        out = RunOutputs(self.root / "queue", "q")
        out.budget = RunBudget(total=0.15)
        session = InvestigationSession(
            out, task="q", model=Model([chunk("never")]), native=EmptyNative()
        )
        async with session:
            await session.model_semaphore.acquire()
            runner = session.runner("review", definitions={})
            try:
                with self.assertRaises(PhaseExpired):
                    await runner.request(tools=False)
            finally:
                session.model_semaphore.release()


if __name__ == "__main__":
    unittest.main()