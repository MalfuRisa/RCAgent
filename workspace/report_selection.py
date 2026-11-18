"""Build a report packet from verified model-selected observations and typed claims."""

import json

from .repository import WorkspaceError
from .observation_selection import register_observations, selected_observations
from .report_packets import prepare_packet


async def supplemental_items(b, batch, qids, sources):
    """Validate limits/advice before accepting the report, not after selection."""
    from .report_checks import check_items

    tasks = [
        t["id"]
        for t in await b.repository.all(b.incident_id, "task")
        if set(t.get("question_ids") or []) & set(qids)
    ]
    refs = {
        "claim_ids": [],
        "task_ids": tasks,
        "evidence_ids": [] if tasks else sorted(sources),
    }
    items = [
        {"section": section, "content": text, **refs}
        for section, lines in (
            ("limitations", batch.limitations),
            ("next_steps", batch.next_steps),
        )
        for text in lines
    ]
    _, errors = await check_items(b, items, [])
    if errors:
        raise WorkspaceError(
            "report_supplement_invalid: "
            + json.dumps({"errors": errors[:3]}, ensure_ascii=False)
        )
    return items


async def prepare_selection(b, batch, opening):
    questions = [
        q
        for q in await b.repository.all(b.incident_id, "question")
        if q.get("run_id") == b.run_id
    ]
    qids = [q["id"] for q in questions]
    if set(batch.unresolved_reasons) != set(qids) or any(
        not s.strip() for s in batch.unresolved_reasons.values()
    ):
        raise WorkspaceError("Supervisor must explicitly dispose every question")
    ids = await register_observations(
        b, batch.candidate_ids, batch.observations, qids, opening, batch.limitations
    )
    ids = list(dict.fromkeys(ids + batch.observation_ids))
    for refs in batch.claim_observation_refs.values():
        ids = list(dict.fromkeys(ids + refs))
    observations = await selected_observations(b, ids, qids)
    groups = {}
    sources = {}
    for row in observations:
        source_id = row["source_ref"]["evidence_id"]
        if source_id not in sources:
            sources[source_id] = await b.repository.read(
                b.incident_id, source_id, "evidence"
            )
        for resource in sources[source_id].get("resource_ids", []):
            groups.setdefault(resource, []).append(row["id"])
    claims = [
        {
            "claim_id": "observed_" + str(i),
            "type": "observation",
            "resource_id": rid,
            "content": "Selected exact observed fields",
            "limitations": batch.limitations,
        }
        for i, rid in enumerate(groups)
    ]
    refs = {c["claim_id"]: groups[c["resource_id"]] for c in claims}
    for proposed in batch.claims:
        if (
            not proposed.claim_id
            or proposed.claim_id in refs
            or not batch.claim_observation_refs.get(proposed.claim_id)
        ):
            raise WorkspaceError(
                "additional claims need unique IDs and registered observation references"
            )
        body = proposed.model_dump()
        body["limitations"] = list(
            dict.fromkeys(body["limitations"] + batch.limitations)
        )
        claims.append(body)
        refs[body["claim_id"]] = batch.claim_observation_refs[body["claim_id"]]
    extra = await supplemental_items(b, batch, qids, sources)
    packet = await prepare_packet(
        b,
        claims,
        refs,
        question_dispositions=batch.unresolved_reasons,
        supplemental_items=extra,
    )
    if packet.get("status") != "accepted":
        raise WorkspaceError("report packet rejected: " + str(packet))
    return {
        "status": "accepted",
        "packet_id": packet["packet_id"],
        "next_steps": batch.next_steps,
    }
