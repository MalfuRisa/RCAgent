"""PRD29 section 13: actual request inputs, sends, owner cleanup and CLI state."""

import asyncio
import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import messages_to_dict

from agents.investigation_budget import ENTRY_BUDGET, PhaseExpired, RunBudget
from agents.investigation_session import InvestigationSession
from agents.investigation_transport import RunCancellation
from local_testing.tests.test_dispatch_delivery_r8 import Checks as R8
from local_testing.tests.test_task_convergence_r7 import (
    Model,
    Native,
    chunk,
    dispatch,
    query,
    spec,
)
from models.native_tasks import TaskSpec
from platforms.insights_v5.native_session import NativeMcpSession
from platforms.insights_v5.native_task_scope import TaskExecution
from workflow.investigation_dispatch import Dispatcher
from workspace.investigation_outputs import RunOutputs


class Checks(unittest.IsolatedAsyncioTestCase):
    setUp, tearDown, product = R8.setUp, R8.tearDown, R8.product

    def archive_request(self, name, request):
        root = os.getenv("RCAGENT_TEST_EVIDENCE_DIR")
        if root:
            Path(root, name + ".json").write_text(json.dumps({
                "kind": "scripted_model_actual_input_not_HTTP_wire",
                "messages": messages_to_dict(request["messages"]), "tools": request["tools"]},
                ensure_ascii=False, indent=2))

    def source(self, out, *, value=None, status="ok", tool="insights_query_metric"):
        raw = {"structuredContent": {"data": {"list": value}, "meta": {"pagination": {
            "count_kind": "business_rows", "total_status": "not_requested"}}}}
        return json.loads(out.tool_result({"call_id": "c", "tool": tool, "arguments": query(),
            "role": "worker", "started_at": 0, "finished_at": 1, "status": status,
            "raw_result": raw, "error": {"code": "bad_query"} if status != "ok" else None,
            "mcp_call_started": True}))["source_id"]

    async def test_actual_planner_and_worker_receive_consistent_minimal_guidance(self):
        model = Model([dispatch(spec(tools=[])), chunk("目录已得，业务需另派"), chunk("有限结果")])
        await self.product(model)
        planner = str(model.requests[0]["messages"])
        self.assertNotIn("定位和业务探测seed", planner)
        self.assertIn("不强制业务探测", planner)
        self.assertIn("insights_k8s_resource", planner)
        worker = str(model.requests[1]["messages"])
        self.assertIn("first_result首批目标调用结算即交回", worker)
        self.assertIn("effective_tools", worker)
        names = [t["function"]["name"] for t in model.requests[1]["tools"]]
        self.assertNotIn("insights_query_metric", names)
        self.archive_request("section13-planner-input", model.requests[0])
        self.archive_request("section13-worker-input", model.requests[1])

    async def test_focused_actual_request_has_originals_uncited_errors_empty_and_receipts(self):
        from workflow.investigation_review import review_draft
        out = RunOutputs(self.root / "focused", "r")
        out.budget = RunBudget()
        out.instruction = "原问题：只查目标窗口"
        cited = self.source(out, value=[{"byte": 9876543210123456789}, {"byte": 4}] * 20)
        empty = self.source(out, value=[])
        failed = self.source(out, value=None, status="tool_error")
        out.event("task_rejected", "planner", {"mcp_call_started": False, "error": "能力不足"}, call_id="reject")
        out.save_draft("正文只引用 " + cited)
        model = Model([chunk("短纠错提示")])
        async with InvestigationSession(out, task=out.instruction, model=model, native=Native(out)) as session:
            session.closing = True
            await review_draft(session, "focused")
        sent = str(model.requests[0]["messages"])
        for text in ("9876543210123456789", "/structuredContent/data/list", cited, empty,
                     failed, "bad_query", "event_id", "mcp_call_started", "原问题", "truncated"):
            self.assertIn(text, sent)
        self.assertEqual(model.requests[0]["tools"], [])
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(out.answer("worker"), "正文只引用 " + cited)
        self.archive_request("section13-focused-input", model.requests[0])

    async def test_closeout_and_program_partial_keep_unfinished_original_values(self):
        from workflow.investigation_review import assemble_partials
        out = RunOutputs(self.root / "closing", "r")
        out.budget = RunBudget()
        sid = self.source(out, value=[{"packet": 7654321}])
        model = Model([chunk("有限业务结论")])
        async with InvestigationSession(out, task="q", model=model, native=Native(out)) as session:
            dispatcher = Dispatcher(session)
            session.closing = True
            await dispatcher.draft("budget")
        sent = str(model.requests[0]["messages"])
        self.assertIn("7654321", sent)
        self.assertIn(sid, sent)
        self.assertIn("/structuredContent/data/list", sent)
        self.assertEqual(out.task_results, [])
        self.archive_request("section13-closing-input", model.requests[0])
        out.texts.clear()
        assemble_partials(out)
        self.assertIn("7654321", out.partial_body)
        self.assertIn(sid, out.partial_body)

    def test_projection_capacity_and_omissions_are_explicit(self):
        from workspace.investigation_projection import project_materials
        out = RunOutputs(self.root / "projection", "r")
        for _ in range(4):
            self.source(out, value=[{"v": "x" * 4000}])
        result = project_materials(out, max_sources=1, max_chars=8000)
        self.assertEqual(len(result["original_values"]), 1)
        self.assertEqual(result["omitted_source_count"], 3)
        self.assertTrue(result["original_values"][0]["previews"][0]["items_or_text"][0]["preview_is_fragment"])
        self.assertIn("not exhaustively", str(result["limitations"]))

    async def test_native_initialization_cancel_and_hanging_failure_cleanup_same_owner(self):
        for mode in ("cancel", "failure"):
            entered = asyncio.Event()
            owners = []
            class Initializing(NativeMcpSession):
                async def _initialize(self, owners=owners, entered=entered, mode=mode):
                    owners.append(asyncio.current_task())
                    async def release_resource():
                        owners.append(asyncio.current_task())
                        await asyncio.Event().wait()
                    self.stack.push_async_callback(release_resource)
                    entered.set()
                    if mode == "failure":
                        raise RuntimeError("initial connection failed")
                    await asyncio.Event().wait()
            out = RunOutputs(self.root / mode, "r")
            out.budget = RunBudget(total=.25)
            cancellation = RunCancellation()
            native = Initializing(out, cancellation=cancellation)
            session = InvestigationSession(out, task="q", cancellation=cancellation, model=Model([]), native=native)
            async def enter(session=session):
                async with session:
                    self.fail("initialization must not complete")
            started = time.monotonic()
            job = asyncio.create_task(enter())
            await entered.wait()
            if mode == "cancel":
                cancellation.set()
                with self.assertRaises(asyncio.CancelledError):
                    await job
            else:
                with self.assertRaisesRegex(RuntimeError, "initial connection failed"):
                    await job
            self.assertLess(time.monotonic() - started, .8)
            self.assertEqual(owners, [job, job])
            events = [json.loads(line) for line in (out.root / "events.jsonl").read_text().splitlines()]
            cleanup = [e["payload"] for e in events if e["type"] == "cleanup_finished"]
            self.assertGreaterEqual(len(cleanup), 2)
            self.assertTrue(all(not c["success"] for c in cleanup))
            self.assertTrue(out.cleanup_failures)

    async def test_batch_and_direct_send_share_investigation_deadline(self):
        out = RunOutputs(self.root / "batch", "r")
        out.budget = RunBudget(total=.18)
        native = Native(out)
        async with InvestigationSession(out, task="q", model=Model([]), native=native) as session:
            dispatcher = Dispatcher(session)
            accepted, errors = dispatcher.accept([spec(tools=[])])
            self.assertFalse(errors)
            out.budget = session.budget = native.budget = RunBudget(total=.18)
            async def hang(context):
                await asyncio.Event().wait()
            with patch.object(dispatcher, "run_task", side_effect=hang), self.assertRaises(PhaseExpired):
                await dispatcher.run_batch(accepted)
            # Actual Native.call still checks the budget even without RoleRunner.
            task = TaskSpec.model_validate(spec(tools=["insights_k8s_resource"])).normalize(
                seed=True, window=None, tools=native.tools, source_ids=[])
            context = TaskExecution("direct", "direct", task, status="running")
            native.authority.register(context)
            with self.assertRaises(PhaseExpired):
                await native.call("late", "insights_k8s_resource", {"query": {"pod_id": 259}}, "worker", execution=context)
            self.assertEqual(native.sent, [])

    async def test_cancelled_remote_receipt_says_sent_and_retains_originals(self):
        from workflow.native_tool_calls import execute_batch
        out = RunOutputs(self.root / "receipt", "r")
        out.budget = RunBudget(total=.6)
        native = Native(out)
        native.hold_slow = asyncio.Event()
        async with InvestigationSession(out, task="q", model=Model([]), native=native) as session:
            task = TaskSpec.model_validate(spec(tools=["insights_k8s_resource"])).normalize(
                seed=True, window=None, tools=native.tools, source_ids=[])
            context = TaskExecution("sent", "sent", task, status="running")
            native.authority.register(context)
            runner = session.runner("worker", execution=context)
            job = asyncio.create_task(execute_batch([{"id": "real", "name": "insights_k8s_resource",
                "args": {"query": {"pod_id": 259}}}], runner))
            while not native.sent:
                await asyncio.sleep(.005)
            session.cancellation.set()
            with self.assertRaises(asyncio.CancelledError):
                await job
            self.assertTrue(next(iter(out.sources.values()))["mcp_call_started"])
            self.assertIn('"mcp_call_started": true', runner.conversation.messages[-1].content)

    async def test_actual_send_rechecks_after_scheduling(self):
        now = [0]
        out = RunOutputs(self.root / "send-race", "r")
        out.budget = RunBudget(clock=lambda: now[0])
        native = Native(out)
        task = TaskSpec.model_validate(spec(tools=["insights_k8s_resource"])).normalize(
            seed=True, window=None, tools=native.tools, source_ids=[])
        context = TaskExecution("race", "race", task, status="running")
        native.authority.register(context)
        async def scheduled(awaitable, *args, **kwargs):
            now[0] = 201
            return await awaitable
        with patch("platforms.insights_v5.native_session.controlled", side_effect=scheduled), self.assertRaises(PhaseExpired):
            await native.call("late", "insights_k8s_resource", {"query": {"pod_id": 259}}, "worker", execution=context)
        self.assertEqual(native.sent, [])
        self.assertEqual(native.started_calls, set())

    async def test_unsettled_batch_is_revoked_and_cannot_mutate_delivery(self):
        from workspace.native_tasks import save_task
        out = RunOutputs(self.root / "unsettled", "r")
        out.budget = RunBudget(total=.18)
        native = Native(out)
        jobs, release = [], asyncio.Event()
        async with InvestigationSession(out, task="q", model=Model([]), native=native) as session:
            dispatcher = Dispatcher(session)
            accepted, _ = dispatcher.accept([spec(tools=[])])
            out.budget = session.budget = native.budget = RunBudget(total=.3)
            async def late(context):
                jobs.append(asyncio.current_task())
                context.status = "running"
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
                save_task(out, context, "迟到内容", status="returned", reason="late")
                try:
                    await native.call("late", "insights_list_databases", {}, "worker", execution=context)
                except PhaseExpired:
                    pass
            try:
                with patch.object(dispatcher, "run_task", side_effect=late), self.assertRaises(PhaseExpired):
                    await dispatcher.run_batch(accepted)
                self.assertTrue(accepted[0].revoked)
                self.assertTrue(out.errors)
                report = out.publish("interrupted")
                out.sealed = True
                release.set()
                await asyncio.gather(*jobs)
                self.assertEqual(len(report["task_results"]), 1)
                self.assertEqual(report["task_results"][0]["status"], "cancelled")
                self.assertNotIn("迟到内容", report["body"])
                self.assertEqual(native.sent, [])
            finally:
                release.set()
                await asyncio.gather(*jobs, return_exceptions=True)

    async def test_stop_causes_keep_first_and_distinguish_user_and_product_deadline(self):
        from workflow.investigation import run_roles
        out = RunOutputs(self.root / "causes", "r")
        out.budget = RunBudget()
        async def no_run(*args):
            raise PhaseExpired("investigation")
        async def no_draft(*args):
            raise PhaseExpired("generation")
        async with InvestigationSession(out, task="q", model=Model([]), native=Native(out)) as session:
            with patch.object(Dispatcher, "run", no_run), patch.object(Dispatcher, "draft", no_draft):
                await run_roles(session, out, {})
        report = out.publish()
        self.assertEqual(report["stop_reason"], "budget_investigation")
        self.assertEqual(report["stop_reasons"], ["budget_investigation", "budget_generation"])
        self.assertTrue(report["budget_limited"])
        now = [0]
        for deadline, reason in ((None, "budget_delivery"), (time.time()+100, "deadline")):
            now[0] = 0
            budget = RunBudget(deadline, clock=lambda: now[0])
            now[0] = 400
            with self.assertRaises(PhaseExpired) as caught:
                budget.check("investigation")
            self.assertEqual(caught.exception.reason, reason)

    async def test_sealed_outputs_cannot_be_modified_by_late_worker(self):
        from workspace.native_tasks import save_task
        out = RunOutputs(self.root / "sealed", "r")
        task = TaskExecution("late", "late", TaskSpec.model_validate(spec()))
        out.save_draft("已交付")
        out.publish()
        before = (out.root / "result.json").read_text()
        out.sealed = True
        out.save_answer("worker", "迟到答案")
        self.assertIsNone(save_task(out, task, "迟到阶段", status="returned", reason="late"))
        self.assertEqual(out.answer("worker"), "已交付")
        self.assertEqual(out.task_results, [])
        self.assertEqual((out.root / "result.json").read_text(), before)

    async def test_cli_context_reset_on_error_and_sidecar_failure_keeps_printed_body(self):
        from local_testing.run_investigation import run
        input_path = self.root / "input.json"
        input_path.write_text(json.dumps({"incident_text": "q"}))
        previous = ENTRY_BUDGET.set("sentinel")
        class FailedGraph:
            async def astream(self, *args, **kwargs):
                raise ValueError("graph input failed")
                yield
        class GoodGraph:
            async def astream(self, *args, **kwargs):
                yield "values", {"final_report": {"body": "已经完整打印的报告", "answer_source": "model"}}
        try:
            with patch("local_testing.run_investigation.build_parent_graph", return_value=FailedGraph()), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(await run(self.root / "cli-fail", input_path))
            self.assertEqual(ENTRY_BUDGET.get(), "sentinel")
            stdout = io.StringIO()
            with patch("local_testing.run_investigation.build_parent_graph", return_value=GoodGraph()), patch.object(Path, "write_text", side_effect=OSError("disk full")), contextlib.redirect_stdout(stdout):
                self.assertTrue(await run(self.root / "cli-full", input_path))
            self.assertIn("已经完整打印的报告", stdout.getvalue())
            self.assertIn('"answer_already_printed": true', stdout.getvalue())
            self.assertEqual(ENTRY_BUDGET.get(), "sentinel")
        finally:
            ENTRY_BUDGET.reset(previous)


if __name__ == "__main__":
    unittest.main()
