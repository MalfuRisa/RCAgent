"""Acceptance-repair contracts (PRD §17): real fields, bounded reads, coverage.

Every test here observes the actual returned payload, the visible messages, the
stored read receipts or the recorded audit - never a private helper name. The
first group reproduces the 2026-10-15 review counter-examples; they must fail
before the fixes and pass after.
"""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from config import settings
from context.builder import prepare
from workspace.queries import save_query
from workspace.read_tools import CURRENT_TOOL_CALL, query_evidence, read_evidence
from workspace.repository import WorkspaceError, new_id
from workspace.runtime import bind
from test_workspace import WorkspaceCase

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "workspace_tool_samples.json"


def sample(tool):
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["samples"][tool]


def workbench(messages):
    text = next(
        str(m.content)
        for m in messages
        if getattr(m, "type", "") == "system" and str(m.content).startswith("Incident workbench")
    )
    from context.exact_representation import expand

    return expand(json.loads(text.split("\n", 1)[1]))


class RealFieldTests(unittest.TestCase):
    """S1: the projection must keep the values the tool actually returned."""

    def test_real_dns_sample_keeps_codes_error_info_and_sources(self):
        from context.tool_views import project

        row = sample("get_abnormal_requests")
        projected = project("get_abnormal_requests", row["observations"])
        block = projected["observations"]["samples"]
        shown = block["samples"]
        codes = {(item.get("response_code"), item.get("response_status")) for item in shown}
        self.assertIn((5, 3), codes, "the response_code=5 rows must stay visible")
        self.assertIn((3, 4), codes, "the response_code=3 rows must stay visible")
        self.assertTrue(
            all("error_info" in item for item in shown),
            "the original error_info must not be projected away",
        )
        self.assertTrue(all(item.get("protocol") == "DNS" for item in shown))
        self.assertTrue(all("duration_ms" in item for item in shown))
        for key in (
            "protocol",
            "endpoint",
            "response_code",
            "response_status",
            "error_info",
            "duration_ms",
        ):
            self.assertEqual(
                projected["source"]["samples"][key],
                "samples[]." + key,
                "each projected value needs its real source path",
            )
        indexes = [item["source_index"] for item in shown]
        self.assertEqual(
            len(indexes), len(set(indexes)), "the same list index must not be selected twice"
        )
        stamps = [item.get("request_time") for item in shown]
        self.assertEqual(len(stamps), len(set(stamps)), "no duplicate sample rows")
        groups = {
            (item["response_code"], item["response_status"]): item for item in block["groups"]
        }
        self.assertEqual(groups[(5, 3)]["count"], 16)
        self.assertEqual(groups[(3, 4)]["count"], 14)
        self.assertEqual(block["total"], 30)
        self.assertTrue(block["detail_required"])

    def test_absent_fields_are_not_silently_null(self):
        from context.tool_views import project

        raw = {
            "samples": [
                {"protocol": "HTTP", "status_code": 500, "error": "boom"},
                {"protocol": "HTTP", "status_code": 502, "error": "bad gateway"},
            ]
        }
        projected = project("get_abnormal_requests", raw)
        block = projected["observations"]["samples"]
        self.assertIn("status_code", block["samples"][0], "present extra fields stay visible")
        self.assertIn("error", block["samples"][0])
        self.assertIn(
            "samples[].response_code",
            block.get("unknown_fields", []),
            "fields absent from the actual return are reported, not invented",
        )

    def test_transport_error_with_top_level_gaps_keeps_limits(self):
        from context.views import evidence_view, window_table

        gap = {
            "source": "kubernetes",
            "reason": "inventory_only",
            "impact": "phase 来自 Controller MySQL 资源清单",
        }
        evidence = {
            "id": "e1",
            "tool_name": "get_pod_runtime_status",
            "observations": {},
            "transport_status": "error",
            "source_status": "error",
            "data_gaps": [gap],
            "warnings": [dict(gap)],
            "resource_ids": ["r1"],
        }
        windows, refs = window_table([evidence])
        view = evidence_view(evidence, refs)
        gaps = view["limitations"]["data_gaps"]
        self.assertEqual(len(gaps), 1, "the same gap stored twice must be deduplicated")
        self.assertEqual((gaps[0]["source"], gaps[0]["reason"]), ("kubernetes", "inventory_only"))
        self.assertEqual(view["limitations"]["warnings"][0]["impact"], gap["impact"])
        self.assertEqual(view["observations"], {}, "no invented observations for an empty result")
        self.assertEqual(view["result_transport_status"], "error")

    def test_unshown_categories_are_named_not_hidden(self):
        from context.tool_views import project

        rows = [
            {"protocol": "DNS", "response_code": code, "response_status": 0} for code in range(6)
        ]
        rows = [
            dict(
                row,
                endpoint="svc",
                error_info="code=%d" % row["response_code"],
                duration_ms=1,
                trace_id="",
            )
            for row in rows
        ]
        projected = project("get_abnormal_requests", {"samples": rows})
        block = projected["observations"]["samples"]
        self.assertLessEqual(len(block["samples"]), 4)
        self.assertGreaterEqual(
            len(block["groups"]), 4, "categories stay countable beyond the shown sample"
        )


class ReadPageTests(WorkspaceCase):
    """S2: every returned payload respects max_chars and every page tells the truth."""

    async def seed(self, tool, observations, *, arguments=None, tool_call_id="seed-call"):
        evidence_id = new_id("evidence")
        await save_query(
            self.b, evidence_id, tool, arguments or {}, observations, "now", 1, "ok", tool_call_id
        )
        return evidence_id

    def size(self, payload):
        return len(json.dumps(payload, ensure_ascii=False))

    async def test_thirty_rows_page_truthfully_and_never_skip(self):
        rows = [
            {
                "protocol": "DNS",
                "endpoint": "svc-%d" % index,
                "response_code": 5,
                "response_status": 3,
                "error_info": "pad-" + "x" * 120,
                "duration_ms": 1.0 + index,
                "trace_id": "",
            }
            for index in range(30)
        ]
        evidence_id = await self.seed(
            "get_abnormal_requests", {"samples": rows}, arguments={"sample_size": 30}
        )
        with bind(self.b):
            page = await read_evidence.ainvoke(
                {"evidence_id": evidence_id, "field": "samples", "offset": 0, "max_chars": 512}
            )
            seen, offset, pages = [], 0, 0
            while True:
                self.assertLessEqual(
                    self.size(page), 512, "每个返回的整份 JSON 必须受 max_chars 限制"
                )
                self.assertNotIn(
                    "page_limit_too_small", page, "一条约 250 字符的原文必须能放进 512 的页"
                )
                items = page["items"]
                self.assertEqual(
                    page["range"],
                    [offset, offset + len(items)],
                    "range must describe the rows actually returned",
                )
                self.assertEqual(page["next"], offset + len(items) if page["truncated"] else None)
                for position, item in enumerate(items):
                    self.assertEqual(
                        item, rows[offset + position], "returned rows must equal the stored rows"
                    )
                seen.extend(range(offset, offset + len(items)))
                pages += 1
                if not page["truncated"]:
                    break
                offset = page["next"]
                page = await read_evidence.ainvoke(
                    {
                        "evidence_id": evidence_id,
                        "field": "samples",
                        "offset": offset,
                        "max_chars": 512,
                    }
                )
            self.assertLess(pages, 40)
        self.assertEqual(
            seen, list(range(30)), "thirty rows must be covered exactly once, without skips"
        )

    async def test_char_pages_rejoin_to_the_serialized_field(self):
        text = '故障 detail: code=5\nquote " backslash \\ newline\n' + "中" * 200
        evidence_id = await self.seed("get_pod_runtime_status", {"note": text})
        expected = json.dumps(text, ensure_ascii=False)
        with bind(self.b):
            joined, offset, pages = "", 0, 0
            while True:
                page = await read_evidence.ainvoke(
                    {
                        "evidence_id": evidence_id,
                        "field": "note",
                        "char_offset": offset,
                        "max_chars": 512,
                    }
                )
                self.assertLessEqual(self.size(page), 512)
                self.assertEqual(page["range"], [offset, offset + len(page["text"])])
                joined += page["text"]
                pages += 1
                if page["next_char_offset"] is None:
                    break
                offset = page["next_char_offset"]
            self.assertLess(pages, 40)
        self.assertEqual(joined, expected, "pages must rejoin into the serialized field exactly")

    async def test_metadata_field_read_has_its_own_source_and_limit(self):
        evidence_id = await self.seed("get_pod_runtime_status", {"status": {"phase": "Running"}})
        record = await self.b.repository.read(self.b.incident_id, evidence_id, "evidence")
        record["note"] = "meta-" + "m" * 3000
        await self.b.repository.save(self.b.incident_id, "evidence", record, record["revision"])
        token = CURRENT_TOOL_CALL.set("meta-call")
        try:
            with bind(self.b):
                page = await read_evidence.ainvoke(
                    {
                        "evidence_id": evidence_id,
                        "view": "metadata",
                        "field": "note",
                        "char_offset": 0,
                        "max_chars": 512,
                    }
                )
        finally:
            CURRENT_TOOL_CALL.reset(token)
        self.assertLessEqual(self.size(page), 512, "metadata pages follow the same limit")
        self.assertEqual(page["source"], "workbench_metadata")
        receipts = await self.b.repository.all(self.b.incident_id, "read_receipt")
        receipt = next(item for item in receipts if item["id"] == page["read_receipt_id"])
        self.assertEqual(receipt["source_kind"], "evidence_metadata")
        self.assertEqual(receipt["field"], "note")

    async def test_empty_catalog_and_old_offset_stay_distinguishable(self):
        for index in range(6):
            await self.seed(
                "get_pod_runtime_status",
                {"status": {"phase": "Running"}},
                tool_call_id="c%d" % index,
            )
        with bind(self.b):
            empty = await query_evidence.ainvoke({"tool_name": "no_such_tool"})
            self.assertEqual(empty["items"], [])
            self.assertEqual(empty["total"], 0)
            self.assertFalse(empty["has_more"])
            self.assertIsNone(empty["next_cursor"])
            self.assertNotIn("page_limit_too_small", empty)
            page = await query_evidence.ainvoke({"limit": 2, "offset": 2, "view": "minimal"})
            self.assertEqual(len(page["items"]), 2)
            everything = await query_evidence.ainvoke({"limit": 100, "view": "minimal"})
        self.assertEqual(
            [row["evidence_id"] for row in page["items"]],
            [row["evidence_id"] for row in everything["items"][2:4]],
            "the legacy offset must select the same rows as a full listing",
        )

    async def test_read_task_and_assessment_accept_bounded_pages(self):
        from workspace.read_tools import read_task

        task = await self.begin_task()
        with bind(self.b):
            brief = await read_task.ainvoke({"task_id": task.task_id})
            field = await read_task.ainvoke(
                {
                    "task_id": task.task_id,
                    "view": "raw",
                    "field": "investigation_goal",
                    "char_offset": 0,
                    "max_chars": 512,
                }
            )
        self.assertEqual(brief["task"]["investigation_goal"], "diagnose")
        self.assertEqual(field["source"], "task_record")
        self.assertLessEqual(self.size(field), 512)


class CoverageTests(WorkspaceCase):
    """S3: dedup follows recorded coverage, never a guess from the evidence id."""

    async def seed(
        self, observations, tool="get_pod_runtime_status", arguments=None, call_id="seed"
    ):
        evidence_id = new_id("evidence")
        await save_query(
            self.b, evidence_id, tool, arguments or {}, observations, "now", 1, "ok", call_id
        )
        return evidence_id

    async def call_read(self, name, args, call_id):
        from workspace.read_tools import READ_TOOLS

        tool = next(item for item in READ_TOOLS if item.name == name)
        token = CURRENT_TOOL_CALL.set(call_id)
        try:
            with bind(self.b):
                return await tool.ainvoke(args)
        finally:
            CURRENT_TOOL_CALL.reset(token)

    def tool_message(self, name, call_id, payload):
        return [
            AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}]),
            ToolMessage(
                content=json.dumps(payload, ensure_ascii=False), name=name, tool_call_id=call_id
            ),
        ]

    async def test_metadata_read_does_not_delete_unread_observations(self):
        evidence_id = await self.seed(
            {"status": "ok", "summary": {"rtt_avg_ms": 12, "rst_count": 7}},
            tool="get_pod_network_summary",
        )
        page = await self.call_read(
            "read_evidence", {"evidence_id": evidence_id, "view": "metadata"}, "meta-1"
        )
        visible, manifest = await prepare(
            self.b, self.tool_message("read_evidence", "meta-1", page)
        )
        brief = next(
            row for row in workbench(visible)["evidence"] if row["evidence_id"] == evidence_id
        )
        self.assertEqual(
            brief["observations"]["rst_count"],
            7,
            "a metadata read must never delete observations it did not return",
        )

    async def test_partial_sample_page_does_not_delete_the_whole_block(self):
        rows = [
            {"protocol": "DNS", "endpoint": "svc-%d" % index, "error_info": "code=5"}
            for index in range(6)
        ]
        evidence_id = await self.seed(
            {"samples": rows},
            tool="get_abnormal_requests",
            arguments={"sample_size": 6},
            call_id="dns-1",
        )
        page = await self.call_read(
            "read_evidence",
            {"evidence_id": evidence_id, "field": "samples", "offset": 0, "limit": 2},
            "page-1",
        )
        visible, manifest = await prepare(
            self.b, self.tool_message("read_evidence", "page-1", page)
        )
        brief = next(
            row for row in workbench(visible)["evidence"] if row["evidence_id"] == evidence_id
        )
        self.assertIn(
            "samples",
            brief["observations"],
            "reading two rows must not delete the sample categories shown in the workbench",
        )

    async def test_full_field_read_is_deduplicated_with_its_coverage(self):
        evidence_id = await self.seed(
            {"status": "ok", "summary": {"rtt_avg_ms": 12, "rst_count": 7}},
            tool="get_pod_network_summary",
        )
        page = await self.call_read(
            "read_evidence", {"evidence_id": evidence_id, "field": "summary"}, "full-1"
        )
        visible, manifest = await prepare(
            self.b, self.tool_message("read_evidence", "full-1", page)
        )
        brief = next(
            row for row in workbench(visible)["evidence"] if row["evidence_id"] == evidence_id
        )
        self.assertTrue(
            brief.get("details_in_recent_message"), "the fully shown field is not repeated twice"
        )
        self.assertNotIn("rst_count", brief.get("observations") or {})
        self.assertIn("rst_count", brief.get("deduplicated_paths") or [])

    async def test_reduced_history_rechecks_coverage(self):
        observations = {
            "status": "ok",
            "summary": {"rtt_avg_ms": 12, "rst_count": 7, "filler": "y" * 2000},
        }
        evidence_id = await self.seed(observations, tool="get_pod_network_summary")
        page = await self.call_read(
            "read_evidence", {"evidence_id": evidence_id, "field": "summary"}, "read-1"
        )
        self.assertGreater(
            len(json.dumps(page)), 1000, "the read must be large enough to be externalized later"
        )
        first = self.tool_message("read_evidence", "read-1", page)
        visible, _ = await prepare(self.b, first)
        brief = next(
            row for row in workbench(visible)["evidence"] if row["evidence_id"] == evidence_id
        )
        self.assertTrue(brief.get("details_in_recent_message"))
        self.assertNotIn("rtt_avg_ms", brief.get("observations") or {})
        padding = []
        for index in range(2, 5):
            padding += [
                AIMessage(
                    content="",
                    tool_calls=[{"name": "read_artifact", "args": {}, "id": "pad-%d" % index}],
                ),
                ToolMessage(
                    content=json.dumps({"artifact_id": "a%d" % index, "text": "x" * 5000}),
                    name="read_artifact",
                    tool_call_id="pad-%d" % index,
                ),
            ]
        visible, manifest = await prepare(self.b, first + padding)
        replacements = {
            item.get("tool_call_id") for item in manifest.get("history_replacements") or []
        }
        self.assertIn("read-1", replacements, "the old full read is externalized in a later call")
        brief = next(
            row for row in workbench(visible)["evidence"] if row["evidence_id"] == evidence_id
        )
        self.assertEqual(
            brief["observations"]["rtt_avg_ms"],
            12,
            "once its message is reduced, the value must reappear in the workbench",
        )
        self.assertNotIn(
            "details_in_recent_message",
            brief,
            "coverage is re-checked against the messages that are actually visible now",
        )

    async def test_read_receipt_records_paths_revision_and_artifact(self):
        from workspace.artifacts import read_artifact_raw

        evidence_id = await self.seed({"summary": {"rtt_avg_ms": 12, "rst_count": 7}})
        page = await self.call_read(
            "read_evidence", {"evidence_id": evidence_id, "field": "summary"}, "rc-1"
        )
        receipt_id = page["read_receipt_id"]
        receipts = await self.b.repository.all(self.b.incident_id, "read_receipt")
        receipt = next(item for item in receipts if item["id"] == receipt_id)
        record = await self.b.repository.read(self.b.incident_id, evidence_id, "evidence")
        self.assertEqual((receipt["source_kind"], receipt["source_id"]), ("evidence", evidence_id))
        self.assertEqual(receipt["source_revision"], record["revision"])
        self.assertEqual(receipt["tool_call_id"], "rc-1")
        self.assertEqual(receipt["role"], "worker")
        self.assertIn("summary", receipt["complete_paths"])
        self.assertEqual(receipt["result_chars"], len(json.dumps(page, ensure_ascii=False)))
        stored = await read_artifact_raw(self.b, receipt["result_artifact_id"])
        self.assertEqual(
            stored["read_receipt_id"],
            receipt_id,
            "the result artifact holds the exact returned body",
        )
        self.assertNotIn(
            receipt["result_artifact_id"],
            json.dumps(stored),
            "the stored body must not reference its own artifact id",
        )

    async def test_failed_read_audits_the_error_without_a_coverage_receipt(self):
        with self.assertRaises(WorkspaceError):
            await self.call_read("read_evidence", {"evidence_id": "evidence_missing"}, "bad-1")
        receipts = await self.b.repository.all(self.b.incident_id, "read_receipt")
        self.assertEqual(receipts, [], "a failed read must not claim that facts were covered")
        events = await self.b.repository.transact(
            lambda db: [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM events WHERE incident=?", (self.b.incident_id,)
                )
            ]
        )
        self.assertTrue(
            any(event.get("kind") == "local_read_error" for event in events),
            "the failure must stay auditable",
        )


class SupervisorCarryTests(WorkspaceCase):
    """S4: a value read during the reading phase stays visible when deciding."""

    async def seed_runtime(self):
        evidence_id = new_id("evidence")
        observations = {
            "status": {"phase": "Running"},
            "containers": [{"name": "iam", "restart_count": 17}],
            "data_gaps": [
                {
                    "source": "kubernetes",
                    "reason": "inventory_only",
                    "impact": "containers come from inventory",
                }
            ],
        }
        await save_query(
            self.b,
            evidence_id,
            "get_pod_runtime_status",
            {"cluster": "c", "pod_uid": "u"},
            observations,
            "now",
            1,
            "ok",
            "runtime-call",
        )
        return evidence_id

    async def test_value_read_while_reading_survives_submission_and_correction(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(
            supervisor_read=True, invalid_report=True, raw={"summary": {"tcp_errors": 7, "zero": 0}}
        )
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertTrue(rows["read_receipt"])
        self.assertTrue(rows["observation"])
        self.assertTrue(all(o["verification"]["value_matches"] for o in rows["observation"]))
        reviews = [
            messages
            for role, phase, messages in model.calls
            if role == "supervisor" and phase == "tools"
        ]
        self.assertGreaterEqual(len(reviews), 3)
        self.assertIn("reference missing", str(reviews[-1]))
        self.assertTrue(all(o["id"] in str(reviews[-1]) for o in rows["observation"]))
        self.assertFalse(state["final_report"]["report_errors"])


class RealWorldCapacityTests(WorkspaceCase):
    """The 2026-10-16 real run refused a worker call at 27,441 chars: a large
    directory plus four fresh read pages. The budget step must turn a
    pointer-bearing page into a preview, and the deepest level must cap the
    catalog instead of refusing while it can still shorten."""

    async def test_pointer_bearing_page_is_reduced_by_the_budget_step(self):
        from context import parts

        page = {
            "source": "tool_raw",
            "field": None,
            "read_receipt_id": "read_receipt_" + "0" * 32,
            "evidence_id": "evidence_pointer",
            "items": [{"detail": "x" * 1200}],
            "range": [0, 1],
            "truncated": False,
            "next": None,
        }
        messages = [
            AIMessage(
                content="", tool_calls=[{"name": "read_evidence", "args": {}, "id": "read-1"}]
            ),
            ToolMessage(content=json.dumps(page), name="read_evidence", tool_call_id="read-1"),
        ]
        kinds = parts.classify(messages, phase="worker_step", recent_positions={1})
        manifest = {}
        with bind(self.b):
            visible, overflowed = await parts.shrink_optional(
                self.b, messages, kinds, manifest, json.dumps({"tiny": True}), 800, True
            )
        self.assertTrue(
            overflowed, "a pointer without another full visible copy cannot justify deletion"
        )
        self.assertEqual(
            visible[1].content,
            messages[1].content,
            "the unproven recent page stays complete, for explicit refusal by the builder",
        )

    async def test_large_protected_requirement_shrinks_optional_catalog_before_refusal(self):
        """Attempt6: a twelve-row directory alone still exceeded 24k.

        Keep the actual requirement byte-for-byte and remove only optional
        directory rows, with coverage counts and every omission still audited.
        """
        task = await self.begin_task()
        resource = await self.b.repository.read(self.b.incident_id, self.b.resource_id, "resource")
        for index in range(24):
            await save_query(
                self.b,
                "catalog_%02d" % index,
                "get_pod_network_summary",
                {"cluster": resource["cluster"], "pod_uid": resource["uid"]},
                {"summary": {"tcp_errors": index}, "data_gaps": ["logs unavailable"]},
                "now",
                1,
                "ok",
                "seed-%02d" % index,
            )
        requirement = "KEEP_COMPLETE_REQUIREMENT_" + "r" * 19000
        messages = [
            SystemMessage(content="Keep scope, unknowns and evidence references."),
            HumanMessage(content=requirement),
        ]
        visible, manifest = await prepare(self.b, messages, phase="worker_step")
        self.assertIn(requirement, [m.content for m in visible])
        self.assertLessEqual(manifest["input_chars"], 24000)
        self.assertTrue(
            any("optional catalog limit" in s["step"] for s in manifest["budget_steps"])
        )
        payload = workbench(visible)
        self.assertEqual(payload["coverage"]["totals"]["evidence"], 24)
        shown = {r["evidence_id"] for r in payload.get("evidence_directory", [])}
        omitted = {
            r["id"]
            for r in manifest["omitted_references_and_reasons"]
            if r.get("kind") == "evidence"
        }
        self.assertEqual(shown | omitted, {"catalog_%02d" % i for i in range(24)})
        self.assertTrue(payload["coverage"]["detail_entry"])
        self.assertEqual(payload["tasks"][0]["task_id"], task.task_id)

    async def test_page_whose_summary_is_not_smaller_still_becomes_a_pointer(self):
        """The 2026-10-16 replay: read pages whose loss-aware summary was not
        smaller than the page itself were recorded as no_reduction, so nothing
        shrank. They must fall back to the archived pointer instead."""
        from context import history, parts

        page = {
            "source": "tool_raw",
            "field": None,
            "read_receipt_id": "read_receipt_" + "1" * 32,
            "evidence_id": "evidence_pointer",
            "items": [{"note": "x" * 900}, {"note": "y" * 900}],
            "range": [0, 2],
            "truncated": False,
            "next": None,
        }
        original = json.dumps(page)
        summary_size = len(json.dumps(history.bounded_summary(original), ensure_ascii=False))
        self.assertGreaterEqual(
            summary_size,
            len(original),
            "the counter-example depends on a summary that is not smaller than the page",
        )
        messages = [
            AIMessage(content="", tool_calls=[{"name": "read_evidence", "args": {}, "id": "p-1"}]),
            ToolMessage(content=original, name="read_evidence", tool_call_id="p-1"),
        ]
        kinds = parts.classify(messages, phase="worker_step", recent_positions=set())
        manifest = {}
        with bind(self.b):
            visible, _ = await parts.shrink_optional(
                self.b, messages, kinds, manifest, json.dumps({"tiny": True}), 700, True
            )
        decision = manifest["history_replacements"][-1]
        self.assertEqual(
            decision["strategy"],
            "archived_preview",
            "a bigger summary must not block the pointer path",
        )
        replacement = json.loads(visible[1].content)
        self.assertEqual(replacement["read_receipt_id"], page["read_receipt_id"])
        self.assertLess(decision["after_chars"], decision["before_chars"])

    async def test_worker_assembly_with_many_records_stays_in_budget(self):
        for index in range(30):
            evidence_id = new_id("evidence")
            await save_query(
                self.b,
                evidence_id,
                "get_pod_runtime_status",
                {"cluster": "c", "pod_uid": "pod-uid-%02d" % index},
                {
                    "status": {"phase": "Running"},
                    "containers": [{"name": "iam-%02d" % index, "restart_count": index}],
                    "data_gaps": [
                        {
                            "source": "kubernetes",
                            "reason": "inventory_only",
                            "impact": "containers come from inventory",
                        }
                    ],
                },
                "now",
                1,
                "ok",
                "seed-%02d" % index,
            )
        task = await self.begin_task()
        self.b.role = "worker"
        messages = []
        for index in range(12):
            call_id = "read-%02d" % index
            page = {
                "source": "tool_raw",
                "field": None,
                "read_receipt_id": "read_receipt_%032d" % index,
                "evidence_id": "evidence_x",
                "items": [{"detail": "x" * 1200}],
                "range": [0, 1],
                "truncated": False,
                "next": None,
            }
            messages += [
                AIMessage(
                    content="", tool_calls=[{"name": "read_evidence", "args": {}, "id": call_id}]
                ),
                ToolMessage(content=json.dumps(page), name="read_evidence", tool_call_id=call_id),
            ]
        messages.append(
            HumanMessage(
                content=json.dumps({"task_id": task.task_id, "investigation_goal": "diagnose"})
            )
        )
        visible, manifest = await prepare(self.b, messages, phase="worker_step")
        self.assertNotIn(
            "context_incomplete",
            manifest,
            "a worker with many records and pages must still assemble",
        )
        self.assertLessEqual(manifest["input_chars"], settings.CONTEXT_MAX_CHARS)
        payload = json.loads(visible[0].content.split(chr(10), 1)[1])
        if manifest["projection"]["level"] >= 2:
            self.assertLessEqual(
                len(payload.get("evidence_directory") or []),
                12,
                "the deepest level caps the catalog and keeps counts",
            )
        self.assertTrue(
            manifest["omitted_references_and_reasons"],
            "records outside the capped catalog keep an audit reason",
        )


class ProtectedBudgetTests(WorkspaceCase):
    """S5: the current correction and other protected parts are never genericized."""

    async def correction_messages(self):
        correction = (
            "Correct this proposal once. Errors and formal statement IDs:\n"
            + json.dumps(
                {
                    "errors": [{"reason": "causal claim lacks direct oom_termination evidence"}],
                    "claims": [
                        {
                            "claim_id": "correction-claim-7",
                            "type": "causal",
                            "content": "OOM killed the pod",
                        }
                    ],
                    "limitations": ["no termination event visible"],
                    "repair_template": {
                        "section": "hypotheses",
                        "content": "Candidate OOM, unverified",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
            + "Detailed instructions: "
            + "k" * 1200
        )
        return [
            SystemMessage(content="You are the supervisor. Follow the rules."),
            HumanMessage(content=correction),
        ]

    async def test_current_correction_stays_visible_when_it_fits(self):
        self.b.role = "supervisor"
        messages = await self.correction_messages()
        with patch.object(settings, "CONTEXT_MAX_CHARS", 9000):
            visible, manifest = await prepare(self.b, messages, phase="correction")
        body = "\n".join(str(m.content) for m in visible)
        self.assertIn("correction-claim-7", body, "the formal claim id stays visible")
        self.assertIn("repair_template", body)
        self.assertIn("no termination event visible", body)
        self.assertNotIn(
            "Key observations and limitations are in workbench",
            body,
            "a correction is never replaced by a generic sentence",
        )
        self.assertNotIn("context_incomplete", manifest)

    async def test_over_budget_correction_refuses_with_protected_parts(self):
        self.b.role = "supervisor"
        messages = await self.correction_messages()
        with patch.object(settings, "CONTEXT_MAX_CHARS", 1500):
            with self.assertRaises(WorkspaceError) as caught:
                await prepare(self.b, messages, phase="correction")
        self.assertIn("protected_parts_exceed_budget", str(caught.exception))
        stored = await self.b.repository.all(self.b.incident_id, "context")
        refused = [row for row in stored if row.get("context_incomplete")]
        self.assertTrue(refused, "the refused assembly is still saved")
        self.assertTrue(refused[-1]["candidate_messages_artifact_id"])
        kinds = {part["kind"] for part in refused[-1]["protected_parts"]}
        self.assertIn("correction", kinds)
        self.assertIn("system_rules", kinds)
        # A refusal must still show how big each step was and what the payload held.
        self.assertTrue(
            refused[-1]["budget_steps"], "the per-level sizes are recorded for refusals too"
        )
        self.assertEqual(refused[-1]["projection"]["level"], 2)
        self.assertIn("workbench", refused[-1]["part_chars"])


def trace_path(raw, path):
    """Resolve a projection source path in the real sample; list segments allowed."""
    node = raw
    for key in path.split("."):
        if key == "" or key == "[]":
            if isinstance(node, list) and node:
                node = node[0]
                continue
            return None, False
        if isinstance(node, list):
            if not key.isdigit():
                return None, False
            index = int(key)
            if index >= len(node):
                return None, False
            node = node[index]
            continue
        if not isinstance(node, dict) or key not in node:
            return None, False
        node = node[key]
    return node, True


class ProjectionAuditTests(unittest.TestCase):
    """§17.4.5: every fixed projection is checked against the real return."""

    def samples(self):
        return json.loads(FIXTURE.read_text(encoding="utf-8"))["samples"]

    def test_every_projected_value_traces_to_the_real_sample(self):
        from context.tool_views import project

        for tool, row in self.samples().items():
            raw = row["observations"] or {}
            projected = project(tool, raw)
            declared_absent = set(projected.get("unknown_fields") or [])
            for value in projected["observations"].values():
                if isinstance(value, dict):
                    declared_absent.update(value.get("unknown_fields") or [])
            for key, path in (projected.get("source") or {}).items():
                value = projected["observations"].get(key)
                listed = path if isinstance(path, list) else [path]
                for single in listed:
                    if isinstance(single, dict):
                        paths = list(single.values())
                    else:
                        paths = [single]
                    for candidate in paths:
                        if candidate in declared_absent:
                            continue  # declared but absent from this return: reported, not invented
                        resolved, found = trace_path(raw, candidate.replace("[]", ".0"))
                        self.assertTrue(
                            found, "%s: %s has no real source path %s" % (tool, key, candidate)
                        )
                        if isinstance(value, (dict, list)):
                            continue  # a capped block is a projection of the subtree
                        if candidate.split(".")[-1] != key:
                            continue  # a derived note (for example a gap marker) names why, not a copy
                        self.assertEqual(
                            value,
                            resolved,
                            "%s: %s does not match its source %s" % (tool, key, candidate),
                        )

    def test_declared_statuses_and_inferences_survive_the_brief(self):
        from context.tool_views import project
        from context.views import evidence_view, window_table

        for tool, row in self.samples().items():
            raw = row["observations"] or {}
            meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
            status = meta.get("status") or raw.get("status")
            gaps = list(raw.get("data_gaps") or []) + list(meta.get("data_gaps") or [])
            evidence = {
                "id": "e",
                "tool_name": tool,
                "observations": raw,
                "transport_status": "ok",
                "source_status": status if isinstance(status, str) else None,
                "data_gaps": gaps,
                "warnings": [],
            }
            windows, refs = window_table([evidence])
            view = evidence_view(evidence, refs)
            if isinstance(status, str):
                self.assertEqual(
                    view["result_source_status"],
                    status,
                    "%s: the declared status must not be upgraded" % tool,
                )
            if gaps:
                self.assertTrue(
                    view["limitations"]["data_gaps"],
                    "%s: stored gaps must survive the short card" % tool,
                )
            projected = project(tool, raw)
            if tool == "diagnose_pod" and projected.get("inferences"):
                self.assertEqual(
                    projected["inferences"]["kind"], "tool_candidate_diagnosis_not_confirmed_fact"
                )


class ExternalizeStrategyTests(WorkspaceCase):
    """§17.6.3: no_reduction keeps the original; previews name a real artifact."""

    async def test_no_reduction_keeps_the_original_visible_content(self):
        from context import history
        from workspace.artifacts import read_artifact_raw

        big = {"summary": {("k%02d" % index): "v" * 200 for index in range(20)}}
        await save_query(
            self.b,
            "e-big",
            "get_pod_network_summary",
            {"cluster": "c", "pod_uid": "u"},
            big,
            "now",
            1,
            "ok",
            "call-1",
        )
        original = json.dumps({"status": "ok", "summary": {"tcp_errors": 7, "pad": "x" * 1000}})
        messages = [
            AIMessage(
                content="",
                tool_calls=[{"name": "get_pod_network_summary", "args": {}, "id": "call-1"}],
            ),
            ToolMessage(content=original, name="get_pod_network_summary", tool_call_id="call-1"),
        ]
        batches, _ = history.plan_batches(messages)
        snapshot = await self.b.repository.transact(
            lambda db: {"evidence": self.b.repository.list(db, self.b.incident_id, "evidence")}
        )
        visible, decisions = await history.externalize_earlier(
            self.b, messages, batches, 0, history.evidence_index(snapshot), {}
        )
        self.assertEqual(
            visible[1].content, original, "no_reduction must keep the original text unchanged"
        )
        decision = next(row for row in decisions if row["tool_call_id"] == "call-1")
        self.assertEqual(decision["strategy"], "no_reduction")
        self.assertEqual(decision["after_chars"], decision["before_chars"])
        self.assertIn("kept", decision["reason"])

    async def test_unknown_tool_error_tail_stays_recoverable(self):
        from context import parts
        from workspace.artifacts import read_artifact_raw

        tail = "reason=identity_or_scope_rejected; next_step=use the registered UID"
        content = (
            json.dumps({"status": "error", "detail": "x" * 2000})[:-2] + ', "tail": "' + tail + '"}'
        )
        messages = [
            AIMessage(
                content="", tool_calls=[{"name": "mystery_tool", "args": {}, "id": "call-2"}]
            ),
            ToolMessage(content=content, name="mystery_tool", tool_call_id="call-2"),
        ]
        kinds = parts.classify(messages, phase="worker_step", recent_positions={1})
        manifest = {}
        with bind(self.b):
            visible, overflowed = await parts.shrink_optional(
                self.b, messages, kinds, manifest, json.dumps({"tiny": True}), 1500, True
            )
        self.assertTrue(overflowed)
        self.assertEqual(visible[1].content, content)
        self.assertIn(tail, visible[1].content, "the sole visible error tail is protected")
