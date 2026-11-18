"""Offline integration: unchanged archived responses, visible-message-only model.

These are scripted workflow checks, never real-model diagnostic acceptance.
"""

import copy
import json
import unittest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from models.schemas import SymptomList, RCATaskList, SupervisorDecision
from test_workspace_graph import FakeGateway
import test_workspace_graph as graph_support
from test_round2_contracts import archive_root, dump_result, messages_json
from test_acceptance_repair import workbench
from workspace.read_tools import READ_TOOLS


class ArchivedGateway(FakeGateway):
    def __init__(self, records):
        self.records, self.calls = records, []

    async def invoke(self, name, args):
        self.calls.append((name, dict(args)))
        rows = [r for r in self.records["evidence"] if r["tool_name"] == name]
        if args.get("pod_uid"):
            rows = [r for r in rows if r.get("arguments", {}).get("pod_uid") == args["pod_uid"]]
        if args.get("pod"):
            rows = [r for r in rows if r.get("arguments", {}).get("pod") == args["pod"]]
        if not rows:
            raise AssertionError("No archived response for requested tool/identity: " + name)
        return copy.deepcopy(rows[0]["observations"])


class ArchivedVisibleModel:
    """Every answer and reference is extracted from the actual visible messages."""

    def __init__(self):
        self.calls = []

    def facts(self, messages):
        view = workbench(messages)
        row = next(
            r
            for r in view["evidence"] + view.get("evidence_directory", [])
            if r["tool_name"] == "get_pod_network_summary"
        )
        refs = {r["id"] for r in row.get("resource_revision_refs", [])}
        refs.update(row.get("resource_ids") or [])
        resource = next(r for r in view["resources"] if r["id"] in refs)
        value = (row.get("observations") or {}).get("rst_count")
        for message in messages:
            if message.type != "human":
                continue
            start = str(message.content).find('{"archived_evidence"')
            if start < 0:
                continue
            original, _ = json.JSONDecoder().raw_decode(message.content[start:])
            shown = next(
                (
                    e
                    for e in original["archived_evidence"]
                    if e["evidence_id"] == row["evidence_id"]
                ),
                {},
            )
            value = shown.get("observations", {}).get("rst_count", value)
        for message in messages:
            if message.type != "tool" or message.name != "read_evidence":
                continue
            body = json.loads(message.content)
            if body.get("evidence_id") == row["evidence_id"] and body.get("field") == "summary":
                value = body["items"][0]["rst_count"]
        for detail in view.get("verified_read_details", []):
            if (
                detail.get("source_refs", {}).get("source_id") == row["evidence_id"]
                and detail.get("field") == "summary"
            ):
                value = detail["content"]["items"][0]["rst_count"]
        assert isinstance(value, (int, float)), "rst_count must come from visible data"
        return view, row, resource, value

    def with_structured_output(self, schema):
        async def answer(messages):
            self.calls.append((schema.__name__, messages))
            view, evidence, resource, value = self.facts(messages)
            if schema == SymptomList:
                return SymptomList.model_validate(
                    {
                        "symptoms": [
                            {
                                "potential_symptom": "Recorded network counter; check its scope and limits",
                                "resource_type": resource["kind"],
                                "affected_resource": resource["name"],
                                "evidence": "Observed rst_count=" + str(value),
                            }
                        ]
                    }
                )
            if schema == RCATaskList:
                return RCATaskList.model_validate(
                    {
                        "rca_tasks": [
                            {
                                "priority": 1,
                                "investigation_goal": "Check recorded resets and limits; do not infer a cause",
                                "target_resource": resource["name"],
                                "resource_type": "pod",
                            }
                        ]
                    }
                )
            content = (
                "Observed rst_count="
                + str(value)
                + "; this observation does not establish a root cause"
            )
            return SupervisorDecision.model_validate(
                {
                    "final_report": {
                        "root_cause": "Unresolved",
                        "affected_resources": [resource["name"]],
                        "evidence_summary": content,
                        "investigation_summary": "Archived response replay; mechanism remains unverified",
                        "detection": False,
                    },
                    "claims": [
                        {
                            "claim_id": "archive-observation",
                            "type": "observation",
                            "resource_id": resource["id"],
                            "content": content,
                            "supporting_evidence_ids": [evidence["evidence_id"]],
                            "opposing_evidence_ids": [],
                            "limitations": [
                                "Only the archived target/window; no mechanism established"
                            ],
                        }
                    ],
                    "report_items": [
                        {
                            "section": "observations",
                            "content": content,
                            "claim_ids": ["archive-observation"],
                        }
                    ],
                }
            )

        return RunnableLambda(answer)

    def bind_tools(self, tools, **kwargs):
        supervisor = all(
            t.name in ({tool.name for tool in READ_TOOLS} | {"commit_investigation_stage"})
            for t in tools
        )

        async def answer(messages):
            self.calls.append(("supervisor-read" if supervisor else "worker", messages))
            view, evidence, resource, value = self.facts(messages)
            reads = [m for m in messages if m.type == "tool" and m.name == "read_evidence"]
            if not reads:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_evidence",
                            "id": "archive-local-read",
                            "args": {"evidence_id": evidence["evidence_id"], "field": "summary"},
                        }
                    ],
                )
            page = json.loads(reads[-1].content)
            assert page["items"][0]["rst_count"] == value
            if supervisor:
                return AIMessage(content="Observed the original network summary; ready to submit")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_final_diagnosis",
                        "id": "archive-submit",
                        "args": {
                            "diagnosis": "Recorded resets; cause unresolved",
                            "reasoning": "Visible summary rst_count=" + str(value),
                            "supporting_evidence_ids": [page["evidence_id"]],
                            "answer_status": "inconclusive",
                            "gaps": ["Archived observations do not demonstrate a mechanism"],
                        },
                    }
                ],
            )

        return RunnableLambda(answer)


class ArchivedParentTests(unittest.IsolatedAsyncioTestCase):
    run_graph = graph_support.ParentGraphTests.run_graph

    async def check_case(self, case):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario
        from workspace.observations import resolve_pointer

        folder = archive_root() / "attempt6" / case
        original = json.loads((folder / "records.json").read_text())
        network = next(
            e for e in original["evidence"] if e["tool_name"] == "get_pod_network_summary"
        )
        resource = next(
            r for r in original["resource"] if r.get("uid") == network["arguments"]["pod_uid"]
        )
        state, rows, events, sent, model = await run_scenario(
            raw=network["observations"],
            resource=resource,
            window=network["time_range"],
            worker_read=True,
            supervisor_read=True,
            actions=[
                {"name": "get_pod_network_summary", "arguments": {"resource_id": resource["id"]}}
            ],
        )
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(rows["evidence"][0]["observations"], network["observations"])
        self.assertEqual(rows["evidence"][0]["resource_identities"][0]["uid"], resource["uid"])
        self.assertTrue(rows["report_proposal"])
        self.assertTrue(rows["read_receipt"])
        self.assertFalse(state["final_report"]["fallback"])
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
        for observation in rows["observation"]:
            value, found, _ = resolve_pointer(
                network["observations"], observation["source_ref"]["json_pointer"]
            )
            self.assertTrue(found)
            self.assertEqual(value, observation["field_value"])
        self.assertFalse(state["final_report"]["report_errors"])

    async def test_attempt6_iam_original_data_complete_parent(self):
        await self.check_case("iam-pod")

    async def test_attempt6_frontend_original_data_complete_parent(self):
        await self.check_case("frontend-service")
