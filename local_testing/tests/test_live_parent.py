"""Compiled parent acceptance; scripted decisions use actual messages only."""

import asyncio
import json
import unittest
from unittest.mock import patch
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from config import settings
from models.schemas import SymptomList, RCATaskList, SupervisorDecision
from workspace.read_tools import READ_TOOLS
import test_acceptance_rework as support
from test_acceptance_rework import evidence_id
import test_workspace_graph as graph_support


READ_TOOL_NAMES = {tool.name for tool in READ_TOOLS}
SUPERVISOR_TOOL_NAMES = READ_TOOL_NAMES | {"commit_investigation_stage", "prepare_report_packet"}


class VisibleBatchModel(support.VisibleOnlyModel):
    def __init__(self, planner_failure=False, worker_timeout=False):
        super().__init__()
        self.planner_failure, self.worker_timeout = planner_failure, worker_timeout
        self.proposals = 0

    def with_structured_output(self, schema):
        base = super().with_structured_output(schema)

        async def respond(messages):
            if schema == SymptomList:
                return await base.ainvoke(messages)
            view, evidence, resource = self.facts(messages)
            role = "planner" if schema == RCATaskList else "supervisor"
            self.calls.append((role, schema.__name__, messages))
            if schema == RCATaskList:
                if self.planner_failure:

                    class RateLimit(Exception):
                        status_code = 429
                        response = type("Response", (), {"headers": {"Retry-After": "0"}})()

                    raise RateLimit("synthetic account limit")
                return RCATaskList.model_validate(
                    {
                        "rca_tasks": [
                            {
                                "priority": i + 1,
                                "investigation_goal": goal,
                                "target_resource": resource["name"],
                                "resource_type": "pod",
                            }
                            for i, goal in enumerate(
                                ("Check DNS observations", "Check alternative network explanation")
                            )
                        ]
                    }
                )
            self.proposals += 1
            if self.proposals == 1:
                claim_id, resource_id = "fact", evidence_id(evidence)
            else:
                correction = json.loads(
                    next(
                        m.content.split("\n", 1)[1]
                        for m in reversed(messages)
                        if isinstance(m.content, str)
                        and m.content.startswith("Correct this proposal once.")
                    )
                )
                claim_id, resource_id = correction["report_claims"][0]["claim_id"], resource["id"]
            content = "Observed TCP errors: 7; DNS configuration and logs are unavailable"
            return {
                "final_report": {
                    "root_cause": "Unverified proposal",
                    "affected_resources": [resource["name"]],
                    "evidence_summary": content,
                    "investigation_summary": "Unverified",
                    "detection": True,
                },
                "claims": [
                    {
                        "claim_id": claim_id,
                        "type": "observation",
                        "resource_id": resource_id,
                        "content": content,
                        "supporting_evidence_ids": [evidence_id(evidence)],
                        "limitations": ["Only this Pod/window; mechanism unverified"],
                    }
                ],
                "report_items": [
                    {"section": "observations", "content": content, "claim_ids": [claim_id]}
                ],
            }

        return RunnableLambda(respond)

    def bind_tools(self, tools, **kwargs):
        if all(t.name in SUPERVISOR_TOOL_NAMES for t in tools):
            return super().bind_tools(tools, **kwargs)

        async def respond(messages):
            self.calls.append(("worker", "tools", messages))
            view, evidence, resource = self.facts(messages)
            task_payload = next(
                json.loads(m.content)
                for m in messages
                if isinstance(m.content, str) and m.content.startswith('{"task_id"')
            )
            if (
                self.worker_timeout
                and task_payload["investigation_goal"] == "Check DNS observations"
            ):
                await asyncio.Event().wait()
            replies = [m for m in messages if m.type == "tool" and m.name == "diagnose_pod"]
            if not replies:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "diagnose_pod",
                            "args": {
                                "cluster": resource["cluster"],
                                "namespace": resource["namespace"],
                                "pod": resource["name"],
                            },
                            "id": "visible-diagnose",
                        }
                    ],
                )
            original = json.loads(replies[-1].content)["_rcagent_query"]["evidence_id"]
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_final_diagnosis",
                        "args": {
                            "diagnosis": "Observed network errors; cause unresolved",
                            "reasoning": "Actual tool reply",
                            "supporting_evidence_ids": [original],
                            "answer_status": "inconclusive",
                            "gaps": ["Configuration/logs missing"],
                        },
                        "id": "visible-submit",
                    }
                ],
            )

        return RunnableLambda(respond)


class WindowGateway(support.LargeResultGateway):
    async def invoke(self, name, args):
        if name == "diagnose_pod":
            self.calls.append((name, dict(args)))
            await asyncio.sleep(0.03)
            return {
                "status": "ok",
                "summary": {"tcp_errors": 7, "marker": "OBSERVED_FAILURE"},
                "meta": {"time_range": {k: args[k] for k in ("start_time", "end_time")}},
            }
        return await super().invoke(name, args)


class LiveParent(unittest.IsolatedAsyncioTestCase):
    run_graph = graph_support.ParentGraphTests.run_graph

    async def test_supervisor_correction_timeout_retains_first_valid_observation(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(fault="supervisor_correction_timeout")
        self.assertTrue(rows["observation"])
        self.assertEqual(state["execution_error"]["code"], "model_timeout")
        self.assertTrue(
            any(
                a["operation"] == "submit_report" and a["outcome"].get("status") == "rejected"
                for a in rows["model_action"]
            )
        )
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")

    async def test_budget_off_parallel_query_reuse_and_one_report_correction(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        with patch.object(settings, "CONTEXT_BUDGET_ENABLED", False):
            state, rows, events, sent, model = await run_scenario(
                two_tasks=True, invalid_report=True
            )
            self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(len(sent), 1)
        self.assertTrue(any(e["action"] == "query_reused" for e in events))
        self.assertEqual(
            sum(
                a["operation"] == "submit_report" and a["outcome"].get("status") == "rejected"
                for a in rows["model_action"]
            ),
            1,
        )
        self.assertFalse(state["final_report"]["report_errors"])
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")

    async def test_planner_final_429_preserves_evidence_and_stops_workers(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(
            fault="planner_429", initial_evidence=True
        )
        self.assertEqual(sum(role == "planner" for role, phase, messages in model.calls), 1)
        self.assertFalse(any(role == "worker" for role, phase, messages in model.calls))
        self.assertEqual(state["execution_error"]["code"], "model_rate_limited")
        self.assertTrue(rows["evidence"])
        self.assertTrue(any(e["action"] == "run_stopped" for e in events))
        self.assertEqual(rows["run"][0]["stop_reason"], "model_rate_limited")
        self.assertFalse([a for a in rows["model_attempt"] if a.get("retry_wait_seconds")])

    async def test_one_worker_timeout_other_worker_finishes(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(
            fault="one_worker_timeout", two_tasks=True
        )
        self.assertEqual(sorted(a["status"] for a in rows["attempt"]), ["failed", "finished"])
        self.assertTrue(rows["report_packet"])
        self.assertFalse(state["final_report"]["fallback"])
        self.assertEqual(state["execution_error"]["code"], "model_timeout")
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")

    async def test_missing_and_wrong_namespace_stop_before_model(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario
        from test_unified_investigation import RESOURCE
        from workspace.repository import WorkspaceError

        resource = dict(RESOURCE, kind="pod", namespace="forbidden", uid="fixture-pod")
        with patch.object(settings, "ALLOWED_NAMESPACES", ["app"]):
            with self.assertRaises(WorkspaceError):
                await run_scenario(resource=resource)
        state, rows, events, sent, model = await run_scenario(
            input_state={"triage_status": "awaiting_confirmation"}
        )
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertFalse(model.calls)
        self.assertFalse(rows["task"])

    async def test_normal_observations_without_symptoms_remain_discovered(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(
            raw={"summary": {"tcp_errors": 0}, "missing": None}
        )
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(state["triage_status"], "discovered")
        self.assertTrue(any(o.get("field_value") == 0 for o in rows["observation"]))
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
        self.assertFalse(state["final_report"]["fallback"])
