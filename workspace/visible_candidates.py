"""A current candidate snapshot from exact cards and this owner's read pages."""

import hashlib
import json
from context.source_paths import source_values
from context.json_projection import scalar_fields
from .observations import resolve_pointer, values_match
from .queries import read_raw
from .artifacts import read_artifact_raw


async def candidate_snapshot(b, evidence, cards, receipts):
    by_id = {e["id"]: e for e in evidence}
    result = {}
    originals = {}
    hashes = {}

    async def offer(eid, pointer, value, *, span=None, unit=None, aggregate=None):
        e = by_id.get(eid)
        if not e:
            return
        if e.get("transport_status") != "ok" or e.get("source_status") in (
            "error",
            "cancelled",
        ):
            return
        if eid not in originals:
            originals[eid] = await read_raw(b, eid)
            artifact = await b.repository.read(
                b.incident_id, e["artifact_id"], "artifact"
            )
            hashes[eid] = artifact.get("sha256")
        actual, found, _ = resolve_pointer(originals[eid], pointer)
        if found and span is not None:
            from .material_ranges import slice_value

            actual, _ = slice_value(actual, span, unit)
        if not found or not values_match(actual, value):
            return
        identity = json.dumps(
            [
                b.incident_id,
                b.run_id,
                eid,
                e["revision"],
                pointer,
                value,
                span,
                unit,
                aggregate,
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        cid = "candidate_" + hashlib.sha256(identity.encode()).hexdigest()[:20]
        source = {
            "evidence_id": eid,
            "source_revision": e["revision"],
            "source_hash": hashes[eid],
            "json_pointer": pointer,
        }
        if span is not None:
            source.update(range=span, range_unit=unit)
        result[cid] = {
            "candidate_id": cid,
            "ref_id": cid,
            "local_id": cid,
            "source_ref": source,
            "expected_value": value,
            "limitations": [
                "Exact displayed field; source scope/coverage and unknowns still apply"
            ],
        }
        if aggregate:
            result[cid]["aggregate"] = aggregate

    for card in cards:
        for item in card.get("exact_fields", []) + card.get("reference_candidates", []):
            await offer(card["evidence_id"], item["json_pointer"], item["value"])
        for key, value in card.get("observations", {}).items():
            leaves, _ = source_values(card.get("source", {}).get(key), value)
            for path, v in leaves:
                pointer = "/" + path.replace(".", "/") if path else ""
                try:
                    await offer(card["evidence_id"], pointer, v)
                except (ValueError, KeyError):
                    continue
    for receipt in receipts:
        if receipt.get("source_kind") == "artifact":
            associated = next(
                (
                    e
                    for e in evidence
                    if e.get("artifact_id") == receipt.get("source_id")
                ),
                None,
            )
            if associated:
                receipt = {
                    **receipt,
                    "source_kind": "evidence",
                    "source_id": associated["id"],
                }
        if receipt.get("source_kind") != "evidence" or (
            receipt.get("run_id"),
            receipt.get("role"),
            receipt.get("task_id"),
            receipt.get("attempt_id"),
        ) != (b.run_id, b.role, b.task_id, b.attempt_id):
            continue
        body = await read_artifact_raw(b, receipt["result_artifact_id"])
        prefix = (
            "/" + (body.get("field") or "").replace(".", "/")
            if body.get("field")
            else ""
        )
        items, span = body.get("items"), body.get("range") or []
        if body.get("page_limit_too_small") or body.get("read_page_limit_too_small"):
            continue
        if receipt.get("range_unit") == "chars" and len(span) == 2 and body.get("text"):
            await offer(
                receipt["source_id"], prefix, body["text"], span=span, unit="chars"
            )
        if not isinstance(items, list) or len(span) != 2:
            continue
        raw = await read_raw(b, receipt["source_id"])
        original, found, _ = resolve_pointer(raw, prefix)
        if not found:
            continue
        if isinstance(original, list):
            # A whole returned page is a precise subtree/range, including empty lists.
            if span == [0, len(original)] and not body.get("truncated"):
                await offer(receipt["source_id"], prefix, items)
            elif span[1] > span[0]:
                await offer(
                    receipt["source_id"], prefix, items, span=span, unit="items"
                )
            if span[1] > span[0]:
                for operation in ("count", "distinct_count"):
                    await offer(
                        receipt["source_id"],
                        prefix,
                        items,
                        span=span,
                        unit="items",
                        aggregate={
                            "operation": operation,
                            "json_pointer": prefix,
                            "start_index": span[0],
                            "end_index": span[1],
                        },
                    )
        for i, value in enumerate(items):
            pointer = (
                prefix + "/" + str(span[0] + i)
                if isinstance(original, list)
                else prefix
            )
            await offer(receipt["source_id"], pointer, value)
            for p, v in scalar_fields(value, pointer, max_fields=200, max_list=200)[0]:
                await offer(receipt["source_id"], p, v)
    preferred = {
        e["id"]
        for e in evidence
        if b.task_id and e.get("producer_task_id") == b.task_id
    }
    recent = {}
    for i, receipt in enumerate(reversed(receipts)):
        recent.setdefault(receipt["source_id"], i)
    created = {e["id"]: i for i, e in enumerate(reversed(evidence))}
    return sorted(
        result.values(),
        key=lambda r: (
            r["source_ref"]["evidence_id"] not in recent,
            recent.get(r["source_ref"]["evidence_id"], 0),
            r["source_ref"]["evidence_id"] not in preferred,
            created[r["source_ref"]["evidence_id"]],
        ),
    )
