import copy
import jsonschema
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock
from langchain_core.messages import AIMessage
from tools.v5_cpu_client import cpu_arguments, decode_result, V5CPUClient, ROLE_TOOLS
from workspace.v5_cpu_evidence import validate_cpu_page
from workspace.repository import utcnow

WINDOW = {"start_time": "2026-10-28T06:00:00+00:00", "end_time": "2026-10-28T06:15:00+00:00"}
TARGET = {
    "id": "resource_node",
    "name": "test-node",
    "platform_id": 7,
    "platform_cluster_id": 1,
    "kind": "node",
    "cluster": "rd2",
    "namespace": None,
    "uid": None,
    "identity_status": "resolved",
    "verified_at": "2026-10-28T06:15:00+00:00",
    "source_refs": [],
    "candidate_resource_ids": [],
    "lookup_scope": {},
}
RAW = {
    "data": {
        "list": [
            {
                "timestamp": 1793167200123456789,
                "latency_ns": 10000001,
                "latency_threshold_ns": 10000000,
                "pid": 123,
                "cpu": 0,
                "comm": "test",
                "pod_id": 0,
                "agent_id": 7,
                "pod_node_id": 7,
                "pod_cluster_id": 1,
                "pod_cluster": "rd2",
            }
        ],
        "total": 1,
    },
    "meta": {
        "backend": "queries",
        "request_id": "test",
        "effective_start_time": WINDOW["start_time"],
        "effective_end_time": WINDOW["end_time"],
        "page": 1,
        "page_size": 20,
    },
}


class CPUContracts(unittest.TestCase):
    def test_precise_integers_empty_and_error_are_distinct(self):
        result = SimpleNamespace(isError=False, structuredContent=RAW, content=[])
        self.assertEqual(decode_result(result)["data"]["list"][0]["timestamp"], 1793167200123456789)
        result.structuredContent = {"data": [], "meta": {"backend": "queries"}}
        self.assertEqual(decode_result(result)["data"], [])
        result.isError = True
        with self.assertRaises(ValueError):
            decode_result(result)
        result.isError, result.structuredContent = False, {"data": {"code": 500}, "meta": {}}
        with self.assertRaises(ValueError):
            decode_result(result)

    def test_argument_boundaries_and_no_free_sql(self):
        for bad in (True, 0, "1 OR 1=1", "uid-foo", -1, 1.0):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cpu_arguments(WINDOW, agent_id=bad)
        for bad in (0, True, 21):
            with self.assertRaises(ValueError):
                cpu_arguments(WINDOW, page=bad)
        with self.assertRaises(ValueError):
            cpu_arguments({"start_time": "2026-10-28T06:00:00", "end_time": WINDOW["end_time"]})
        self.assertEqual(
            cpu_arguments(WINDOW, agent_id=9007199254740993)["agent_id"], "9007199254740993"
        )

    def test_window_scope_units_page_and_no_fake_pod(self):
        args = cpu_arguments(WINDOW, agent_id=7)
        self.assertFalse(validate_cpu_page(RAW, args, TARGET)[1])
        for field, value in [
            ("pod_cluster", "rd1"),
            ("pod_node_id", 8),
            ("timestamp", 1.7931672e18),
        ]:
            raw = copy.deepcopy(RAW)
            raw["data"]["list"][0][field] = value
            with self.subTest(field=field), self.assertRaises(Exception):
                validate_cpu_page(raw, args, TARGET)
        partial = copy.deepcopy(RAW)
        partial["data"]["total"] = 30
        self.assertTrue(validate_cpu_page(partial, args, TARGET)[1])
        self.assertEqual(RAW["data"]["list"][0]["pod_id"], 0)

    def test_projection_maps_only_exact_shown_fields(self):
        from context.v5_cpu_view import cpu_view
        from context.source_paths import source_values

        values, paths, _, _ = cpu_view(RAW)
        leaves = [pair for k, v in values.items() for pair in source_values(paths.get(k), v)[0]]
        self.assertIn(("data.list.0.timestamp", 1793167200123456789), leaves)
        self.assertFalse(any(p == "data.list.3.timestamp" for p, v in leaves))

    def test_report_review_preserves_opposition_and_exact_limitations(self):
        from context.v5_cpu_report import review_payload

        rows = [
            {
                "id": f"observation_{i}",
                "run_id": "run_test",
                "question_ids": ["q1"],
                "field": "json_pointer:/data/total",
                "field_value": 0,
                "source_ref": {"evidence_id": f"evidence_{i}"},
                "limitations": ["Same scope limit", "Unique limit " + str(i)],
            }
            for i in range(2)
        ]
        snap = {
            "scope": {"cluster": "rd2"},
            "resource": [],
            "question": [],
            "task": [],
            "evidence": [],
            "observation": rows,
            "recovery_issue": [],
            "report_packet": [],
            "judgment": [
                {
                    "id": "j",
                    "run_id": "run_test",
                    "opposing_observation_ids": ["observation_1"],
                    "limitations": ["Known counterevidence remains unresolved"],
                }
            ],
        }
        payload = review_payload(SimpleNamespace(run_id="run_test", query_window=WINDOW), snap)
        self.assertEqual(len(payload["observations"]), 2)  # same numeric value, different source
        for original, shown in zip(rows, payload["observations"]):
            self.assertEqual(
                [payload["shared_limitations"][ref] for ref in shown["limitation_refs"]],
                original["limitations"],
            )
            self.assertEqual(shown["source_ref"], original["source_ref"])
        self.assertEqual(payload["judgments"][0]["opposing_observation_ids"], ["observation_1"])


class CPUTransport(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_calls_rejected_before_remote(self):
        client = V5CPUClient("http://127.0.0.1/mcp", "test")
        client.session = SimpleNamespace(call_tool=AsyncMock())
        for name in (
            "resolve_pod",
            "insights_query_list",
            "insights_server_agent_commands",
            "insights_list_cpu_events",
        ):
            with self.assertRaises(ValueError):
                await client.call("supervisor", name, {})
        client.session.call_tool.assert_not_called()
        self.assertTrue(
            {"insights_list_cpu_events", "insights_query_list", "insights_query_metric"}
            <= ROLE_TOOLS["worker"]
        )
        self.assertNotIn("insights_server_agent_commands", ROLE_TOOLS["worker"])

    async def test_native_schema_unknown_and_changed_pages_rejected(self):
        client = V5CPUClient("http://127.0.0.1/mcp", "test")
        client.schemas = {
            "insights_list_cpu_events": {
                "inputSchema": {
                    "type": "object",
                    "properties": {"page_size": {"type": "integer", "maximum": 20}},
                    "additionalProperties": False,
                }
            }
        }
        client.session = SimpleNamespace(call_tool=AsyncMock())
        for args in ({"page_size": 200}, {"unknown": "not authorized"}):
            with self.assertRaises(jsonschema.ValidationError):
                await client.call("worker", "insights_list_cpu_events", args)
        client.session.call_tool.assert_not_called()


from test_unified_investigation import InvestigationModel


class ScriptModel(InvestigationModel):
    """CPU is fixture telemetry for the same role contracts as every other input."""

    def __init__(self):
        super().__init__(
            [{"name": "insights_list_cpu_events", "arguments": {"event_type": "runqlat"}}]
        )


class CPUParent(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_preserves_evidence_and_finishes_through_model_packet(self):
        from config import settings
        from graph import build_parent_graph
        from workspace.repository import Repository
        from workspace.model_requests import ModelRequestError

        class TimeoutAfterQuery(ScriptModel):
            def bind_tools(self, tools, **kwargs):
                from langchain_core.runnables import RunnableLambda
                from workspace.runtime import current

                ordinary = super().bind_tools(tools, **kwargs)

                async def respond(messages):
                    if (
                        current.get().role == "worker"
                        and self.steps.get(current.get().attempt_id, 0) > 0
                    ):
                        raise TimeoutError("simulated after CPU query")
                    return await ordinary.ainvoke(messages)

                return RunnableLambda(respond)

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(settings, "WORKSPACE_ROOT", Path(tmp)),
            patch.object(settings, "CONTEXT_LIFECYCLE_MODE", "active"),
        ):
            client = SimpleNamespace(
                schemas={
                    "insights_list_cpu_events": {"inputSchema": {}},
                    "insights_k8s_resource": {"inputSchema": {}},
                },
                call=AsyncMock(return_value=copy.deepcopy(RAW)),
            )
            graph = build_parent_graph(
                profile="v5_cpu",
                client=client,
                target=dict(copy.deepcopy(TARGET), verified_at=utcnow()),
                identity_original={},
                discovery_source={},
                agent_id=7,
                model=TimeoutAfterQuery(),
            )
            state = await graph.ainvoke(
                {
                    "target_cluster": "rd2",
                    "incident_text": "CPU runqlat on rd2",
                    "query_window": WINDOW,
                    "app_name": "cpu",
                    "app_summary": "cpu",
                    "prompts_config": {},
                }
            )
            repo = Repository(tmp)
            self.assertFalse(state["final_report"]["fallback"])
            self.assertEqual(state["execution_status"], "failed")
            self.assertEqual(state["stop_reason"], "model_timeout")
            self.assertEqual(state["execution_error"]["code"], "model_timeout")
            self.assertIn("model_timeout", json.dumps(state["final_report"]))
            self.assertEqual(len(await repo.all(state["incident_id"], "evidence")), 1)
            self.assertEqual(len(await repo.all(state["incident_id"], "report_packet")), 1)

    async def test_fresh_roles_real_storage_visibility_packet_and_gate(self):
        from config import settings
        from graph import build_parent_graph
        from workspace.repository import Repository

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(settings, "WORKSPACE_ROOT", Path(tmp)),
            patch.object(settings, "CONTEXT_LIFECYCLE_MODE", "active"),
        ):
            client = SimpleNamespace(
                schemas={
                    "insights_list_cpu_events": {"inputSchema": {}},
                    "insights_k8s_resource": {"inputSchema": {}},
                },
                call=AsyncMock(return_value=copy.deepcopy(RAW)),
            )
            graph = build_parent_graph(
                profile="v5_cpu",
                client=client,
                target=dict(copy.deepcopy(TARGET), verified_at=utcnow()),
                identity_original={},
                discovery_source={},
                agent_id=7,
                model=ScriptModel(),
            )
            state = await graph.ainvoke(
                {
                    "target_cluster": "rd2",
                    "incident_text": "CPU runqlat on rd2",
                    "query_window": WINDOW,
                    "app_name": "cpu",
                    "app_summary": "cpu",
                    "prompts_config": {},
                }
            )
            repo = Repository(tmp)
            self.assertEqual(len(await repo.all(state["incident_id"], "report_packet")), 1)
            self.assertEqual(len(await repo.all(state["incident_id"], "report_proposal")), 1)
            self.assertFalse(state["final_report"]["fallback"])
            self.assertEqual(state["final_report"]["report_errors"], [])
            self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
            self.assertEqual(state["execution_status"], "completed")
            client.call.assert_awaited_once()
            obs = await repo.all(state["incident_id"], "observation")
            self.assertTrue(obs)
            # Hidden/raw facts were never auto-registered; each retained candidate used the shared gate.
            self.assertTrue(
                all(o["verification"]["value_matches"] and o["producer"] == "model" for o in obs)
            )

    async def test_checked_report_is_distinct_from_program_fallback(self):
        from workspace.report_render import render_checked

        result = render_checked(
            {"fallback": True, "investigation_summary": "No model report"}, [], [], []
        )
        self.assertTrue(result["fallback"])
