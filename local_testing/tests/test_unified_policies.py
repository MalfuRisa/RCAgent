import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.runnables import RunnableLambda
from config import settings
from tools.diagnostic_catalog import Capability, CapabilityCatalog, QueryResult
from workspace.runtime import Binding, bind, initialize_incident, invoke_model
from workspace.repository import Repository, WorkspaceError
from workspace.query_execution import execute_query
from workspace.observations import verify_observation
from workspace.read_tools import read_evidence
from context.investigation import common_preparers, prepare_investigation
from workflow.structured import ask_structured
from models.investigation import ScopeReview
from test_unified_investigation import WINDOW, RESOURCE


class UnifiedPolicies(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_tool_processing_is_linked_to_the_actual_model_response(self):
        from langchain_core.tools import tool
        from workflow.role_tools import take_step

        entered = asyncio.Event()

        @tool
        async def pending_read() -> dict:
            """Wait for a fixture cancellation."""
            entered.set()
            await asyncio.Event().wait()

        class Model:
            def bind_tools(self, tools, **kwargs):
                return RunnableLambda(
                    lambda messages: AIMessage(
                        content="",
                        tool_calls=[{"name": "pending_read", "id": "cancelled-read", "args": {}}],
                    )
                )

        with bind(self.b):
            task = asyncio.create_task(
                take_step(
                    self.b,
                    Model(),
                    [pending_read],
                    [],
                    phase="worker_step",
                    empty_instruction="submit",
                    denied_reason="denied",
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        responses = await self.b.repository.all(self.b.incident_id, "model_response")
        actions = await self.b.repository.all(self.b.incident_id, "model_action")
        self.assertTrue(responses[0]["response_available"])
        self.assertEqual(actions[-1]["response_id"], responses[0]["id"])
        self.assertEqual(actions[-1]["tool_call_id"], "cancelled-read")
        self.assertEqual(actions[-1]["outcome"]["type"], "CancelledError")

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.object(settings, "WORKSPACE_ROOT", Path(self.temp.name)):
            state = await initialize_incident({"target_cluster": "rd2", "query_window": WINDOW})
        self.b = Binding(
            Repository(self.temp.name),
            state["incident_id"],
            state["run_id"],
            "worker",
            time.time() + 120,
        )
        self.b.query_window = state["query_window"]
        self.b.context_preparers = common_preparers()
        await self.b.repository.save(self.b.incident_id, "resource", dict(RESOURCE))
        self.sent = []
        self.partial = False
        self.echo = True
        self.raw = {f"f{i}": i for i in range(100)}

        async def normalize(b, args):
            return args

        async def invoke(b, args):
            self.sent.append(args)
            return QueryResult(
                dict(self.raw),
                ["resource_node"],
                b.query_window if self.echo else None,
                truncated=self.partial,
            )

        self.cap = Capability(
            "read_fixture",
            "Fixture diagnostic read",
            {"type": "object"},
            normalize,
            invoke,
            "v1",
            True,
        )
        self.catalog = CapabilityCatalog(
            [self.cap], service_identity={"service": "fixture", "revision": "one"}
        )

    async def query(self, args=None, **options):
        return await execute_query(self.b, self.catalog, "read_fixture", args or {}, **options)

    async def context(self):
        _, manifest = await prepare_investigation(
            self.b, [HumanMessage(content="inspect")], "worker_step", {}
        )
        self.b.last_context_id = manifest["id"]

    async def test_reuse_is_shared_across_tasks_but_parameters_identity_and_contract_are_bound(
        self,
    ):
        first = await self.query({"page": 1, "filter": "a"})
        self.b.task_id = "other-task"
        second = await self.query({"page": 1, "filter": "a"})
        self.assertEqual(second["_rcagent_query"]["mode"], "reused")
        self.assertEqual(
            first["_rcagent_query"]["evidence_id"], second["_rcagent_query"]["evidence_id"]
        )
        await self.query({"page": 2, "filter": "a"})
        await self.query({"page": 1, "filter": "b"})
        self.cap.contract_version = "v2"
        await self.query({"page": 1, "filter": "a"})
        self.catalog.service_identity["revision"] = "two"
        await self.query({"page": 1, "filter": "a"})
        await self.query({"page": 1, "filter": "a"}, force_refresh=True)
        self.assertEqual(len(self.sent), 6)

    async def test_partial_or_unknown_window_is_not_silently_reused(self):
        self.partial = True
        await self.query()
        await self.query()
        self.assertEqual(len(self.sent), 2)
        reused = await self.query(allow_partial=True)
        self.assertEqual(reused["_rcagent_query"]["mode"], "reused")
        self.echo = False
        await self.query(force_refresh=True)
        await self.query(allow_partial=True)
        # A prior fully window-proven partial result may still be reused; the new unknown-window one may not.
        rows = await self.b.repository.all(self.b.incident_id, "evidence")
        self.assertNotEqual(
            (await self.query(allow_partial=True))["_rcagent_query"]["evidence_id"], rows[-1]["id"]
        )

    async def test_full_read_beyond_preview_submits_only_for_own_visible_context(self):
        result = await self.query()
        eid = result["_rcagent_query"]["evidence_id"]
        proposal = {
            "local_id": "field80",
            "source_ref": {"evidence_id": eid, "json_pointer": "/f80"},
            "expected_value": 80,
        }
        await self.context()
        self.assertIn("rejected", await verify_observation(self.b, proposal))
        with bind(self.b):
            await read_evidence.ainvoke({"evidence_id": eid, "field": "f80"})
        await self.context()
        self.assertIn("accepted", await verify_observation(self.b, proposal))
        from workspace.observation_selection import current_candidates

        before = await current_candidates(self.b)
        await self.context()
        after = await current_candidates(self.b)
        self.assertEqual([c["candidate_id"] for c in before], [c["candidate_id"] for c in after])
        self.b.role = "supervisor"
        await self.context()
        self.assertIn("rejected", await verify_observation(self.b, proposal))

    async def test_unknown_capability_is_rejected_before_send(self):
        with self.assertRaises(WorkspaceError):
            await execute_query(self.b, self.catalog, "delete_everything", {})
        self.assertFalse(self.sent)

    async def test_compatibility_node_uses_and_restores_common_policy(self):
        from workflow.role_compat import invoke_role

        self.b.capability_catalog = self.catalog
        self.b.role = "planner"
        previous = self.b.context_preparers, self.b.lifecycle_mode

        async def action(binding, model, state, catalog):
            self.assertIs(catalog, self.catalog)
            self.assertEqual(binding.lifecycle_mode, "active")
            self.assertIs(binding.context_preparers["worker_step"], prepare_investigation)
            raise WorkspaceError("fixture failure")

        with (
            bind(self.b),
            patch("agents.investigation_planning.planner", side_effect=action),
            patch.object(settings, "get_chat_model", return_value=object()),
        ):
            with self.assertRaises(WorkspaceError):
                await invoke_role({}, "planner")
        self.assertEqual((self.b.context_preparers, self.b.lifecycle_mode), previous)

    async def test_exact_pointer_for_literal_dot_slash_and_empty_keys(self):
        self.raw = {"a.b": 1, "a/b": 2, "": 0}
        result = await self.query()
        eid = result["_rcagent_query"]["evidence_id"]
        await self.context()
        for pointer, value in [("/a.b", 1), ("/a~1b", 2), ("/", 0)]:
            outcome = await verify_observation(
                self.b,
                {
                    "local_id": "literal",
                    "source_ref": {"evidence_id": eid, "json_pointer": pointer},
                    "expected_value": value,
                },
            )
            self.assertIn("accepted", outcome)

    async def test_parse_failure_and_terminal_response_are_audited(self):
        class InvalidModel:
            def with_structured_output(self, schema, **kwargs):
                return RunnableLambda(
                    lambda messages: {"raw": AIMessage(content="bad"), "parsed": None}
                )

        with bind(self.b):
            with self.assertRaises(WorkspaceError):
                await ask_structured(
                    InvalidModel(),
                    ScopeReview,
                    [HumanMessage(content="check")],
                    phase="worker_step",
                )
        responses = await self.b.repository.all(self.b.incident_id, "model_response")
        actions = await self.b.repository.all(self.b.incident_id, "model_action")
        self.assertEqual(len(responses), 2)  # initial response + one bounded parsing correction
        self.assertTrue(all(r["provider_raw_available"] for r in responses))
        self.assertEqual({a["response_id"] for a in actions}, {r["id"] for r in responses})
        self.assertEqual(actions[-1]["outcome"]["status"], "rejected")

    async def test_cpu_compatibility_delegates_to_same_roles_and_factory(self):
        from domains.cpu.roles import worker as compatibility
        from agents.investigation_worker import worker

        self.assertIs(compatibility, worker)
        from domains.cpu.workflow import build_cpu_graph

        with patch("graph.build_parent_graph", return_value="same") as factory:
            self.assertEqual(build_cpu_graph(None, {}, None, None, None), "same")
            factory.assert_called_once()

    async def test_rate_limit_stop_and_absent_response_audit_share_normal_boundary(self):
        class RateLimit(Exception):
            status_code = 429
            response = type("Response", (), {"headers": {}})()

        sends = []

        async def send(messages):
            sends.append(1)
            raise RateLimit("fixture request limit")

        from workspace.model_requests import ModelRequestError

        with bind(self.b):
            for _ in range(2):
                with self.assertRaises(ModelRequestError) as caught:
                    await invoke_model(
                        RunnableLambda(send), [HumanMessage(content="check")], phase="worker_step"
                    )
                self.assertEqual(caught.exception.detail["code"], "model_rate_limited")
        self.assertEqual(len(sends), 1)
        responses = await self.b.repository.all(self.b.incident_id, "model_response")
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(not r["response_available"] for r in responses))
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, "model_action")), 2)

    async def test_query_cancellation_persists_unknown_result(self):
        entered = asyncio.Event()

        async def never(b, args):
            entered.set()
            await asyncio.Event().wait()

        self.cap.invoke = never
        task = asyncio.create_task(self.query())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        records = await self.b.repository.all(self.b.incident_id, "evidence")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["transport_status"], "cancelled")
        self.assertEqual(records[0]["source_status"], "cancelled")

    async def test_model_cancellation_archives_missing_terminal_response(self):
        entered = asyncio.Event()

        async def never(messages):
            entered.set()
            await asyncio.Event().wait()

        with bind(self.b):
            task = asyncio.create_task(
                invoke_model(
                    RunnableLambda(never), [HumanMessage(content="check")], phase="worker_step"
                )
            )
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        responses = await self.b.repository.all(self.b.incident_id, "model_response")
        self.assertEqual(len(responses), 1)
        self.assertFalse(responses[0]["response_available"])
