"""PRD27 §16: service timeout versus user stop, through the product graph."""

import asyncio
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import ToolMessage

from agents.investigation_session import InvestigationSession
from agents.investigation_transport import RunCancellation
from graph import build_parent_graph
from local_testing.tests.test_task_convergence_r7 import (
    END,
    START,
    Model,
    Native,
    chunk,
    dispatch,
    query,
    spec,
)
from workflow.investigation import cancel


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        artifact_root = os.getenv("RCAGENT_TEST_ARTIFACT_ROOT")
        self.root = Path(artifact_root or self.tmp.name) / self._testMethodName
        self.root.mkdir(parents=True)
        self.sessions, self.natives = [], []
        logging.getLogger("asyncio").setLevel(logging.ERROR)

    def tearDown(self):
        self.tmp.cleanup()

    def factory(self, model, native_factory=None):
        def create(out, **kw):
            native = (native_factory or Native)(
                out, cancellation=kw["cancellation"], deadline=kw["deadline"]
            )
            session = InvestigationSession(out, model=model, native=native, **kw)
            self.sessions.append(session)
            self.natives.append(native)
            return session

        return create

    async def product(self, model, name="run", *, native_factory=None, **state):
        return await build_parent_graph(
            runtime_factory=self.factory(model, native_factory)
        ).ainvoke(
            {
                "incident_text": "脚本核验超时及停止原因",
                "run_id": name,
                "query_window": {"start_time": START, "end_time": END},
                "output_directory": str(self.root / name),
                **state,
            }
        )

    def events(self, name="run"):
        return [
            json.loads(line)
            for line in (self.root / name / "events.jsonl").read_text().splitlines()
        ]

    def assert_closed_and_no_review_mcp(self, events):
        self.assertTrue(self.natives[-1].closed)
        self.assertEqual(self.natives[-1].active, 0)
        self.assertEqual(
            sum(
                e["role"] == "review" and e["type"] == "mcp_call_started"
                for e in events
            ),
            0,
        )

    async def review_timeout(self, deadline, *, partial=False, idle=False):
        script = [
            dispatch(spec(business_tools=[])),
            chunk("阶段事实"),
            chunk("完整Planner草稿"),
        ]
        if idle:

            class IdleModel(Model):
                async def generate(self, messages, definitions):
                    if not definitions:
                        if partial:
                            yield chunk("复核已收部分文字")
                        await asyncio.Event().wait()
                    else:
                        async for item in super().generate(messages, definitions):
                            yield item

            model = IdleModel(script)
        else:
            model = Model(script + [TimeoutError("service timeout")] * 3)
        with (
            patch("agents.investigation_transport.retry_delay", return_value=0),
            patch("config.settings.INVESTIGATION_IDLE_TIMEOUT", 0.2),
        ):
            result = await self.product(
                model, **({"deadline": deadline} if deadline is not None else {})
            )
        report, events = result["final_report"], self.events()
        self.assertEqual(result["execution_status"], "completed")
        self.assertEqual(report["body"], "完整Planner草稿")
        self.assertEqual(
            report["review_status"], "partial" if partial else "unavailable"
        )
        self.assertIsNone(result["execution_error"])
        self.assertFalse(any("用户明确指定" in str(e) for e in events))
        attempts = [
            e for e in events if e["type"] == "model_started" and e["role"] == "review"
        ]
        self.assertEqual(
            [e["payload"]["transport_attempt"] for e in attempts], [1, 2, 3]
        )
        self.assertEqual(len({e["model_round_id"] for e in attempts}), 1)
        self.assertEqual(sum(e["type"] == "review_started" for e in events), 1)
        failure = next(e for e in events if e["type"] == "review_failed")
        self.assertEqual(failure["payload"]["reason"], "service_timeout")
        self.assertEqual(self.natives[-1].sent, [])
        self.assert_closed_and_no_review_mcp(events)
        if partial:
            self.assertEqual(
                (self.root / "run/review-partial.md").read_text(), "复核已收部分文字"
            )

    async def test_review_service_timeout_without_deadline(self):
        await self.review_timeout(None)

    async def test_review_service_timeout_before_user_deadline(self):
        await self.review_timeout(time.time() + 60)

    async def test_review_idle_timeout_preserves_partial_and_draft(self):
        await self.review_timeout(None, partial=True, idle=True)

    async def test_worker_service_timeout_is_local_failure_not_user_interrupt(self):
        model = Model(
            [dispatch(spec(business_tools=[]))]
            + [TimeoutError("worker service")] * 3
            + [chunk("保留缺口的完整草稿"), chunk("复核完整稿")]
        )
        with patch("agents.investigation_transport.retry_delay", return_value=0):
            report = (await self.product(model))["final_report"]
        self.assertEqual(report["execution_status"], "completed")
        self.assertEqual(report["task_results"][0]["end_reason"], "service_error")
        self.assertEqual(report["task_results"][0]["status"], "failed")
        self.assertIn("worker service", str(report["task_results"][0]["errors"]))
        self.assert_closed_and_no_review_mcp(self.events())

    async def test_escaped_service_timeout_is_execution_error_not_deadline(self):
        # Initial plan and a stage succeed; subsequent Planner and draft transport both fail.
        for index, deadline in enumerate([None, time.time() + 60]):
            model = Model(
                [dispatch(spec(business_tools=[])), chunk("阶段文字仍保留")]
                + [TimeoutError("planner service")] * 6
            )
            name = "escaped" + str(index)
            with patch("agents.investigation_transport.retry_delay", return_value=0):
                result = await self.product(
                    model, name=name, **({"deadline": deadline} if deadline else {})
                )
            report = result["final_report"]
            self.assertEqual(result["execution_status"], "execution_error")
            self.assertIn("服务超时", result["execution_error"]["message"])
            self.assertNotIn("用户明确指定", result["execution_error"]["message"])
            self.assertEqual(report["answer_status"], "partial")
            self.assertIn("阶段文字仍保留", report["body"])
            self.assert_closed_and_no_review_mcp(self.events(name))

    async def batch_interruption(self, *, user_cancel):
        first_mcp_started, never = asyncio.Event(), asyncio.Event()

        class HeldNative(Native):
            async def call_tool(self, name, args):
                if args.get("body", {}).get("queries", [{}])[0].get("select") == "held":
                    first_mcp_started.set()
                    self.hold_slow = never
                return await super().call_tool(name, args)

        p = 0

        def scripted(request):
            nonlocal p
            task = request["task"]
            if task:
                if task["question"] == "seed":
                    return chunk("定位已有文字")
                return chunk(
                    calls=[("held", "insights_query_list", query(select="held"))]
                )
            p += 1
            if p == 1:
                return dispatch(spec(business_tools=[]))
            if p == 2:
                return dispatch(
                    spec(
                        "running",
                        dataset={"database": "flow_metrics", "table": "network"},
                    ),
                    spec(
                        "queued",
                        dataset={"database": "flow_metrics", "table": "application"},
                    ),
                )
            raise AssertionError("No further Planner or review allowed after stop")

        model = Model(scripted)
        kwargs = {} if user_cancel else {"deadline": time.time() + 5}
        with patch("config.settings.NATIVE_WORKER_CONCURRENCY", 1):
            job = asyncio.create_task(
                self.product(model, native_factory=HeldNative, **kwargs)
            )
            await asyncio.wait_for(first_mcp_started.wait(), 4)
            if user_cancel:
                self.assertTrue(cancel("run"))
            result = await asyncio.wait_for(job, 7)
        expected = "cancelled" if user_cancel else "deadline"
        report = result["final_report"]
        self.assertEqual(result["execution_status"], "interrupted")
        affected = report["task_results"][1:]
        self.assertEqual(len(affected), 2)
        self.assertTrue(
            all(
                t["status"] == "cancelled" and t["end_reason"] == expected
                for t in affected
            )
        )
        events = self.events()
        started = {e["task_id"] for e in events if e["type"] == "task_started"}
        self.assertIn(affected[0]["task_id"], started)
        self.assertNotIn(affected[1]["task_id"], started)
        runner = next(
            r
            for r in self.sessions[0].runners.values()
            if r.execution and r.execution.task_id == affected[0]["task_id"]
        )
        self.assertEqual(
            [
                m.tool_call_id
                for m in runner.conversation.messages
                if isinstance(m, ToolMessage)
            ],
            ["held"],
        )
        self.assertIn("定位已有文字", report["body"])
        self.assertEqual(
            sum(e["role"] == "review" and e["type"] == "model_started" for e in events),
            0,
        )
        self.assert_closed_and_no_review_mcp(events)

    async def test_explicit_deadline_classifies_running_and_queued_tasks(self):
        await self.batch_interruption(user_cancel=False)

    async def test_user_cancel_classifies_running_and_queued_tasks(self):
        await self.batch_interruption(user_cancel=True)

    async def test_first_stop_reason_is_preserved_during_cleanup(self):
        from agents.investigation_transport import interruption_reason

        cancellation = RunCancellation()
        cancellation.set()
        self.assertEqual(
            interruption_reason(cancellation, time.time() - 1), "cancelled"
        )
        cancellation = RunCancellation()
        self.assertEqual(interruption_reason(cancellation, time.time() - 1), "deadline")
        cancellation.set()
        self.assertEqual(interruption_reason(cancellation), "deadline")
