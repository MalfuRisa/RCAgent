"""R3 sent-reference authority survives presentation changes, never scope changes."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from config import settings
from local_testing.skill_fixture import seed
from workspace.runtime import bind, invoke_model
from workspace.evidence_references import resolve
from workspace.reference_registration import (
    register,
    ReferenceRegistration,
    record_observations,
)
from workspace.repository import WorkspaceError
from workspace import investigation_phases as phases


class StableReferencesR3(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state, self.b, self.catalog, self.data = await seed(
            Path(self.temp.name), "E1"
        )
        pace = patch.object(settings, "MODEL_MIN_INTERVAL_SECONDS", 0)
        pace.start()
        self.addCleanup(pace.stop)

    async def sent(self, messages=None):
        with bind(self.b):
            await invoke_model(
                RunnableLambda(lambda _: AIMessage(content="received")),
                messages or [HumanMessage(content="Review the stored exact fields")],
                phase="worker_step",
            )
        return await self.b.repository.all(self.b.incident_id, "candidate")

    async def selected(self):
        candidates = await self.sent()
        return next(
            r
            for r in candidates
            if "latency_ns" in r["proposal"]["source_ref"]["json_pointer"]
        )

    async def submission(self, ref_ids, limitations=None):
        task = await self.b.repository.read(self.b.incident_id, self.b.task_id, "task")
        return ReferenceRegistration(
            ref_ids=ref_ids,
            question_ids=task["question_ids"],
            limitations=limitations or [],
        )

    async def test_sent_ref_survives_zero_candidate_view_and_idempotent_registration(
        self,
    ):
        chosen = await self.selected()
        old_context = self.b.last_context_id
        from context.investigation_preparation import reduced_view

        def clipped(payload, level, manifest):
            view = reduced_view(payload, 2, manifest)
            view["evidence"] = []
            view["current_candidates"] = []
            return view

        with patch(
            "context.investigation_preparation.reduced_view", side_effect=clipped
        ):
            await self.sent()
        self.assertNotEqual(old_context, self.b.last_context_id)
        self.assertEqual((await resolve(self.b, chosen["id"]))["id"], chosen["id"])
        submission = await self.submission([chosen["id"]])
        first = await register(self.b, submission)
        again = await register(self.b, submission)
        self.assertEqual(first["observation_ids"], again["observation_ids"])
        self.assertTrue(again["already_registered"])
        self.assertEqual(
            len(await self.b.repository.all(self.b.incident_id, "observation")), 1
        )
        changed = await register(
            self.b, await self.submission([chosen["id"]], ["Scope limitation changed"])
        )
        self.assertNotEqual(changed["observation_ids"], first["observation_ids"])

    async def test_cross_reader_and_source_changes_are_not_authorized_by_id(self):
        chosen = await self.selected()
        previous = self.b.role
        self.b.role = "supervisor"
        with self.assertRaisesRegex(WorkspaceError, "not_visible"):
            await resolve(self.b, chosen["id"])
        self.b.role = previous
        eid = chosen["proposal"]["source_ref"]["evidence_id"]
        evidence = await self.b.repository.read(self.b.incident_id, eid, "evidence")
        await self.b.repository.save(
            self.b.incident_id, "evidence", evidence, evidence["revision"]
        )
        with self.assertRaisesRegex(WorkspaceError, "source_stale"):
            await resolve(self.b, chosen["id"])

    async def test_invalid_batch_does_not_partially_register_and_sdk_contract_is_small(
        self,
    ):
        chosen = await self.selected()
        with self.assertRaisesRegex(WorkspaceError, "unknown_ref"):
            await register(self.b, await self.submission([chosen["id"], "unknown_ref"]))
        self.assertEqual(
            await self.b.repository.all(self.b.incident_id, "observation"), []
        )
        schema = convert_to_openai_tool(record_observations)["function"]["parameters"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["properties"]), {"ref_ids", "question_ids", "limitations"}
        )
        self.assertNotIn("source_hash", json.dumps(schema))

    async def test_last_consolidation_rejection_has_real_registration_and_completion_calls(
        self,
    ):
        from agents.investigation_worker import worker
        from test_unified_investigation import payload

        class Model:
            def __init__(inner):
                inner.step = 0
                inner.contracts = []

            def bind_tools(inner, tools, **kwargs):
                inner.contracts.extend(convert_to_openai_tool(t) for t in tools)

                async def respond(messages):
                    p = payload(messages)
                    step = inner.step
                    inner.step += 1
                    qids = (await self.submission(["placeholder"])).question_ids
                    if step == 0:
                        name, args = (
                            "read_evidence",
                            {
                                "evidence_id": self.data["evidence_id"],
                                "field": "data.list",
                                "limit": 1,
                            },
                        )
                    elif step == 1:
                        name, args = (
                            "record_observations",
                            {"ref_ids": ["unknown_ref"], "question_ids": qids},
                        )
                    elif step == 2:
                        rows = await self.b.repository.all(
                            self.b.incident_id, "candidate"
                        )
                        ref = next(
                            r
                            for r in rows
                            if "latency_ns"
                            in r["proposal"]["source_ref"]["json_pointer"]
                        )
                        name, args = (
                            "record_observations",
                            {"ref_ids": [ref["id"]], "question_ids": qids},
                        )
                    else:
                        name, args = (
                            "complete_task",
                            {
                                "interpretation": "Delay observed; mechanism remains unresolved",
                                "supporting_observation_ids": [
                                    o["id"] for o in p["investigation"]["observations"]
                                ],
                                "limitations": ["Fixture scope only"],
                                "unresolved_reasons": {
                                    q: "No causal evidence" for q in qids
                                },
                            },
                        )
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {"name": name, "args": args, "id": "r3-" + str(step)}
                        ],
                    )

                return RunnableLambda(respond)

        model = Model()
        with (
            patch.object(settings, "WORKER_EXPLORATION_STEPS", 0),
            patch.object(settings, "INVESTIGATION_CONSOLIDATION_STEPS", 2),
            patch.object(settings, "INVESTIGATION_REPAIR_STEPS", 2),
            patch.object(settings, "INVESTIGATION_SKILLS_ENABLED", False),
            bind(self.b),
        ):
            result = await worker(self.b, model, self.state, self.catalog)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(model.step, 4)
        self.assertEqual(self.b.phase_budget["used"]["consolidating"], 2)
        self.assertEqual(self.b.phase_budget["used"]["repairing"], 2)
        self.assertEqual(self.b.phase_budget["phase"], "finished")
        complete = next(
            r["function"]["parameters"]
            for r in model.contracts
            if r["function"]["name"] == "complete_task"
        )
        self.assertNotIn("observations", complete["properties"])
        self.assertNotIn("candidate_ids", complete["properties"])
        del self.b.phase_budget
        await phases.initialize(self.b)
        self.assertEqual(self.b.phase_budget["logical_steps"], 4)
        self.assertEqual(self.b.phase_budget["phase"], "finished")

    async def test_rows_empty_text_and_page_aggregates_preserve_exact_ranges(self):
        from tools.diagnostic_catalog import Capability, CapabilityCatalog, QueryResult
        from workspace.query_execution import execute_query
        from workspace.read_tools import read_evidence

        number = 1793324238832796119
        raw = {
            "rows": [
                {"time": number + i, "nullable": None, "label": "row-" + str(i)}
                for i in range(20)
            ],
            "empty": [],
            "log": "event line\n" * 900,
        }

        async def normalize(b, args):
            return args

        async def query(b, args):
            return QueryResult(raw, [self.b.resource_id], self.b.query_window)

        catalog = CapabilityCatalog(
            [
                Capability(
                    "shape_fixture",
                    "Synthetic exact-shape fixture",
                    {"type": "object"},
                    normalize,
                    query,
                    "synthetic-r3",
                    True,
                )
            ],
            service_identity={"synthetic": True},
        )
        saved = await execute_query(self.b, catalog, "shape_fixture", {})
        eid = saved["_rcagent_query"]["evidence_id"]
        with bind(self.b):
            await read_evidence.ainvoke(
                {"evidence_id": eid, "field": "rows", "limit": 1}
            )
            await read_evidence.ainvoke({"evidence_id": eid, "field": "empty"})
            text = await read_evidence.ainvoke(
                {
                    "evidence_id": eid,
                    "field": "log",
                    "char_offset": 50,
                    "max_chars": 1024,
                }
            )
        await self.sent()
        rows = [
            r
            for r in await self.b.repository.all(self.b.incident_id, "candidate")
            if r["proposal"]["source_ref"]["evidence_id"] == eid
        ]
        integer = next(
            r
            for r in rows
            if r["proposal"]["source_ref"]["json_pointer"] == "/rows/0/time"
        )
        empty = next(
            r for r in rows if r["proposal"]["source_ref"]["json_pointer"] == "/empty"
        )
        chars = next(
            r for r in rows if r["proposal"]["source_ref"].get("range_unit") == "chars"
        )
        count = next(
            r
            for r in rows
            if (r["proposal"].get("aggregate") or {}).get("operation") == "count"
        )
        self.assertFalse(
            any(
                r["proposal"]["source_ref"]["json_pointer"].startswith("/rows/10")
                for r in rows
            )
        )
        result = await register(
            self.b,
            await self.submission([r["id"] for r in [integer, empty, chars, count]]),
        )
        formal = [
            await self.b.repository.read(
                self.b.incident_id, r["observation_id"], "observation"
            )
            for r in result["observations"]
        ]
        self.assertEqual(formal[0]["field_value"], number)
        self.assertIs(type(formal[0]["field_value"]), int)
        self.assertEqual(formal[1]["field_value"], [])
        self.assertEqual(formal[2]["field_value"], text["text"])
        self.assertEqual(formal[3]["field_value"], 1)
        self.assertEqual(formal[3]["verification"]["coverage_status"], "partial")
        self.assertFalse(
            any("expected_value" in r["proposal"] for r in rows),
            "Index points to originals rather than duplicating full source values",
        )

    async def test_runtime_skill_entries_reach_requests_without_model_skill_calls(self):
        await self.sent()
        records = await self.b.repository.all(self.b.incident_id, "skill_read")
        self.assertTrue(
            any(
                r.get("name") == "submitting-observations"
                and r.get("activation_source") == "runtime"
                for r in records
            )
        )
        self.assertFalse(
            any(
                r.get("activation_source") in ("model_read", "reference_read")
                for r in records
            )
        )
        delivered = await self.b.repository.all(self.b.incident_id, "skill_delivery")
        self.assertTrue(
            any(
                r.get("event") == "runtime_activated"
                and r.get("name") == "submitting-observations"
                and r["context_id"] == self.b.last_context_id
                for r in delivered
            )
        )
        self.b.role, self.b.task_id, self.b.attempt_id = "planner", None, None
        with bind(self.b):
            await invoke_model(
                RunnableLambda(lambda _: AIMessage(content="planned")),
                [HumanMessage(content="Plan")],
                phase="plan",
            )
        delivered = await self.b.repository.all(self.b.incident_id, "skill_delivery")
        self.assertTrue(
            any(
                r.get("role") == "planner"
                and r.get("name") == "planning-investigation"
                and r.get("event") == "runtime_activated"
                for r in delivered
            )
        )

    async def test_all_reserved_pure_skill_calls_execute_without_business_budget(self):
        from workflow.role_tools import take_step

        class Reader:
            def bind_tools(inner, tools, **kwargs):
                self.assertEqual([t.name for t in tools], ["read_skill"])

                async def respond(_):
                    inner.count = getattr(inner, "count", 0) + 1
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_skill",
                                "args": {"name": "submitting-observations"},
                                "id": "skill-call-" + str(inner.count),
                            }
                        ],
                    )

                return RunnableLambda(respond)

        with patch.object(settings, "WORKER_EXPLORATION_STEPS", 0), bind(self.b):
            await phases.initialize(self.b)
            self.b.phase_budget["used"]["consolidating"] = self.b.phase_budget[
                "limits"
            ]["consolidating"]
            await phases.save(self.b)
            messages = [HumanMessage(content="Read guidance only")]
            model = Reader()
            for _ in range(settings.SKILL_READ_EXTRA_ROUNDS):
                outcome = await take_step(
                    self.b,
                    model,
                    [],
                    messages,
                    phase="consolidating",
                    empty_instruction="Use only knowledge",
                    denied_reason="not permitted",
                )
                self.assertEqual(outcome[0][1]["status"], "accepted")
            self.assertEqual(
                self.b.phase_budget["used"]["skill"], settings.SKILL_READ_EXTRA_ROUNDS
            )
            self.assertEqual(self.b.knowledge_rounds, settings.SKILL_READ_EXTRA_ROUNDS)
            with self.assertRaisesRegex(WorkspaceError, "phase_budget_exhausted"):
                phases.can_send(self.b)

    async def test_acknowledged_page_leaves_input_but_exact_ref_remains_registerable(
        self,
    ):
        from workspace.read_tools import read_evidence
        from workspace.artifacts import read_artifact_raw
        from langchain_core.messages import ToolMessage
        from context.exact_representation import expand

        await phases.initialize(self.b)
        args = {
            "evidence_id": self.data["evidence_id"],
            "field": "data.list",
            "offset": 10,
            "limit": 1,
        }
        with bind(self.b):
            page = await read_evidence.ainvoke(args)
        messages = [
            HumanMessage(content="Review this existing exact page"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_evidence", "args": args, "id": "prior-page"}
                ],
            ),
            ToolMessage(
                content=json.dumps(page, ensure_ascii=False),
                name="read_evidence",
                tool_call_id="prior-page",
            ),
        ]
        rows = await self.sent(messages)
        ref = next(
            r
            for r in rows
            if r["proposal"]["source_ref"]["json_pointer"] == "/data/list/10/latency_ns"
        )
        await self.sent(messages)
        manifest = await self.b.repository.read(
            self.b.incident_id, self.b.last_context_id, "context"
        )
        self.assertTrue(
            any(
                r.get("receipt_id") == page["read_receipt_id"]
                for r in manifest["history_replacements"]
            )
        )
        visible = await read_artifact_raw(
            self.b, manifest["visible_messages_artifact_id"]
        )
        view = expand(
            next(
                json.loads(m["content"].split("\n", 1)[1])
                for m in visible
                if isinstance(m.get("content"), str)
                and m["content"].startswith("Incident workbench")
            )
        )
        self.assertFalse(
            any(
                r.get("receipt_id") == page["read_receipt_id"]
                for r in view.get("verified_read_details", [])
            )
        )
        result = await register(self.b, await self.submission([ref["id"]]))
        self.assertTrue(result["observation_ids"])


if __name__ == "__main__":
    unittest.main()
