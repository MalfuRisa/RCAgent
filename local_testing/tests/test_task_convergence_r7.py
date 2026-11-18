"""PRD27 product graph behavior with scripted model/native transport boundaries."""

import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from agents.investigation_session import InvestigationSession
from graph import build_parent_graph
from models.native_tasks import TaskSpec, Window
from platforms.insights_v5.native_session import NativeMcpSession
from platforms.insights_v5.native_task_scope import TaskExecution
from workflow.investigation_dispatch import Dispatcher
from workspace.investigation_outputs import RunOutputs, public

DEFINITIONS = {
    d["function"]["name"]: d
    for d in json.loads(
        (Path(__file__).parents[1] / "fixtures/native_phase5_tools.json").read_text(
            encoding="utf-8-sig"
        )
    )
}
START, END = 1793681255, 1793682155


def query(table="network", select="Sum(byte)", start=START, end=END):
    return {
        "body": {
            "database": "flow_metrics",
            "table": table,
            "time_start": start,
            "time_end": end,
            "queries": [{"query_id": "q", "select": select}],
            "total": False,
        }
    }


def spec(question="seed", **kw):
    return dict(
        question=question,
        deliverable="交回事实或缺口",
        completion_criteria="足够回答当前小问题",
        why_needed="已有材料有缺口",
        decision_impact="判断是否需要继续",
        **kw,
    )


def chunk(text="", calls=(), finish="stop"):
    return AIMessageChunk(
        content=text,
        tool_call_chunks=[
            {"name": name, "args": json.dumps(args), "id": cid, "index": i}
            for i, (cid, name, args) in enumerate(calls)
        ],
        response_metadata={"finish_reason": finish},
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )


def dispatch(*tasks):
    return chunk(calls=[("dispatch", "dispatch_tasks", {"tasks": list(tasks)})])


class Model:
    """Every bind returns a distinct immutable binding; requests deep-copied."""

    def __init__(self, script):
        self.script = script if callable(script) else list(script)
        self.requests, self.active, self.peak = [], 0, 0

    def bind_tools(self, definitions, **kw):
        parent, bound = self, copy.deepcopy(definitions)

        class Bound:
            async def astream(self, messages):
                async for c in parent.generate(messages, bound):
                    yield c

        return Bound()

    async def astream(self, messages):
        async for c in self.generate(messages, []):
            yield c

    async def generate(self, messages, definitions):
        request = {
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(definitions),
        }
        request["task"] = None
        for m in messages:
            if isinstance(m.content, str) and m.content.startswith("本次小任务："):
                request["task"] = json.loads(m.content.split("：", 1)[1])
        self.requests.append(request)
        assert len(self.requests) < 50, (
            "Script fixture failed to converge; this is not a product budget"
        )
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.002)
            item = self.script(request) if callable(self.script) else self.script.pop(0)
            if isinstance(item, BaseException):
                raise item
            yield item
        finally:
            self.active -= 1


class Native(NativeMcpSession):
    def __init__(self, outputs, **kw):
        super().__init__(outputs, **kw)
        self.tools = copy.deepcopy(DEFINITIONS)
        self.client = self
        self.sent, self.active, self.peak, self.closed = [], 0, 0, False
        self.fast = asyncio.Event()

    async def __aenter__(self):
        self.entered_on = asyncio.current_task()
        return self

    async def __aexit__(self, *args):
        assert asyncio.current_task() is self.entered_on
        assert self.active == 0
        self.closed = True

    async def call_tool(self, name, args):
        self.sent.append((name, copy.deepcopy(args)))
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            select = (args.get("body", {}).get("queries") or [{}])[0].get("select", "")
            if select != "error" and getattr(self, "hold_slow", None):
                await self.hold_slow.wait()
            await asyncio.sleep(0.005 if select == "error" else 0.04)
            if select == "error":
                self.fast.set()
            raw = {
                "structuredContent": {
                    "data": {
                        "list": []
                        if select == "empty"
                        else [{"v": 1234567890123456789}],
                        "total": 0,
                    },
                    "meta": {
                        "pagination": {
                            "count_kind": "business_rows",
                            "total_status": "not_requested",
                        }
                    },
                },
                "isError": select == "error",
            }
            return SimpleNamespace(isError=raw["isError"], model_dump=lambda **kw: raw)
        finally:
            self.active -= 1


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions, self.natives = [], []

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, model):
        def create(out, **kw):
            native = Native(
                out, cancellation=kw.get("cancellation"), deadline=kw.get("deadline")
            )
            session = InvestigationSession(out, model=model, native=native, **kw)
            self.sessions.append(session)
            self.natives.append(native)
            return session

        return create

    async def product(self, model, name="run", **state):
        return await build_parent_graph(runtime_factory=self.factory(model)).ainvoke(
            {
                "incident_text": "调查目标现象",
                "target_namespace": "kylin-insights",
                "query_window": {"start_time": START, "end_time": END},
                "output_directory": str(self.root / name),
                **state,
            }
        )

    def events(self, name="run"):
        return [
            json.loads(s)
            for s in (self.root / name / "events.jsonl").read_text().splitlines()
        ]

    async def test_seed_feedback_scope_wire_order_review_and_snapshots(self):
        model = Model(
            [
                dispatch(spec()),
                chunk(
                    calls=[
                        ("a", "insights_query_list", query()),
                        ("b", "insights_query_list", query("application")),
                        ("c", "insights_query_list", query(select="empty")),
                    ]
                ),
                chunk("阶段事实，另一个数据集需要Planner决定。"),
                chunk("完整草稿 source_missing；关联host不能作Pod直证。"),
                chunk("完整修订报告：材料有限，根因未决。"),
            ]
        )
        with (
            patch("config.settings.NATIVE_TRACE_REQUESTS", True),
            patch("config.settings.LLM_API_KEY", "test-secret"),
            patch(
                "agents.opencode_session.OpenCodeSession.__aenter__",
                side_effect=AssertionError("external delegation"),
            ),
        ):
            result = await self.product(model)
        report, native = result["final_report"], self.natives[0]
        self.assertEqual(len(native.sent), 2, report)
        self.assertTrue(all(a["body"]["table"] == "network" for _, a in native.sent))
        self.assertEqual(report["review_status"], "completed")
        self.assertEqual(report["draft_producer"], "planner_synthesis")
        self.assertEqual(report["evidence_status"], "unchecked")
        task = report["task_results"][0]
        self.assertEqual(task["end_reason"], "scope_change_required")
        self.assertTrue(Path(task["answer_path"]).exists())
        summary = model.requests[2]
        self.assertEqual(summary["tools"], [])
        self.assertEqual(
            [m.tool_call_id for m in summary["messages"] if isinstance(m, ToolMessage)],
            ["a", "b", "c"],
        )
        self.assertIn("阶段事实", str(model.requests[3]["messages"]))
        self.assertEqual(model.requests[-1]["tools"], [])
        self.assertTrue((self.root / "run/draft-answer.md").exists())
        checks = json.loads((self.root / "run/checks.json").read_text())
        self.assertFalse(checks["references"][0]["exists_in_run"])
        business = next(
            c
            for c in checks["source_cards"]
            if c["status"] == "ok" and c["returned_rows"] == 1
        )
        self.assertEqual(business["total_status"], "not_requested")
        events = self.events()
        self.assertEqual(
            sum(
                e["role"] == "review" and e["type"] == "mcp_call_started"
                for e in events
            ),
            0,
        )
        rounds = {e["model_round_id"] for e in events if e["type"] == "model_started"}
        self.assertEqual(len(rounds), 5)
        self.assertEqual(
            {e["model_round_id"] for e in events if e["type"] == "model_first_chunk"},
            rounds,
        )
        snapshots = list((self.root / "run/requests").glob("*.json"))
        self.assertEqual(len(snapshots), 5)
        self.assertNotIn("test-secret", "".join(p.read_text() for p in snapshots))
        self.assertTrue(native.closed)

    async def test_helper_recovery_uses_original_parameters_and_guidance(self):
        bad = {"endpoint": "fields", "database": "flow_metrics", "table": "network"}
        missing = {
            "endpoint": "list",
            "database": "flow_metrics",
            "table": "network",
            "limit": 12,
        }
        good = {**missing, "section": "fields"}
        model = Model(
            [
                dispatch(spec()),
                chunk(calls=[("bad", "insights_describe_query", bad)]),
                chunk(calls=[("missing", "insights_describe_query", missing)]),
                chunk(calls=[("good", "insights_describe_query", good)]),
                chunk(calls=[("q", "insights_query_list", query())]),
                chunk("阶段"),
                chunk("草稿"),
                chunk("修订"),
            ]
        )
        result = await self.product(model)
        self.assertEqual(
            [n for n, _ in self.natives[0].sent],
            ["insights_describe_query", "insights_query_list"],
        )
        self.assertEqual(self.natives[0].sent[0][1], good)
        sources = result["final_report"]["sources"]
        self.assertEqual(sources[0]["arguments"], bad)
        first_feedback = str(model.requests[2]["messages"])
        self.assertIn("/endpoint", first_feedback)
        self.assertIn("/section", str(model.requests[3]["messages"]))
        self.assertIn("skeleton", first_feedback)
        first = str(model.requests[1]["messages"])
        self.assertIn('"section":"fields"', first)
        self.assertIn('"section":"overview"', first)

    async def test_parallel_isolated_workers_global_limit_and_stable_return(self):
        counter = {"planner": 0}

        def script(r):
            task = r["task"]
            if task:
                return (
                    chunk("阶段-" + task["question"])
                    if not r["tools"]
                    else chunk(
                        calls=[
                            (
                                task["question"],
                                "insights_query_list",
                                query(
                                    task.get("dataset", {}).get("table", "network")
                                    if task.get("dataset")
                                    else "network"
                                ),
                            )
                        ]
                    )
                )
            if not r["tools"]:
                return chunk("修订")
            counter["planner"] += 1
            if counter["planner"] == 1:
                return dispatch(spec())
            if counter["planner"] == 2:
                return dispatch(
                    spec("CPU", dataset={"database": "flow_metrics", "table": "cpu"}),
                    spec(
                        "内存", dataset={"database": "flow_metrics", "table": "memory"}
                    ),
                )
            return chunk("最终草稿")

        model = Model(script)
        result = await self.product(model)
        self.assertEqual(self.natives[0].peak, 2)
        self.assertEqual(model.peak, 1)
        results = result["final_report"]["task_results"]
        self.assertEqual(
            {r["spec"]["question"] for r in results}, {"seed", "CPU", "内存"}
        )
        workers = [r for r in model.requests if r["task"] and r["tools"]]
        self.assertEqual(len({r["task"]["task_id"] for r in workers}), 3)
        for r in workers:
            self.assertEqual(str(r["messages"]).count("# 起步、恢复与引用"), 1)
            other = "内存" if r["task"]["question"] == "CPU" else "CPU"
            if r["task"]["question"] != "seed":
                self.assertNotIn("阶段-" + other, str(r["messages"]))
        for s in result["final_report"]["sources"]:
            owner = next(r for r in results if r["task_id"] == s["task_id"])
            self.assertEqual(s["conversation_id"], owner["conversation_id"])
        feedback = str(model.requests[-2]["messages"])
        self.assertLess(feedback.index("阶段-CPU"), feedback.index("阶段-内存"))

    async def test_invalid_control_corrected_seed_mixed_and_duplicate_degrade(self):
        cpu_task = spec("valid", tools=["insights_query_list"],
                        dataset={"database": "flow_metrics", "table": "cpu"})
        model = Model(
            [
                dispatch({"question": ""}),
                dispatch(spec(tools=["insights_query_list"])),
                chunk(calls=[("q", "insights_query_list", query())]),
                chunk("阶段"),
                dispatch(cpu_task, {"question": ""}),
                chunk(calls=[("cpu", "insights_query_list", query("cpu"))]),
                chunk("CPU阶段"),
                dispatch(cpu_task),
                dispatch(cpu_task),
                chunk("降级完整草稿"),
                chunk("修订"),
            ]
        )
        result = await self.product(model)
        self.assertEqual(len(self.natives[0].sent), 2)
        self.assertEqual(result["final_report"]["draft_producer"], "planner_synthesis")
        self.assertTrue(any(e["type"] == "plan_degraded" for e in self.events()))
        self.assertTrue(any(e["type"] == "task_rejected" for e in self.events()))
        for r in model.requests:
            assert_pairs(self, r["messages"])

    async def test_continue_reuses_latest_conversation_and_preserves_answers(self):
        out = RunOutputs(self.root / "continue", "r")
        model = Model([chunk("第一阶段"), chunk("第二阶段")])
        native = Native(out)
        async with InvestigationSession(
            out, task="q", model=model, native=native
        ) as session:
            d = Dispatcher(session, Window(start=START, end=END))
            a, _ = d.accept([spec("metadata", business_tools=[])])
            await d.run_batch(a)
            b, errors = d.accept(
                [spec("继续", continue_task_id=a[0].task_id, business_tools=[])]
            )
            self.assertFalse(errors)
            await d.run_batch(b)
            self.assertEqual(a[0].conversation_id, b[0].conversation_id)
            self.assertNotEqual(a[0].task_id, b[0].task_id)
            self.assertEqual(
                Path(out.task_results[0]["answer_path"]).read_text(), "第一阶段"
            )
            _, rejected = d.accept(
                [spec("旧续作", continue_task_id=a[0].task_id, business_tools=[])]
            )
            self.assertTrue(rejected)
            _, rejected = d.accept(
                [
                    spec(
                        "跨窗",
                        continue_task_id=b[0].task_id,
                        business_tools=[],
                        window={"start": START - 10, "end": END},
                        window_reason="对照",
                    )
                ]
            )
            self.assertTrue(rejected)
            self.assertEqual(
                str(model.requests[-1]["messages"]).count("# 起步、恢复与引用"), 1
            )

    async def test_worker_failure_other_task_survives_and_review_violation_keeps_draft(
        self,
    ):
        model = Model(
            [
                dispatch(spec()),
                chunk("定位阶段"),
                dispatch(spec("坏", business_tools=[]), spec("好", business_tools=[])),
                RuntimeError("worker failed"),
                chunk("有效阶段结论"),
                chunk("完整草稿"),
                chunk("违规复核部分", calls=[("x", "insights_query_list", query())]),
            ]
        )
        result = await self.product(model)
        report = result["final_report"]
        self.assertEqual(self.natives[0].sent, [])
        self.assertEqual(report["body"], "完整草稿")
        self.assertEqual(report["review_status"], "partial")
        self.assertIn("failed", [r["status"] for r in report["task_results"]])
        self.assertIn("有效阶段结论", str(model.requests[-2]["messages"]))

    async def test_actual_send_guard_forged_ended_and_forbidden_tools(self):
        out = RunOutputs(self.root / "guard", "r")
        native = Native(out)
        s = TaskSpec(**spec()).normalize(
            seed=True,
            window=Window(start=START, end=END),
            tools=native.tools,
            source_ids=[],
        )
        context = TaskExecution("t", "c", s, status="running")
        for role in ("planner", "review", "worker"):
            outcome = await native.call("x", "insights_query_list", query(), role)
            self.assertEqual(outcome["error"]["code"], "execution_context_required")
        outcome = await native.call(
            "x", "insights_query_list", query(), "worker", execution=context
        )
        self.assertEqual(outcome["error"]["code"], "execution_context_required")
        native.authority.register(context)
        for role in ("planner", "review"):
            outcome = await native.call(
                "x", "insights_query_list", query(), role, execution=context
            )
            self.assertEqual(outcome["error"]["code"], "execution_context_required")
        context.status = "returned"
        await native.call(
            "x", "insights_query_list", query(), "worker", execution=context
        )
        for name in ("insights_query_dimensions", "insights_delete_pod"):
            await native.call("x", name, {}, "worker", execution=context)
        self.assertEqual(native.sent, [])

    async def test_window_total_precision_and_request_redaction(self):
        for seconds, display in [
            (63755, "17小时42分35秒"),
            (3600, "1小时0分0秒"),
            (63000, "17小时30分0秒"),
        ]:
            self.assertEqual(
                Window(start=START, end=START + seconds).card()["duration"], display
            )
        out = RunOutputs(self.root / "redact", "r")
        out.secrets.append("secret")
        out.save(
            "request.json",
            public(
                {
                    "key": "secret",
                    "reasoning_content": "private",
                    "nested": {"api_key": "secret"},
                }
            ),
        )
        self.assertNotIn("private", (out.root / "request.json").read_text())
        self.assertNotIn("secret", (out.root / "request.json").read_text())

    async def test_cancellation_settles_calls_before_connection_exit(self):
        model = Model(
            [
                dispatch(spec()),
                chunk(
                    calls=[
                        ("fast", "insights_query_list", query(select="error")),
                        ("slow", "insights_query_list", query()),
                    ]
                ),
            ]
        )
        job = asyncio.create_task(self.product(model))
        while not self.natives:
            await asyncio.sleep(0.002)
        self.natives[0].hold_slow = asyncio.Event()
        await asyncio.wait_for(self.natives[0].fast.wait(), 3)
        self.sessions[0].cancellation.set()
        result = await job
        self.assertEqual(result["execution_status"], "interrupted")
        self.assertTrue(self.natives[0].closed)
        self.assertEqual(len(model.requests), 2)
        worker = next(
            r for r in self.sessions[0].runners.values() if r.role == "worker"
        )
        self.assertEqual(
            [
                m.tool_call_id
                for m in worker.conversation.messages
                if isinstance(m, ToolMessage)
            ],
            ["fast", "slow"],
        )
        self.assertEqual(
            {s["status"] for s in result["final_report"]["sources"]},
            {"tool_error", "cancelled"},
        )

    async def test_normal_task_dataset_window_and_business_family_rejected(self):
        model = Model(
            [
                dispatch(spec()),
                chunk("定位阶段"),
                dispatch(
                    spec("CPU", dataset={"database": "flow_metrics", "table": "cpu"})
                ),
                chunk(
                    calls=[
                        ("other", "insights_query_list", query("memory")),
                        ("wide", "insights_query_list", query("cpu", start=START - 1)),
                        (
                            "family",
                            "insights_list_cpu_events",
                            {"event_type": "runqlat"},
                        ),
                        ("ok", "insights_query_list", query("cpu", start=START + 1)),
                    ]
                ),
                chunk("CPU有限阶段"),
                chunk("草稿"),
                chunk("修订"),
            ]
        )
        result = await self.product(model)
        self.assertEqual(len(self.natives[0].sent), 1)
        self.assertEqual(self.natives[0].sent[0][1]["body"]["table"], "cpu")
        self.assertEqual(
            result["final_report"]["task_results"][1]["end_reason"],
            "scope_change_required",
        )
        self.assertEqual(
            [
                m.tool_call_id
                for m in model.requests[4]["messages"]
                if isinstance(m, ToolMessage)
            ],
            ["other", "wide", "family", "ok"],
        )

    async def test_empty_and_backend_error_are_stage_results_not_local_validation(self):
        for select, expected in [("empty", "ok"), ("error", "tool_error")]:
            model = Model(
                [
                    dispatch(spec()),
                    chunk(calls=[("q", "insights_query_list", query(select=select))]),
                    chunk("明确空或业务失败"),
                    chunk("有限草稿"),
                    chunk("有限修订"),
                ]
            )
            report = (await self.product(model, name=select))["final_report"]
            self.assertEqual(
                report["task_results"][0]["end_reason"], "business_batch_settled"
            )
            self.assertEqual(report["sources"][0]["status"], expected)
            self.assertEqual(len(model.requests), 5)

    async def test_run_tool_limit_shared_across_workers(self):
        p = 0

        def script(r):
            nonlocal p
            if r["task"]:
                task = r["task"]
                if not r["tools"]:
                    return chunk("阶段")
                args = query(task["dataset"]["table"])
                if task["business_tools"][0] == "insights_query_metric":
                    args["body"].pop("total")
                return chunk(
                    calls=[(str(i), task["business_tools"][0], args) for i in range(3)]
                )
            if not r["tools"]:
                return chunk("复核")
            p += 1
            if p == 1:
                return dispatch(spec(business_tools=[]))
            if p == 2:
                return dispatch(
                    spec(
                        "CPU",
                        dataset={"database": "flow_metrics", "table": "cpu"},
                        business_tools=["insights_query_list"],
                    ),
                    spec(
                        "内存",
                        dataset={"database": "flow_metrics", "table": "memory"},
                        business_tools=["insights_query_metric"],
                    ),
                )
            return chunk("草稿")

        # Pure metadata seed returns without business; callback uses its empty bindings.
        model = Model(script)
        original = model.script
        model.script = lambda r: (
            chunk("定位")
            if r["task"] and not r["task"]["business_tools"]
            else original(r)
        )
        with patch("config.settings.NATIVE_CONCURRENCY", 2):
            await self.product(model)
        self.assertEqual(len(self.natives[0].sent), 6)
        self.assertEqual(self.natives[0].peak, 2)
        self.assertEqual(model.peak, 1)
        for r in model.requests:
            assert_pairs(self, r["messages"])
            if r["task"] and r["task"]["business_tools"] and r["tools"]:
                names = {d["function"]["name"] for d in r["tools"]}
                denied = (
                    "insights_query_metric"
                    if r["task"]["question"] == "CPU"
                    else "insights_query_list"
                )
                self.assertNotIn(denied, names)

    async def test_review_failure_empty_length_and_no_extra_requests(self):
        for index, response in enumerate(
            [RuntimeError("unavailable"), chunk(), chunk("部分修订", finish="length")]
        ):
            model = Model(
                [
                    dispatch(spec(business_tools=[])),
                    chunk("阶段"),
                    chunk("完整草稿"),
                    response,
                ]
            )
            report = (await self.product(model, name="review" + str(index)))[
                "final_report"
            ]
            self.assertEqual(report["body"], "完整草稿")
            self.assertIn(report["review_status"], ["unavailable", "partial"])
            self.assertEqual(len(model.requests), 4)
            self.assertEqual(self.natives[-1].sent, [])

    async def test_authentication_stops_other_queued_requests(self):
        class AuthFailure(Exception):
            status_code = 401

        model = Model([dispatch(spec(business_tools=[])), AuthFailure("auth")])
        report = (await self.product(model))["final_report"]
        self.assertEqual(report["execution_status"], "execution_error")
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(self.natives[0].sent, [])
        self.assertTrue(self.natives[0].closed)

    async def test_deadline_stops_without_summary_or_review(self):
        model = Model(
            [dispatch(spec()), chunk(calls=[("q", "insights_query_list", query())])]
        )

        # Explicit deadline expires while a scripted native request is in flight.
        def factory(out, **kw):
            session = self.factory(model)(out, **kw)
            session.native.hold_slow = asyncio.Event()
            return session

        result = await build_parent_graph(runtime_factory=factory).ainvoke(
            {
                "incident_text": "q",
                "query_window": {"start_time": START, "end_time": END},
                "deadline": time.time() + 1.5,
                "output_directory": str(self.root / "run"),
            }
        )
        self.assertEqual(result["execution_status"], "interrupted")
        self.assertLessEqual(len(model.requests), 2)
        self.assertTrue(self.natives[0].closed)

    async def test_local_schema_rejection_does_not_trigger_business_return(self):
        model = Model(
            [
                dispatch(spec()),
                chunk(calls=[("bad", "insights_query_list", {"body": {}})]),
                chunk(calls=[("good", "insights_query_list", query())]),
                chunk("阶段"),
                chunk("草稿"),
                chunk("修订"),
            ]
        )
        report = (await self.product(model))["final_report"]
        self.assertEqual(len(self.natives[0].sent), 1)
        self.assertTrue(model.requests[2]["tools"])
        self.assertFalse(model.requests[3]["tools"])
        self.assertEqual(
            report["task_results"][0]["end_reason"], "business_batch_settled"
        )

    async def test_control_order_and_no_control_records_in_business_sources(self):
        model = Model(
            [
                chunk(
                    calls=[
                        ("before", "search_materials", {"query": "x"}),
                        (
                            "dispatch",
                            "dispatch_tasks",
                            {"tasks": [spec(business_tools=[])]},
                        ),
                        ("after", "read_skill", {"name": "finalizing-investigation"}),
                        ("second", "dispatch_tasks", {"tasks": [spec()]}),
                    ]
                ),
                chunk("阶段"),
                chunk("草稿"),
                chunk("修订"),
            ]
        )
        report = (await self.product(model))["final_report"]
        self.assertEqual(report["sources"], [])
        self.assertEqual(len(report["task_results"]), 1)
        feedback = model.requests[2]["messages"]
        tools = [m for m in feedback if isinstance(m, ToolMessage)]
        self.assertEqual(
            [m.tool_call_id for m in tools], ["before", "dispatch", "after", "second"]
        )
        self.assertIn("not executed", tools[2].content)
        assert_pairs(self, feedback)

    async def test_no_complete_synthesis_keeps_model_stages_as_program_partial(self):
        model = Model(
            [
                dispatch(spec(business_tools=[])),
                chunk("已有阶段事实"),
                chunk(),
                chunk("不完整汇总", finish="length"),
            ]
        )
        report = (await self.product(model))["final_report"]
        self.assertEqual(report["answer_source"], "program_assembled_model_partials")
        self.assertEqual(report["answer_status"], "partial")
        self.assertIn("已有阶段事实", report["body"])
        self.assertIsNone(report["draft_answer_path"])
        self.assertEqual(report["review_status"], "not_started")

    async def test_helper_unknown_contract_does_not_apply_static_supplement(self):
        from agents.native_validation import validate

        schema = {
            "type": "object",
            "properties": {
                "endpoint": {"enum": ["fields"]},
                "limit": {"type": "integer"},
            },
        }
        hint = validate(
            "insights_describe_query", {"endpoint": "fields", "limit": 12}, schema
        )
        self.assertIn("版本合同未核对", hint["contract"])
        changed = copy.deepcopy(DEFINITIONS["insights_describe_query"])
        changed["function"]["parameters"] = schema
        model = Model(
            [
                dispatch(spec()),
                chunk(
                    calls=[
                        (
                            "help",
                            "insights_describe_query",
                            {"endpoint": "fields", "limit": 12},
                        )
                    ]
                ),
                chunk("阶段"),
                chunk("草稿"),
                chunk("修订"),
            ]
        )
        with patch.dict(DEFINITIONS, {"insights_describe_query": changed}):
            await self.product(model)
        self.assertEqual(len(self.natives[0].sent), 1)
        self.assertIn("版本合同未核对", str(model.requests[2]["messages"]))

    async def test_partial_stream_cancel_retains_stage_without_promoting_completion(
        self,
    ):
        class Partial(Model):
            def __init__(self):
                super().__init__([dispatch(spec(business_tools=[]))])
                self.started = asyncio.Event()

            async def generate(self, messages, definitions):
                if any(
                    d["function"]["name"] == "insights_system_version"
                    for d in definitions
                ):
                    yield chunk("已收到但未完成的阶段文字")
                    self.started.set()
                    await asyncio.Event().wait()
                else:
                    async for c in super().generate(messages, definitions):
                        yield c

        model = Partial()
        job = asyncio.create_task(self.product(model))
        await asyncio.wait_for(model.started.wait(), 3)
        self.sessions[0].cancellation.set()
        report = (await job)["final_report"]
        self.assertEqual(report["answer_status"], "partial")
        self.assertIn("已收到但未完成", report["body"])
        self.assertTrue(self.natives[0].closed)

    async def test_malformed_body_still_pairs_and_retains_original_arguments(self):
        bad = {"body": "not-an-object"}
        model = Model(
            [
                dispatch(spec()),
                chunk(calls=[("bad", "insights_query_list", bad)]),
                chunk("参数失败阶段"),
                chunk("有限草稿"),
                chunk("修订"),
            ]
        )
        report = (await self.product(model))["final_report"]
        self.assertEqual(report["sources"][0]["arguments"], bad)
        self.assertEqual(self.natives[0].sent, [])
        feedback = [
            m for m in model.requests[2]["messages"] if isinstance(m, ToolMessage)
        ]
        self.assertEqual(feedback[0].tool_call_id, "bad")
        self.assertIn("ValidationError", feedback[0].content)

    async def test_compaction_does_not_modify_other_worker_and_retains_task(self):
        from langchain_core.messages import HumanMessage

        out = RunOutputs(self.root / "compact", "r")
        model, native = Model([chunk("摘要：source_old 原窗口与反证")]), Native(out)
        async with InvestigationSession(
            out, task="original", model=model, native=native
        ) as session:
            d = Dispatcher(session, Window(start=START, end=END))
            contexts, _ = d.accept([spec("seed", business_tools=[])])
            # First-round contract: independent tasks start only after the seed settles.
            d.any_settled = True
            others, _ = d.accept([spec("other", business_tools=[])])
            first = session.runner("worker", execution=contexts[0])
            second = session.runner("worker", execution=others[0])
            second.guide("submitting-observations")
            before = copy.deepcopy(second.conversation.messages)
            first.guide("submitting-observations")
            for i in range(7):
                first.conversation.messages += [
                    AIMessage(
                        content="",
                        tool_calls=[
                            {"id": str(i), "name": "read_material", "args": {}}
                        ],
                    ),
                    ToolMessage("旧原文" * 1000, tool_call_id=str(i)),
                ]
            first.instruction("当前任务和 source_current")
            self.assertTrue(
                await first.conversation.compact(first.summarize, first.outputs)
            )
            self.assertEqual(second.conversation.messages, before)
            self.assertTrue(
                any(
                    isinstance(m, HumanMessage)
                    and "当前任务和 source_current" in m.content
                    for m in first.conversation.messages
                )
            )
            assert_pairs(self, first.conversation.messages)

    async def test_transport_retries_share_logical_round_and_snapshot_is_opt_in(self):
        class Temporary(Exception):
            status_code = 503

        out = RunOutputs(self.root / "retry-round", "r")
        model, native = Model([Temporary("transient"), chunk("恢复")]), Native(out)
        async with InvestigationSession(
            out, task="q", model=model, native=native
        ) as session:
            runner = session.runner("review", definitions={})
            with (
                patch("agents.investigation_transport.retry_delay", return_value=0),
                patch("config.settings.NATIVE_TRACE_REQUESTS", False),
            ):
                await runner.request(tools=False)
        events = [
            json.loads(line)
            for line in (out.root / "events.jsonl").read_text().splitlines()
        ]
        started = [e for e in events if e["type"] == "model_started"]
        self.assertEqual(len(started), 2)
        self.assertEqual(len({e["model_round_id"] for e in started}), 1)
        self.assertEqual([e["payload"]["transport_attempt"] for e in started], [1, 2])
        self.assertFalse((out.root / "requests").exists())

    async def test_task_archives_and_partial_body_keep_submission_order(self):
        from workflow.investigation_review import assemble_partials

        out = RunOutputs(self.root / "return-order", "r")
        model, native = Model([]), Native(out)
        async with InvestigationSession(
            out, task="q", model=model, native=native
        ) as session:
            dispatcher = Dispatcher(session, Window(start=START, end=END))
            seed, _ = dispatcher.accept([spec("seed", business_tools=[])])
            seed[0].status = "returned"
            dispatcher.any_settled = True
            tasks, errors = dispatcher.accept(
                [spec("first", business_tools=[]), spec("second", business_tools=[])]
            )
            self.assertFalse(errors)
            first_started, second_returned = asyncio.Event(), asyncio.Event()

            class ScriptRunner:
                def __init__(self, context):
                    self.execution, self.definitions = context, {}
                    self.outputs = out
                    self.last_public_text = ""

                def guide(self, _name):
                    pass

                def instruction(self, _text):
                    pass

                async def work(self):
                    if self.execution is tasks[0]:
                        first_started.set()
                        await second_returned.wait()
                    else:
                        await first_started.wait()
                        second_returned.set()
                    return self.execution.spec.question + "-stage", "model_returned"

            with patch.object(
                session,
                "runner",
                side_effect=lambda _role, execution: ScriptRunner(execution),
            ):
                await dispatcher.run_batch(tasks)
            finished = [
                json.loads(line)["task_id"]
                for line in (out.root / "events.jsonl").read_text().splitlines()
                if json.loads(line)["type"] == "task_finished"
            ]
            self.assertEqual(finished, [tasks[1].task_id, tasks[0].task_id])
            self.assertEqual(
                [item["task_id"] for item in out.task_results],
                [task.task_id for task in tasks],
            )
            assemble_partials(out)
            self.assertLess(
                out.partial_body.index("first-stage"),
                out.partial_body.index("second-stage"),
            )


def assert_pairs(test, messages):
    pending = []
    for m in messages:
        if isinstance(m, AIMessage) and m.tool_calls:
            test.assertFalse(pending)
            pending = [c["id"] for c in m.tool_calls]
        elif isinstance(m, ToolMessage):
            test.assertIn(m.tool_call_id, pending)
            pending.remove(m.tool_call_id)
        else:
            test.assertFalse(pending)
    test.assertFalse(pending)


if __name__ == "__main__":
    unittest.main()
