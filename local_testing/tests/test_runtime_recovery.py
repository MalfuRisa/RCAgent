"""Normal runtime budgets and local failures must permit an honest final result."""

import asyncio
import unittest
from unittest.mock import patch
from langchain_core.runnables import RunnableLambda
from langchain_core.messages import AIMessage, SystemMessage
from config import settings
from context.builder import prepare
from context.request_budget import tool_schemas
from models.schemas import RCATaskList, SymptomList
from workspace.model_requests import ModelRequestError, send
from workspace.runtime import bind
from workspace.stage_tools import commit_investigation_stage
from test_workspace import WorkspaceCase
from test_live_parent import VisibleBatchModel, WindowGateway
import test_workspace_graph as graph_support


class RuntimeBudgetTests(WorkspaceCase):
    async def test_normal_budget_sends_large_necessary_content_and_full_schema(self):
        with (
            patch.object(settings, "CONTEXT_BUDGET_POLICY", "soft"),
            patch.object(settings, "CONTEXT_MAX_CHARS", 96000),
            patch.object(settings, "CONTEXT_HARD_MAX_CHARS", 512000),
            patch.object(settings, "REQUEST_BUDGET_BASIS", "v2"),
        ):
            text = "Necessary investigation material. " * 4000
            visible, manifest = await prepare(
                self.b,
                [SystemMessage(content=text)],
                request={"tools": tool_schemas([commit_investigation_stage])},
            )
        self.assertGreater(manifest["request_chars_v2"], 96000)
        self.assertLess(manifest["request_chars_v2"], 512000)
        self.assertIn(text, [message.content for message in visible])

    async def test_planner_has_independent_timeout_but_same_sender_and_deadline(self):
        async def slow(_):
            await asyncio.sleep(0.04)
            return AIMessage(content="planned")

        with (
            patch.object(settings, "MODEL_TIMEOUT_SECONDS", 0.01),
            # Exercise timeout selection without making planner success depend
            # on sub-second scheduling while the audited sender runs under load.
            patch.object(settings, "PLANNER_MODEL_TIMEOUT_SECONDS", 10),
        ):
            self.b.role = "planner"
            result = await send(RunnableLambda(slow), [], self.b, {"phase": "plan"})
            self.assertEqual(result.content, "planned")
            self.b.role = "worker"
            with self.assertRaises(ModelRequestError) as caught:
                await send(RunnableLambda(slow), [], self.b, {"phase": "worker_step"})
        self.assertEqual(caught.exception.detail["code"], "model_timeout")
        calls = await self.b.repository.all(self.b.incident_id, "model_call")
        self.assertEqual({row["request_timeout_seconds"] for row in calls}, {0.01, 10})

    async def test_sdk_timeout_does_not_undercut_planner(self):
        with (
            patch.object(settings, "MODEL_TIMEOUT_SECONDS", 60),
            patch.object(settings, "PLANNER_MODEL_TIMEOUT_SECONDS", 300),
            patch.object(settings, "RCAChatOpenAI") as constructor,
        ):
            settings._build_chat_model("test-model")
        self.assertEqual(constructor.call_args.kwargs["timeout"], 300)


class RecoveredParentTests(unittest.IsolatedAsyncioTestCase):
    run_graph = graph_support.ParentGraphTests.run_graph

    async def test_planner_timeout_runs_workers_supervisor_and_honest_final_report(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(
            fault="planner_timeout", initial_evidence=True
        )
        self.assertEqual(state["execution_error"]["code"], "model_timeout")
        self.assertEqual(
            rows["task"], [], "no invented program plan replaces the failed model plan"
        )
        self.assertTrue(rows["evidence"])
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
        self.assertTrue(state["final_report"]["fallback"])

    async def test_bad_optional_triage_stage_does_not_discard_symptoms(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario()
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(state["triage_status"], "discovered")
        self.assertTrue(
            rows["question"],
            "Triage now hands off registered questions, not an optional stage draft",
        )
        self.assertTrue(rows["task"][0]["question_ids"])
        self.assertTrue(rows["report_packet"])
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
