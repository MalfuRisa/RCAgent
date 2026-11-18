"""Validated report batches and source-sensitive packet expansion through the old gate."""

import hashlib
import json
from langchain_core.tools import tool
from .repository import WorkspaceError, new_id, utcnow


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


async def relevant_state(b, question_ids, evidence_ids):
    """No timestamps/events: an unrelated audit write must not invalidate a report."""

    def read(db):
        result = {}
        for kind in (
            "question",
            "observation",
            "judgment",
            "task",
            "evidence",
            "resource",
            "artifact",
        ):
            rows = b.repository.list(db, b.incident_id, kind)
            if kind == "question":
                rows = [r for r in rows if r.get("run_id") == b.run_id]
            elif kind in ("observation", "judgment"):
                rows = [
                    r
                    for r in rows
                    if r.get("run_id") == b.run_id
                    and (set(r.get("question_ids") or []) | {r.get("question_id")})
                    & set(question_ids)
                ]
            elif kind == "evidence":
                target_ids = {
                    ref
                    for r in rows
                    if r["id"] in evidence_ids
                    for ref in r.get("resource_ids") or []
                }
                rows = [
                    r
                    for r in rows
                    if r["id"] in evidence_ids
                    or (
                        r.get("run_id") == b.run_id
                        and target_ids.intersection(r.get("resource_ids") or [])
                    )
                ]
                rows = [
                    {
                        k: r.get(k)
                        for k in (
                            "id",
                            "revision",
                            "artifact_id",
                            "resource_ids",
                            "resource_identities",
                            "tool_name",
                            "arguments",
                            "time_range",
                            "returned_time_range",
                            "transport_status",
                            "source_status",
                            "data_gaps",
                            "warnings",
                            "truncated",
                        )
                    }
                    for r in rows
                ]
            elif kind == "resource":
                refs = {
                    ref
                    for r in result.get("evidence", [])
                    for ref in r.get("resource_ids") or []
                }
                rows = [r for r in rows if r["id"] in refs]
            elif kind == "artifact":
                refs = {r.get("artifact_id") for r in result.get("evidence", [])}
                rows = [r for r in rows if r["id"] in refs]
            else:
                rows = [
                    r
                    for r in rows
                    if set(r.get("question_ids") or []) & set(question_ids)
                ]
            result[kind] = sorted(rows, key=lambda row: row["id"])
        return result

    return await b.repository.transact(read)


def observation_text(rows):
    return "\n".join(
        "%s = %s (source %s)"
        % (
            r["field"],
            json.dumps(r["field_value"], ensure_ascii=False),
            (r.get("source_ref") or {}).get("evidence_id"),
        )
        for r in rows
    )


async def prepare_packet(
    b,
    claims,
    observation_refs,
    *,
    previous_packet_id=None,
    question_dispositions=None,
    supplemental_items=(),
):
    from .report_checks import check_statement

    previous = None
    if previous_packet_id:
        previous = await current_packet(b, previous_packet_id)
    stored = {
        r["id"]: r
        for r in await b.repository.all(b.incident_id, "observation")
        if r.get("run_id") == b.run_id
    }
    judgments = [
        r
        for r in await b.repository.all(b.incident_id, "judgment")
        if r.get("run_id") == b.run_id
    ]
    statements = list((previous or {}).get("statements") or [])
    question_ids = set((previous or {}).get("question_ids") or [])
    evidence_ids = set((previous or {}).get("evidence_ids") or [])
    errors, mapping = [], {}
    for index, raw in enumerate(claims):
        raw = dict(raw)
        local = raw.get("claim_id")
        refs = observation_refs.get(local) or []
        rows = [stored.get(ref) for ref in refs]
        if not rows or any(row is None for row in rows):
            errors.append(
                {
                    "field": "claims[%d]" % index,
                    "reason": "each statement needs registered observations",
                }
            )
            continue
        changed = False
        for row in rows:
            ref = row.get("source_ref") or {}
            if not ref.get("evidence_id"):
                changed = True
                break
            source = await b.repository.read(
                b.incident_id, ref["evidence_id"], "evidence"
            )
            if ref.get("source_revision") != source.get("revision"):
                changed = True
                break
        if changed:
            errors.append(
                {
                    "field": "claims[%d]" % index,
                    "reason": "packet_stale: observation source changed; recheck it",
                }
            )
            continue
        sources = {row["source_ref"].get("evidence_id") for row in rows}
        if None in sources:
            errors.append(
                {
                    "field": "claims[%d]" % index,
                    "reason": "report observations need real evidence IDs",
                }
            )
            continue
        if raw.get("type") == "observation":
            raw["content"] = observation_text(rows)
        raw["supporting_evidence_ids"] = sorted(sources)
        related_questions = {
            qid for row in rows for qid in row.get("question_ids") or []
        }
        related_judgments = [
            j for j in judgments if j.get("question_id") in related_questions
        ]
        known_opposition = {
            ref
            for j in related_judgments
            for ref in j.get("opposing_observation_ids") or []
        }
        if any(ref not in stored for ref in known_opposition):
            errors.append(
                {
                    "field": "claims[%d]" % index,
                    "reason": "unreadable authoritative opposition",
                }
            )
            continue
        opposing_sources = {
            stored[ref]["source_ref"].get("evidence_id") for ref in known_opposition
        }
        raw["opposing_evidence_ids"] = sorted(
            set(raw.get("opposing_evidence_ids") or []) | (opposing_sources - {None})
        )
        raw["limitations"] = list(
            dict.fromkeys(
                (raw.get("limitations") or [])
                + [text for row in rows for text in row.get("limitations") or []]
                + [
                    text
                    for j in related_judgments
                    for text in j.get("limitations") or []
                ]
            )
        )
        raw["claim_id"] = new_id("claim")
        checked = await check_statement(b, raw)
        if not checked["retained"]:
            errors.extend(checked["errors"])
            continue
        statement = {
            "statement_id": new_id("statement"),
            "claim": checked["statement"],
            "supports_cause": checked["supports_cause"],
            "observation_ids": refs,
            "limitations": raw["limitations"],
        }
        statements.append(statement)
        mapping[local] = statement["statement_id"]
        question_ids.update(
            qid for row in rows for qid in row.get("question_ids") or []
        )
        evidence_ids.update(sources)
        evidence_ids.update(raw.get("opposing_evidence_ids") or [])
    if errors:
        return {
            "status": "rejected",
            "reason": "packet_invalid",
            "errors": errors[:5],
            "error_count": len(errors),
        }
    state = await relevant_state(b, sorted(question_ids), sorted(evidence_ids))
    dispositions = {
        **((previous or {}).get("question_dispositions") or {}),
        **(question_dispositions or {}),
    }
    covered = {
        qid
        for r in await b.repository.all(b.incident_id, "stage_commit")
        if r.get("owner") == "supervisor" and r.get("run_id") == b.run_id
        for qid in r.get("question_ids") or []
    }
    missing = [
        r["id"]
        for r in state["question"]
        if r["id"] not in covered and not dispositions.get(r["id"])
    ]
    if missing:
        return {
            "status": "rejected",
            "reason": "unreviewed_questions",
            "question_ids": missing[:8],
            "total": len(missing),
            "note": "review or give an explicit unresolved/out-of-scope reason per direction",
        }
    pending = [
        r["id"]
        for r in state["question"]
        if r.get("status") not in ("answered", "out_of_scope")
    ]
    packet = {
        "id": new_id("report_packet"),
        "run_id": b.run_id,
        "producer_role": b.role,
        "created_at": utcnow(),
        "previous_packet_id": previous_packet_id,
        "supplemental_items": list((previous or {}).get("supplemental_items") or [])
        + list(supplemental_items),
        "statements": statements,
        "question_ids": sorted(question_ids),
        "evidence_ids": sorted(evidence_ids),
        "question_dispositions": dispositions,
        "unresolved_question_ids": pending,
        "source_state": state,
        "source_fingerprint": fingerprint(state),
    }
    saved = await b.repository.save(
        b.incident_id, "report_packet", packet, action="report_packet_prepared"
    )
    return {
        "status": "accepted",
        "packet_id": saved["id"],
        "statement_ids": dict(list(mapping.items())[:8]),
        "statement_total": len(statements),
        "unresolved_question_ids": pending,
        "read": 'read_investigation(kind="report_packet", record_id=<id>)',
    }


async def current_packet(b, packet_id):
    packet = await b.repository.read(b.incident_id, packet_id, "report_packet")
    if packet.get("run_id") != b.run_id:
        raise WorkspaceError("packet belongs to another run")
    state = await relevant_state(b, packet["question_ids"], packet["evidence_ids"])
    from .artifacts import read_artifact_raw

    for artifact in state["artifact"]:
        await read_artifact_raw(b, artifact["id"])
    if fingerprint(state) != packet["source_fingerprint"]:
        raise WorkspaceError(
            "packet_stale: relevant questions, opposition or sources changed; review again"
        )
    return packet


async def model_selected_packet(b, packet_id):
    """A packet alone is insufficient: require its accepted, audited model selection."""
    packet = await current_packet(b, packet_id)
    responses = {
        r["id"]
        for r in await b.repository.all(b.incident_id, "model_response")
        if r.get("run_id") == b.run_id
        and r.get("role") == "supervisor"
        and r.get("response_available")
    }
    proposals = await b.repository.all(b.incident_id, "report_proposal")
    return any(
        p.get("packet_id") == packet_id
        and not p.get("errors")
        and p.get("response_id") in responses
        and (p.get("selection") or {}).get("packet_id") == packet_id
        and p.get("normalized") is not None
        for p in proposals
    )


async def expand_packet(
    b, packet_id, selected_statement_ids, conclusion_status, unresolved_question_ids
):
    from .report_checks import inspect_proposal

    packet = await current_packet(b, packet_id)
    available = {r["statement_id"]: r for r in packet["statements"]}
    if (available and not selected_statement_ids) or any(
        ref not in available for ref in selected_statement_ids
    ):
        raise WorkspaceError("packet_invalid: select registered statement IDs")
    if not available and (
        conclusion_status != "inconclusive" or not packet.get("question_dispositions")
    ):
        raise WorkspaceError(
            "packet_invalid: an empty report requires explicit unresolved dispositions"
        )
    if set(unresolved_question_ids) != set(packet["unresolved_question_ids"]):
        raise WorkspaceError("packet_invalid: unresolved directions cannot be omitted")
    selected = [available[ref] for ref in dict.fromkeys(selected_statement_ids)]
    claims = [row["claim"] for row in selected]
    sections = {
        "observation": "observations",
        "causal": "observations",
        "hypothesis": "hypotheses",
        "exclusion": "exclusions",
    }
    items = [
        {
            "section": sections[c["type"]],
            "content": c["content"],
            "claim_ids": [c["claim_id"]],
        }
        for c in claims
    ]
    if packet.get("question_dispositions"):
        tasks = [
            t["id"]
            for t in await b.repository.all(b.incident_id, "task")
            if set(t.get("question_ids", [])) & set(packet["unresolved_question_ids"])
        ]
        if not tasks and not claims:
            raise WorkspaceError(
                "packet_invalid: unresolved report requires registered task references"
            )
        items.extend(
            {
                "section": "limitations",
                "content": "Unresolved " + qid + ": " + reason,
                "claim_ids": [] if tasks else [c["claim_id"] for c in claims],
                "evidence_ids": [],
                "task_ids": tasks,
            }
            for qid, reason in packet["question_dispositions"].items()
        )
    for claim in claims:
        items.append(
            {
                "section": "limitations",
                "content": "\n".join(claim["limitations"]),
                "claim_ids": [claim["claim_id"]],
            }
        )
    items.extend(packet.get("supplemental_items") or [])
    checks, accepted, errors = await inspect_proposal(b, claims, items)
    if errors:
        raise WorkspaceError("packet_invalid: gate recheck failed: " + str(errors[:3]))
    causes = [
        check.get("mechanism_statement") for check in checks if check["supports_cause"]
    ]
    if conclusion_status not in ("supported", "inconclusive") or (
        conclusion_status == "supported" and not causes
    ):
        raise WorkspaceError(
            "packet_invalid: confirmed root cause needs the existing causal rule"
        )
    report = {
        "root_cause": "\n".join(causes)
        if conclusion_status == "supported"
        else "Unresolved",
        "affected_resources": sorted(
            {c["resource_id"] for c in claims if c.get("resource_id")}
        ),
        "evidence_summary": "\n".join(
            item["content"] for item in accepted if item["section"] == "observations"
        ),
        "investigation_summary": "Packet %s; unresolved questions: %s"
        % (packet_id, unresolved_question_ids),
        "detection": bool(claims),
    }
    return {"final_report": report, "report_claims": claims, "report_items": accepted}


@tool
async def prepare_report_packet(
    claims: list[dict],
    observation_refs: dict[str, list[str]],
    previous_packet_id: str | None = None,
    question_dispositions: dict[str, str] | None = None,
) -> dict:
    """Validate a report batch from registered observations; observations use program-generated text.

    observation_refs maps each proposal claim_id to its real observation IDs.
    Each claim uses claim_id, type (observation/hypothesis/exclusion/causal),
    resource_id (verified ID), content (nonempty), and limitations (nonempty list).
    Use type=observation for counters; supporting evidence IDs are program-filled.
    Append a batch with previous_packet_id; stale packets require renewed review.
    """
    from .runtime import current

    b = current.get()
    if b is None or b.role != "supervisor":
        raise WorkspaceError("only Supervisor may prepare report packets")
    return await prepare_packet(
        b,
        claims,
        observation_refs,
        previous_packet_id=previous_packet_id,
        question_dispositions=question_dispositions,
    )
