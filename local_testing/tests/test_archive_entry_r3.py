"""Calibrate the paid live-03 replay entry with deterministic model choices first."""

import json
import tempfile
import unittest
from pathlib import Path
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from workspace.runtime import current
from local_testing.evaluate_investigation_skills import run_case
from test_unified_investigation import InvestigationModel, payload


class ArchiveEntryR3(unittest.IsolatedAsyncioTestCase):
    async def test_same_entry_worker_refs_and_model_selected_report_use_real_archive(
        self,
    ):
        from config import settings

        source = (
            settings.INVESTIGATION_HISTORY_ROOT
            / "观测提交与调查收尾Skill-2026-10-29/R2-2026-10-30/live-03"
        )
        if not (source / "mcp-calls.json").exists():
            self.skipTest("Explicit live-03 archive not mounted on this machine")

        class Model(InvestigationModel):
            def bind_tools(inner, tools, **kwargs):
                ordinary = super().bind_tools(tools, **kwargs)

                async def respond(messages):
                    b = current.get()
                    view = payload(messages)
                    if b.role == "worker" and not view["investigation"]["observations"]:
                        refs = [
                            r["candidate_id"]
                            for r in view["current_candidates"]
                            if r["source_ref"]["json_pointer"].endswith("/latency_ns")
                        ]
                        if refs:
                            return AIMessage(
                                content="",
                                tool_calls=[
                                    {
                                        "name": "record_observations",
                                        "id": "archive-register",
                                        "args": {
                                            "ref_ids": refs[:2],
                                            "question_ids": [
                                                q["id"]
                                                for q in view["investigation"][
                                                    "questions"
                                                ]
                                            ],
                                            "limitations": [
                                                "Archived events only; no current health or causality"
                                            ],
                                        },
                                    }
                                ],
                            )
                    return await ordinary.ainvoke(messages)

                return RunnableLambda(respond)

        with tempfile.TemporaryDirectory() as root:
            error = await run_case(
                Path(root),
                "R3",
                True,
                model=Model(
                    [
                        {
                            "name": "insights_list_cpu_events",
                            "arguments": {"event_type": "runqlat"},
                        }
                    ]
                ),
            )
            self.assertIsNone(error)
            output = Path(root) / "R3-skill"
            outcome = json.loads((output / "outcome.json").read_text())
            self.assertTrue(outcome["worker_completed"])
            self.assertTrue(outcome["selected_worker_observation_ids"])
            records = json.loads((output / "records.json").read_text())
            self.assertTrue(
                all(e["freshness"] == "archived_fixture" for e in records["evidence"])
            )
            self.assertTrue(
                any(o["field"].endswith("latency_ns") for o in records["observation"])
            )
            self.assertEqual(outcome["remote_mcp_requests"], 0)
            self.assertFalse(outcome["actual_model"])


if __name__ == "__main__":
    unittest.main()
