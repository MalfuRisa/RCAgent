"""Mechanical adaptation of explicitly selected formal work to the existing report gate."""

from models.investigation import ReportSubmission
from models.schemas import EvidenceClaim
from .observation_selection import selected_observations
from .report_selection import prepare_selection
from .repository import WorkspaceError, new_id
from .resources import scope, allowed


async def finalize(b, submission):
    questions = [
        q
        for q in await b.repository.all(b.incident_id, "question")
        if q.get("run_id") == b.run_id
    ]
    qids = [q["id"] for q in questions]
    # Resolve every reference before constructing any report packet. A claim's
    # observation selection is explicit too; it is not inferred from evidence.
    await selected_observations(b, submission.observation_ids, qids)
    claims, refs = [], {}
    access = await scope(b)
    for draft in submission.claims:
        resource = await b.repository.read(b.incident_id, draft.resource_id, "resource")
        if (
            resource.get("identity_status") != "resolved"
            or resource.get("replaced_by")
            or not allowed(access, resource.get("cluster"), resource.get("namespace"))
        ):
            raise WorkspaceError(
                "report_claim_resource_invalid: registered in-scope identity required"
            )
        rows = await selected_observations(b, draft.observation_ids, qids)
        sources = sorted(
            {
                row["source_ref"].get("evidence_id")
                for row in rows
                if row["source_ref"].get("evidence_id")
            }
        )
        local_id = new_id("draft_claim")
        claims.append(
            EvidenceClaim(
                claim_id=local_id,
                type=draft.type,
                content=draft.content,
                resource_id=draft.resource_id,
                supporting_evidence_ids=sources,
                limitations=list(
                    dict.fromkeys(submission.limitations + draft.limitations)
                ),
                mechanism=draft.mechanism,
            )
        )
        refs[local_id] = draft.observation_ids
    internal = ReportSubmission(
        observation_ids=submission.observation_ids,
        claims=claims,
        claim_observation_refs=refs,
        unresolved_reasons=submission.unresolved_reasons,
        limitations=submission.limitations,
        next_steps=submission.next_steps,
    )
    # Empty candidate/proposal lists deliberately reuse only the old assembly
    # and gate. This path never registers new observations.
    return await prepare_selection(b, internal, None)
