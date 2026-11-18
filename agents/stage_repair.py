"""Shared stage submission and one local question-assignment Repair."""

import asyncio
import copy
import re
from pydantic import ValidationError
from config import settings
from context.request_budget import structured_request
from models.stage_repair import StageRepair
from workspace import investigations
from workspace import stage_pending
from workspace.artifacts import read_artifact_raw, write_artifact
from workspace.repository import WorkspaceError
from workspace.runtime import invoke_stage_repair


async def repair_scope(b, proposal, outcome):
    """Only a question-assignment error is eligible; prove every other field first."""
    paths = set()
    for error in outcome.get("field_errors", []):
        path = error["path"]
        if not re.fullmatch(r"/judgments/(0|[1-9]\d*)/question_id", path):
            raise WorkspaceError("stage_repair_not_applicable: " + path)
        paths.add(path)
    registered = {r["id"]: r for r in await investigations.questions(b)}
    local = {
        r.get("local_id"): r
        for r in proposal.get("question_proposals", [])
        if r.get("local_id")
    }
    ids = proposal.get("question_ids") or []
    candidates = []
    for ref in ids:
        if ref in registered:
            row = registered[ref]
            candidates.append(
                {
                    "id": ref,
                    "registered": True,
                    **{
                        k: row.get(k)
                        for k in (
                            "text",
                            "scope",
                            "resource_ids",
                            "owner_task_ids",
                            "status",
                        )
                    },
                }
            )
        elif ref in local:
            candidates.append({"id": ref, "registered": False, **local[ref]})
    if not candidates:
        raise WorkspaceError("stage_repair_cannot_repair: no legal question candidates")
    stored = {
        r["id"]: r
        for r in await investigations.observations_for(
            b,
            [
                ref
                for row in proposal.get("judgments", [])
                for ref in row.get("supporting_observation_ids", [])
                + row.get("opposing_observation_ids", [])
            ],
        )
    }
    proposed = {
        r.get("local_id"): r
        for r in proposal.get("observation_proposals", [])
        if r.get("local_id")
    }
    allowed = {}
    for path in sorted(paths):
        row = proposal["judgments"][int(path.split("/")[2])]
        values = set(c["id"] for c in candidates)
        for ref in row.get("supporting_observation_ids", []) + row.get(
            "opposing_observation_ids", []
        ):
            if ref in stored:
                if stored[ref].get("run_id") != b.run_id:
                    raise WorkspaceError(
                        "stage_repair_not_applicable: observation from another run"
                    )
                values.intersection_update(stored[ref].get("question_ids") or [])
            elif ref in proposed:
                values.intersection_update(proposed[ref].get("question_ids") or ids)
            else:
                raise WorkspaceError("stage_repair_not_applicable: unknown observation")
        if not values:
            raise WorkspaceError(
                "stage_repair_cannot_repair: no common question for supporting/opposing facts"
            )
        allowed[path] = sorted(values)
    trial = copy.deepcopy(proposal)
    for path, values in allowed.items():
        trial["judgments"][int(path.split("/")[2])]["question_id"] = values[0]
    preflight = await investigations.commit_stage(
        b, trial, validate_only=True, phase="repair_preflight"
    )
    if not preflight.get("accepted"):
        raise WorkspaceError(
            "stage_repair_not_applicable: other validation failed: "
            + str(preflight["field_errors"])
        )
    return allowed, candidates


async def save_status(b, pending, status, **details):
    row = await b.repository.read(b.incident_id, pending["id"], "stage_pending")
    row.update(status=status, **details)
    return await b.repository.save(b.incident_id, "stage_pending", row, row["revision"])


async def submit_stage(b, model, proposal, *, phase="stage", raw_output=None):
    supplied = proposal
    proposal = investigations.normalize_proposal(proposal)
    if proposal is None:
        if supplied is None:
            return None
        return await investigations.reject(
            b,
            supplied,
            b.role,
            phase,
            "stage_repair_not_applicable",
            [{"field": "stage_proposal", "reason": "unparseable stage object"}],
        )
    from context.lifecycle import effective_mode

    if effective_mode(b) != "active":
        return await investigations.apply_stage_proposal(b, proposal, phase=phase)
    # An already accepted derived version has the original digest as well.
    for pending in await b.repository.all(b.incident_id, "stage_pending"):
        if (
            all(pending.get(k) == v for k, v in stage_pending.identity(b).items())
            and pending["proposal_digest"] == investigations.digest(proposal)
            and pending.get("outcome")
        ):
            return pending["outcome"]
    outcome = await investigations.commit_stage(b, proposal, phase=phase)
    if outcome.get("accepted"):
        return outcome
    if outcome.get("reason") == "checkpoint_conflict":
        return {
            **outcome,
            "reason": "stage_repair_conflict",
            "repair_error": "stage_repair_conflict: original checkpoint/key conflict",
        }
    pending = None
    try:
        if await stage_pending.stage_slot_used(b):
            raise WorkspaceError(
                "stage_repair_budget_exhausted: this real stage already used Repair"
            )
        allowed, candidates = await repair_scope(b, proposal, outcome)
        pending = await stage_pending.create(
            b, proposal, outcome, allowed, candidates, raw_output=raw_output
        )
        from workspace.model_recovery import step_available

        try:
            step_available(b)
        except WorkspaceError as error:
            raise WorkspaceError(
                "stage_repair_budget_exhausted: no role steps remain"
            ) from error
        pending = await stage_pending.reserve(b, pending)
        from .stage_outputs import raw_structured, extract

        structured = raw_structured(model, StageRepair)
        patch = await invoke_stage_repair(
            structured, pending, structured_request(StageRepair)
        )
        raw_patch = patch
        patch_artifact = await write_artifact(
            b, {"pending_id": pending["id"], "raw_response": raw_patch}
        )
        await b.repository.save(b.incident_id, "artifact", patch_artifact)
        try:
            patch = extract(patch, StageRepair)
        except WorkspaceError as exc:
            raise WorkspaceError("stage_repair_invalid_patch: " + str(exc)) from exc
        if hasattr(patch, "model_dump"):
            patch = patch.model_dump()
        merged = stage_pending.merge(pending, proposal, patch)
        await b.repository.transact(
            lambda db: stage_pending.check_guard(b, db, pending)
        )
        for ref in pending["versions"]:
            if ref["kind"] == "artifact":
                await read_artifact_raw(b, ref["id"])
        derived = await write_artifact(
            b,
            {
                "pending_id": pending["id"],
                "proposal_version": 2,
                "original_artifact_id": pending["original_artifact_id"],
                "proposal": merged,
            },
        )
        await b.repository.save(b.incident_id, "artifact", derived)
        result = await investigations.commit_stage(
            b,
            merged,
            phase=phase + "_repaired",
            source_context_id=pending["source_context_id"],
            repair_guard=pending,
        )
        if not result.get("accepted"):
            raise WorkspaceError(
                "stage_repair_conflict"
                if result.get("reason") == "checkpoint_conflict"
                else "stage_repair_invalid_patch: full revalidation rejected: "
                + str(result["field_errors"])
            )
        result.update(
            repair_pending_id=pending["id"], patch_artifact_id=patch_artifact["id"]
        )
        await save_status(
            b,
            pending,
            "accepted",
            outcome=result,
            patch_artifact_id=patch_artifact["id"],
            derived_artifact_id=derived["id"],
            repair_context_id=b.last_context_id,
        )
        return result
    except asyncio.CancelledError:
        if pending:
            await asyncio.shield(save_status(b, pending, "cancelled"))
        raise
    except ValidationError as exc:
        error = "stage_repair_invalid_patch: " + str(exc)
    except WorkspaceError as exc:
        error = str(exc)
        if "repair_context_unsatisfiable" in error:
            error = "repair_context_unsatisfiable: required Repair input exceeds limit"
    except BaseException as exc:
        # Preserve model timeout/rate limit codes and cancellation policy.
        if pending:
            await save_status(
                b,
                pending,
                "failed",
                error=str(exc),
                repair_context_id=b.last_context_id,
            )
        raise
    if pending:
        await save_status(
            b, pending, "failed", error=error, repair_context_id=b.last_context_id
        )
    await b.repository.transact(
        lambda db: b.repository.event(
            db,
            b.incident_id,
            b.task_id or b.run_id,
            "stage_repair_failed",
            {
                "reason": error,
                "pending_id": (pending or {}).get("id"),
                "proposal_artifact_id": outcome["proposal_artifact_id"],
            },
        )
    )
    return {
        **outcome,
        "reason": error.split(":", 1)[0],
        "repair_error": error,
        "repair_pending_id": (pending or {}).get("id"),
    }
