"""R3: recovery, stage continuation and protected long material on the actual provider."""

import asyncio
import json
import tempfile
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from config import settings
from context.exact_representation import expand
from context.investigation import common_preparers
from local_testing.unified_scenarios import ScenarioModel, run_scenario
from test_unified_investigation import payload
from test_workspace import WorkspaceCase
from workspace import investigations
from workspace.queries import save_query
from workspace.runtime import bind, invoke_model
from workspace.read_tools import read_evidence
from workspace.stage_tools import commit_investigation_stage, new_segment_after_commit
from local_testing.investigation_export import export_run


class InterruptedModel(ScenarioModel):
    def __init__(self, target, *, permanent=False, code=None):
        super().__init__([{"name": "network_summary", "arguments": {}}])
        self.target, self.permanent, self.code = target, permanent, code
        self.failures = 0

    async def maybe_fail(self, target):
        if target == self.target and (self.permanent or not self.failures):
            self.failures += 1
            if self.code:
                from workspace.model_requests import ModelRequestError

                raise ModelRequestError(self.code, "fixture stop")
            raise TimeoutError("single request fixture timeout")

    def with_structured_output(self, schema, **kwargs):
        base = super().with_structured_output(schema, **kwargs)

        async def answer(messages):
            await self.maybe_fail(
                {
                    "ScopeReview": "triage",
                    "InvestigationPlan": "planner",
                    "PacketSelection": "packet",
                }.get(schema.__name__)
            )
            return await base.ainvoke(messages)

        return RunnableLambda(answer)

    def bind_tools(self, tools, **kwargs):
        base = super().bind_tools(tools, **kwargs)

        async def answer(messages):
            from workspace.runtime import current

            if current.get().role == "supervisor":
                await self.maybe_fail("supervisor")
            return await base.ainvoke(messages)

        return RunnableLambda(answer)


class RecoveryFlow(IsolatedAsyncioTestCase):
    def test_smaller_review_view_keeps_existing_source_directory_and_opposition(self):
        from context.investigation_preparation import reduced_view

        source = {
            "evidence_id": "source",
            "limitations": {"warnings": ["opposition must remain"]},
            "resource_revision_refs": [{"id": "verified_resource", "revision": 1}],
        }
        payload = {"evidence": [], "evidence_directory": [source], "current_candidates": []}
        for level in (1, 2):
            reduced = reduced_view(payload, level, {"omitted_references_and_reasons": []})
            self.assertEqual(reduced["evidence_directory"], [source])

    async def test_worker_failure_keeps_raw_and_supervisor_can_review_it(self):
        state, rows, *_ = await run_scenario(fault="worker_timeout_after_query")
        self.assertEqual(rows["attempt"][0]["status"], "failed")
        self.assertEqual(len(rows["evidence"]), 1)
        self.assertTrue(rows["observation"])
        self.assertFalse(state["final_report"]["fallback"])

    async def test_triage_planner_supervisor_and_final_selection_recover_without_losing_evidence(
        self,
    ):
        for role in ("triage", "planner", "supervisor", "packet"):
            with self.subTest(role=role):
                model = InterruptedModel(role)
                state, rows, *_ = await run_scenario(model=model, initial_evidence=True)
                self.assertIsNone(state.get("execution_error"))
                self.assertFalse(state["final_report"]["fallback"])
                self.assertEqual(len(rows["evidence"]), 1)
                self.assertTrue(rows["observation"])
                recovered = [
                    r
                    for r in rows["recovery_issue"]
                    if r["action"] == "same_role_smaller_view_retry"
                ]
                self.assertEqual(len(recovered), 1)
                self.assertEqual(model.failures, 1)
                self.assertTrue(any(c.get("recovery_view") for c in rows["context"]))
                self.assertTrue(any(not r["response_available"] for r in rows["model_response"]))

    async def test_permanent_failure_keeps_prior_raw_and_does_not_invent_a_plan(self):
        model = InterruptedModel("planner", permanent=True)
        state, rows, *_ = await run_scenario(model=model, initial_evidence=True)
        self.assertEqual(model.failures, 2)
        self.assertTrue(rows["question"])
        self.assertTrue(rows["evidence"])
        self.assertEqual(rows["task"], [])
        self.assertEqual(state["execution_error"]["code"], "model_timeout")
        self.assertTrue(state["final_report"]["fallback"])

    async def test_final_role_without_response_keeps_formal_observations_and_packet(self):
        model = InterruptedModel("packet", permanent=True)
        state, rows, *_ = await run_scenario(model=model)
        self.assertEqual(model.failures, 2)
        self.assertTrue(rows["observation"])
        self.assertTrue(rows["report_packet"])
        self.assertTrue(state["final_report"]["fallback"])
        self.assertTrue(rows["model_response"])

    async def test_auth_rate_limit_and_deadline_do_not_recover(self):
        for code in ("model_authentication_failed", "model_rate_limited", "investigation_deadline"):
            with self.subTest(code=code):
                model = InterruptedModel("triage", permanent=True, code=code)
                state, rows, *_ = await run_scenario(model=model, initial_evidence=True)
                self.assertEqual(model.failures, 1)
                self.assertEqual(state["execution_error"]["code"], code)
                self.assertFalse(
                    any(
                        r["action"] == "same_role_smaller_view_retry"
                        for r in rows["recovery_issue"]
                    )
                )
                self.assertTrue(rows["evidence"])

    async def test_actual_worker_handoff_continues_one_task_without_resetting_steps(self):
        class SegmentedModel(ScenarioModel):
            def __init__(self):
                super().__init__([{"name": "network_summary", "arguments": {}}])
                self.worker_step = 0

            def bind_tools(inner, tools, **kwargs):
                base = super().bind_tools(tools, **kwargs)

                async def answer(messages):
                    from workspace.runtime import current

                    b = current.get()
                    if b.role != "worker":
                        return await base.ainvoke(messages)
                    p = expand(payload(messages))
                    step = inner.worker_step
                    inner.worker_step += 1
                    if step == 0:
                        name, args = (
                            "query_diagnostics",
                            {"name": "network_summary", "arguments": {}},
                        )
                    elif step == 1:
                        name, args = (
                            "read_evidence",
                            {
                                "evidence_id": p["evidence"][0]["evidence_id"],
                                "field": "note",
                                "char_offset": 0,
                            },
                        )
                    elif step == 2:
                        page = next(
                            json.loads(m.content)
                            for m in reversed(messages)
                            if m.type == "tool" and m.name == "read_evidence"
                        )
                        refs = p["current_candidates"]
                        ref = next(
                            r for r in refs
                            if r["source_ref"].get("json_pointer") == "/note"
                            and r["source_ref"].get("range") == page["range"]
                        )
                        name, args = (
                            "record_observations",
                            {"ref_ids": [ref["ref_id"]],
                             "question_ids": p["stage_control"]["question_ids"]},
                        )
                    elif step == 3:
                        control = p["stage_control"]
                        name, args = (
                            "commit_investigation_stage",
                            {
                                "question_ids": control["question_ids"],
                                "observation_ids": control["registered_observation_ids"],
                                "interpretation": "Exact page registered; continue the same task",
                                "limitations": ["fixture"],
                                "open_items": ["causal explanation remains unresolved"],
                                "next_focus": ["finish the same task"],
                            },
                        )
                    else:
                        task = await b.repository.read(b.incident_id, b.task_id, "task")
                        inner.before_completion_status = task["execution_status"]
                        inner.before_completion_steps = b.model_steps
                        # Range bodies stay in storage after handoff; the
                        # delivered stage judgment retains their formal IDs.
                        inner.completion_ids = sorted({
                            oid
                            for judgment in p["investigation"]["judgments"]
                            for oid in judgment["supporting_observation_ids"]
                        })
                        name, args = (
                            "complete_task",
                            {
                                "interpretation": "Exact page registered; cause unresolved",
                                "limitations": ["fixture"],
                                "supporting_observation_ids": inner.completion_ids,
                            },
                        )
                    return AIMessage(
                        content="",
                        tool_calls=[{"id": "segment-" + str(step), "name": name, "args": args}],
                    )

                return RunnableLambda(answer)

        model = SegmentedModel()
        state, rows, *_ = await run_scenario(
            model=model, raw={"note": "abcdef", "signal": 0}, max_steps=6
        )
        self.assertIsNone(
            state.get("execution_error"),
            str(
                [
                    (a["operation"], a["outcome"])
                    for a in rows["model_action"]
                    if a["outcome"].get("status") in ("rejected", "failed")
                ]
            )[-4500:],
        )
        self.assertEqual(model.before_completion_status, "running")
        self.assertEqual(model.before_completion_steps, 5)
        self.assertTrue(model.completion_ids)
        self.assertTrue(any(
            set(model.completion_ids) <= set(j.get("supporting_observation_ids", []))
            for j in rows["judgment"] if j.get("producer_role") == "worker"
        ))
        self.assertEqual(len(rows["attempt"]), 1)
        self.assertEqual(rows["task"][0]["execution_status"], "finished")
        from workspace.material_ranges import disposition_covers

        page_receipt = next(r for r in rows["read_receipt"] if r.get("field") == "note")
        # R3 may externalize an acknowledged page before the explicit handoff.
        # Verify its exact receipt was archived and covered by formal work.
        externalized = {
            item.get("receipt_id")
            for context in rows["context"]
            for item in context.get("history_replacements", [])
        }
        retired = {
            rid for checkpoint in rows["checkpoint"]
            for rid in checkpoint.get("retired_receipt_ids", [])
        }
        self.assertIn(page_receipt["id"], retired | externalized)
        self.assertTrue(page_receipt.get("delivered_context_id"))
        self.assertTrue(any(
            disposition_covers(disposition, page_receipt)
            for commit in rows["stage_commit"]
            for disposition in commit.get("material_dispositions", [])
        ), json.dumps({"receipt": page_receipt, "commits": rows["stage_commit"],
                       "rejected": [a for a in rows["model_action"]
                                    if a["outcome"].get("status") == "rejected"]}))
        self.assertTrue(rows["observation"])
        self.assertTrue(any(r.get("segment_archive_artifact_id") for r in rows["checkpoint"]))
        self.assertFalse(state["final_report"]["fallback"])

    async def test_runner_mid_node_failure_exports_even_without_latest_state(self):
        from local_testing.run_investigation import run
        from workspace.runtime import initialize_incident, from_state

        async def interrupted(output, preflight, revision):
            state = await initialize_incident(
                {"target_cluster": "c", "target_resource": "known-node"}
            )
            b = from_state(state, "triage")
            await save_query(b, "evidence_mid_node", "fixture", {}, {"count": 7}, "now", 1, "ok")
            raise RuntimeError("fixture node interrupted after persistence")

        with (
            tempfile.TemporaryDirectory() as parent,
            patch("local_testing.run_investigation._run", interrupted),
        ):
            output = Path(parent) / "fresh"
            self.assertFalse(await run(output, "unused"))
            summary = json.loads((output / "run-analysis.json").read_text())
            self.assertEqual(summary["export"]["located_from"], "persistent_workspace")
            self.assertEqual(summary["record_counts"]["evidence"], 1)
            self.assertEqual(summary["failure"]["type"], "RuntimeError")


class UnifiedLongContext(WorkspaceCase):
    async def test_worker_local_limit_is_not_reset_or_overridden_by_global_steps(self):
        self.b.local_model_limit = 1
        self.b.model_steps = 1
        with bind(self.b), self.assertRaisesRegex(Exception, "role model step budget exhausted"):
            await invoke_model(
                RunnableLambda(lambda _: self.fail("no send past the existing local limit")),
                [],
                phase="worker_step",
            )
        self.assertEqual(self.b.model_steps, 1)
        self.assertEqual(await self.b.repository.all(self.b.incident_id, "model_call"), [])

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.b.context_preparers = common_preparers()
        self.b.lifecycle_mode = "active"
        for key, value in [
            ("CONTEXT_BUDGET_POLICY", "soft"),
            ("CONTEXT_MAX_CHARS", 96000),
            ("CONTEXT_HARD_MAX_CHARS", 512000),
        ]:
            p = patch.object(settings, key, value)
            p.start()
            self.addCleanup(p.stop)

    async def test_long_read_history_is_externalized_but_uncovered_exact_pages_survive_soft_expansion(
        self,
    ):
        raw = {"first": "UNIQUE_FACT_" + "a" * 55000, "second": "OPPOSING_FACT_" + "b" * 55000}
        await save_query(self.b, "evidence_long", "fixture_telemetry", {}, raw, "now", 1, "ok")
        question = await investigations.save_question(
            self.b, text="Check both independent sources and opposition"
        )
        messages = [SystemMessage(content="Protect source values and all unhandled opposition")]
        pages = []
        with bind(self.b):
            for field in raw:
                offset = 0
                while True:
                    page = await read_evidence.ainvoke(
                        {
                            "evidence_id": "evidence_long",
                            "field": field,
                            "char_offset": offset,
                            "max_chars": 8000,
                        }
                    )
                    pages.append(page)
                    call_id = "page-" + str(len(pages))
                    messages.extend(
                        [
                            AIMessage(
                                content="",
                                tool_calls=[{"id": call_id, "name": "read_evidence", "args": {}}],
                            ),
                            ToolMessage(
                                name="read_evidence", tool_call_id=call_id, content=json.dumps(page)
                            ),
                        ]
                    )
                    if not page.get("truncated"):
                        break
                    offset = page["next_char_offset"]
            seen = []

            async def answer(actual):
                seen.append(actual)
                return AIMessage(content="continue")

            await invoke_model(RunnableLambda(answer), messages, phase="worker_step")
            context = await self.b.repository.read(
                self.b.incident_id, self.b.last_context_id, "context"
            )
            self.assertEqual(context["context_policy"], "unified_sre")
            self.assertEqual(context["lifecycle_mode"], "active")
            self.assertTrue(context["history_replacements"])
            self.assertTrue(context["soft_target_expanded"])
            self.assertTrue(
                all(r["required_values_retained"] for r in context["read_retention_checks"])
            )
            self.assertGreater(context["request_chars_v2"], 96000)
            self.assertLess(context["request_chars_v2"], 512000)
            workbench = expand(payload(seen[-1]))
            shown = "\n".join(str(m.content) for m in seen[-1]) + json.dumps(
                workbench, ensure_ascii=False
            )
            self.assertIn("UNIQUE_FACT_", shown)
            self.assertIn("OPPOSING_FACT_", shown)
            page = pages[0]
            proposal = {
                "client_commit_key": "intermediate-page",
                "stage_goal": "Preserve the exact first page",
                "question_ids": [question["id"]],
                "observation_proposals": [
                    {
                        "local_id": "page",
                        "source_ref": {
                            "evidence_id": "evidence_long",
                            "json_pointer": "/first",
                            "range": page["range"],
                            "range_unit": "chars",
                        },
                        "expected_value": page["text"],
                    }
                ],
                "material_dispositions": [
                    {
                        "source_id": "evidence_long",
                        "field": "first",
                        "range": page["range"],
                        "range_unit": "chars",
                        "disposition": "covered_by",
                        "covered_by": ["page"],
                    }
                ],
                "open_items": ["The uncovered pages remain active"],
                "next_focus": ["Review the remaining pages"],
            }
            result = await commit_investigation_stage.ainvoke(proposal)
            self.assertEqual(result["status"], "accepted", result)
            used = self.b.model_steps
            segment = await new_segment_after_commit(
                self.b, result["checkpoint_id"], messages[0], messages=messages
            )
            await invoke_model(RunnableLambda(answer), segment, phase="worker_step")
            self.assertEqual(self.b.model_steps, used + 1)
            current = expand(payload(seen[-1]))
            self.assertNotIn(
                page["read_receipt_id"],
                [r["receipt_id"] for r in current.get("verified_read_details", [])],
            )
            text = "\n".join(str(m.content) for m in seen[-1]) + json.dumps(
                current, ensure_ascii=False
            )
            self.assertIn("OPPOSING_FACT_", text)
            self.assertTrue(current["investigation"]["committed_pages"])
            self.assertTrue(await self.b.repository.all(self.b.incident_id, "checkpoint"))

    async def test_engineering_ceiling_refusal_keeps_the_exact_candidate_and_composition(self):
        with bind(self.b), self.assertRaisesRegex(Exception, "engineering ceiling"):
            await invoke_model(
                RunnableLambda(lambda _: self.fail("oversized candidate must not send")),
                [HumanMessage(content="protected requirement " * 30000)],
                phase="worker_step",
            )
        contexts = await self.b.repository.all(self.b.incident_id, "context")
        refused = contexts[-1]
        self.assertEqual(refused["disposition"], "rejected_before_send")
        self.assertTrue(refused["candidate_messages_artifact_id"])
        self.assertTrue(refused["component_chars"])

    async def test_directory_candidate_is_visible_but_hidden_value_version_and_other_role_are_rejected(
        self,
    ):
        from workspace.observations import verify_observation

        raw = {f"field{i}": i for i in range(400)}
        await save_query(
            self.b, "evidence_candidates", "fixture_telemetry", {}, raw, "now", 1, "ok"
        )
        self.b.role = "supervisor"
        with bind(self.b):
            seen = []

            async def answer(messages):
                seen.append(messages)
                return AIMessage(content="review")

            await invoke_model(RunnableLambda(answer), [], phase="local_read")
            p = expand(payload(seen[-1]))
            self.assertEqual(p["evidence"], [])
            candidate = p["current_candidates"][0]
            proposal = {k: v for k, v in candidate.items() if k != "candidate_id"}
            accepted = await verify_observation(self.b, proposal)
            self.assertIn("accepted", accepted)
            tampered = {**proposal, "expected_value": -999}
            self.assertIn("rejected", await verify_observation(self.b, tampered))
            self.assertFalse(
                any(c["source_ref"]["json_pointer"] == "/field399" for c in p["current_candidates"])
            )
            hidden = {
                "local_id": "hidden",
                "source_ref": {"evidence_id": "evidence_candidates", "json_pointer": "/field399"},
                "expected_value": 399,
            }
            self.assertEqual(
                (await verify_observation(self.b, hidden))["rejected"]["reason"],
                "not_shown_to_model",
            )
            changed = {**proposal, "source_ref": {**proposal["source_ref"], "source_revision": 999}}
            self.assertIn("rejected", await verify_observation(self.b, changed))
            self.b.role = "worker"
            self.assertIn("rejected", await verify_observation(self.b, proposal))
        self.assertEqual(await self.b.repository.all(self.b.incident_id, "read_receipt"), [])

    async def test_export_locates_initialized_run_without_a_node_update(self):
        await save_query(
            self.b,
            "evidence_before_crash",
            "fixture_telemetry",
            {},
            {"zero": 0, "missing": None},
            "now",
            1,
            "ok",
        )
        with tempfile.TemporaryDirectory() as directory:
            summary = await export_run(
                directory,
                self.root,
                None,
                {"type": "RuntimeError", "message": "mid-node failure"},
                elapsed_seconds=1,
                mcp_calls=[],
            )
            self.assertEqual(summary["export"]["status"], "exported")
            self.assertEqual(summary["export"]["located_from"], "persistent_workspace")
            self.assertEqual(summary["record_counts"]["evidence"], 1)
            self.assertFalse(summary["software_flow_completed"])
            self.assertIn("program_partial_result", (Path(directory) / "report.md").read_text())

    async def test_export_failure_is_not_reported_as_no_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "local_testing.investigation_export.read_workspace",
                side_effect=OSError("unreadable database"),
            ):
                summary = await export_run(
                    directory, self.root, None, None, elapsed_seconds=1, mcp_calls=[]
                )
            self.assertEqual(summary["export"]["status"], "export_failed")
            self.assertIn("not proof", summary["export"]["note"])
