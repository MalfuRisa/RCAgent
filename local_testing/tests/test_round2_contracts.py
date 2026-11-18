"""PRD 18: assertions on returned values and the final visible input."""

import json
from pathlib import Path
import unittest
import os
from langchain_core.runnables import RunnableLambda
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from config import settings
from context.builder import prepare
from workspace.artifacts import read_artifact_raw
from workspace.queries import save_query
from workspace.read_tools import READ_TOOLS, CURRENT_TOOL_CALL
from workspace.repository import WorkspaceError, new_id
from workspace.runtime import bind
from test_workspace import WorkspaceCase
from test_acceptance_repair import workbench, sample


class Round2Tests(WorkspaceCase):
    async def test_analysis_records_repeat_reads_and_following_pages(self):
        from local_testing.run_analysis import read_usage

        eid = await self.seed({"summary": {"rst_count": 17}, "samples": [1, 2, 3]})
        await self.read("read_evidence", evidence_id=eid, field="summary")
        await self.read("read_evidence", evidence_id=eid, field="summary")
        await self.read("read_evidence", evidence_id=eid, field="samples", offset=1)
        receipts = await self.b.repository.all(self.b.incident_id, "read_receipt")
        report = read_usage({"read_receipt": receipts}, self.root)
        self.assertEqual(report["rows"][0]["new_payload_values"], 1)
        self.assertEqual(report["rows"][1]["repeated_payload_values"], 1)
        self.assertTrue(report["rows"][2]["continued_page"])
        self.assertIsNone(report["rows"][0]["retained_in_later_input"])

    async def test_changed_source_and_broken_result_are_explicit_at_submission(self):
        self.b.role = "supervisor"
        eid = await self.seed({"summary": {"rst_count": 917827}})
        page, _ = await self.read("read_evidence", evidence_id=eid, field="summary.rst_count")
        row = await self.b.repository.read(self.b.incident_id, eid, "evidence")
        row["observations"]["summary"]["rst_count"] = 0
        await self.b.repository.save(self.b.incident_id, "evidence", row, row["revision"])
        visible, _ = await prepare(self.b, [], "initial_submission")
        detail = workbench(visible)["verified_read_details"][0]
        self.assertIn("917827", json.dumps(detail["content"]))
        self.assertIn("source_changed", detail)
        receipt = await self.receipt(page)
        artifact = await self.b.repository.read(
            self.b.incident_id, receipt["result_artifact_id"], "artifact"
        )
        (self.root / artifact["storage_reference"]).write_text("corrupt")
        visible, manifest = await prepare(self.b, [], "initial_submission")
        self.assertIn("storage_reference_error", workbench(visible)["verified_read_details"][0])
        self.assertEqual(manifest["read_reference_errors"][0]["receipt_id"], receipt["id"])

    async def test_resource_assessment_and_artifact_envelopes(self):
        from workspace.tasks import finish
        from workspace.artifacts import write_artifact

        task = await self.begin_task()
        assessment = await finish(
            self.b,
            {"diagnosis": "Unresolved", "reasoning": "y" * 6000, "gaps": ["x" * 6000]},
            "finished",
        )
        artifact = await write_artifact(self.b, {"long_field": "x" * 6000})
        artifact["note"] = "x" * 6000
        await self.b.repository.save(self.b.incident_id, "artifact", artifact)
        resource = await self.b.repository.read(self.b.incident_id, task.resource_id, "resource")
        resource["note"] = "x" * 6000
        await self.b.repository.save(self.b.incident_id, "resource", resource, resource["revision"])
        for cap in (512, 4000, 8000):
            for name, args in [
                ("get_resource", {"resource_id": task.resource_id}),
                ("read_assessment", {"assessment_id": assessment["id"]}),
                ("read_artifact", {"artifact_id": artifact["id"], "view": "metadata"}),
            ]:
                page, _ = await self.read(name, max_chars=cap, **args)
                self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), cap)
                receipt = await self.receipt(page)
                self.assertEqual(
                    await read_artifact_raw(self.b, receipt["result_artifact_id"]), page
                )
                if page.get("truncated"):
                    self.assertFalse(receipt["complete_paths"])

    async def seed(self, raw, tool="get_pod_network_summary", arguments=None):
        eid = new_id("evidence")
        await save_query(self.b, eid, tool, arguments or {}, raw, "now", 1, "ok", new_id("call"))
        return eid

    async def read(self, name, **args):
        call = new_id("call")
        token = CURRENT_TOOL_CALL.set(call)
        try:
            with bind(self.b):
                page = await next(t for t in READ_TOOLS if t.name == name).ainvoke(args)
        finally:
            CURRENT_TOOL_CALL.reset(token)
        self.read_results = getattr(self, "read_results", []) + [
            {
                "tool": name,
                "arguments": args,
                "returned": page,
                "serialized_chars": len(json.dumps(page, ensure_ascii=False)),
            }
        ]
        dump_result(self._testMethodName + "-reads", self.read_results)
        return page, [
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call}]),
            ToolMessage(content=json.dumps(page, ensure_ascii=False), name=name, tool_call_id=call),
        ]

    async def receipt(self, page):
        return await self.b.repository.read(
            self.b.incident_id, page["read_receipt_id"], "read_receipt"
        )

    async def test_all_default_and_metadata_exits_are_bounded(self):
        task = await self.begin_task()
        record = await self.b.repository.read(self.b.incident_id, task.task_id, "task")
        record["investigation_goal"] = "x" * 6000
        await self.b.repository.save(self.b.incident_id, "task", record, record["revision"])
        eid = await self.seed({"summary": {"rst_count": 1}}, arguments={"padding": "x" * 6000})
        for cap in (512, 4000, 8000):
            for name, args in [
                ("read_task", {"task_id": task.task_id}),
                ("read_evidence", {"evidence_id": eid, "view": "metadata"}),
            ]:
                with self.subTest(cap=cap, name=name):
                    page, _ = await self.read(name, max_chars=cap, **args)
                    self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), cap)
                    rc = await self.receipt(page)
                    self.assertEqual(
                        await read_artifact_raw(self.b, rc["result_artifact_id"]), page
                    )

    async def test_tail_is_not_full_field(self):
        eid = await self.seed({"summary": {"rst_count": 731921, "padding": "x" * 2000}})
        page, messages = await self.read(
            "read_evidence", evidence_id=eid, field="summary", char_offset=2024
        )
        self.assertFalse(page["truncated"])
        rc = await self.receipt(page)
        self.assertEqual(rc["complete_paths"], [])
        self.assertGreater(rc["total_chars"], rc["range"][0])
        visible, _ = await prepare(self.b, messages)
        self.assertIn("731921", str([m.content for m in visible]))
        dump_result(
            "tail-coverage", {"page": page, "receipt": rc, "visible": messages_json([visible])}
        )

    async def test_preview_never_deletes_only_visible_fact(self):
        eid = await self.seed({"summary": {"padding": "x" * 2000, "rst_count": 812967}})
        page, messages = await self.read("read_evidence", evidence_id=eid, field="summary")
        with (
            patch.object(settings, "CONTEXT_BUDGET_ENABLED", True),
            patch.object(settings, "CONTEXT_MAX_CHARS", 5000),
        ):
            try:
                visible, _ = await prepare(self.b, messages)
            except WorkspaceError as exc:
                self.assertIn("context_incomplete", str(exc))
                manifest = (await self.b.repository.all(self.b.incident_id, "context"))[-1]
                candidate = await read_artifact_raw(
                    self.b, manifest["candidate_messages_artifact_id"]
                )
                dump_result(
                    "preview-coverage",
                    {"disposition": "refused", "manifest": manifest, "candidate": candidate},
                )
            else:
                self.assertIn("812967", str([m.content for m in visible]))
                dump_result(
                    "preview-coverage",
                    {"disposition": "assembled", "visible": messages_json([visible])},
                )

    async def test_tampered_receipt_body_cannot_cover(self):
        eid = await self.seed({"summary": {"rst_count": 812967}})
        page, messages = await self.read("read_evidence", evidence_id=eid, field="summary")
        page["items"] = [{"rst_count": 0}]
        messages[-1] = messages[-1].model_copy(update={"content": json.dumps(page)})
        visible, _ = await prepare(self.b, messages)
        self.assertIn("812967", json.dumps(workbench(visible)))
        dump_result("tampered-body", {"visible": messages_json([visible])})

    async def test_nested_sources_worker_and_supervisor(self):
        raw = sample("get_abnormal_requests")["observations"]
        eid = await self.seed(raw, "get_abnormal_requests")
        page, messages = await self.read(
            "read_evidence", evidence_id=eid, view="raw", max_chars=8000
        )
        visible, _ = await prepare(self.b, messages)
        self.assertIn("response_code", str([m.content for m in visible]))
        page, messages = await self.read("read_evidence", evidence_id=eid, field="samples.0")
        visible, _ = await prepare(self.b, messages)
        self.assertIn("response_code", str([m.content for m in visible]))
        self.b.role = "supervisor"
        self.b.task_id = self.b.attempt_id = None
        page, _ = await self.read("read_evidence", evidence_id=eid, field="samples.0")
        for phase in ("initial_submission", "correction"):
            visible, _ = await prepare(self.b, [HumanMessage(content="submit")], phase)
            details = workbench(visible)["verified_read_details"]
            self.assertIn("response_code", json.dumps(details))
            self.assertTrue(details[0]["content"])

    async def test_details_outside_default_briefs_survive_directory(self):
        self.b.role = "supervisor"
        for i in range(13):
            eid = await self.seed({"summary": {"rst_count": 913827 if i == 12 else i}})
        await self.read("read_evidence", evidence_id=eid, field="summary.rst_count")
        with patch.object(settings, "CONTEXT_MAX_CHARS", 7000):
            visible, _ = await prepare(
                self.b, [HumanMessage(content="x" * 4000)], "initial_submission"
            )
        self.assertIn("913827", json.dumps(workbench(visible)["verified_read_details"]))
        dump_result("directory-carry", {"visible": messages_json([visible])})

    async def test_artifact_read_survives_submission(self):
        self.b.role = "supervisor"
        eid = await self.seed({"summary": {"details": "ARTIFACT_ONLY_FACT_914827"}})
        evidence = await self.b.repository.read(self.b.incident_id, eid, "evidence")
        await self.read(
            "read_artifact", artifact_id=evidence["artifact_id"], field="summary.details"
        )
        visible, _ = await prepare(self.b, [HumanMessage(content="submit")], "initial_submission")
        self.assertIn(
            "ARTIFACT_ONLY_FACT_914827", json.dumps(workbench(visible)["verified_read_details"])
        )
        dump_result("artifact-carry", {"visible": messages_json([visible])})

    async def test_negative_index_and_list_tail_are_partial(self):
        eid = await self.seed({"samples": [{"rst_count": i} for i in range(3)]})
        page, _ = await self.read("read_evidence", evidence_id=eid, field="samples.-1.rst_count")
        self.assertEqual((await self.receipt(page))["complete_paths"], ["samples.2.rst_count"])
        page, _ = await self.read("read_evidence", evidence_id=eid, field="samples", offset=2)
        self.assertEqual((await self.receipt(page))["complete_paths"], [])
        page, _ = await self.read("read_evidence", evidence_id=eid, field="samples", offset=3)
        self.assertEqual((await self.receipt(page))["returned_paths"], [])

    async def test_long_metadata_pages_recover_original_arguments(self):
        value = {"text": "x" * 6000, "tail": "FACT_AT_END_915627"}
        eid = await self.seed({}, arguments=value)
        text, offset = "", 0
        while True:
            page, _ = await self.read(
                "read_evidence",
                evidence_id=eid,
                view="metadata",
                field="arguments",
                char_offset=offset,
                max_chars=512,
            )
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 512)
            text += page["text"]
            if page["next_char_offset"] is None:
                break
            offset = page["next_char_offset"]
        self.assertEqual(json.loads(text), value)

    async def test_actual_iam_empty_samples_pass_through_worker(self):
        path = archive_root() / "attempt6/iam-pod/records.json"
        records = json.loads(path.read_text())
        ids = {
            "evidence_736c235702ff4c6baa011fb136335930",
            "evidence_c91bccf9c4f1443da3cad9b1cdc279fb",
        }
        for row in records["evidence"]:
            if row["id"] not in ids:
                continue
            eid = await self.seed(row["observations"], row["tool_name"], row["arguments"])
            page, messages = await self.read("read_evidence", evidence_id=eid, view="raw")
            self.assertEqual(page["items"][0], row["observations"])
            visible, _ = await prepare(self.b, messages, "worker_step")
            # The scripted consumer can see original meta and the empty result.
            observed = json.loads(next(m.content for m in visible if m.type == "tool"))
            self.assertEqual(observed["items"][0]["samples"], [])
            self.assertEqual(observed["items"][0]["meta"], row["observations"]["meta"])

    async def test_catalog_and_read_tail_combination_preserves_or_refuses(self):
        await self.begin_task()
        for i in range(24):
            eid = await self.seed(
                {"summary": {"rst_count": i, "padding": "x" * 2000, "tail": "UNIQUE_FACT_916827"}}
            )
        page, messages = await self.read("read_evidence", evidence_id=eid, field="summary")
        messages.insert(0, HumanMessage(content="REQUIREMENT_" + "x" * 19000))
        for enabled in (True, False):
            with patch.object(settings, "CONTEXT_BUDGET_ENABLED", enabled):
                try:
                    visible, manifest = await prepare(self.b, messages, "worker_step")
                except WorkspaceError as exc:
                    self.assertTrue(enabled)
                    self.assertIn("protected_parts_exceed_budget", str(exc))
                else:
                    self.assertIn("UNIQUE_FACT_916827", str([m.content for m in visible]))
                    self.assertIn(messages[0].content, [m.content for m in visible])
                    if enabled:
                        self.assertLessEqual(manifest["input_chars"], 24000)

    async def test_actual_iam_read_then_actual_worker_continues_and_submits(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        records = json.loads((archive_root() / "attempt6/iam-pod/records.json").read_text())
        original = next(
            e for e in records["evidence"] if e["tool_name"] == "get_pod_network_summary"
        )
        resource = next(
            r for r in records["resource"] if r.get("uid") == original["arguments"]["pod_uid"]
        )
        state, rows, events, sent, model = await run_scenario(
            raw=original["observations"],
            resource=resource,
            window=original["time_range"],
            worker_read=True,
            actions=[{"name": "get_pod_network_summary", "arguments": {}}],
        )
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(rows["evidence"][0]["observations"], original["observations"])
        self.assertTrue(any(r["role"] == "worker" for r in rows["read_receipt"]))
        self.assertEqual(rows["attempt"][0]["status"], "finished")
        self.assertFalse(state["final_report"]["report_errors"])

    async def test_dns_actual_supervisor_submission_and_correction_at_both_catalog_caps(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario
        from config import settings

        for enabled in (False, True):
            with patch.object(settings, "CONTEXT_BUDGET_ENABLED", enabled):
                state, rows, events, sent, model = await run_scenario(
                    supervisor_read=True, invalid_report=True
                )
                self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
            self.assertTrue(rows["read_receipt"])
            self.assertTrue(rows["report_packet"])
            self.assertTrue(
                any(
                    a["operation"] == "submit_report" and a["outcome"].get("status") == "rejected"
                    for a in rows["model_action"]
                )
            )
            self.assertFalse(state["final_report"]["report_errors"])


def archive_root():
    relative = "学习材料归档/04-Agent学习/改动优化/上下文输入改造-2026-10-14/intranet-verification-2026-10-16"
    for root in (Path("/mnt/d/Business/Agents/RCAgent"), Path("D:/Business/Agents/RCAgent")):
        if (root / relative).is_dir():
            return root / relative
    raise FileNotFoundError("attempt6 archive unavailable")


def messages_json(calls):
    return [
        [
            {"type": m.type, "content": m.content, "tool_call_id": getattr(m, "tool_call_id", None)}
            for m in messages
        ]
        for messages in calls
    ]


def dump_result(name, value):
    if os.environ.get("RCAGENT_ROUND2_OUTPUT"):
        folder = Path(os.environ["RCAGENT_ROUND2_OUTPUT"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (name + ".json")).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, default=str)
        )


class ArchivedAnalysisTests(unittest.TestCase):
    def test_k8s_log_selector_ignores_timestamps_sql_predicates_and_normal_close(self):
        from local_testing.inspect_incident_sample import abnormal_line

        self.assertFalse(
            abnormal_line(
                '2026-10-16T16:50:53+08:00 {"level":"INFO","status_code":200,"msg":"http request"}'
            )
        )
        self.assertFalse(
            abnormal_line(
                "2026-10-16T14:26:18+08:00 [218.915ms] [rows:0] SELECT * WHERE status = 'failed'"
            )
        )
        self.assertFalse(
            abnormal_line(
                '2026-10-16T16:19:55+08:00 {"level":"INFO","msg":"websocket: read error: websocket: close 1001 (going away)"}'
            )
        )
        self.assertTrue(
            abnormal_line(
                '2026-10-16T12:22:54+08:00 {"level":"INFO","msg":"websocket: read error: read tcp x:8001->y:38194: i/o timeout"}'
            )
        )

    def test_attempt6_exact_failures_peaks_and_unknown_usage(self):
        from local_testing.run_analysis import analyse_records

        root = archive_root() / "attempt6"
        folder = root / "iam-pod"
        records = json.loads((folder / "records.json").read_text())
        final = json.loads((folder / "final-state.json").read_text())
        result = analyse_records(records, final)
        self.assertEqual(
            (result["sent_peak_input_chars"], result["candidate_peak_input_chars"]), (23984, 26097)
        )
        self.assertEqual(result["completed_model_attempts"], 35)
        self.assertEqual(result["stages"]["final_report_proposals"], 0)
        self.assertEqual(len(result["task_failures"]), 2)
        reasons = str(result["task_failures"])
        self.assertIn("protected_parts_exceed_budget", reasons)
        self.assertIn("'dict' object has no attribute 'replace'", reasons)
        frontend = analyse_records(json.loads((root / "frontend-service/records.json").read_text()))
        self.assertIsNone(frontend["token_usage"]["total_tokens"])
        self.assertGreater(frontend["sent_peak_input_chars"], 0)
