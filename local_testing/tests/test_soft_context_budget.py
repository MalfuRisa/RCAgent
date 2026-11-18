"""Soft efficiency goals must not stop valid work; the hard ceiling still does."""

from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda

from config import settings
from context.builder import prepare
from context.stage_budget import next_page_budget
from workspace.repository import WorkspaceError
from workspace.runtime import bind, invoke_model
from test_investigation_contracts import ActiveWorkspaceCase


class SoftContextBudget(ActiveWorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for name, value in [
            ("CONTEXT_BUDGET_POLICY", "soft"),
            ("CONTEXT_MAX_CHARS", 24000),
            ("CONTEXT_HARD_MAX_CHARS", 48000),
        ]:
            change = patch.object(settings, name, value)
            change.start()
            self.addCleanup(change.stop)

    async def test_all_four_roles_can_send_protected_content_above_soft_target(self):
        sent = []

        async def respond(messages):
            sent.append(messages)
            return AIMessage(content="ok")

        for role, phase in [
            ("triage", "triage"),
            ("planner", "plan"),
            ("worker", "worker_step"),
            ("supervisor", "local_read"),
        ]:
            with self.subTest(role=role):
                self.b.role = role
                self.b.checkpoint_required = False
                required = SystemMessage(content="keep-exact:" + "x" * 26000)
                with bind(self.b):
                    await invoke_model(
                        RunnableLambda(respond),
                        [required, HumanMessage(content="original request")],
                        phase=phase,
                    )
                manifest = await self.b.repository.read(
                    self.b.incident_id, self.b.last_context_id, "context"
                )
                self.assertTrue(manifest["soft_target_exceeded"])
                self.assertTrue(manifest["soft_target_expanded"])
                self.assertLessEqual(manifest["request_chars_v2"], 48000)
                self.assertFalse(
                    self.b.checkpoint_required, "soft goal alone must not prohibit reads"
                )
                self.assertIn(required.content, [message.content for message in sent[-1]])
                if role in ("worker", "supervisor"):
                    from test_investigation_contracts import workbench

                    notice = workbench(sent[-1])["budget_notice"]
                    self.assertFalse(notice["blocks_necessary_reads"])
        self.assertEqual(len(sent), 4)

    async def test_hard_ceiling_refuses_before_model_and_preserves_candidate(self):
        calls = []

        async def respond(messages):
            calls.append(messages)
            return AIMessage(content="unexpected")

        with bind(self.b), self.assertRaisesRegex(WorkspaceError, "context_unsatisfiable"):
            await invoke_model(
                RunnableLambda(respond), [SystemMessage(content="protected:" + "x" * 50000)]
            )
        self.assertEqual(calls, [])
        contexts = await self.b.repository.all(self.b.incident_id, "context")
        self.assertEqual(contexts[-1]["disposition"], "rejected_before_send")
        self.assertTrue(contexts[-1]["engineering_ceiling_exceeded"])
        self.assertTrue(contexts[-1]["candidate_messages_artifact_id"])

    async def test_disabling_soft_reductions_does_not_disable_hard_ceiling(self):
        with (
            patch.object(settings, "CONTEXT_BUDGET_ENABLED", False),
            patch.object(settings, "CONTEXT_LIFECYCLE_MODE", "observe"),
            self.assertRaisesRegex(WorkspaceError, "engineering_ceiling_exceeded"),
        ):
            await prepare(self.b, [SystemMessage(content="x" * 50000)])

    async def test_read_allowance_uses_hard_headroom_after_soft_goal(self):
        visible, manifest = await prepare(
            self.b, [SystemMessage(content="x" * 26000)], "worker_step"
        )
        self.b.last_context_id = manifest["id"]
        allowed = await next_page_budget(
            self.b, [{"id": "next", "name": "read_evidence", "args": {}}]
        )
        self.assertGreaterEqual(allowed, 512)
        self.assertFalse(self.b.checkpoint_required)

    async def test_strict_rollback_still_refuses_at_original_limit(self):
        with (
            patch.object(settings, "CONTEXT_BUDGET_POLICY", "strict"),
            self.assertRaisesRegex(WorkspaceError, "context_unsatisfiable"),
        ):
            await prepare(self.b, [SystemMessage(content="x" * 26000)])

    async def test_structured_roles_keep_fact_cards_when_they_have_no_read_loop(self):
        from test_investigation_contracts import register_pod, workbench
        from workspace.queries import save_query

        await register_pod(self.b, "iam", "app")
        for i in range(6):
            await save_query(
                self.b,
                "evidence_card_" + str(i),
                "get_pod_network_summary",
                {"cluster": "c", "pod_uid": "iam-uid"},
                {"summary": {"rst_count": i}},
                "now",
                1,
            )
        for role in ("triage", "planner"):
            self.b.role = role
            with patch.object(settings, "CONTEXT_MAX_CHARS", 1000):
                visible, manifest = await prepare(
                    self.b, [SystemMessage(content="Keep facts")], role
                )
            cards = workbench(visible)["evidence"]
            self.assertEqual(len(cards), 6)
            self.assertEqual({row["observations"]["rst_count"] for row in cards}, set(range(6)))
            self.assertTrue(manifest["soft_target_expanded"])

    async def test_shared_observation_is_injected_once_with_both_question_links(self):
        from test_investigation_contracts import register_pod, workbench
        from workspace import investigations, observations
        from workspace.queries import save_query

        await register_pod(self.b, "iam", "app")
        await save_query(
            self.b,
            "evidence_shared",
            "get_pod_network_summary",
            {"cluster": "c", "pod_uid": "iam-uid"},
            {"summary": {"rst_count": 3}},
            "now",
            1,
        )

        async def respond(messages):
            return AIMessage(content="ok")

        with bind(self.b):
            await invoke_model(RunnableLambda(respond), [SystemMessage(content="Read the counter")])
        qs = [
            await investigations.save_question(self.b, text=text)
            for text in ("Question A", "Question B")
        ]
        result = await observations.verify_observation(
            self.b,
            {
                "source_ref": {
                    "evidence_id": "evidence_shared",
                    "json_pointer": "/summary/rst_count",
                },
                "expected_value": 3,
            },
            question_ids=[q["id"] for q in qs],
        )
        await self.b.repository.save(self.b.incident_id, "observation", result["accepted"])
        self.b.role = "planner"
        visible, manifest = await prepare(self.b, [SystemMessage(content="Plan")], "plan")
        shown = workbench(visible)["investigation"]["observations"]
        self.assertEqual(len(shown), 1)
        self.assertEqual(set(shown[0]["question_ids"]), {q["id"] for q in qs})

    async def test_stage_tool_exposes_the_real_material_fields_and_roundtrips_them(self):
        from pydantic import ValidationError
        from workspace.stage_tools import commit_investigation_stage
        from workspace import investigations
        from workspace.queries import save_query
        from test_investigation_contracts import register_pod
        from context.request_budget import tool_schemas

        schema_text = str(tool_schemas([commit_investigation_stage]))
        self.assertIn("source_id", schema_text)
        self.assertIn("disposition", schema_text)
        self.assertIn("unanalyzed", schema_text)
        await register_pod(self.b, "iam", "app")
        await save_query(
            self.b,
            "evidence_material",
            "get_pod_network_summary",
            {"cluster": "c", "pod_uid": "iam-uid"},
            {"summary": {"rst_count": 3}},
            "now",
            1,
        )
        question = await investigations.save_question(self.b, text="Request correlation?")
        args = {
            "client_commit_key": "material-shape",
            "stage_goal": "Retain active material",
            "completion": "partial",
            "question_ids": [question["id"]],
            "material_dispositions": [{"source_id": "evidence_material", "disposition": "active"}],
        }
        with bind(self.b):
            result = await commit_investigation_stage.ainvoke(args)
        self.assertEqual(result["status"], "accepted", result)
        commits = await self.b.repository.all(self.b.incident_id, "stage_commit")
        self.assertEqual(commits[-1]["material_dispositions"][0]["source_id"], "evidence_material")
        with bind(self.b):
            refused = await commit_investigation_stage.ainvoke(
                {
                    **args,
                    "client_commit_key": "bad-shape",
                    "material_dispositions": [
                        {"evidence_id": "evidence_material", "status": "active"}
                    ],
                }
            )
        self.assertEqual(refused["status"], "rejected")
        self.assertEqual(refused["reason"], "stage_repair_not_applicable")


class SoftParentFlow(ActiveWorkspaceCase):
    async def test_four_role_parent_contract_works_with_default_soft_policy(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario()
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(
            {c["role"] for c in rows["context"]}, {"triage", "planner", "worker", "supervisor"}
        )
        self.assertTrue(all(c["context_policy"] == "unified_sre" for c in rows["context"]))
        self.assertTrue(rows["question"])
        self.assertTrue(rows["task"][0]["question_ids"])
        self.assertTrue(rows["observation"])
        self.assertTrue(rows["stage_commit"])
        self.assertTrue(rows["report_packet"])
        self.assertFalse(state["final_report"]["report_errors"])
