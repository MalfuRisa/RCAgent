"""One graph/model/role policy; varied telemetry is fixture data, not routing."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from config import settings
from tools.diagnostic_catalog import Capability, CapabilityCatalog, QueryResult
from workspace.runtime import current
from graph import build_parent_graph

WINDOW = {
    "start_time": "2026-10-28T06:00:00+00:00",
    "end_time": "2026-10-28T06:15:00+00:00",
}
RESOURCE = {
    "id": "resource_node",
    "kind": "node",
    "name": "test-node",
    "cluster": "rd2",
    "namespace": None,
    "uid": None,
    "platform_id": 7,
    "identity_status": "resolved",
    "source_refs": [],
    "candidate_resource_ids": [],
    "lookup_scope": {},
    "verified_at": "2026-10-28T06:15:00+00:00",
}


def payload(messages):
    return next(
        json.loads(m.content.split("\n", 1)[1])
        for m in messages
        if isinstance(m.content, str) and m.content.startswith("Incident workbench")
    )


class InvestigationModel:
    """Tests choose actions from the supplied plan; production has no such policy."""

    def __init__(self, actions, *, followup=False, invalid_report=False):
        self.actions = actions
        self.steps = {}
        self.traces = []
        self.followup = followup
        self.invalid_report = invalid_report

    def with_structured_output(self, schema, **kwargs):
        async def answer(messages):
            b = current.get()
            p = payload(messages)
            self.traces.append((b.role, schema.__name__, p["context_view_version"]))
            if schema.__name__ == "ScopeReview":
                return {
                    "questions": ["What does the available evidence establish?"],
                    "limitations": ["fixture"],
                }
            if schema.__name__ == "InvestigationPlan":
                return {
                    "tasks": [
                        {
                            "investigation_goal": "Check evidence",
                            "question_ids": [
                                q["id"] for q in p["investigation"]["questions"]
                            ],
                            "resource_id": p["resources"][0]["id"],
                            "suggested_capabilities": [],
                        }
                    ]
                }
            if schema.__name__ == "ReportSubmission":
                return {
                    "observation_ids": [
                        o["id"] for o in p["investigation"]["observations"]
                    ],
                    "unresolved_reasons": {
                        q["id"]: "Evidence is insufficient for causality"
                        for q in p["investigation"]["questions"]
                    },
                    "limitations": ["fixture; root cause unresolved"],
                    "next_steps": [],
                }
            if schema.__name__ == "PacketSelection":
                packet = p["report_packets"][-1]
                return {
                    "packet_id": packet["id"],
                    "selected_statement_ids": [
                        s["statement_id"] for s in packet["statements"]
                    ],
                    "conclusion_status": "inconclusive",
                    "unresolved_question_ids": packet["unresolved_question_ids"],
                }
            raise AssertionError(schema.__name__)

        return RunnableLambda(answer)

    def bind_tools(self, tools, **kwargs):
        async def answer(messages):
            b = current.get()
            p = payload(messages)
            step = self.steps.get(b.attempt_id, 0)
            self.steps[b.attempt_id] = step + 1
            self.traces.append((b.role, "tools", p["context_view_version"]))
            if b.role == "supervisor":
                if self.followup:
                    self.followup = False
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "request_followup",
                                "id": "followup-call",
                                "args": {
                                    "tasks": [
                                        {
                                            "investigation_goal": "Check counterevidence with the same Worker",
                                            "question_ids": [
                                                q["id"]
                                                for q in p["investigation"]["questions"]
                                            ],
                                            "resource_id": p["resources"][0]["id"],
                                            "suggested_capabilities": [
                                                "network_summary"
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    )
                if self.invalid_report:
                    self.invalid_report = False
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "submit_report",
                                "id": "invalid-report-call",
                                "args": {
                                    "observation_ids": ["invented"],
                                    "unresolved_reasons": {
                                        q["id"]: "Unresolved"
                                        for q in p["investigation"]["questions"]
                                    },
                                    "limitations": ["fixture"],
                                    "next_steps": [],
                                },
                            }
                        ],
                    )
                if (
                    not p["investigation"]["observations"]
                    and p["current_candidates"]
                    and any(t.name == "record_observations" for t in tools)
                ):
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "record_observations",
                                "id": "register-review",
                                "args": {
                                    "ref_ids": [
                                        c["candidate_id"]
                                        for c in p["current_candidates"][:2]
                                    ],
                                    "limitations": ["fixture; unresolved"],
                                    "question_ids": [
                                        q["id"] for q in p["investigation"]["questions"]
                                    ],
                                },
                            }
                        ],
                    )
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "submit_report",
                            "id": "report-call",
                            "args": {
                                "observation_ids": [
                                    o["id"] for o in p["investigation"]["observations"]
                                ],
                                "unresolved_reasons": {
                                    q["id"]: "Evidence insufficient for causality"
                                    for q in p["investigation"]["questions"]
                                },
                                "limitations": ["fixture; unresolved"],
                                "next_steps": [],
                            },
                        }
                    ],
                )
            if step < len(self.actions):
                name, args = "query_diagnostics", self.actions[step]
            elif not p["investigation"]["observations"] and p["current_candidates"]:
                name, args = (
                    "record_observations",
                    {
                        "ref_ids": [
                            r["candidate_id"] for r in p["current_candidates"][:2]
                        ],
                        "question_ids": [
                            q["id"] for q in p["investigation"]["questions"]
                        ],
                        "limitations": ["fixture coverage only"],
                    },
                )
            else:
                name, args = (
                    "complete_task",
                    {
                        "supporting_observation_ids": [
                            r["id"] for r in p["investigation"]["observations"]
                        ],
                        "opposing_observation_ids": [],
                        "interpretation": "Tentative; no root cause established",
                        "limitations": ["fixture coverage only"],
                    },
                )
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": "call_" + str(step)}],
            )

        return RunnableLambda(answer)


class UnifiedInvestigation(unittest.IsolatedAsyncioTestCase):
    async def run_case(
        self,
        text,
        actions,
        *,
        raw=None,
        resource=None,
        window=None,
        followup=False,
        invalid_report=False,
    ):
        resource = resource or RESOURCE
        window = window or WINDOW
        sent = []

        async def normalize(b, args):
            return args

        async def invoke(b, args):
            sent.append(args)
            return QueryResult(
                raw
                if raw is not None
                else {
                    "value": args.get("metric", "network"),
                    "zero": 0,
                    "missing": None,
                },
                [resource["id"]],
                window,
            )

        async def bootstrap(b, state):
            await b.repository.save(b.incident_id, "resource", dict(resource))

        catalog = CapabilityCatalog(
            [
                Capability(
                    name,
                    name,
                    {
                        "type": "object",
                        "properties": {"metric": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    normalize,
                    invoke,
                    "fixture-v1",
                    True,
                )
                for name in (
                    "cpu_events",
                    "network_summary",
                    "metric_series",
                    "insights_list_cpu_events",
                    "get_pod_network_summary",
                )
            ],
            service_identity={"fixture": "same"},
            bootstrap=bootstrap,
        )
        model = InvestigationModel(
            actions, followup=followup, invalid_report=invalid_report
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(settings, "WORKSPACE_ROOT", Path(directory)),
        ):
            result = await build_parent_graph(catalog=catalog, model=model).ainvoke(
                {
                    "incident_text": text,
                    "target_cluster": "rd2",
                    "target_resource": resource["name"],
                    "query_window": window,
                },
                config={"recursion_limit": 30},
            )
            from workspace.repository import Repository

            repo = Repository(directory)
            rows = {
                k: await repo.all(result["incident_id"], k)
                for k in (
                    "evidence",
                    "model_response",
                    "model_action",
                    "report_packet",
                    "report",
                    "recovery_issue",
                    "task",
                    "attempt",
                )
            }
            self.assertIsNone(result.get("execution_error"), result)
            self.assertTrue(rows["report_packet"], result)
            if rows["evidence"]:
                self.assertFalse(
                    result["final_report"].get("fallback"), result["final_report"]
                )
            self.assertFalse(
                result["final_report"].get("report_errors"), result["final_report"]
            )
            self.assertTrue(rows["model_response"])
            self.assertTrue(rows["model_action"])
            self.assertTrue(all(t[2] == "sre-investigation-v1" for t in model.traces))
            self.assertEqual(
                len(rows["evidence"]),
                len(
                    {
                        json.dumps(a, sort_keys=True)
                        for a in actions
                        if a["name"] in catalog.capabilities
                    }
                ),
            )
            return result, rows, sent

    async def test_four_inputs_same_graph_and_roles(self):
        for text, cap, args in [
            ("CPU wait", "cpu_events", {}),
            ("Network retransmissions", "network_summary", {}),
            ("Memory pressure", "metric_series", {"metric": "memory_used"}),
            ("Storage wait", "metric_series", {"metric": "disk_latency"}),
        ]:
            with self.subTest(text=text):
                await self.run_case(text, [{"name": cap, "arguments": args}])

    async def test_cpu_wording_accepts_network_and_same_run_cross_domain(self):
        await self.run_case(
            "CPU slow",
            [
                {"name": "network_summary", "arguments": {}},
                {"name": "cpu_events", "arguments": {}},
            ],
        )

    async def test_exact_query_reused_without_mandatory_new_query(self):
        await self.run_case(
            "Network issue", [{"name": "network_summary", "arguments": {}}] * 2
        )

    async def test_unavailable_capability_then_model_selects_alternative(self):
        await self.run_case(
            "CPU slow",
            [
                {"name": "unavailable_probe", "arguments": {}},
                {"name": "network_summary", "arguments": {}},
            ],
        )

    async def test_no_available_evidence_can_close_with_explicit_unresolved_packet(
        self,
    ):
        await self.run_case(
            "CPU issue", [{"name": "unavailable_probe", "arguments": {}}]
        )

    async def test_submission_slots_reserved_under_same_worker_limit(self):
        with patch.object(settings, "WORKER_MAX_MODEL_STEPS", 3):
            await self.run_case(
                "Storage issue",
                [{"name": "metric_series", "arguments": {"metric": "disk_latency"}}]
                * 2,
            )

    async def test_supervisor_followup_uses_same_worker_and_reuses_query(self):
        result, rows, sent = await self.run_case(
            "CPU issue", [{"name": "network_summary", "arguments": {}}], followup=True
        )
        self.assertEqual(len(rows["task"]), 2)
        self.assertEqual(len(rows["attempt"]), 2)
        self.assertEqual(len(sent), 1)
        self.assertEqual(result["supervisor_rounds"], 2)

    async def test_supervisor_invalid_selection_is_rejected_then_corrected_with_error_visible(
        self,
    ):
        result, rows, sent = await self.run_case(
            "Network issue",
            [{"name": "network_summary", "arguments": {}}],
            invalid_report=True,
        )
        rejected = [
            a
            for a in rows["model_action"]
            if a["operation"] == "submit_report"
            and a["outcome"].get("status") == "rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertIn("reference", rejected[0]["outcome"]["reason"])

    async def test_archived_cpu_and_network_values_use_same_roles(self):
        fixtures = Path(__file__).parents[1] / "fixtures"
        cpu = json.loads((fixtures / "unified_cpu_archive.json").read_text())
        await self.run_case(
            "Scheduling wait",
            [{"name": "insights_list_cpu_events", "arguments": {}}],
            raw=cpu["raw"],
            resource=cpu["resource"],
            window=cpu["window"],
        )
        network = json.loads((fixtures / "workspace_tool_samples.json").read_text())[
            "samples"
        ]["get_pod_network_summary"]
        history = json.loads((fixtures / "workspace_iam_history.json").read_text())
        resource = dict(
            RESOURCE,
            id="resource_archived_pod",
            kind="pod",
            name=history["input"]["target_resource"],
            namespace=history["input"]["target_namespace"],
            uid=network["arguments"]["pod_uid"],
        )
        await self.run_case(
            "Network latency",
            [{"name": "get_pod_network_summary", "arguments": {}}],
            raw=network["observations"],
            resource=resource,
            window=network["observations"]["meta"]["time_range"],
        )
