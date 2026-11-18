"""R2: actual role tools, archive entry, final selection and gate share one contract."""

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool
from config import settings
from context.exact_representation import expand
from local_testing.skill_fixture import seed
from local_testing.evaluate_investigation_skills import run_case
from local_testing.unified_scenarios import ScenarioModel, run_scenario
from test_unified_investigation import payload
from models.investigation import ReportFinalization
from workspace.runtime import bind, invoke_model
from workspace.investigations import active_checkpoint
from workspace.observation_selection import register_observations
from workspace.report_finalization import finalize
from workspace.report_packets import expand_packet, current_packet
from workspace.repository import WorkspaceError
from workspace.read_tools import read_evidence
from workspace.windows import normalize_window
from workflow.gate import evidence_gate
from agents.investigation_supervisor import supervisor


def response(name, args, index):
    return AIMessage(
        content="", tool_calls=[{"id": "r2-" + str(index), "name": name, "args": args}]
    )


class ReportModel(ScenarioModel):
    def __init__(self, mode="empty"):
        super().__init__([])
        self.mode, self.step, self.seen, self.schemas = mode, 0, [], []

    def bind_tools(self, tools, **kwargs):
        schemas = [convert_to_openai_tool(t) for t in tools]
        self.schemas.append(schemas)
        final = next(t for t in schemas if t["function"]["name"] == "submit_report")
        properties = final["function"]["parameters"]["properties"]
        assert set(properties) == {
            "observation_ids",
            "unresolved_reasons",
            "limitations",
            "next_steps",
            "claims",
        }
        assert final["function"]["parameters"]["additionalProperties"] is False

        async def answer(messages):
            p = expand(payload(messages))
            self.seen.append(p)
            i = self.step
            self.step += 1
            qids = [q["id"] for q in p["investigation"]["questions"]]
            base = {
                "unresolved_reasons": {q: "材料不足，原因和业务影响未决" for q in qids},
                "limitations": ["未建立因果；保留原文和失败任务"],
                "next_steps": ["核验缺失数据"],
            }
            if self.mode == "register" and i == 0:
                return response(
                    "record_observations",
                    {
                        "ref_ids": [p["current_candidates"][0]["candidate_id"]],
                        "question_ids": qids,
                        "limitations": ["only archived raw"],
                    },
                    i,
                )
            if self.mode == "rejected" and i == 0:
                return response(
                    "record_observations",
                    {
                        "observations": [{"local_id": "bad", "task_id": "foreign"}],
                        "limitations": ["original malformed draft"],
                    },
                    i,
                )
            if self.mode == "old-report" and i == 0:
                return response(
                    "submit_report",
                    {**base, "observations": [{"task_id": "old draft"}]},
                    i,
                )
            if self.mode == "bad-advice" and i == 0:
                # Exact rejected future-advice wording from archive-model-01.
                return response(
                    "submit_report",
                    {
                        **base,
                        "next_steps": [
                            "在具备正式观测后，重新形成可引用的观测与判定，并据此更新本问题为已确认或排除。"
                        ],
                    },
                    i,
                )
            if self.mode == "final-stage" and i == 0:
                return response(
                    "query_diagnostics", {"name": "forbidden", "arguments": {}}, i
                )
            return response(
                "submit_report",
                {
                    **base,
                    "observation_ids": [
                        r["id"] for r in p["investigation"]["observations"]
                    ],
                },
                i,
            )

        return RunnableLambda(answer)


class FinalizationR2(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state, self.b, self.catalog, self.data = await seed(self.temp.name, "E4")
        p = patch.object(settings, "MODEL_MIN_INTERVAL_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)

    async def test_same_paid_archive_entry_real_schema_window_packet_and_gate(self):
        # The exact paid entry; only the sender differs. No hidden alternate workflow.
        with tempfile.TemporaryDirectory() as root:
            for mode in ("empty", "register", "rejected", "old-report", "bad-advice"):
                directory = Path(root) / mode
                model = ReportModel(mode)
                with patch.object(settings, "get_chat_model", return_value=model):
                    self.assertIsNone(await run_case(directory, "E4", True))
                output = directory / "E4-skill"
                outcome = json.loads((output / "outcome.json").read_text())
                self.assertTrue(outcome["goal_met"], outcome)
                rows = json.loads((output / "records.json").read_text())
                self.assertIn(
                    "未建立因果；保留原文和失败任务",
                    rows["report"][-1]["final"]["investigation_summary"],
                )
                self.assertTrue(rows["report_packet"])
                self.assertEqual(rows["report_proposal"][-1]["errors"], [])
                e = rows["evidence"][0]
                meta = self.data["raw"]["meta"]
                expected = normalize_window(
                    {
                        "start_time": meta["effective_start_time"],
                        "end_time": meta["effective_end_time"],
                    }
                )
                self.assertEqual(e["returned_time_range"], expected)
                self.assertEqual(normalize_window(e["time_range"]), expected)
                self.assertEqual(model.seen[0]["query_window"], expected)
                self.assertEqual(e["resource_ids"], ["resource_archive"])
                attempt = rows["attempt"][0]
                task = rows["task"][0]
                self.assertEqual(task["execution_status"], "failed")
                self.assertEqual(attempt["status"], "failed")
                self.assertEqual(
                    len(rows.get("observation", [])), 1 if mode == "register" else 0
                )
                if mode in ("rejected", "old-report"):
                    rejected = [
                        r
                        for r in rows["model_action"]
                        if r["outcome"].get("status") == "rejected"
                    ]
                    self.assertEqual(
                        rejected[0]["outcome"]["skill_hint"]["name"],
                        "submitting-observations",
                    )
                    self.assertTrue(rejected[0]["outcome"]["field_errors"])

    async def test_final_phase_forbids_registration_and_business_reads_without_reset(
        self,
    ):
        from workspace import investigation_phases as phases

        model = ReportModel("final-stage")
        with bind(self.b), patch.object(settings, "SUPERVISOR_EXPLORATION_STEPS", 0):
            result = await supervisor(self.b, model, {}, self.catalog)
        self.assertTrue(result["report_packet_id"])
        for schema in model.schemas:
            names = {t["function"]["name"] for t in schema}
            self.assertNotIn("query_diagnostics", names)
            self.assertNotIn("request_followup", names)
            self.assertIn("record_observations", names)
            self.assertIn("read_evidence", names)
        actions = await self.b.repository.all(self.b.incident_id, "model_action")
        denied = next(
            r["outcome"] for r in actions if r["operation"] == "query_diagnostics"
        )
        self.assertEqual(denied["phase"], "consolidating")
        self.assertTrue(denied["code"].startswith("phase_disallowed"))
        self.assertGreater(self.b.phase_budget["used"]["repairing"], 0)
        self.assertEqual(
            await self.b.repository.all(self.b.incident_id, "observation"), []
        )
        self.assertEqual(
            await self.b.repository.all(self.b.incident_id, "read_receipt"), []
        )

    async def test_claim_adapter_preserves_identity_cause_and_stale_selection_checks(
        self,
    ):
        with bind(self.b):
            seen = []
            await invoke_model(
                RunnableLambda(lambda m: seen.append(m) or AIMessage(content="review")),
                [],
                phase="local_read",
            )
            p = expand(payload(seen[-1]))
            qids = [q["id"] for q in p["investigation"]["questions"]]
            ids = await register_observations(
                self.b,
                [p["current_candidates"][0]["candidate_id"]],
                [],
                qids,
                await active_checkpoint(self.b, "supervisor"),
                ["raw only"],
            )
            base = {
                "observation_ids": ids,
                "unresolved_reasons": {q: "unknown cause" for q in qids},
                "limitations": ["archive only"],
            }
            for kind in ("hypothesis", "exclusion"):
                packet = await finalize(
                    self.b,
                    ReportFinalization(
                        **base,
                        claims=[
                            {
                                "type": kind,
                                "content": "Scoped possible interpretation",
                                "resource_id": "resource_archive",
                                "observation_ids": ids,
                            }
                        ],
                    ),
                )
                stored = await current_packet(self.b, packet["packet_id"])
                self.assertEqual(stored["statements"][-1]["claim"]["type"], kind)
                self.assertEqual(
                    stored["statements"][-1]["claim"]["supporting_evidence_ids"],
                    [self.data["evidence_id"]],
                )
            for claim in (
                {
                    "type": "causal",
                    "mechanism": "oom_termination",
                    "resource_id": "resource_archive",
                },
                {"type": "hypothesis", "resource_id": "outside"},
            ):
                with self.assertRaises((WorkspaceError, ValueError)):
                    await finalize(
                        self.b,
                        ReportFinalization(
                            **base,
                            claims=[
                                {
                                    **claim,
                                    "content": "proposed cause",
                                    "observation_ids": ids,
                                }
                            ],
                        ),
                    )
            packet = await finalize(self.b, ReportFinalization(**base))
            with self.assertRaises(WorkspaceError):
                await expand_packet(
                    self.b, packet["packet_id"], ["invalid"], "inconclusive", qids
                )
            evidence = await self.b.repository.read(
                self.b.incident_id, self.data["evidence_id"], "evidence"
            )
            await self.b.repository.save(self.b.incident_id, "evidence", evidence)
            with self.assertRaisesRegex(WorkspaceError, "stale"):
                await current_packet(self.b, packet["packet_id"])
            self.assertEqual(
                len(await self.b.repository.all(self.b.incident_id, "observation")),
                len(ids),
            )

    async def test_reference_json_examples_use_current_schema_and_real_workspace(self):
        text = Path(
            "skills/finalizing-investigation/references/review-and-selection.md"
        ).read_text()
        examples = [
            json.loads(s) for s in re.findall(r"```json\n(.*?)\n```", text, re.S)
        ]
        with bind(self.b):
            seen = []
            await invoke_model(
                RunnableLambda(lambda m: seen.append(m) or AIMessage(content="review")),
                [],
                phase="local_read",
            )
            p = expand(payload(seen[-1]))
            qids = [q["id"] for q in p["investigation"]["questions"]]
            ids = await register_observations(
                self.b,
                [p["current_candidates"][0]["candidate_id"]],
                [],
                qids,
                await active_checkpoint(self.b, "supervisor"),
                ["archive only"],
            )
            for report, selection in [
                (examples[1], examples[2]),
                (examples[3], examples[4]),
            ]:
                proposal = json.loads(
                    json.dumps(report, ensure_ascii=False)
                    .replace("<Q>", qids[0])
                    .replace("<当前问题>", qids[0])
                    .replace("<该包未决问题>", qids[0])
                    .replace("<O>", ids[0])
                )
                result = await finalize(
                    self.b, ReportFinalization.model_validate(proposal)
                )
                packet = await current_packet(self.b, result["packet_id"])
                chosen = json.loads(
                    json.dumps(selection, ensure_ascii=False)
                    .replace("<Q>", qids[0])
                    .replace("<当前问题>", qids[0])
                    .replace("<该包未决问题>", qids[0])
                    .replace("<P>", result["packet_id"])
                    .replace("<刚接受的包>", result["packet_id"])
                    .replace(
                        "<S>",
                        packet["statements"][0]["statement_id"]
                        if packet["statements"]
                        else "unused",
                    )
                )
                from agents.packet_submission import submit_packet
                from agents.investigation_rules import RULES

                class Selection:
                    def with_structured_output(inner, schema, **kwargs):
                        return RunnableLambda(lambda _: chosen)

                expanded = await submit_packet(
                    self.b, Selection(), RULES, result["packet_id"]
                )
                final = await evidence_gate(
                    {
                        **self.state,
                        **expanded,
                        "report_packet_id": result["packet_id"],
                        "supervisor_rounds": 1,
                        "execution_status": "completed",
                        "tasks_to_execute_ids": [],
                    }
                )
                self.assertFalse(final["final_report"]["fallback"])

    async def test_program_created_empty_packet_is_not_a_model_report(self):
        with bind(self.b):
            qids = [
                q["id"]
                for q in await self.b.repository.all(self.b.incident_id, "question")
            ]
            packet = await finalize(
                self.b,
                ReportFinalization(
                    unresolved_reasons={q: "unknown" for q in qids},
                    limitations=["no formal observations"],
                ),
            )
            expanded = await expand_packet(
                self.b, packet["packet_id"], [], "inconclusive", qids
            )
            result = await evidence_gate(
                {
                    **self.state,
                    **expanded,
                    "report_packet_id": packet["packet_id"],
                    "supervisor_rounds": 1,
                    "tasks_to_execute_ids": [],
                    "execution_status": "completed",
                }
            )
            self.assertTrue(result["final_report"]["fallback"])

    async def test_same_unified_graph_keeps_formal_work_with_worker_success_and_failure(
        self,
    ):
        for domain, fault in [
            ("network", None),
            ("storage", "worker_timeout_after_query"),
        ]:
            state, rows, *_ = await run_scenario(
                raw={"summary": {domain: 7}, "missing": None}, fault=fault
            )
            self.assertFalse(state["final_report"]["fallback"])
            self.assertTrue(rows["observation"])
            self.assertTrue(rows["report_packet"])
            self.assertEqual(rows["report_proposal"][-1]["errors"], [])


class FeedbackR2(unittest.TestCase):
    def test_guidance_preserves_the_rejected_operation_and_available_submission(self):
        from workflow.submission_feedback import feedback

        for operation, reason in [
            ("query_diagnostics", "bounded pages require page1..20/page_size20"),
            ("read_evidence", "json_pointer not found in this page"),
        ]:
            result = feedback(operation, {"reason": reason}, "worker_step")
            self.assertNotIn("skill_hint", result)
            self.assertNotIn("submit_report", result["next_action"])
            self.assertNotIn("record_observations", result["next_action"])
        for operation in ("complete_task", "commit_investigation_stage"):
            result = feedback(
                operation, {"reason": "expected_value mismatch"}, "worker_step"
            )
            self.assertIn(operation, result["next_action"])
            self.assertNotIn("record_observations", result["next_action"])
            self.assertEqual(result["skill_hint"]["name"], "submitting-observations")
        result = feedback(
            "query_diagnostics", {"reason": "tool not allowed"}, "finalize_report"
        )
        self.assertIn("submit_report", result["next_action"])
        self.assertEqual(result["skill_hint"]["name"], "finalizing-investigation")


if __name__ == "__main__":
    unittest.main()
