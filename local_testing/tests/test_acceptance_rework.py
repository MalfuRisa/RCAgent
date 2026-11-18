"""Architect boundary reproductions against real storage and compiled workflow."""

import json
import os
import unittest
from unittest.mock import patch
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from config import settings
from models.schemas import RCATask
from workspace.repository import WorkspaceError, utcnow
from workspace.resources import register
from workspace.queries import execute, save_query
from workspace.tasks import register_tasks, start, finish
from workspace.read_tools import read_evidence
from workspace.reports import adjudicate
from context.builder import prepare
from workspace.read_tools import READ_TOOLS
from test_workspace import WorkspaceCase
import test_workspace_graph as graph_test_support
from langchain_core.runnables import RunnableLambda
from models.schemas import Symptom, SymptomList, RCATaskList, SupervisorDecision, FinalReport


class AcceptanceBoundaries(WorkspaceCase):
    async def resolved(self, name, namespace, uid, kind="pod"):
        r = await register(self.b, kind, name, namespace)
        r.update(cluster="c", uid=uid, identity_status="resolved", verified_at=utcnow())
        return await self.b.repository.save(self.b.incident_id, "resource", r, r["revision"])

    async def task_for(self, resource):
        task = RCATask(
            priority=1,
            investigation_goal="Investigate termination",
            target_resource=resource["name"],
            resource_type=resource["kind"],
        )
        task = (await register_tasks(self.b, [task], resource["name"], resource["namespace"]))[0]
        attempt = await start(self.b, task.task_id)
        self.b.task_id, self.b.attempt_id, self.b.resource_id = (
            task.task_id,
            attempt["id"],
            task.resource_id,
        )
        return task

    async def oom(self, resource, reference="oom"):
        await save_query(
            self.b,
            reference,
            "get_pod_runtime_status",
            {"pod_uid": resource["uid"], "cluster": "c", "namespace": resource["namespace"]},
            {
                "status": "ok",
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

    def claim(self, resource, reference="oom"):
        return {
            "type": "causal",
            "content": "Observed OOM termination candidate",
            "resource_id": resource["id"],
            "supporting_evidence_ids": [reference],
            "mechanism": "oom_termination",
            "limitations": ["Only termination; trigger unverified"],
        }

    async def test_same_name_metric_cannot_support_other_namespace_task(self):
        one = await self.resolved("same-name", "one", "uid-one")
        two = await self.resolved("same-name", "two", "uid-two")

        async def raw():
            return {"status": "ok", "series": [{"value": 5}]}

        await execute(
            self.b, "diagnose_pod", {"cluster": "c", "namespace": "one", "pod": "same-name"}, raw
        )
        e = (await self.b.repository.all(self.b.incident_id, "evidence"))[-1]
        self.assertEqual(e["resource_ids"], [one["id"]])
        await self.task_for(two)
        with self.assertRaisesRegex(WorkspaceError, "another target"):
            await finish(
                self.b,
                {
                    "diagnosis": "proven",
                    "answer_status": "supported",
                    "supporting_evidence_ids": [e["id"]],
                },
            )
        await execute(
            self.b, "diagnose_pod", {"cluster": "c", "namespace": "two", "pod": "same-name"}, raw
        )
        own = (await self.b.repository.all(self.b.incident_id, "evidence"))[-1]
        result = await finish(
            self.b,
            {
                "diagnosis": "observed",
                "answer_status": "supported",
                "supporting_evidence_ids": [own["id"]],
            },
        )
        self.assertEqual(result["answer_status"], "supported")

    async def test_large_result_keeps_key_observations_in_triage_and_planner_input(self):
        """The workbench carries the key observations of a large result, and a giant
        raw blob stays protected input: it refuses with the specific reason instead
        of being silently summarized away (PRD §17.5, §17.8)."""
        r = await self.resolved("iam", "app", "iam-uid")
        raw = {
            "status": "ok",
            "summary": {"tcp_errors": 7, "important_observation_marker": "OBSERVED_FAILURE"},
            "samples": [{"detail": "x" * 30000}],
            "meta": {
                "time_range": {"start_time": "a", "end_time": "b"},
                "data_gaps": [{"reason": "limited"}],
            },
        }
        await save_query(
            self.b,
            "network",
            "get_pod_network_summary",
            {"cluster": "c", "pod_uid": r["uid"]},
            raw,
            "now",
            1,
        )
        for role in ("triage", "planner"):
            self.b.role = role
            visible, manifest = await prepare(
                self.b,
                [
                    SystemMessage(content="Extract symptoms"),
                    HumanMessage(content="iam pod 出现 DNS 失败，提取症状"),
                ],
            )
            text = chr(10).join(str(m.content) for m in visible)
            self.assertIn("tcp_errors", text)
            self.assertIn("OBSERVED_FAILURE", text)
            self.assertIn("network", text)
            self.assertLessEqual(
                sum(len(str(m.content)) for m in visible), settings.CONTEXT_MAX_CHARS
            )
        # A 30,000-character operator blob cannot be reduced without losing it.
        with self.assertRaisesRegex(WorkspaceError, "protected_parts_exceed_budget"):
            await prepare(
                self.b,
                [SystemMessage(content="Extract symptoms"), HumanMessage(content=json.dumps(raw))],
            )

    async def test_omitted_opposition_in_claim_still_blocks_related_oom(self):
        r = await self.resolved("coredns", "system", "dns-uid")
        await self.task_for(r)
        await self.oom(r)
        await save_query(
            self.b,
            "counter",
            "get_pod_runtime_status",
            {"cluster": "c", "pod_uid": r["uid"]},
            {"status": "ok", "summary": {"conflict": True}},
            "now",
            1,
        )
        await finish(
            self.b,
            {
                "diagnosis": "OOM conflict",
                "answer_status": "inconclusive",
                "supporting_evidence_ids": ["oom"],
                "opposing_evidence_ids": ["counter"],
                "gaps": ["Conflict unresolved"],
            },
        )
        report = await adjudicate(self.b, {}, [self.claim(r)])
        self.assertEqual(report["answer_status"], "inconclusive")

    async def test_other_target_conflict_does_not_block_oom(self):
        good = await self.resolved("good", "app", "good-uid")
        other = await self.resolved("other", "elsewhere", "other-uid")
        await self.oom(good)
        await self.task_for(other)
        await self.oom(other, "other-oom")
        await save_query(
            self.b,
            "other-counter",
            "get_pod_runtime_status",
            {"cluster": "c", "pod_uid": other["uid"]},
            {"status": "ok", "summary": {"conflict": True}},
            "now",
            1,
        )
        await finish(
            self.b,
            {
                "diagnosis": "Other conflict",
                "answer_status": "inconclusive",
                "supporting_evidence_ids": ["other-oom"],
                "opposing_evidence_ids": ["other-counter"],
                "gaps": ["Conflict unresolved"],
            },
        )
        report = await adjudicate(self.b, {}, [self.claim(good)])
        self.assertEqual(report["answer_status"], "supported")

    async def test_malformed_claim_gate_returns_audited_unresolved(self):
        r = await self.resolved("coredns", "system", "dns-uid")
        bad = self.claim(r)
        bad["supporting_evidence_ids"] = None
        report = await adjudicate(self.b, {}, [bad])
        self.assertEqual(report["answer_status"], "inconclusive")
        audit = await self.b.repository.read(self.b.incident_id, report["audit_id"], "report")
        self.assertFalse(audit["checks"][0]["accepted"])

    async def test_claim_contract_rejects_bad_references_mechanism_and_limitations(self):
        from pydantic import ValidationError

        r = await self.resolved("coredns", "system", "dns-uid")
        variants = (
            {"supporting_evidence_ids": None},
            {"supporting_evidence_ids": "oom"},
            {"supporting_evidence_ids": []},
            {"opposing_evidence_ids": [None]},
            {"mechanism": "invented"},
            {"mechanism": None},
            {"limitations": "one sample"},
            {"limitations": []},
        )
        for update in variants:
            bad = dict(self.claim(r), **update)
            with self.subTest(update=update), self.assertRaises(ValidationError):
                SupervisorDecision(claims=[bad])
            report = await adjudicate(self.b, {}, [bad])
            self.assertEqual(report["answer_status"], "inconclusive")

    async def test_foreign_namespace_hypothesis_reference_is_rejected(self):
        one = await self.resolved("same-name", "one", "uid-one")
        two = await self.resolved("same-name", "two", "uid-two")
        await self.oom(one)
        await self.task_for(two)
        with self.assertRaisesRegex(WorkspaceError, "another target"):
            await finish(
                self.b,
                {
                    "diagnosis": "Candidate",
                    "hypotheses": [
                        {"content": "Foreign cause", "supporting_evidence_ids": ["oom"]}
                    ],
                },
            )

    async def test_uid_kind_cluster_and_inventory_namespace_are_not_aliases(self):
        one = await self.resolved("same-name", "one", "uid-one")
        two = await self.resolved("same-name", "two", "uid-two")
        service = await self.resolved("same-name", "one", "uid-one", "service")
        foreign = await self.resolved("same-name", "one", "uid-one")
        # A replaced/foreign identity is historical metadata, not an alias for c.
        foreign = dict(foreign, id="foreign-resource", cluster="other")
        await self.b.repository.save(self.b.incident_id, "resource", foreign)
        await save_query(
            self.b,
            "uid-query",
            "get_pod_runtime_status",
            {"cluster": "c", "pod_uid": "uid-one"},
            {"status": "ok"},
            "now",
            1,
        )
        e = await self.b.repository.read(self.b.incident_id, "uid-query", "evidence")
        self.assertEqual(e["resource_ids"], [one["id"]])

        async def inventory():
            return {
                "status": "ok",
                "candidates": [
                    {
                        "kind": "pod",
                        "name": "same-name",
                        "namespace": "one",
                        "cluster": "c",
                        "uid": "uid-one",
                    }
                ],
            }

        await execute(
            self.b,
            "search_k8s_targets",
            {"cluster": "c", "namespace": "one", "kind": "pod", "query": "same-name"},
            inventory,
        )
        evidence = (await self.b.repository.all(self.b.incident_id, "evidence"))[-1]
        self.assertEqual(evidence["resource_ids"], [one["id"]])
        unchanged = await self.b.repository.read(self.b.incident_id, two["id"], "resource")
        self.assertEqual(
            (unchanged["uid"], unchanged["namespace"], unchanged["identity_status"]),
            ("uid-two", "two", "resolved"),
        )
        unchanged_service = await self.b.repository.read(
            self.b.incident_id, service["id"], "resource"
        )
        self.assertEqual(
            (unchanged_service["uid"], unchanged_service["identity_status"]),
            ("uid-one", "resolved"),
        )

        async def services():
            return {
                "status": "ok",
                "candidates": [
                    {
                        "kind": "service",
                        "name": "same-name",
                        "namespace": "one",
                        "cluster": "c",
                        "uid": "uid-one",
                    }
                ],
            }

        await execute(
            self.b,
            "search_k8s_targets",
            {"cluster": "c", "namespace": "one", "kind": "auto", "query": "same-name"},
            services,
        )
        unchanged_pod = await self.b.repository.read(self.b.incident_id, one["id"], "resource")
        self.assertEqual(
            (unchanged_pod["uid"], unchanged_pod["identity_status"]), ("uid-one", "resolved")
        )

    async def test_service_backend_evidence_links_exact_service_and_pod_only(self):
        service = await self.resolved("iam", "one", "service-uid", "service")
        one = await self.resolved("backend", "one", "pod-one")
        two = await self.resolved("backend", "two", "pod-two")

        async def inventory():
            return {
                "status": "ok",
                "pods": [
                    {"name": "backend", "namespace": "one", "cluster": "c", "pod_uid": "pod-one"}
                ],
            }

        await execute(
            self.b,
            "get_service_backends",
            {"cluster": "c", "namespace": "one", "service_uid": "service-uid"},
            inventory,
        )
        e = (await self.b.repository.all(self.b.incident_id, "evidence"))[-1]
        self.assertEqual(set(e["resource_ids"]), {service["id"], one["id"]})
        self.assertNotIn(two["id"], e["resource_ids"])
        await self.task_for(one)
        result = await finish(
            self.b,
            {
                "diagnosis": "Backend observed",
                "answer_status": "supported",
                "supporting_evidence_ids": [e["id"]],
            },
        )
        self.assertEqual(result["answer_status"], "supported")

    async def test_hidden_hypothesis_counter_evidence_blocks_claim(self):
        r = await self.resolved("coredns", "system", "dns-uid")
        await self.task_for(r)
        await self.oom(r)
        await save_query(
            self.b,
            "counter",
            "get_pod_runtime_status",
            {"cluster": "c", "pod_uid": r["uid"]},
            {"status": "ok"},
            "now",
            1,
        )
        await finish(
            self.b,
            {
                "diagnosis": "Candidate",
                "answer_status": "inconclusive",
                "supporting_evidence_ids": ["oom"],
                "hypotheses": [
                    {
                        "content": "OOM conflict",
                        "mechanism": "oom_termination",
                        "status": "unresolved",
                        "supporting_evidence_ids": ["oom"],
                        "opposing_evidence_ids": ["counter"],
                    }
                ],
            },
        )
        report = await adjudicate(self.b, {}, [self.claim(r)])
        self.assertEqual(report["answer_status"], "inconclusive")

    async def test_long_observation_is_marked_preview_and_reads_back_in_full(self):
        """A very long observation is not silently clipped and not fatal either.

        PRD §6.4.3: keep the value as an explicitly incomplete preview with a
        detail entry; the original stays readable through read_evidence. The
        old behaviour (refuse the whole call) is replaced by this contract.
        """
        from workspace.runtime import invoke_model, bind

        r = await self.resolved("iam", "app", "iam-uid")
        self.b.role = "triage"
        marker = "necessary_observation"
        await save_query(
            self.b,
            "huge-summary",
            "get_pod_network_summary",
            {"cluster": "c", "pod_uid": r["uid"]},
            {"status": "ok", "summary": {marker: "x" * 30000, "tcp_errors": 7}},
            "now",
            1,
        )
        calls = []

        async def model(messages):
            calls.append(messages)
            return AIMessage(content="reachable")

        with bind(self.b):
            await invoke_model(RunnableLambda(model), [HumanMessage(content="Extract symptoms")])
            visible = calls[0][0].content
            text, offset = "", 0
            while offset is not None:
                page = await read_evidence.ainvoke(
                    {
                        "evidence_id": "huge-summary",
                        "field": "summary.necessary_observation",
                        "char_offset": offset,
                    }
                )
                text += page["text"]
                offset = page["next_char_offset"]
        self.assertEqual(len(calls), 1)
        payload = json.loads(visible.split("\n", 1)[1])
        brief = next(row for row in payload["evidence"] if row["evidence_id"] == "huge-summary")
        preview = brief["observations"][marker]
        self.assertTrue(preview["preview_incomplete"], "long observation must be marked incomplete")
        self.assertEqual(preview["chars"], 30000)
        self.assertEqual(brief["observations"]["tcp_errors"], 7)
        self.assertTrue(brief["detail_required"])
        self.assertLess(len(json.dumps(preview)), 1000)
        # char pages carry the JSON-serialised scalar (outer quotes included).
        self.assertEqual(len(text.strip('"')), 30000)
        self.assertEqual(set(text.strip('"')), {"x"})
        manifest = (await self.b.repository.all(self.b.incident_id, "context"))[-1]
        self.assertNotIn("context_incomplete", manifest)

    async def test_structural_context_overflow_refuses_before_send_with_candidate(self):
        """Structure that cannot shrink (identity/scope/limits) still refuses.

        The candidate input is archived with disposition=rejected_before_send and
        the model is not called (PRD §10.2 step 5, §10.3).
        """
        from workspace.runtime import invoke_model, bind

        r = await self.resolved("iam", "app", "iam-uid")
        self.b.role = "triage"
        await save_query(
            self.b,
            "needed",
            "get_pod_runtime_status",
            {"cluster": "c", "pod_uid": r["uid"]},
            {"status": "ok", "data_gaps": ["gap kept verbatim"]},
            "now",
            1,
        )
        calls = []

        async def model(messages):
            calls.append(messages)
            return AIMessage(content="unreachable")

        with (
            bind(self.b),
            patch.object(settings, "CONTEXT_MAX_CHARS", 200),
            self.assertRaisesRegex(WorkspaceError, "context_incomplete"),
        ):
            await invoke_model(RunnableLambda(model), [])
        self.assertEqual(calls, [])
        manifest = (await self.b.repository.all(self.b.incident_id, "context"))[-1]
        self.assertTrue(manifest["context_incomplete"])
        self.assertEqual(manifest["disposition"], "rejected_before_send")
        self.assertTrue(manifest["candidate_messages_artifact_id"])
        self.assertIn("rejection_reason", manifest)

    async def test_unrelated_large_judgment_is_not_required_for_current_target(self):
        one = await self.resolved("iam", "app", "iam-uid")
        other = await self.resolved("other", "two", "other-uid")
        await self.task_for(other)
        await finish(self.b, {"diagnosis": "x" * 30000, "gaps": ["unrelated"]})
        self.b.role = "triage"
        self.b.task_id = self.b.attempt_id = self.b.resource_id = None
        self.b.focus_resource_ids = [one["id"]]
        visible, manifest = await prepare(self.b, [HumanMessage(content="Extract IAM symptoms")])
        self.assertNotIn("unrelated", visible[0].content)
        self.assertTrue(
            any(
                row["reason"].startswith("outside current")
                for row in manifest["omitted_references_and_reasons"]
            )
        )


READ_TOOL_NAMES = {tool.name for tool in READ_TOOLS}
SUPERVISOR_TOOL_NAMES = READ_TOOL_NAMES | {"commit_investigation_stage", "prepare_report_packet"}


def workbench(messages):
    for message in messages:
        if isinstance(message.content, str) and message.content.startswith("Incident workbench"):
            from context.exact_representation import expand

            return expand(json.loads(message.content.split("\n", 1)[1]))
    raise AssertionError("Script requires visible workbench; no hidden database fallback")


def evidence_id(evidence):
    """Real evidence id from the visible workbench (compact rows use evidence_id)."""
    return evidence.get("evidence_id") or evidence.get("id")


class VisibleOnlyModel:
    """No Binding, Repository or fixture access: every dynamic ID comes from messages."""

    def __init__(self, malformed=False):
        self.calls = []
        self.malformed = malformed

    def facts(self, messages):
        """Read only the visible workbench; detail comes back through local tools."""
        view = workbench(messages)
        observations = [
            e
            for e in view["evidence"]
            if "OBSERVED_FAILURE" in json.dumps(e.get("observations", {}))
        ]
        # A verified full local read may replace the duplicated short-card
        # value. Consume the actual ToolMessage too, never a hidden DB lookup.
        if not observations:
            for message in messages:
                if message.type != "tool":
                    continue
                try:
                    body = json.loads(message.content)
                except ValueError:
                    continue
                if "OBSERVED_FAILURE" in json.dumps(body.get("items", [])):
                    observations.extend(
                        e for e in view["evidence"] if e["evidence_id"] == body.get("evidence_id")
                    )
        if not observations:
            raise AssertionError("Critical observation absent from actual model input")
        evidence = observations[-1]
        refs = {ref["id"] for ref in evidence.get("resource_revision_refs", [])}
        resource = next(r for r in view["resources"] if r["id"] in refs and r["kind"] == "pod")
        return view, evidence, resource

    def with_structured_output(self, schema):
        async def respond(messages):
            role = (
                "triage"
                if schema == SymptomList
                else "planner"
                if schema == RCATaskList
                else "supervisor"
            )
            self.calls.append((role, schema.__name__, messages))
            view, evidence, resource = self.facts(messages)
            if schema == SymptomList:
                return SymptomList(
                    symptoms=[
                        Symptom(
                            potential_symptom="Network errors",
                            resource_type=resource["kind"],
                            affected_resource=resource["name"],
                            evidence="tcp_errors=7; OBSERVED_FAILURE; evidence "
                            + evidence_id(evidence),
                        )
                    ]
                )
            if schema == RCATaskList:
                return RCATaskList(
                    rca_tasks=[
                        RCATask(
                            priority=1,
                            investigation_goal="Explain observed network errors",
                            target_resource=resource["name"],
                            resource_type=resource["kind"],
                        )
                    ]
                )
            report = FinalReport(
                root_cause="Suggested OOM cause",
                affected_resources=[resource["name"]],
                evidence_summary="Network observations",
                investigation_summary="Candidate, unverified mechanism",
                detection=True,
            )
            claim = {
                "type": "causal",
                "content": "Candidate OOM termination, unverified",
                "resource_id": resource["id"],
                "mechanism": "oom_termination",
                "supporting_evidence_ids": None if self.malformed else [evidence_id(evidence)],
                "limitations": ["Network observations do not demonstrate a termination"],
            }
            # Simulate decoded malformed output bypassing a provider's schema enforcement.
            return (
                {"final_report": report.model_dump(), "claims": [claim]}
                if self.malformed
                else SupervisorDecision(final_report=report, claims=[claim])
            )

        return RunnableLambda(respond)

    def bind_tools(self, tools, **kwargs):
        supervisor = all(t.name in SUPERVISOR_TOOL_NAMES for t in tools)

        async def respond(messages):
            role = "supervisor" if supervisor else "worker"
            self.calls.append((role, "tools", messages))
            view, evidence, resource = self.facts(messages)
            replies = [m for m in messages if m.type == "tool"]
            if supervisor:
                if not replies:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_evidence",
                                "args": {"evidence_id": evidence_id(evidence), "field": "summary"},
                                "id": "supervisor-visible-read",
                            }
                        ],
                    )
                return AIMessage(content="Ready to decide")
            if not any(m.name == "get_pod_network_summary" for m in replies):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_pod_network_summary",
                            "args": {"cluster": resource["cluster"], "pod_uid": resource["uid"]},
                            "id": "visible-network",
                        },
                        {
                            "name": "get_resource",
                            "args": {"resource_id": resource["id"]},
                            "id": "visible-identity",
                        },
                    ],
                )
            if not any(m.name == "read_evidence" for m in replies):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "read_evidence",
                            "args": {"evidence_id": evidence_id(evidence), "field": "summary"},
                            "id": "visible-evidence",
                        }
                    ],
                )
            observed = json.loads(next(m.content for m in replies if m.name == "read_evidence"))[
                "items"
            ][0]
            if observed["tcp_errors"] != 7:
                raise AssertionError("Local ToolMessage lost the critical observation")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_final_diagnosis",
                        "args": {
                            "diagnosis": "Observed network errors",
                            "reasoning": "Local read confirms tcp_errors=7",
                            "answer_status": "inconclusive",
                            "supporting_evidence_ids": [evidence_id(evidence)],
                            "gaps": ["Mechanism unverified"],
                        },
                        "id": "visible-submission",
                    }
                ],
            )

        return RunnableLambda(respond)


class LargeResultGateway(graph_test_support.FakeGateway):
    async def invoke(self, name, args):
        if name == "get_pod_network_summary":
            self.calls.append((name, dict(args)))
            return {
                "status": "ok",
                "summary": {"tcp_errors": 7, "important_observation_marker": "OBSERVED_FAILURE"},
                "samples": [{"detail": "x" * 30000}],
                "meta": {
                    "data_source": "synthetic",
                    "time_range": {
                        "start_time": "2026-10-10T00:00:00+00:00",
                        "end_time": "2026-10-10T00:10:00+00:00",
                    },
                },
            }
        return await super().invoke(name, args)


class AcceptanceParentGraph(unittest.IsolatedAsyncioTestCase):
    run_graph = graph_test_support.ParentGraphTests.run_graph

    async def run_visible(self, malformed=False):
        archive = os.environ.get("RCAGENT_TEST_ARCHIVE")
        if archive:
            with patch.dict(
                os.environ,
                {
                    "RCAGENT_TEST_ARCHIVE": archive
                    + ("/malformed" if malformed else "/visible-only")
                },
            ):
                return await self.run_graph(
                    model=VisibleOnlyModel(malformed),
                    gateway=LargeResultGateway(),
                    budget_enabled=True,
                )
        return await self.run_graph(
            model=VisibleOnlyModel(malformed), gateway=LargeResultGateway(), budget_enabled=True
        )

    async def test_visible_messages_only_complete_parent_with_large_observation(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        raw = {
            "summary": {"tcp_errors": 7},
            "samples": [{"detail": "original long detail " * 7000}],
        }
        state, rows, events, sent, model = await run_scenario(raw=raw, supervisor_read=True)
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertTrue(rows["observation"])
        self.assertTrue(rows["read_receipt"])
        self.assertFalse(state["final_report"]["fallback"])
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
        self.assertTrue(all(c["visible_messages_artifact_id"] for c in rows["context"]))
        self.assertEqual(state["final_report"]["report_errors"], [])

    async def test_malformed_claim_in_actual_parent_corrects_once_then_audited_unresolved(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(invalid_report=True)
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        errors = [
            a
            for a in rows["model_action"]
            if a["operation"] == "submit_report" and a["outcome"].get("status") == "rejected"
        ]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0]["response_id"])
        self.assertEqual(state["final_report"]["answer_status"], "inconclusive")
        self.assertFalse(state["final_report"]["report_errors"])
        self.assertTrue(
            any(
                "reference missing" in str(messages)
                for role, phase, messages in model.calls
                if role == "supervisor"
            )
        )
