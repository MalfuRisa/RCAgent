"""Real contracts and unified role loops, with deterministic public responses."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from config import settings
from context.exact_representation import expand
from local_testing.skill_fixture import seed
from local_testing.unified_scenarios import ScenarioModel, run_scenario
from test_unified_investigation import payload
from models.investigation import TaskSubmission
from workspace.runtime import bind, invoke_model
from workspace.repository import WorkspaceError
from workspace.skill_knowledge import read_skill, view, hydrate, business_steps
from workspace.read_tools import read_evidence
from workspace.observation_selection import register_observations
from workspace.investigations import active_checkpoint
from agents.investigation_worker import worker
from agents.investigation_supervisor import supervisor


def call(name, args, index=0):
    return AIMessage(
        content="", tool_calls=[{"id": f"fixture-{index}", "name": name, "args": args}]
    )


class SkillContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state, self.b, self.catalog, self.data = await seed(self.temp.name)
        self.evidence_id = self.data["evidence_id"]
        for key, value in (
            ("MODEL_MIN_INTERVAL_SECONDS", 0),
            ("INVESTIGATION_SKILLS_ENABLED", True),
        ):
            p = patch.object(settings, key, value)
            p.start()
            self.addCleanup(p.stop)

    async def test_E1_original_draft_rejected_then_candidates_and_explicit_null_register(
        self,
    ):
        with self.assertRaises(ValueError):
            TaskSubmission.model_validate(self.data["rejected_task_submission"])
        seen = []
        with bind(self.b):
            await read_skill.ainvoke({"name": "submitting-observations"})
            await read_evidence.ainvoke(
                {"evidence_id": self.evidence_id, "field": "explicit_null"}
            )
            await invoke_model(
                RunnableLambda(lambda m: seen.append(m) or AIMessage(content="draft")),
                [],
                phase="worker_step",
            )
            p = expand(payload(seen[-1]))
            offered = p["current_candidates"]
            self.assertTrue(offered)
            opening = await active_checkpoint(self.b, "worker")
            ids = await register_observations(
                self.b,
                [offered[0]["candidate_id"]],
                [],
                [q["id"] for q in p["investigation"]["questions"]],
                opening,
                ["root cause unknown"],
            )
            self.assertTrue(ids)
            opening = await active_checkpoint(self.b, "worker")
            ids = await register_observations(
                self.b,
                [],
                [
                    {
                        "local_id": "null",
                        "source_ref": {
                            "evidence_id": self.evidence_id,
                            "json_pointer": "/explicit_null",
                        },
                        "expected_value": None,
                    }
                ],
                [q["id"] for q in p["investigation"]["questions"]],
                opening,
                ["synthetic null"],
            )
            self.assertTrue(ids)

    async def test_E2_unread_batch_and_foreign_read_remain_rejected_after_guidance(
        self,
    ):
        with bind(self.b):
            await read_evidence.ainvoke(
                {"evidence_id": self.evidence_id, "field": "data.list", "limit": 1}
            )
            self.b.role = "supervisor"
            await read_evidence.ainvoke(
                {"evidence_id": self.evidence_id, "field": "meta"}
            )
            self.b.role = "worker"
            before = await self.b.repository.all(self.b.incident_id, "read_receipt")
            await read_skill.ainvoke(
                {
                    "name": "submitting-observations",
                    "resource": "references/recovery-examples.md",
                }
            )
            self.assertEqual(
                before, await self.b.repository.all(self.b.incident_id, "read_receipt")
            )
            qids = [
                q["id"]
                for q in await self.b.repository.all(self.b.incident_id, "question")
            ]
            for pointer, value in (
                ("/data/list", self.data["raw"]["data"]["list"]),
                ("/meta", self.data["raw"]["meta"]),
                ("/absent", None),
            ):
                with self.subTest(pointer=pointer), self.assertRaises(WorkspaceError):
                    await register_observations(
                        self.b,
                        [],
                        [
                            {
                                "local_id": "unread",
                                "source_ref": {
                                    "evidence_id": self.evidence_id,
                                    "json_pointer": pointer,
                                },
                                "expected_value": value,
                            }
                        ],
                        qids,
                        await active_checkpoint(self.b, "worker"),
                        ["unread"],
                    )
            self.assertEqual(
                await self.b.repository.all(self.b.incident_id, "observation"), []
            )

    async def test_pointer_document_recovery_uses_exact_raw_and_preserves_atomic_rejection(
        self,
    ):
        with bind(self.b):
            await read_evidence.ainvoke(
                {"evidence_id": self.evidence_id, "field": "data.total"}
            )
            await invoke_model(
                RunnableLambda(lambda _: AIMessage(content="reviewed exact source")),
                [],
                phase="worker_step",
            )
            qids = [
                q["id"]
                for q in await self.b.repository.all(self.b.incident_id, "question")
            ]
            proposal = {
                "local_id": "total",
                "source_document": "evidence",
                "source_ref": {
                    "evidence_id": self.evidence_id,
                    "json_pointer": "/data/total",
                },
                "expected_value": self.data["raw"]["data"]["total"],
            }
            opening = await active_checkpoint(self.b, "worker")
            with self.assertRaisesRegex(WorkspaceError, "pointer_not_found"):
                await register_observations(
                    self.b, [], [proposal], qids, opening, ["archive only"]
                )
            self.assertEqual(
                await self.b.repository.all(self.b.incident_id, "observation"), []
            )
            proposal["source_document"] = "artifact"
            ids = await register_observations(
                self.b, [], [proposal], qids, opening, ["archive only"]
            )
            self.assertEqual(len(ids), 1)

    async def test_registry_paths_versions_missing_and_duplicate_body(self):
        with bind(self.b):
            initial = await view(self.b)
            self.assertFalse(initial["loaded"])
            for name, resource in (
                ("unknown", "SKILL.md"),
                ("submitting-observations", "../.env"),
                ("submitting-observations", "/etc/passwd"),
            ):
                result = await read_skill.ainvoke({"name": name, "resource": resource})
                self.assertEqual(result["status"], "rejected")
            for _ in range(2):
                await read_skill.ainvoke({"name": "submitting-observations"})
            current = await view(self.b)
            self.assertEqual(len(current["loaded"]), 1)
            self.assertNotIn(
                "recovery-examples.md", [r["resource"] for r in current["loaded"]]
            )
            with tempfile.TemporaryDirectory() as root:
                directory = Path(root) / "submitting-observations"
                directory.mkdir()
                entry = directory / "SKILL.md"
                entry.write_text(
                    "---\nname: submitting-observations\ndescription: fixture\n---\nv1"
                )
                with patch("workspace.skill_knowledge.ROOT", Path(root)):
                    v1 = await read_skill.ainvoke({"name": "submitting-observations"})
                    entry.write_text(entry.read_text().replace("v1", "v2"))
                    v2 = await read_skill.ainvoke({"name": "submitting-observations"})
                    self.assertNotEqual(v1["content_hash"], v2["content_hash"])
                    entry.write_text(
                        "---\nname: submitting-observations\ndescription: no closing header"
                    )
                    self.assertEqual(
                        (await read_skill.ainvoke({"name": "submitting-observations"}))[
                            "status"
                        ],
                        "rejected",
                    )
                    entry.unlink()
                    self.assertEqual(
                        (await read_skill.ainvoke({"name": "submitting-observations"}))[
                            "status"
                        ],
                        "rejected",
                    )
                    outside = Path(root).parent / (Path(root).name + "-secret")
                    outside.write_text("SECRET")
                    self.addCleanup(outside.unlink)
                    entry.symlink_to(outside)
                    self.assertEqual(
                        (await read_skill.ainvoke({"name": "submitting-observations"}))[
                            "status"
                        ],
                        "rejected",
                    )
            self.assertEqual(
                await self.b.repository.all(self.b.incident_id, "observation"), []
            )

    async def test_guidance_survives_recovery_history_and_changed_segment_without_duplicate_bodies(
        self,
    ):
        from workspace.skill_knowledge import credit_round, records
        from workspace.stage_tools import (
            new_segment_after_commit,
            commit_investigation_stage,
        )
        from workspace.investigations import active_checkpoint
        from agents.investigation_rules import RULES
        from langchain_core.messages import ToolMessage

        messages = [RULES]
        seen = []
        with bind(self.b):
            for index in range(3):
                result = await read_skill.ainvoke({"name": "submitting-observations"})
                request = call("read_skill", {"name": "submitting-observations"}, index)
                messages.extend(
                    [
                        request,
                        ToolMessage(
                            name="read_skill",
                            tool_call_id=f"fixture-{index}",
                            content=json.dumps(result),
                        ),
                    ]
                )
            self.b.recovery_view = True
            await invoke_model(
                RunnableLambda(lambda m: seen.append(m) or AIMessage(content="draft")),
                messages,
                phase="worker_step",
            )
            shown = expand(payload(seen[-1]))
            body = shown["skill_knowledge"]["loaded"][0]["content"]
            self.assertEqual(
                len(
                    {
                        (r["name"], r["resource"])
                        for r in shown["skill_knowledge"]["loaded"]
                    }
                ),
                len(shown["skill_knowledge"]["loaded"]),
            )
            self.assertTrue(
                any(
                    r["name"] == "continuing-investigation"
                    for r in shown["skill_knowledge"]["loaded"]
                )
            )
            self.assertTrue(
                all(
                    "content" not in json.loads(m.content)
                    for m in seen[-1]
                    if m.type == "tool" and m.name == "read_skill"
                )
            )
            qids = [q["id"] for q in shown["investigation"]["questions"]]
            cp = await active_checkpoint(self.b, "worker") or {}
            commit = await commit_investigation_stage.ainvoke(
                {
                    "client_commit_key": "knowledge-continuation",
                    "stage_goal": "retain gaps",
                    "question_ids": qids,
                    "expected_checkpoint_id": cp.get("id"),
                    "expected_revision": cp.get("revision"),
                    "open_items": ["unknown root cause"],
                    "next_focus": ["finish"],
                    "completion": "partial",
                }
            )
            self.assertEqual(commit["status"], "accepted", commit)
            segment = await new_segment_after_commit(
                self.b,
                commit["checkpoint_id"],
                RULES,
                task_id=self.b.task_id,
                messages=messages,
            )
            await invoke_model(
                RunnableLambda(lambda m: seen.append(m) or AIMessage(content="draft")),
                segment,
                phase="worker_step",
            )
            self.assertEqual(
                len(
                    {
                        (r["name"], r["resource"])
                        for r in expand(payload(seen[-1]))["skill_knowledge"]["loaded"]
                    }
                ),
                len(expand(payload(seen[-1]))["skill_knowledge"]["loaded"]),
            )
            self.b.role = "supervisor"
            self.assertEqual((await view(self.b))["loaded"], [])

    async def test_explicit_total_limit_and_stops_do_not_receive_extra_requests(self):
        from workspace.skill_knowledge import credit_round
        from workspace.model_requests import ModelRequestError

        with bind(self.b):
            self.b.model_steps = 1
            await credit_round(self.b, [{"id": "read", "name": "read_skill"}])
            self.b.local_model_limit = 1
            with self.assertRaisesRegex(WorkspaceError, "step budget"):
                await invoke_model(
                    RunnableLambda(lambda _: self.fail("past total cap")),
                    [],
                    phase="worker_step",
                )
            self.b.local_model_limit = None
            self.b.deadline = 0
            with self.assertRaises(TimeoutError):
                await invoke_model(
                    RunnableLambda(lambda _: self.fail("past deadline")),
                    [],
                    phase="worker_step",
                )
        self.assertEqual(
            await self.b.repository.all(self.b.incident_id, "model_attempt"), []
        )

    async def test_E3_final_and_checkpoint_reader_preserve_submit_and_actual_count(
        self,
    ):
        class Ending:
            def __init__(inner):
                inner.index = 0
                inner.allowed = []

            def bind_tools(inner, tools, **kwargs):
                inner.allowed.append([t.name for t in tools])

                async def answer(messages):
                    i = inner.index
                    inner.index += 1
                    if i == 0:
                        return call(
                            "query_diagnostics",
                            {"name": "forbidden", "arguments": {}},
                            i,
                        )
                    if i < 4:
                        return call(
                            "read_skill",
                            {
                                "name": "submitting-observations",
                                "resource": [
                                    "SKILL.md",
                                    "references/field-and-range.md",
                                    "references/recovery-examples.md",
                                ][i - 1],
                            },
                            i,
                        )
                    return call(
                        "complete_task",
                        {
                            "interpretation": "证据不足，不能确认",
                            "limitations": ["未补查"],
                        },
                        i,
                    )

                return RunnableLambda(answer)

        self.b.model_steps = settings.WORKER_MAX_MODEL_STEPS - 2
        self.b.checkpoint_required = True
        model = Ending()
        with bind(self.b):
            result = await worker(self.b, model, self.state, self.catalog)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.b.model_steps, settings.WORKER_MAX_MODEL_STEPS + 3)
        self.assertEqual(self.b.knowledge_rounds, 3)
        self.assertEqual(business_steps(self.b), settings.WORKER_MAX_MODEL_STEPS)
        self.assertNotIn("read_skill", model.allowed[-1])
        self.assertIn("complete_task", model.allowed[-1])
        self.assertIn("record_observations", model.allowed[-1])
        self.assertIn("read_evidence", model.allowed[-1])
        self.assertTrue(all("query_diagnostics" not in row for row in model.allowed))
        actions = await self.b.repository.all(self.b.incident_id, "model_action")
        rejected = next(
            a["outcome"] for a in actions if a["operation"] == "query_diagnostics"
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["phase"], "consolidating")
        self.assertIn("complete_task", rejected["allowed_tools"])
        self.assertNotIn("query_diagnostics", rejected["allowed_tools"])
        self.assertNotIn("skill_hint", rejected)
        self.assertIn("allowed_tools", rejected["next_action"])
        self.assertNotIn("submit_report", rejected["next_action"])
        self.assertEqual(
            len(await self.b.repository.all(self.b.incident_id, "evidence")), 1
        )
        self.assertEqual(
            len(await self.b.repository.all(self.b.incident_id, "model_response")), 5
        )
        self.b.model_steps = self.b.knowledge_rounds = 0
        await hydrate(self.b)
        self.assertEqual(self.b.model_steps, settings.WORKER_MAX_MODEL_STEPS + 3)

    async def test_E4_zero_observation_unresolved_report_through_real_packet_selection(
        self,
    ):
        self.b.role, self.b.task_id, self.b.attempt_id = "supervisor", None, None

        class Unresolved(ScenarioModel):
            def __init__(inner):
                super().__init__([])
                inner.step = 0
                inner.packet_knowledge = None

            def bind_tools(inner, tools, **kwargs):
                async def answer(messages):
                    p = expand(payload(messages))
                    inner.step += 1
                    if inner.step == 1:
                        return call("read_skill", {"name": "finalizing-investigation"})
                    return call(
                        "submit_report",
                        {
                            "unresolved_reasons": {
                                q["id"]: "Worker提交失败，无正式观测"
                                for q in p["investigation"]["questions"]
                            },
                            "limitations": ["原文不是正式观测，根因未知"],
                            "next_steps": ["补充原值核验"],
                        },
                    )

                return RunnableLambda(answer)

            def with_structured_output(inner, schema, **kwargs):
                base = super().with_structured_output(schema, **kwargs)

                async def answer(messages):
                    inner.packet_knowledge = expand(payload(messages))[
                        "skill_knowledge"
                    ]
                    return await base.ainvoke(messages)

                return RunnableLambda(answer)

        model = Unresolved()
        with bind(self.b):
            result = await supervisor(self.b, model, {}, self.catalog)
        self.assertEqual(result["execution_status"], "completed")
        self.assertTrue(result["report_packet_id"])
        self.assertEqual(
            await self.b.repository.all(self.b.incident_id, "observation"), []
        )
        proposals = await self.b.repository.all(self.b.incident_id, "report_proposal")
        self.assertEqual(proposals[-1]["errors"], [])
        self.assertTrue(
            any(
                r["name"] == "finalizing-investigation"
                for r in model.packet_knowledge["loaded"]
            )
        )
        self.assertFalse(model.packet_knowledge["reader_available"])
        self.assertEqual(model.packet_knowledge["directory"], [])


class SkillGraph(unittest.IsolatedAsyncioTestCase):
    async def test_paid_entry_fixtures_can_finish_without_version_or_target_conflicts(
        self,
    ):
        from local_testing.evaluate_investigation_skills import run_case

        class FinishArchive(ScenarioModel):
            def __init__(inner):
                super().__init__([])

            def bind_tools(inner, tools, **kwargs):
                async def answer(messages):
                    from workspace.runtime import current

                    p = expand(payload(messages))
                    if current.get().role == "supervisor":
                        return call(
                            "submit_report",
                            {
                                "unresolved_reasons": {
                                    q["id"]: "insufficient evidence"
                                    for q in p["investigation"]["questions"]
                                },
                                "limitations": ["worker failed; no root cause"],
                            },
                        )
                    if not p["investigation"]["observations"]:
                        return call(
                            "record_observations",
                            {
                                "ref_ids": [p["current_candidates"][0]["candidate_id"]],
                                "question_ids": [
                                    q["id"] for q in p["investigation"]["questions"]
                                ],
                            },
                        )
                    return call(
                        "complete_task",
                        {
                            "supporting_observation_ids": [
                                o["id"] for o in p["investigation"]["observations"]
                            ],
                            "interpretation": "archived observed value; unknown causality",
                            "limitations": ["archive only"],
                        },
                    )

                return RunnableLambda(answer)

        with (
            tempfile.TemporaryDirectory() as root,
            patch.object(settings, "MODEL_MIN_INTERVAL_SECONDS", 0),
        ):
            for case in ("E1", "E3", "E4"):
                with patch.object(
                    settings, "get_chat_model", return_value=FinishArchive()
                ):
                    self.assertIsNone(await run_case(root, case, False))
                outcome = json.loads(
                    (Path(root) / (case + "-control") / "outcome.json").read_text()
                )
                self.assertTrue(outcome["goal_met"], outcome)
                if case == "E4":
                    self.assertEqual(outcome["observation_ids"], [])

    async def test_E5_same_graph_domains_and_history_keep_guidance_until_selection(
        self,
    ):
        class Guided(ScenarioModel):
            def __init__(inner):
                super().__init__([{"name": "network_summary", "arguments": {}}])
                inner.loaded = set()
                inner.final_loaded = []
                inner.worker_step = 0

            def bind_tools(inner, tools, **kwargs):
                base = super().bind_tools(tools, **kwargs)

                async def answer(messages):
                    from workspace.runtime import current

                    b = current.get()
                    key = (b.role, b.task_id)
                    if key not in inner.loaded:
                        inner.loaded.add(key)
                        return call(
                            "read_skill",
                            {
                                "name": "finalizing-investigation"
                                if b.role == "supervisor"
                                else "submitting-observations"
                            },
                        )
                    if b.role == "worker":
                        p = expand(payload(messages))
                        index = inner.worker_step
                        inner.worker_step += 1
                        if index == 0:
                            return call(
                                "query_diagnostics",
                                {"name": "network_summary", "arguments": {}},
                                index + 10,
                            )
                        if index in (1, 2):
                            return call(
                                "read_evidence",
                                {
                                    "evidence_id": p["evidence"][0]["evidence_id"],
                                    "field": "supporting_note"
                                    if index == 1
                                    else "opposing_note",
                                },
                                index + 10,
                            )
                        if index == 3:
                            return call(
                                "record_observations",
                                {
                                    "ref_ids": [
                                        c["candidate_id"]
                                        for c in p["current_candidates"]
                                        if c["source_ref"]["json_pointer"].startswith(
                                            "/summary/"
                                        )
                                    ],
                                    "question_ids": [
                                        q["id"] for q in p["investigation"]["questions"]
                                    ],
                                },
                                index + 10,
                            )
                        ids = [o["id"] for o in p["investigation"]["observations"]]
                        if index == 4:
                            return call(
                                "commit_investigation_stage",
                                {
                                    "question_ids": [
                                        q["id"] for q in p["investigation"]["questions"]
                                    ],
                                    "observation_ids": ids[:1],
                                    "opposing_observation_ids": ids[1:],
                                    "interpretation": "支持与反证并存",
                                    "limitations": ["causality unknown"],
                                    "open_items": ["uncovered tail and causality"],
                                    "next_focus": ["complete task"],
                                },
                                index + 10,
                            )
                        return call(
                            "complete_task",
                            {
                                "interpretation": "支持和反证并存，根因未知",
                                "supporting_observation_ids": ids[:1],
                                "opposing_observation_ids": ids[1:],
                                "limitations": ["未覆盖其他窗口和因果"],
                            },
                            index + 10,
                        )
                    return await base.ainvoke(messages)

                return RunnableLambda(answer)

            def with_structured_output(inner, schema, **kwargs):
                base = super().with_structured_output(schema, **kwargs)

                async def answer(messages):
                    if schema.__name__ == "PacketSelection":
                        inner.final_loaded = expand(payload(messages))[
                            "skill_knowledge"
                        ]["loaded"]
                    return await base.ainvoke(messages)

                return RunnableLambda(answer)

        for domain in ("cpu", "network", "storage"):
            model = Guided()
            state, rows, *_ = await run_scenario(
                model=model,
                raw={
                    "summary": {domain: 7, "opposing": 0},
                    "supporting_note": "UNIQUE_SUPPORT" + "s" * 6000,
                    "opposing_note": "UNIQUE_OPPOSITION" + "o" * 6000,
                    "missing": None,
                },
            )
            self.assertFalse(state["final_report"]["fallback"])
            self.assertTrue(rows["observation"])
            self.assertTrue(model.final_loaded)
            self.assertTrue(rows["report_proposal"])
            self.assertTrue(
                any(r["opposing_observation_ids"] for r in rows["judgment"])
            )
            self.assertTrue(any(c["history_replacements"] for c in rows["context"]))
