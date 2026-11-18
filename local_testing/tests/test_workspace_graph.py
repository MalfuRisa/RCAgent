"""Scripted models and fake Gateway execute the compiled parent graph."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool
from config import settings
from models.schemas import (
    Symptom,
    SymptomList,
    RCATask,
    RCATaskList,
    SupervisorDecision,
    FinalReport,
)
from workspace.runtime import current, bind
from workspace.repository import WorkspaceError
from workspace.reports import adjudicate
from workspace.queries import save_query
from workspace.tasks import finish
from test_workspace import WorkspaceCase

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/workspace_iam_history.json").read_text(
        encoding="utf-8-sig"
    )
)


class ScriptedModel:
    def __init__(self, followup=False, invalid=False, stall=False, timeout=False):
        self.calls = []
        self.followup = followup
        self.invalid = invalid
        self.stall = stall
        self.timeout = timeout
        self.worker_steps = {}

    def with_structured_output(self, schema):
        async def result(messages):
            b = current.get()
            self.calls.append((b.role, schema.__name__, messages))
            if schema == SymptomList:
                return SymptomList(
                    symptoms=[
                        Symptom(
                            potential_symptom="DNS failure",
                            resource_type="pod",
                            affected_resource=FIXTURE["input"]["target_resource"],
                            evidence="Archived application DNS error",
                        )
                    ]
                )
            if schema == RCATaskList:
                return RCATaskList(
                    rca_tasks=[
                        RCATask(
                            priority=1,
                            investigation_goal="Investigate DNS evidence",
                            target_resource=name,
                            resource_type="pod",
                        )
                        for name in (FIXTURE["input"]["target_resource"], "coredns")
                    ]
                )
            tasks = await b.repository.all(b.incident_id, "task")
            if self.invalid:
                return SupervisorDecision(
                    tasks_to_execute_ids=["invented"],
                    final_report=FinalReport(**FIXTURE["proposed"]),
                )
            if self.followup:
                self.followup = False
                return SupervisorDecision(tasks_to_execute_ids=[tasks[0]["id"]])
            resources = await b.repository.all(b.incident_id, "resource")
            evidence = await b.repository.all(b.incident_id, "evidence")
            return SupervisorDecision(
                final_report=FinalReport(**FIXTURE["proposed"]),
                claims=[
                    {
                        "content": "DNS configuration definitely broken",
                        "type": "causal",
                        "resource_id": resources[0]["id"],
                        "supporting_evidence_ids": [evidence[0]["id"]],
                        "opposing_evidence_ids": [],
                        "mechanism": "oom_termination",
                        "limitations": ["Actual DNS configuration was not queried"],
                    }
                ],
            )

        return RunnableLambda(result)

    def bind_tools(self, tools, **kwargs):
        async def result(messages):
            b = current.get()
            self.calls.append((b.role, "tools", messages))
            if self.timeout:
                await asyncio.sleep(2)
            if b.role == "supervisor":
                if b.reads == 0:
                    rows = await b.repository.all(b.incident_id, "evidence")
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_evidence",
                                "args": {"evidence_id": rows[0]["id"]},
                                "id": "supervisor-read",
                            }
                        ],
                    )
                return AIMessage(content="Ready for structured decision")
            step = self.worker_steps.get(b.attempt_id, 0)
            self.worker_steps[b.attempt_id] = step + 1
            resource = await b.repository.read(b.incident_id, b.resource_id, "resource")
            if self.stall or step == 0:
                calls = [
                    {
                        "name": "get_pod_runtime_status",
                        "args": {"cluster": "rd2", "pod_uid": "wrong-uid"},
                        "id": "rejected-uid",
                    }
                ]
                calls.extend(
                    {
                        "name": "get_pod_network_summary",
                        "args": {"cluster": "rd2", "pod_uid": resource["uid"]},
                        "id": f"network-{i}",
                    }
                    for i in range(12)
                )
                return AIMessage(content="", tool_calls=calls)
            evidence = await b.repository.all(b.incident_id, "evidence")
            references = [e["id"] for e in evidence if e.get("attempt_id") == b.attempt_id]
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_final_diagnosis",
                        "args": {
                            "diagnosis": "CoreDNS health cannot be established"
                            if resource["name"] == "coredns"
                            else "DNS dependency/configuration is suspected",
                            "reasoning": "Symptoms are not mechanism evidence",
                            "supporting_evidence_ids": references[:1],
                            "opposing_evidence_ids": references[:1]
                            if resource["name"] == "coredns"
                            else [],
                            "gaps": ["DNS configuration missing"],
                            "answer_status": "inconclusive",
                        },
                        "id": "submission",
                    }
                ],
            )

        return RunnableLambda(result)


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.historical = {r["name"]: r["output"] for r in FIXTURE["queries"]}
        self.service_historical = {r["name"]: r["output"] for r in FIXTURE["service_queries"]}
        self.main = self.historical["resolve_pod"]["pods"][0]

    async def invoke(self, name, args):
        self.calls.append((name, dict(args)))
        if name == "search_k8s_targets":
            if args.get("kind") == "auto":
                return self.service_historical[name]
            if args.get("query") == self.main["name"]:
                return {
                    "status": "ok",
                    "candidates": [{**self.main, "kind": "pod", "uid": self.main["pod_uid"]}],
                }
            # Synthetic CoreDNS fixture, explicitly distinct from historical facts.
            return {
                "status": "ok",
                "candidates": [
                    {
                        "kind": "pod",
                        "name": "coredns",
                        "cluster": "rd2",
                        "namespace": "kube-system",
                        "uid": "synthetic-dns",
                    }
                ],
            }
        if args.get("pod_uid") == "synthetic-dns":
            return {
                "status": "partial",
                "summary": {"tcp_error_count": 7},
                "meta": {"data_gaps": [{"reason": "application_no_data"}]},
            }
        if name in ("get_service_backends", "query_metrics", "search_traces"):
            return self.service_historical[name]
        return self.historical.get(name, {"status": "ok", "summary": {}})

    def tools(self):
        names = settings.KYLIN_ALL_TOOLS_ALLOWED
        inventory = json.loads(
            (Path(__file__).parents[1] / "fixtures/workspace_gateway_schemas.json").read_text(
                encoding="utf-8-sig"
            )
        )
        schemas = {t["name"]: t["inputSchema"] for t in inventory}
        result = []
        for name in names:

            async def call(_name=name, **kwargs):
                return await self.invoke(_name, kwargs)

            result.append(
                StructuredTool.from_function(
                    coroutine=call,
                    name=name,
                    description="fake Gateway using original schema",
                    args_schema=schemas[name],
                )
            )
        return result


class ParentGraphTests(unittest.IsolatedAsyncioTestCase):
    async def run_graph(
        self, model=None, max_steps=16, input_state=None, gateway=None, budget_enabled=False
    ):
        from graph import build_parent_graph
        from tools import observability_adapter, mcp_tools

        model = model or ScriptedModel()
        gateway = gateway or FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(settings, "CONTEXT_BUDGET_ENABLED", budget_enabled),
                patch.object(settings, "MODEL_MIN_INTERVAL_SECONDS", 0),
                patch.object(settings, "WORKSPACE_ROOT", Path(directory)),
                patch.object(settings, "get_chat_model", return_value=model),
                patch.object(settings, "WORKER_MAX_MODEL_STEPS", max_steps),
                patch.object(
                    observability_adapter, "_TOOLS_BY_NAME", {t.name: t for t in gateway.tools()}
                ),
                patch.object(mcp_tools, "_TOOLS", gateway.tools()),
            ):
                state = await build_parent_graph().ainvoke(
                    input_state or FIXTURE["input"], {"recursion_limit": 100}
                )
                from workspace.repository import Repository

                repo = Repository(directory)
                rows = {
                    k: await repo.all(state["incident_id"], k)
                    for k in (
                        "resource",
                        "task",
                        "attempt",
                        "evidence",
                        "context",
                        "report",
                        "model_call",
                        "model_attempt",
                        "report_proposal",
                        "run",
                        "question",
                        "observation",
                        "judgment",
                        "stage_commit",
                        "checkpoint",
                        "material_usage",
                        "read_receipt",
                        "report_packet",
                    )
                }
                events = await repo.transact(
                    lambda db: [dict(r) for r in db.execute("SELECT * FROM events")]
                )
                if "RCAGENT_TEST_ARCHIVE" in __import__("os").environ:
                    output = (
                        Path(__import__("os").environ["RCAGENT_TEST_ARCHIVE"])
                        / self._testMethodName
                    )
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "parent_graph_result.json").write_text(
                        json.dumps(
                            {
                                "state": state,
                                "records": rows,
                                "gateway_calls": gateway.calls,
                                "model_calls": [(r, s) for r, s, _ in model.calls],
                                "events": events,
                            },
                            ensure_ascii=False,
                            indent=2,
                            default=lambda v: (
                                v.model_dump() if hasattr(v, "model_dump") else str(v)
                            ),
                        ),
                        encoding="utf-8",
                    )
                    import shutil

                    shutil.copytree(directory, output / "workspace-sample", dirs_exist_ok=True)
                return state, rows, events, gateway, model

    async def test_complete_parent_graph_history_parallel_context_budget_and_gate(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(two_tasks=True, supervisor_read=True)
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(len(rows["task"]), 2)
        self.assertTrue(all(t["execution_status"] == "finished" for t in rows["task"]))
        self.assertEqual(len(sent), 1, "second task reuses the compatible query")
        self.assertFalse(state["final_report"]["fallback"])
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
        self.assertTrue(rows["read_receipt"])
        self.assertTrue(all(c["context_policy"] == "unified_sre" for c in rows["context"]))
        for messages in rows["visible_messages"].values():
            requests = [c["id"] for m in messages for c in m.get("tool_calls", [])]
            replies = [m["tool_call_id"] for m in messages if m["type"] == "tool"]
            self.assertEqual(requests, replies)
        self.assertTrue(all(c["visible_messages_artifact_id"] for c in rows["context"]))

    async def test_followup_finished_inconclusive_task_can_run_again(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(followup=True)
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(len(rows["attempt"]), 2)
        self.assertEqual(state["supervisor_rounds"], 2)
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(t["answer_status"] == "inconclusive" for t in rows["task"]))
        self.assertFalse(state["final_report"]["report_errors"])

    async def test_invalid_decision_corrected_once_then_unresolved(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(fault="invalid_forever")
        self.assertEqual(state["execution_status"], "failed")
        self.assertEqual(state["stop_reason"], "supervisor_incomplete")
        rejected = [
            a
            for a in rows["model_action"]
            if a["operation"] == "submit_report" and a["outcome"].get("status") == "rejected"
        ]
        self.assertGreater(len(rejected), 1)
        self.assertLessEqual(len(rejected), settings.SUPERVISOR_MAX_READS + 2)
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
        self.assertTrue(
            rows["observation"], "bad report proposals do not discard accepted observations"
        )

    async def test_model_step_limit_stops_worker(self):
        state, rows, *_ = await self.run_graph(ScriptedModel(stall=True), max_steps=2)
        self.assertTrue(
            all(a["stop_reason"] == "worker_model_steps_exhausted" for a in rows["attempt"])
        )
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")

    async def test_natural_language_auto_search_empty_backends_stops_without_models(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(
            input_state={
                "triage_status": "insufficient",
                "clarification": {"reason": "identity unresolved"},
            }
        )
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(model.calls, [])
        self.assertEqual(sent, [])
        self.assertEqual(rows["task"], [])
        self.assertEqual(state["triage_status"], "insufficient")
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")


class R4ClaimTests(WorkspaceCase):
    async def test_symptom_not_found_or_fast_reply_downgrades_all_fields_retains_l4(self):
        identity = await self.locate()
        for i, raw in enumerate(
            ({"status": "not_found"}, {"summary": {"latency_ms": 2, "tcp_errors": 8}})
        ):
            await save_query(
                self.b,
                "x" + str(i),
                "get_pod_network_summary",
                {"pod_uid": "dns-uid", "cluster": "c"},
                raw,
                "now",
                1,
            )
        report = await adjudicate(
            self.b,
            {
                "root_cause": "Definitely missing config",
                "investigation_summary": "Definitely DNS misconfigured",
            },
            [
                {
                    "type": "causal",
                    "resource_id": identity["resource"]["id"],
                    "supporting_evidence_ids": ["x1"],
                    "mechanism": "fast_replies",
                    "limitations": ["one sample"],
                }
            ],
        )
        self.assertEqual(report["answer_status"], "inconclusive")
        self.assertNotIn("Definitely", report["investigation_summary"])
        self.assertIn("tcp_errors", report["evidence_summary"])

    async def test_direct_mechanism_explicit_scoped_termination_only(self):
        identity = await self.locate()
        await save_query(
            self.b,
            "oom",
            "get_pod_runtime_status",
            {"pod_uid": "dns-uid", "cluster": "c"},
            {
                "containers": [
                    {
                        "last_state": {
                            "terminated": {
                                "reason": "OOMKilled",
                                "finished_at": "2026-10-10T00:05:00+00:00",
                            }
                        }
                    }
                ],
                "meta": {
                    "time_range": {
                        "start_time": "2026-10-10T00:00:00+00:00",
                        "end_time": "2026-10-10T00:10:00+00:00",
                    }
                },
            },
            "now",
            1,
        )
        claim = {
            "type": "causal",
            "content": "Observed OOM termination candidate",
            "resource_id": identity["resource"]["id"],
            "supporting_evidence_ids": ["oom"],
            "mechanism": "oom_termination",
            "limitations": ["Only this container; trigger unverified"],
        }
        report = await adjudicate(self.b, {}, [claim])
        self.assertEqual(report["answer_status"], "supported")
        self.assertIn("OOMKilled", report["root_cause"])
        claim["opposing_evidence_ids"] = ["oom"]
        report = await adjudicate(self.b, {}, [claim])
        self.assertEqual(report["answer_status"], "inconclusive")

    async def test_async_model_timeout_and_deadline(self):
        from langchain_core.runnables import RunnableLambda
        from workspace.runtime import invoke_model

        async def slow(messages):
            await asyncio.sleep(1)

        from workspace.model_requests import ModelRequestError

        with (
            bind(self.b),
            patch.object(settings, "MODEL_TIMEOUT_SECONDS", 0.01),
            self.assertRaises(ModelRequestError) as caught,
        ):
            await invoke_model(RunnableLambda(slow), [])
        self.assertEqual(caught.exception.detail["code"], "model_timeout")
        self.b.deadline = 0
        with self.assertRaises(TimeoutError):
            self.b.remaining()


if __name__ == "__main__":
    unittest.main()


class R4BoundedTests(WorkspaceCase):
    async def test_no_progress_deduplicates_timestamps_and_summaries(self):
        from agents.supervisor_agent import progress, supervisor_agent

        await self.locate()
        await self.begin_task()
        await finish(self.b, {"diagnosis": "Candidate", "reasoning": "x"})
        raw = {"summary": {"tcp_errors": 2}, "meta": {"query_time": "first"}}
        await save_query(
            self.b, "p1", "get_pod_network_summary", {"pod_uid": "dns-uid"}, raw, "now", 1
        )

        async def snapshot():
            return {
                k: await self.b.repository.all(self.b.incident_id, k)
                for k in ("resource", "task", "evidence")
            }

        signature = progress(await snapshot())
        raw["meta"]["query_time"] = "later"
        await save_query(
            self.b, "p2", "get_pod_network_summary", {"pod_uid": "dns-uid"}, raw, "later", 1
        )
        self.assertEqual(signature, progress(await snapshot()))
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(followup=True)
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(
            len(sent), 1, "a followup does not pretend a cached observation is new progress"
        )
        self.assertLessEqual(state["supervisor_rounds"], settings.SUPERVISOR_MAX_ROUNDS)
        self.assertEqual(state["tasks_to_execute_ids"], [])

    async def test_read_budget_and_model_cancel_updates_attempt(self):
        from langchain_core.runnables import RunnableLambda
        from agents.rca_agent import rcaAgent

        identity = await self.locate()
        task = await self.begin_task()
        self.b.role = "supervisor"
        self.b.reads = settings.SUPERVISOR_MAX_READS
        from workspace.read_tools import get_resource

        with bind(self.b), self.assertRaises(WorkspaceError):
            await get_resource.ainvoke({"resource_id": task.resource_id})
        entered = asyncio.Event()

        async def block(messages):
            entered.set()
            await asyncio.Event().wait()

        class BlockingModel:
            def bind_tools(self, *args, **kwargs):
                return RunnableLambda(block)

        self.b.role = "worker"
        self.b.reads = 0
        from tools.diagnostic_catalog import CapabilityCatalog

        self.b.capability_catalog = CapabilityCatalog([], service_identity={"fixture": "cancel"})
        with (
            bind(self.b),
            patch.object(settings, "get_chat_model", return_value=BlockingModel()),
            patch("agents.rca_agent.worker_tools", return_value=[]),
        ):
            execution = asyncio.create_task(rcaAgent({"rca_task": task, "messages": []}))
            try:
                # Synchronize on the model actually entering; workspace setup
                # and auditing are outside the cancellation behavior.
                await asyncio.wait_for(entered.wait(), 30)
            finally:
                execution.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await execution
        attempt = await self.b.repository.read(self.b.incident_id, self.b.attempt_id, "attempt")
        self.assertEqual(attempt["status"], "cancelled")

    async def test_supervisor_followup_new_question_and_invalid_legacy_priority(self):
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(followup=True)
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(len(rows["task"]), 2)
        self.assertTrue(all(t["question_ids"] for t in rows["task"]))
        known = {q["id"] for q in rows["question"]}
        self.assertTrue(all(set(t["question_ids"]) <= known for t in rows["task"]))
        self.assertEqual(state["supervisor_rounds"], 2)
        self.assertFalse(state["final_report"]["report_errors"])
