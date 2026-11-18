"""Durable candidate index: exact sent fragments, not another evidence store."""

import json
from .repository import WorkspaceError
from .artifacts import read_artifact_raw
from .receipts import matches_read


def reader_key(row):
    return tuple(row.get(k) for k in ("run_id", "role", "task_id", "attempt_id"))


def current_reader(b):
    return (b.run_id, b.role, b.task_id, b.attempt_id)


async def capture_context(b, manifest):
    """Index only exact content in a request that produced a model response."""
    from context.exact_representation import expand
    from context.read_details import read_detail
    from .visible_candidates import candidate_snapshot

    messages = await read_artifact_raw(b, manifest["visible_messages_artifact_id"])
    view = next(
        (
            expand(json.loads(m["content"].split("\n", 1)[1]))
            for m in messages
            if isinstance(m.get("content"), str)
            and m["content"].startswith("Incident workbench")
        ),
        {},
    )
    receipts = {
        r["id"]: r
        for r in await b.repository.all(b.incident_id, "read_receipt")
        if reader_key(r) == current_reader(b)
    }
    delivered = set()
    direct_cards = []
    for m in messages:
        if m.get("type") != "tool":
            continue
        try:
            body = json.loads(m["content"])
        except (ValueError, TypeError):
            continue
        if isinstance(body, dict):
            if body.get("origin") == "current" and isinstance(
                body.get("content"), dict
            ):
                body = body["content"]
            r = receipts.get(body.get("read_receipt_id"))
            if r and await matches_read(b, r, body):
                delivered.add(r["id"])
            elif (
                not body.get("read_receipt_id")
                and body.get("evidence_id")
                and body.get("origin") != "history"
            ):
                direct_cards.append(body)
    for d in view.get("verified_read_details", []):
        r = receipts.get(d.get("receipt_id"))
        if r:
            stored = await read_artifact_raw(b, r["result_artifact_id"])
            if read_detail(r, stored)["content"] == d.get("content"):
                delivered.add(r["id"])
    evidence = [
        e
        for e in await b.repository.all(b.incident_id, "evidence")
        if e.get("run_id") == b.run_id
    ]
    proposals = await candidate_snapshot(
        b,
        evidence,
        view.get("evidence", []) + view.get("evidence_directory", []) + direct_cards,
        [receipts[rid] for rid in delivered],
    )
    proposals.extend(view.get("current_candidates", []))
    await persist(b, proposals, manifest["id"], receipt_ids=delivered)


async def persist(b, proposals, context_id, *, receipt_ids=()):
    proof = dict(
        zip(("run_id", "role", "task_id", "attempt_id"), current_reader(b)),
        context_id=context_id,
    )
    unique = {r["candidate_id"]: r for r in proposals}

    def commit(db):
        existing = {
            r["id"]: r for r in b.repository.list(db, b.incident_id, "candidate")
        }
        for rid, proposal in unique.items():
            row = existing.get(rid)
            if row and any(reader_key(p) == current_reader(b) for p in row["readers"]):
                continue
            row = row or {
                "id": rid,
                "run_id": b.run_id,
                "proposal": {
                    k: v
                    for k, v in proposal.items()
                    if k not in ("candidate_id", "ref_id")
                },
                "readers": [],
            }
            row["readers"].append(proof)
            b.repository.put(db, b.incident_id, "candidate", row)
        for rid in receipt_ids:
            receipt = b.repository.get(db, b.incident_id, rid, "read_receipt")
            if reader_key(receipt) == current_reader(b) and not receipt.get(
                "delivered_context_id"
            ):
                receipt["delivered_context_id"] = context_id
                b.repository.put(
                    db,
                    b.incident_id,
                    "read_receipt",
                    receipt,
                    receipt["revision"],
                    "read_delivered",
                )

    await b.repository.transact(commit)


async def resolve(b, ref_id):
    """Knowing an id grants neither read authority nor a newer source version."""
    from .observations import load_source, source_problems

    try:
        row = await b.repository.read(b.incident_id, ref_id, "candidate")
    except WorkspaceError as error:
        raise WorkspaceError("unknown_ref: " + ref_id) from error
    if row.get("run_id") != b.run_id:
        raise WorkspaceError("scope_mismatch: " + ref_id)
    if not any(reader_key(p) == current_reader(b) for p in row["readers"]):
        raise WorkspaceError("not_visible: " + ref_id)
    proposal = row["proposal"]
    try:
        evidence, raw, artifact = await load_source(b, proposal["source_ref"])
    except (WorkspaceError, OSError, ValueError) as error:
        raise WorkspaceError("source_unavailable: " + ref_id) from error
    if source_problems(evidence, artifact, proposal["source_ref"]):
        raise WorkspaceError("source_stale: " + ref_id)
    from .observations import resolve_pointer

    value, found, _ = resolve_pointer(
        raw, proposal["source_ref"].get("json_pointer", "")
    )
    if not found:
        raise WorkspaceError("source_unavailable: " + ref_id)
    if proposal["source_ref"].get("range") is not None:
        from .material_ranges import slice_value

        value, _ = slice_value(
            value,
            proposal["source_ref"]["range"],
            proposal["source_ref"].get("range_unit"),
        )
    from .observations import values_match

    if "expected_value" in proposal and not values_match(value, proposal["expected_value"]):
        raise WorkspaceError("source_stale: exact value changed for " + ref_id)
    if evidence:
        from .resources import scope, allowed

        access = await scope(b)
        for identity in evidence.get("resource_identities", []):
            resource = await b.repository.read(
                b.incident_id, identity["id"], "resource"
            )
            if (
                resource.get("replaced_by")
                or resource.get("identity_status") != "resolved"
                or not allowed(
                    access, resource.get("cluster"), resource.get("namespace")
                )
                or resource.get("revision") != identity.get("revision")
            ):
                raise WorkspaceError(
                    "scope_mismatch: source identity changed for " + ref_id
                )
    from .observations import scope_of

    return {
        **row,
        "proposal": {**proposal, "expected_value": value},
        "source_scope": scope_of(evidence),
        "source_freshness": (evidence or {}).get("freshness"),
        "source_limits": {
            k: (evidence or {}).get(k) for k in ("truncated", "data_gaps", "warnings")
        },
    }


async def proves(b, source_ref, value):
    """Existing strict verifier may use a durable, same-reader sent proof."""
    from .observations import values_match

    for row in await b.repository.all(b.incident_id, "candidate"):
        proposal = row["proposal"]
        ref = proposal["source_ref"]
        if not any(reader_key(p) == current_reader(b) for p in row["readers"]):
            continue
        if all(
            ref.get(k) == source_ref.get(k)
            for k in (
                "evidence_id",
                "json_pointer",
                "range",
                "range_unit",
                "source_revision",
            )
        ):
            resolved = await resolve(b, row["id"])
            if values_match(resolved["proposal"]["expected_value"], value):
                return True
    return False


def descriptor(row):
    p = row["proposal"]
    return {
        "ref_id": row["id"],
        "source_ref": p["source_ref"],
        "value": p.get("expected_value"),
        "aggregate": p.get("aggregate"),
        "limitations": p.get("limitations", []),
        "scope": row.get("source_scope"),
        "source_freshness": row.get("source_freshness"),
        "source_limits": row.get("source_limits"),
    }


async def externalize_delivered(b, messages, receipts):
    """Only acknowledged pages leave the active request; originals stay addressable."""
    from .investigation_phases import active

    if not active(b):
        return messages, []
    result, audit = [], []
    lookup = {
        r["id"]: r
        for r in receipts
        if reader_key(r) == current_reader(b) and r.get("delivered_context_id")
    }
    for message in messages:
        try:
            body = json.loads(message.content) if message.type == "tool" else None
        except (ValueError, TypeError):
            body = None
        content = (
            body.get("content")
            if isinstance(body, dict) and body.get("origin") == "current"
            else body
        )
        receipt = (
            lookup.get(content.get("read_receipt_id"))
            if isinstance(content, dict)
            else None
        )
        if not receipt or not await matches_read(b, receipt, content):
            result.append(message)
            continue
        summary = {
            "status": "archived_delivered_read",
            "original_read_receipt_id": receipt["id"],
            "source_id": receipt["source_id"],
            "source_kind": receipt["source_kind"],
            "field": receipt.get("field"),
            "range": receipt.get("range"),
            "range_unit": receipt.get("range_unit"),
            "scope": receipt.get("source_scope"),
            "limitations": receipt.get("source_limitations"),
            "origin": "history" if receipt["source_kind"] == "history" else "current",
            "read": {"tool": receipt["tool_name"], "reference": receipt["source_id"]},
            "note": "Exact content previously delivered; original and qualified refs remain locally readable.",
        }
        result.append(
            message.model_copy(
                update={"content": json.dumps(summary, ensure_ascii=False)}
            )
        )
        audit.append(
            {
                "receipt_id": receipt["id"],
                "reason": "acknowledged_read_externalized_with_local_entry",
            }
        )
    return result, audit


async def coverage(b, snapshot):
    """Small actionable inventory; relevance/disposition remains the model's choice."""
    rows = [
        r
        for r in await b.repository.all(b.incident_id, "candidate")
        if r.get("run_id") == b.run_id
        and any(reader_key(p) == current_reader(b) for p in r["readers"])
    ]
    registered = {
        key
        for c in snapshot["stage_commit"]
        if c.get("run_id") == b.run_id
        for key in c.get("id_map", {})
    }
    sources = {}
    for row in rows:
        eid = row["proposal"]["source_ref"]["evidence_id"]
        item = sources.setdefault(
            eid, {"evidence_id": eid, "available_refs": 0, "registered_refs": 0}
        )
        item["available_refs"] += 1
        item["registered_refs"] += int(row["id"] in registered)
    return {
        "available_refs": len(rows),
        "registered_refs": sum(r["id"] in registered for r in rows),
        "sources": list(sources.values())[:10],
        "omitted_sources": max(0, len(sources) - 10),
        "lookup": "find_evidence_references",
        "precise_read": "read_evidence_reference",
        "note": "Already delivered refs survive view clipping; unregistered does not imply relevant or a fault.",
    }
