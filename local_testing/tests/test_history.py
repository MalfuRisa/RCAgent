"""Round-3 history contract: batches keep pairing, earlier results externalize.

Assertions look at the visible messages, the recorded replacement decisions and
the input size - never at private helper names.
"""

import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from config import settings
from context import history
from context.builder import prepare, visible_chars
from workspace.queries import save_query
from workspace.runtime import bind, invoke_model
from test_workspace import WorkspaceCase


def tool_round(index, size=4000):
    call_id = "call-%d" % index
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "read_artifact",
                    "args": {"artifact_id": "a%d" % index},
                    "id": call_id,
                }
            ],
        ),
        ToolMessage(
            content=json.dumps({"artifact_id": "a%d" % index, "text": "x" * size}),
            name="read_artifact",
            tool_call_id=call_id,
        ),
    ] * 1


class BatchTests(WorkspaceCase):
    async def test_complete_batches_are_paired_and_orphans_stay_plain(self):
        messages = (
            tool_round(1)
            + tool_round(2)
            + tool_round(3)
            + [
                ToolMessage(
                    content="orphan", tool_call_id="missing", name="read_evidence"
                )
            ]
        )
        batches, consumed = history.plan_batches(messages)
        self.assertEqual(len(batches), 3)
        self.assertEqual(len(consumed), 6)
        self.assertEqual(
            [
                row.tool_call_id
                for batch in batches
                for row in (messages[i] for i in batch.reply_indices)
            ],
            ["call-1", "call-2", "call-3"],
        )
        self.assertNotIn(
            len(messages) - 1,
            consumed,
            "an orphan ToolMessage is not claimed by any batch",
        )

    async def test_earlier_batches_become_cards_and_recent_batches_stay_intact(self):
        await self.locate()
        await save_query(
            self.b,
            "e1",
            "get_pod_runtime_status",
            {"cluster": "c", "pod_uid": "u"},
            {"status": "ok", "status": {"phase": "Running"}},
            "now",
            1,
            "ok",
            "ev-call",
        )
        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "get_pod_runtime_status", "args": {}, "id": "ev-call"}
                ],
            ),
            ToolMessage(
                content=json.dumps({"big": "y" * 5000}),
                name="get_pod_runtime_status",
                tool_call_id="ev-call",
            ),
        ]
        messages += tool_round(1) + tool_round(2)
        batches, _ = history.plan_batches(messages)
        self.assertEqual(len(batches), 3)
        evidence = await self.b.repository.all(self.b.incident_id, "evidence")
        index = {row.get("tool_call_id"): row for row in evidence}
        visible, decisions = await history.externalize_earlier(
            self.b, messages, batches, 2, index, {}
        )
        self.assertEqual(
            len(visible), len(messages), "message count and order are preserved"
        )
        replaced = visible[1]
        self.assertEqual(
            (replaced.tool_call_id, replaced.name, replaced.type),
            ("ev-call", "get_pod_runtime_status", "tool"),
        )
        card = json.loads(replaced.content)
        self.assertEqual(card["evidence_id"], "e1")
        self.assertTrue(card["replaced_earlier_result"])
        self.assertEqual(decisions[0]["strategy"], "evidence_card")
        self.assertLess(decisions[0]["after_chars"], decisions[0]["before_chars"])
        self.assertIn(
            "x" * 100,
            visible[3].content,
            "the second-newest batch keeps its tool result",
        )
        self.assertIn(
            "x" * 100, visible[5].content, "the newest batch keeps its tool result"
        )

    async def test_unknown_tool_result_becomes_a_pointer_and_only_keeps_no_reduction_when_nothing_shrinks(
        self,
    ):
        """2026-10-16: a page whose loss-aware summary was not smaller stayed at
        full size and was recorded as no_reduction. It must fall back to the
        archived pointer; only content that cannot shrink either way stays whole."""
        await self.locate()
        big = json.dumps({"text": "z" * 1200})
        messages = tool_round(1) + [
            AIMessage(
                content="",
                tool_calls=[{"name": "read_artifact", "args": {}, "id": "c9"}],
            ),
            ToolMessage(content=big, name="read_artifact", tool_call_id="c9"),
        ]
        batches, _ = history.plan_batches(messages)
        visible, decisions = await history.externalize_earlier(
            self.b, messages, batches, 0, {}, {}
        )
        self.assertTrue(decisions)
        for decision in decisions:
            self.assertIn(
                decision["strategy"],
                (
                    "bounded_summary",
                    "evidence_card",
                    "archived_preview",
                    "no_reduction",
                ),
            )
        replaced = next(
            decision for decision in decisions if decision["tool_call_id"] == "c9"
        )
        self.assertEqual(
            replaced["strategy"],
            "archived_preview",
            "an unknown tool result keeps a readable artifact id instead of its full text",
        )
        body = json.loads(visible[-1].content)
        self.assertTrue(body["artifact_id"].startswith("artifact_"))
        self.assertLess(replaced["after_chars"], replaced["before_chars"])
        self.assertEqual(len(visible), len(messages))
        # A short pointer-bearing page follows the same rule, and any decision
        # that does report no_reduction must have kept the text unchanged.
        small = json.dumps(
            {"read_receipt_id": "read_receipt_" + "0" * 32, "items": ["q" * 400]}
        )
        tiny = [
            AIMessage(
                content="",
                tool_calls=[{"name": "read_evidence", "args": {}, "id": "c10"}],
            ),
            ToolMessage(content=small, name="read_evidence", tool_call_id="c10"),
        ]
        batches, _ = history.plan_batches(tiny)
        visible, decisions = await history.externalize_earlier(
            self.b, tiny, batches, 0, {}, {}
        )
        decision = next(row for row in decisions if row["tool_call_id"] == "c10")
        self.assertEqual(
            decision["strategy"],
            "no_reduction",
            "a page smaller than its own pointer keeps its full text",
        )
        self.assertEqual(decision["after_chars"], decision["before_chars"])
        self.assertEqual(
            visible[-1].content, small, "no_reduction keeps the original text unchanged"
        )
        for row in decisions:
            if row["strategy"] == "no_reduction":
                self.assertEqual(row["after_chars"], row["before_chars"])


class DedupTests(WorkspaceCase):
    async def test_recent_full_read_suppresses_repeated_observations(self):
        """Coverage comes from the stored receipt of a still-visible message."""
        await self.locate()
        await save_query(
            self.b,
            "e1",
            "get_pod_network_summary",
            {"cluster": "c", "pod_uid": "u"},
            {"status": "ok", "summary": {"tcp_errors": 7}, "data_gaps": ["limited"]},
            "now",
            1,
        )
        record = await self.b.repository.read(self.b.incident_id, "e1", "evidence")
        receipt = {
            "id": "read_receipt_1",
            "role": "worker",
            "task_id": None,
            "run_id": self.b.run_id,
            "source_kind": "evidence",
            "source_id": "e1",
            "source_revision": record["revision"],
            "view": "field",
            "field": "summary",
            "complete_paths": ["summary"],
            "returned_paths": ["summary"],
            "truncated": False,
        }
        read = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_evidence", "args": {"evidence_id": "e1"}, "id": "r1"}
                ],
            ),
            ToolMessage(
                content=json.dumps(
                    {
                        "evidence_id": "e1",
                        "read_receipt_id": "read_receipt_1",
                        "field": "summary",
                        "items": [{"tcp_errors": 7}],
                    }
                ),
                name="read_evidence",
                tool_call_id="r1",
            ),
        ]
        coverage = history.visible_coverage(self.b, read, {receipt["id"]: receipt})
        self.assertEqual(
            coverage, {}, "an unverified receipt id alone cannot authorize deletion"
        )
        from workspace.receipts import source_hash

        receipt.update(
            incident_id=self.b.incident_id,
            result_hash=source_hash(json.loads(read[-1].content)),
        )
        coverage = history.visible_coverage(self.b, read, {receipt["id"]: receipt})
        self.assertEqual(
            coverage,
            {
                ("evidence", "e1", record["revision"]): {
                    "revision": record["revision"],
                    "paths": {"observations.summary"},
                }
            },
        )
        payload = {
            "evidence": [
                {
                    "evidence_id": "e1",
                    "observations": {"tcp_errors": 7},
                    "source": {"tcp_errors": "summary.tcp_errors"},
                    "result_transport_status": "ok",
                    "limitations": {"data_gaps": ["limited"]},
                }
            ]
        }
        history.deduplicate(payload, coverage, {"e1": record["revision"]})
        row = payload["evidence"][0]
        self.assertNotIn("observations", row)
        self.assertEqual(row["result_transport_status"], "ok")
        self.assertEqual(row["limitations"]["data_gaps"], ["limited"])
        self.assertTrue(row["details_in_recent_message"])
        # A metadata read covers no observations at all.
        metadata = {
            "id": "read_receipt_2",
            "role": "worker",
            "run_id": self.b.run_id,
            "source_kind": "evidence_metadata",
            "source_id": "e1",
            "source_revision": record["revision"],
            "view": "metadata",
            "returned_paths": ["observations_keys"],
            "complete_paths": ["observations_keys"],
        }
        covered = history.visible_coverage(self.b, read, {metadata["id"]: metadata})
        self.assertNotIn(("evidence", "e1"), covered)
        # A partial page covers only its range, and another run's read does not count.
        partial = dict(
            receipt,
            id="read_receipt_3",
            complete_paths=[],
            returned_paths=["samples"],
            range=[0, 2],
            truncated=True,
        )
        self.assertNotIn(
            ("evidence", "e1"),
            history.visible_coverage(self.b, read, {partial["id"]: partial}),
        )
        other_run = dict(receipt, id="read_receipt_4", run_id="other-run")
        self.assertEqual(
            history.visible_coverage(self.b, read, {other_run["id"]: other_run}), {}
        )


class PrepareHistoryTests(WorkspaceCase):
    async def test_prepare_externalizes_earlier_reads_and_records_decisions(self):
        await self.locate()
        await save_query(
            self.b,
            "e1",
            "get_pod_runtime_status",
            {"cluster": "c", "pod_uid": "u"},
            {"status": "ok", "status": {"phase": "Running"}},
            "now",
            1,
            "ok",
            "ev-call",
        )
        evidence = await self.b.repository.all(self.b.incident_id, "evidence")
        index = {row.get("tool_call_id"): row for row in evidence}
        older = tool_round(1, size=6000)
        newer = tool_round(2, size=6000)
        newest = tool_round(3, size=6000)
        messages = [
            AIMessage(
                content="",
                tool_calls=[{"name": "read_task", "args": {}, "id": "ev-call"}],
            ),
            ToolMessage(
                content=json.dumps({"big": "y" * 6000}),
                name="read_task",
                tool_call_id="ev-call",
            ),
        ]
        messages += older + newer + newest
        before = sum(len(str(m.content)) for m in messages)
        visible, manifest = await prepare(self.b, messages)
        after = sum(len(str(m.content)) for m in visible)
        self.assertLess(after, before, "earlier tool results must actually shrink")
        self.assertTrue(manifest["history_replacements"])
        self.assertEqual(
            manifest["history_recent_batches"], settings.CONTEXT_HISTORY_RECENT_BATCHES
        )
        requests = [c["id"] for m in visible for c in getattr(m, "tool_calls", [])]
        replies = [m.tool_call_id for m in visible if m.type == "tool"]
        self.assertEqual(requests, replies, "no orphaned ToolMessage after replacement")


class SupervisorPhaseTests(WorkspaceCase):
    async def test_reading_draft_is_archived_and_not_stacked_into_submission(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(supervisor_read=True)
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        reviews = [
            messages
            for role, phase, messages in model.calls
            if role == "supervisor" and phase == "tools"
        ]
        packet = [
            messages
            for role, phase, messages in model.calls
            if phase == "PacketSelection"
        ]
        self.assertGreaterEqual(len(reviews), 2)
        self.assertTrue(any(m.type == "tool" for m in reviews[1]))
        self.assertTrue(any("reading draft" in str(m.content) for m in reviews[1]))
        self.assertFalse(
            any(
                "reading draft" in str(m.content)
                for messages in packet
                for m in messages
            )
        )
        self.assertTrue(
            rows["model_response"],
            "public reading response is archived even when not reused in packet input",
        )
        self.assertFalse(state["final_report"]["report_errors"])

    async def test_correction_messages_carry_formal_ids_and_are_not_lost(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(invalid_report=True)
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        reviews = [
            messages
            for role, phase, messages in model.calls
            if role == "supervisor" and phase == "tools"
        ]
        self.assertEqual(len(reviews), 2)
        self.assertTrue(any("reference missing" in str(m.content) for m in reviews[1]))
        self.assertTrue(
            any(
                a["outcome"].get("status") == "rejected" and a["response_id"]
                for a in rows["model_action"]
            )
        )
        self.assertTrue(all(o["id"] in str(reviews[1]) for o in rows["observation"]))
        self.assertTrue(rows["report_packet"])
        self.assertFalse(state["final_report"]["report_errors"])


if __name__ == "__main__":
    unittest.main()
