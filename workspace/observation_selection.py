"""One visible-field submission path for every role and telemetry type."""

import json
from .artifacts import read_artifact_raw
from .repository import WorkspaceError
from .observations import verify_observation
from .investigations import commit_stage, digest
from .candidate_submission import (
    select_candidates,
    CandidateSelectionError,
    submit_observations,
)
from functools import partial


async def current_candidates(b):
    if not b.last_context_id:
        return []
    context = await b.repository.read(b.incident_id, b.last_context_id, "context")
    if (
        context.get("run_id"),
        context.get("role"),
        context.get("task_id"),
        context.get("attempt_id"),
    ) != (b.run_id, b.role, b.task_id, b.attempt_id):
        raise WorkspaceError("candidate context owner mismatch")
    messages = await read_artifact_raw(b, context["visible_messages_artifact_id"])
    for m in messages:
        if isinstance(m.get("content"), str) and m["content"].startswith(
            "Incident workbench"
        ):
            from context.exact_representation import expand

            return expand(json.loads(m["content"].split("\n", 1)[1])).get(
                "current_candidates", []
            )
    return []


async def register_observations(b, ids, explicit, question_ids, opening, limitations):
    offered = await current_candidates(b)
    try:
        selected = (
            select_candidates(offered, ids, id_field="candidate_id") if ids else []
        )
    except CandidateSelectionError as exc:
        raise WorkspaceError(
            "invalid current candidate selection: " + exc.reason
        ) from exc
    proposals = [
        {k: v for k, v in row.items() if k not in ("candidate_id", "ref_id")}
        for row in selected
    ]
    for i, raw in enumerate(explicit):
        proposal = (
            raw.model_dump(exclude_unset=True)
            if hasattr(raw, "model_dump")
            else dict(raw)
        )
        proposals.append(proposal)
    if not proposals:
        return []
    local_ids = [p["local_id"] for p in proposals]
    if len(set(local_ids)) != len(local_ids):
        raise WorkspaceError("duplicate observation local IDs")
    for p in proposals:
        p["limitations"] = list(
            dict.fromkeys(list(p.get("limitations") or []) + limitations)
        )
        outcome = await verify_observation(
            b, p, question_ids=question_ids, owner=b.role
        )
        if "accepted" not in outcome:
            raise WorkspaceError(
                "observation rejected: " + json.dumps(outcome, ensure_ascii=False)
            )
    outcome = await submit_observations(
        proposals,
        question_ids,
        opening,
        client_commit_key="selection-" + digest([proposals, question_ids])[:24],
        stage_goal="Register selected exact displayed observations; no causal conclusion",
        commit=partial(commit_stage, b),
    )
    if not outcome.get("accepted"):
        raise WorkspaceError(
            "stage rejected: " + json.dumps(outcome, ensure_ascii=False)
        )
    return [outcome["id_map"][p["local_id"]] for p in proposals]


async def selected_observations(b, ids, question_ids):
    rows = []
    for oid in ids:
        row = await b.repository.read(b.incident_id, oid, "observation")
        if row.get("run_id") != b.run_id or not set(
            row.get("question_ids", [])
        ).intersection(question_ids):
            raise WorkspaceError("observation belongs to another run/question")
        rows.append(row)
    return rows


async def selected_sources(b, ids, question_ids):
    rows = await selected_observations(b, ids, question_ids)
    return sorted({row["source_ref"]["evidence_id"] for row in rows})
