"""Round-2 role-input contract: what each role's message and workbench contain.

These tests pin the §7.4 de-duplication: the program's own prompts stop pasting a
second full copy of facts that are already archived, while the original question
and the workbench references remain available.
"""

import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from workspace.queries import save_query
from workspace.tasks import finish
from workspace.runtime import bind, invoke_model
from test_workspace import WorkspaceCase


class RoleInputTests(WorkspaceCase):
    async def test_triage_digest_refers_to_archived_evidence(self):
        from agents.triage_agent import _triage_digest

        identity = await self.locate()
        raw = {
            "status": "ok",
            "summary": {"tcp_errors": 7, "important_marker": "OBSERVED_FAILURE"},
            "samples": [{"detail": "x" * 30000}],
            "meta": {
                "data_gaps": [{"reason": "limited"}],
                "time_range": {"start_time": "S", "end_time": "E"},
            },
        }
        await save_query(
            self.b,
            "net-1",
            "get_pod_network_summary",
            {"cluster": "c", "pod_uid": identity["resource"]["uid"]},
            raw,
            "now",
            1,
        )
        self.b.role = "triage"
        with bind(self.b):
            digest = await _triage_digest({})
        rendered = digest["metrics"]
        self.assertIn("net-1", rendered)
        self.assertIn("OBSERVED_FAILURE", rendered)
        self.assertIn("limited", rendered)
        self.assertNotIn(
            "x" * 1000, rendered, "the prompt must reference, not re-paste, the archived body"
        )

    async def test_triage_prompt_keeps_original_question_and_stops_on_unarchived_data(self):
        from agents.triage_agent import _triage_digest

        self.b.role = "triage"
        with bind(self.b):
            self.assertEqual(
                await _triage_digest({}),
                {},
                "no archived evidence -> caller keeps original rendering",
            )

    async def test_worker_human_message_is_id_requirement_and_budget(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario()
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        messages = next(messages for role, phase, messages in model.calls if role == "worker")
        human = json.loads(next(m.content for m in messages if m.type == "human"))
        view = json.loads(messages[0].content.split("\n", 1)[1])
        self.assertEqual(human["task"]["id"], rows["task"][0]["id"])
        self.assertTrue(human["capabilities"])
        self.assertEqual(
            view["budget"]["model_step_limit"], __import__("config").settings.WORKER_MAX_MODEL_STEPS
        )
        self.assertEqual([r["id"] for r in view["resources"]], [rows["resource"][0]["id"]])
        self.assertEqual(view["context_view_version"], "sre-investigation-v1")
        self.assertNotIn("evidence", human, "source cards live once in the workbench")

    async def test_supervisor_human_message_is_short_and_workbench_has_the_table(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario()
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        messages = next(
            messages
            for role, phase, messages in model.calls
            if role == "supervisor" and phase == "tools"
        )
        human = json.loads(next(m.content for m in messages if m.type == "human"))
        view = json.loads(messages[0].content.split("\n", 1)[1])
        self.assertEqual(human["role"], "Supervisor")
        self.assertNotIn("observations", human)
        self.assertTrue(view["investigation"]["observations"])
        self.assertEqual(view["tasks"][0]["id"], rows["task"][0]["id"])
        self.assertTrue(view["current_candidates"])


if __name__ == "__main__":
    unittest.main()
