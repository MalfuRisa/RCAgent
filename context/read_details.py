"""Protected read pages carried between Supervisor phases."""

from workspace.artifacts import read_artifact_raw
from workspace.repository import WorkspaceError
import json


async def verify_receipts(binding, messages, receipts, phase):
    """Verify only referenced bodies and same-run submission reads, once per call."""
    from .history import message_receipt_id

    wanted = {message_receipt_id(m) for m in messages}
    bodies = {}
    for receipt in receipts.values():
        submission = (
            binding.role == "supervisor"
            and phase in ("initial_submission", "correction")
            and receipt.get("run_id") == binding.run_id
            and receipt.get("role") == binding.role
        )
        if receipt["id"] not in wanted and not submission:
            continue
        try:
            body = await read_artifact_raw(binding, receipt["result_artifact_id"])
        except (WorkspaceError, OSError, ValueError, KeyError) as exc:
            receipt["storage_error"] = str(exc)
            continue
        bodies[receipt["id"]] = body
        receipt["_verified_body"] = body
        if "total_chars" in receipt:
            continue
        span = body.get("range") or []
        whole = len(span) == 2 and span[0] == 0 and body.get("truncated") is False
        if receipt.get("range_unit") == "chars" and whole:
            try:
                json.loads(body.get("text", ""))
            except (TypeError, ValueError):
                whole = False
        receipt["complete_paths"] = [receipt.get("field") or "<root>"] if whole else []
    return bodies


def transferred_pages(
    binding, original, transformed, receipts, bodies, existing, payload_text=None
):
    """Carry exact pages whose messages became previews and lack a proven copy."""
    from .history import message_receipt_id
    from .parts import fully_represented
    from workspace.receipts import matches_verified_read

    carried = {d.get("receipt_id") for d in existing}
    details = []
    for before, after in zip(original, transformed):
        rid = message_receipt_id(before)
        if before.content == after.content or rid in carried or rid not in bodies:
            continue
        receipt = receipts[rid]
        if (
            receipt.get("incident_id"),
            receipt.get("run_id"),
            receipt.get("role"),
            receipt.get("task_id"),
        ) != (binding.incident_id, binding.run_id, binding.role, binding.task_id):
            continue
        if receipt.get("attempt_id") != binding.attempt_id:
            continue
        if not matches_verified_read(receipt, json.loads(before.content)):
            continue
        if payload_text and fully_represented(before, payload_text):
            continue
        details.append(read_detail(receipt, bodies[rid]))
        carried.add(rid)
    return details


def retention_audit(original, visible, payload):
    """Record how every local read in the input survives the final candidate."""
    from .history import message_receipt_id
    from .parts import fully_represented

    content = json.dumps(payload, ensure_ascii=False)
    messages = {getattr(m, "tool_call_id", None): m for m in visible if m.type == "tool"}
    details = {d.get("receipt_id"): d for d in payload.get("verified_read_details", [])}
    result = []
    for message in original:
        rid = message_receipt_id(message)
        if not rid:
            continue
        body = json.loads(message.content)
        expected = read_detail({"id": rid}, body)["content"]
        same = messages.get(message.tool_call_id)
        retained = bool(same and same.content == message.content)
        in_detail = details.get(rid, {}).get("content") == expected
        equivalent = fully_represented(message, content)
        result.append(
            {
                "receipt_id": rid,
                "tool_call_id": message.tool_call_id,
                "exact_message_kept": retained,
                "exact_page_in_detail": in_detail,
                "values_proven_in_workbench": equivalent,
                "required_values_retained": retained or in_detail or equivalent,
            }
        )
    return result


def read_detail(receipt, body):
    content = {
        key: body[key]
        for key in ("items", "text", "range", "truncated", "next", "next_char_offset")
        if key in body
    }
    if "items" not in body and "text" not in body:
        content = body  # short record pages can still hold unique goals/reasoning
    return {
        "receipt_id": receipt["id"],
        "source_refs": {
            key: receipt.get(key)
            for key in ("source_kind", "source_id", "source_revision", "source_hash", "tool_name")
        },
        "field": receipt.get("field"),
        "range": receipt.get("range"),
        "range_unit": receipt.get("range_unit"),
        "content": content,
        "scope": receipt.get("source_scope"),
        "windows": body.get("windows")
        or {
            "requested": receipt.get("requested_window"),
            "returned": receipt.get("returned_window"),
        },
        "limitations": receipt.get("source_limitations"),
        "coverage": {
            key: receipt.get(key)
            for key in (
                "complete_paths",
                "returned_paths",
                "truncated",
                "has_more",
                "next",
                "total_chars",
                "total_items",
            )
        },
        "protected": True,
    }


async def verified_read_details(binding, snapshot, receipts, phase):
    if binding.role != "supervisor" or phase not in ("initial_submission", "correction"):
        return []
    current = {
        row["id"]: row for kind in ("evidence", "task", "assessment") for row in snapshot[kind]
    }
    evidence_links = {}
    for evidence in snapshot["evidence"]:
        evidence_links.setdefault(evidence.get("artifact_id"), []).append(evidence)
    details, duplicates = [], {}
    from workspace.stage_segments import retired_receipts

    retired = retired_receipts(binding, snapshot.get("checkpoint") or [])
    for receipt in receipts.values():
        if receipt["id"] in retired:
            continue
        if receipt.get("role") != binding.role or receipt.get("run_id") != binding.run_id:
            continue
        if receipt.get("incident_id") != binding.incident_id:
            continue
        if receipt.get("source_kind") not in ("evidence", "task", "assessment", "artifact"):
            continue
        if receipt.get("view") not in ("field", "raw"):
            continue
        try:
            body = await read_artifact_raw(binding, receipt["result_artifact_id"])
        except (WorkspaceError, OSError, ValueError, KeyError) as exc:
            details.append(
                {
                    "receipt_id": receipt["id"],
                    "source_id": receipt.get("source_id"),
                    "storage_reference_error": str(exc),
                    "protected": True,
                }
            )
            continue
        detail = read_detail(receipt, body)
        source = current.get(receipt.get("source_id"))
        if source and source.get("revision") != receipt.get("source_revision"):
            detail["source_changed"] = {
                "read_revision": receipt.get("source_revision"),
                "current_revision": source.get("revision"),
            }
        if receipt.get("source_kind") == "artifact":
            try:
                artifact = await binding.repository.read(
                    binding.incident_id, receipt["source_id"], "artifact"
                )
                # Reading verifies both incident containment and the source hash.
                source_body = await read_artifact_raw(binding, receipt["source_id"])
                if isinstance(source_body, dict) and source_body.get("kind") == "model_draft":
                    detail["origin"] = "model_draft_not_tool_fact"
                links = evidence_links.get(receipt["source_id"], [])
                linked = (
                    links[0]
                    if len(links) == 1 and links[0].get("observations") == source_body
                    else None
                )
                detail["artifact_link"] = {
                    "evidence_ids": [linked["id"]] if linked else [],
                    "status": "verified" if linked else "unknown",
                }
                if linked:
                    detail["linked_evidence_scope"] = {
                        "resource_ids": linked.get("resource_ids") or [],
                        "requested_window": linked.get("time_range"),
                        "returned_window": linked.get("returned_time_range"),
                        "data_gaps": linked.get("data_gaps") or [],
                        "warnings": linked.get("warnings") or [],
                    }
            except (WorkspaceError, OSError, ValueError, KeyError) as exc:
                detail["storage_reference_error"] = str(exc)
        # Only exact pages and scope/limitations can merge, never theoretical cards.
        from workspace.receipts import source_hash

        key = source_hash({k: v for k, v in detail.items() if k != "receipt_id"})
        if key in duplicates:
            duplicates[key].setdefault("receipt_ids", [duplicates[key]["receipt_id"]]).append(
                receipt["id"]
            )
        else:
            duplicates[key] = detail
            details.append(detail)
    return details
