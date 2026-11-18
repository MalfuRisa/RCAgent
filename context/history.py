"""Message-history organisation for the visible input (PRD §9, §17.6).

The archive keeps every original AI/Tool message; this module builds the
*visible copy* only. Recent complete call batches stay intact, earlier batches
keep request/result pairing while their long tool results become cards, bounded
summaries or explicit ``no_reduction`` records, and a fact is deduplicated only
when a read receipt referenced by a message that is still visible proves the
exact same source, revision and field path was returned in full.
"""

import json

from config import settings
from langchain_core.messages import AIMessage, ToolMessage
from .parts import source_references


class Batch:
    """One complete call batch: an AI message with tool_calls plus its replies."""

    def __init__(self, index, ai_index, reply_indices):
        self.index = index
        self.ai_index = ai_index
        self.reply_indices = reply_indices

    @property
    def tool_call_ids(self):
        return set(self.reply_indices)


def plan_batches(messages):
    """Return ``(batches, consumed_positions)`` for complete call batches.

    A batch must contain every ToolMessage matching its AI message's tool_calls;
    an incomplete pair is left alone as a plain message so pairing is never
    broken (PRD §9.2: no orphaned ToolMessage).
    """
    positions = {}
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            positions.setdefault(message.tool_call_id, index)
    batches, consumed = [], set()
    for index, message in enumerate(messages):
        calls = getattr(message, "tool_calls", None) if isinstance(message, AIMessage) else None
        if not calls:
            continue
        replies = []
        for call in calls:
            position = positions.get(call.get("id"))
            if position is None or position <= index:
                replies = None
                break
            replies.append(position)
        if replies is None:
            continue
        batches.append(Batch(index, index, sorted(replies)))
        consumed.add(index)
        consumed.update(replies)
    return batches, consumed


def message_receipt_id(message):
    """The receipt a visible tool message refers to, when it has one."""
    if not isinstance(message, ToolMessage):
        return None
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload.get("read_receipt_id")


def visible_coverage(binding, messages, receipts):
    """Field paths that the *currently visible* messages showed in full.

    Coverage comes from stored read receipts, never from guessing by evidence
    id: metadata/directory reads cover no observations, a partial page covers
    only its range, and a receipt from another role, task or run does not count
    (PRD §17.6.2).
    """
    coverage = {}
    for message in messages:
        receipt = receipts.get(message_receipt_id(message))
        if not receipt:
            continue
        from workspace.receipts import matches_verified_read

        try:
            body = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        if not matches_verified_read(receipt, body):
            continue
        if receipt.get("incident_id") != binding.incident_id:
            continue
        if receipt.get("role") and receipt["role"] != binding.role:
            continue
        if receipt.get("task_id") != binding.task_id:
            continue
        if receipt.get("attempt_id") != binding.attempt_id:
            continue
        if receipt.get("run_id") and receipt["run_id"] != binding.run_id:
            continue
        complete = receipt.get("complete_paths") or []
        if not complete:
            continue
        key = (receipt.get("source_kind"), receipt.get("source_id"), receipt.get("source_revision"))
        entry = coverage.setdefault(
            key, {"revision": receipt.get("source_revision"), "paths": set()}
        )
        entry["paths"].update(
            path
            for path in (receipt_path(receipt.get("source_kind"), item) for item in complete)
            if path
        )
    return coverage


def record_path(source_path):
    """Map a projection source path to its path inside the stored evidence."""
    if not isinstance(source_path, str) or not source_path or "[]" in source_path:
        return None
    cleaned = source_path
    return "observations" + ("" if cleaned.startswith(".") else ".") + cleaned


def receipt_path(source_kind, path):
    """Receipt paths are relative to the stored record; normalise them once.

    For evidence records the projection root (``observations``) *is* the tool
    response root, so a receipt written as ``summary.rst_count`` covers
    ``observations.summary.rst_count``.
    """
    if not path:
        return None
    if path == "<root>":
        return "observations" if source_kind == "evidence" else "<root>"
    if source_kind != "evidence":
        return path.replace("[]", "")
    cleaned = path.replace("[]", "")
    return "observations" + ("" if cleaned.startswith(".") else ".") + cleaned


def path_covered(path, complete_paths):
    """True when some fully returned path is this path or one of its parents."""
    return any(
        path == candidate or path.startswith(candidate + ".") for candidate in complete_paths
    )


def deduplicate(payload, coverage, revisions):
    """Drop repeated observations whose exact record path is proven visible here.

    Only a matching source id *and* revision is dropped; limits, identity,
    windows, previews and unread values stay, and an unproven duplicate is kept
    (PRD §17.6.2).
    """
    removed = []
    for row in payload.get("evidence", []):
        evidence_id = row.get("evidence_id")
        entry = coverage.get(("evidence", evidence_id, revisions.get(evidence_id)))
        if not entry or not row.get("observations"):
            continue
        if entry.get("revision") != revisions.get(evidence_id):
            continue  # a newer stored version was never the one that was read
        source = row.get("source") or {}
        from .source_paths import source_values

        dropped = []
        for key, value in row["observations"].items():
            leaves, proven = source_values(source.get(key), value)
            if proven and all(
                path_covered(record_path(path) or "", entry["paths"]) for path, _ in leaves
            ):
                dropped.append(key)
        if not dropped:
            continue
        for key in dropped:
            row["observations"].pop(key, None)
            row.get("source", {}).pop(key, None)
        if not row["observations"]:
            row.pop("observations", None)
        if not row.get("source"):
            row.pop("source", None)
        row["details_in_recent_message"] = True
        row["deduplicated_paths"] = dropped
        removed.extend("%s:%s" % (evidence_id, key) for key in dropped)
    if removed:
        payload.setdefault("coverage", {})["observations_in_recent_history"] = sorted(removed)
    return payload


def message_size(message):
    """Serialized size of a message as it appears in the visible input."""
    return len(
        json.dumps(
            {
                "type": getattr(message, "type", None),
                "name": getattr(message, "name", None),
                "tool_call_id": getattr(message, "tool_call_id", None),
                "content": message.content,
            },
            ensure_ascii=False,
        )
    )


def receipt_card(evidence, receipt, refs):
    """Card for an earlier result whose exact read is recorded on a receipt."""
    from context.views import evidence_view

    card = evidence_view(evidence, refs)
    card["replaced_earlier_result"] = True
    card["read_coverage"] = {
        "view": receipt.get("view"),
        "field": receipt.get("field"),
        "range": receipt.get("range"),
    }
    return card


def archived_replacement(artifact_id, original, references=None):
    """Short preview plus the real artifact id - never a bare 'read the file' note.

    ``references`` keeps the ids of the stored source (evidence, receipt) so the
    original page can be read back with the same local tool (PRD §17.6.3).
    """
    head = str(original)[:200]
    replacement = {
        "note": "earlier tool result externalized; full text archived",
        "artifact_id": artifact_id,
        "chars": len(str(original)),
        "head": head + ("…" if len(str(original)) > 200 else ""),
        "detail": "read_artifact(artifact_id=%s, view=raw)" % artifact_id,
    }
    for key in ("evidence_id", "read_receipt_id"):
        if (references or {}).get(key):
            replacement[key] = references[key]
    return json.dumps(replacement, ensure_ascii=False)


def bounded_summary(content):
    """JSON summary of a tool result using the same loss-aware rules as before."""
    from .materials import key_observations

    try:
        return key_observations(json.loads(content))
    except (TypeError, ValueError):
        return None


async def externalize_earlier(binding, messages, batches, recent_batches, evidence_index, receipts):
    """Replace earlier batches' long tool results with cards or references.

    Returns ``(messages, decisions)``. Every decision records the before/after
    size and the strategy; when the replacement would not be smaller, the
    original visible content is kept and the decision says ``no_reduction``
    instead of truncating it (PRD §17.6.3).
    """
    from workspace.artifacts import write_artifact
    from context.views import window_table

    keep = set()
    for batch in batches[-recent_batches:] if recent_batches else []:
        keep.update(batch.reply_indices)
    visible = list(messages)
    decisions = []
    for batch in batches:
        for position in batch.reply_indices:
            if position in keep:
                continue
            message = visible[position]
            if not isinstance(message, ToolMessage):
                continue
            original = str(message.content)
            pointer = source_references(message)
            if len(original) < (400 if pointer else 1000):
                continue  # a result that names its archived source can be a pointer much sooner
            receipt = receipts.get(message_receipt_id(message))
            if receipt and receipt.get("storage_error"):
                continue  # the visible original is the only readable copy
            evidence = None
            if receipt and receipt.get("source_kind") == "evidence":
                evidence = evidence_index.get(receipt.get("source_id"))
            if evidence is None:
                evidence = evidence_index.get(getattr(message, "tool_call_id", None))
            decision = {
                "tool_call_id": message.tool_call_id,
                "tool_name": getattr(message, "name", None),
                "before_chars": message_size(message),
                "strategy": None,
                "read_receipt_id": receipt.get("id") if receipt else None,
            }
            strategy = None
            if evidence is not None:
                windows, refs = window_table([evidence])
                replacement = json.dumps(
                    receipt_card(evidence, receipt or {}, refs), ensure_ascii=False
                )
                strategy = "evidence_card"
            else:
                summary = bounded_summary(original)
                summary_text = (
                    json.dumps(summary, ensure_ascii=False) if summary is not None else None
                )
                if summary_text is not None and len(summary_text) < len(original):
                    replacement = summary_text
                    strategy = "bounded_summary"
                else:
                    # Free text or an unknown tool: archive the full message first,
                    # then keep a preview with the real artifact id (§17.6.3).
                    artifact = await write_artifact(
                        binding,
                        {
                            "content": original,
                            "tool_name": getattr(message, "name", None),
                            "tool_call_id": message.tool_call_id,
                        },
                    )
                    await binding.repository.save(binding.incident_id, "artifact", artifact)
                    replacement = archived_replacement(
                        artifact["id"], original, source_references(message)
                    )
                    strategy = "archived_preview"
                    decision["artifact_id"] = artifact["id"]
            if len(replacement) >= len(original):
                decision["strategy"] = "no_reduction"
                decision["reason"] = (
                    "replacement would not be smaller; the original visible content is kept"
                )
                decision["after_chars"] = decision["before_chars"]
                decisions.append(decision)
                continue
            candidate = message.model_copy(update={"content": replacement})
            decision["strategy"] = strategy
            decision["after_chars"] = message_size(candidate)
            visible[position] = candidate
            decisions.append(decision)
    return visible, decisions


def evidence_index(snapshot):
    """Evidence lookup by record id and by legacy tool_call_id, for card building."""
    index = {}
    for evidence in snapshot["evidence"]:
        index[evidence["id"]] = evidence
        call_id = evidence.get("tool_call_id")
        if call_id:
            index.setdefault(call_id, evidence)
    return index


def history_settings():
    return settings.CONTEXT_HISTORY_RECENT_BATCHES
