"""Small-batch reference registration through the existing strict stage transaction."""

from functools import partial
from langchain_core.tools import tool
from pydantic import Field, model_validator
from models.investigation import Contract
from .repository import WorkspaceError
from .runtime import current
from .evidence_references import resolve, descriptor
from .observations import verify_observation
from .investigations import active_checkpoint, commit_stage, digest
from .candidate_submission import submit_observations
from workflow.tool_execution import contract_tool


class ReferenceRegistration(Contract):
    ref_ids: list[str] = Field(min_length=1)
    question_ids: list[str] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.ref_ids)) != len(self.ref_ids) or len(
            set(self.question_ids)
        ) != len(self.question_ids):
            raise ValueError("invalid_shape: reference and question IDs must be unique")
        return self


async def allowed_questions(b):
    questions = {
        q["id"]
        for q in await b.repository.all(b.incident_id, "question")
        if q.get("run_id") == b.run_id
    }
    if b.role == "worker":
        task = await b.repository.read(b.incident_id, b.task_id, "task")
        questions.intersection_update(task.get("question_ids", []))
    elif b.role != "supervisor":
        return set()
    return questions


async def register(b, submission):
    b.remaining()
    from .investigation_phases import guard

    await guard(b, "record_observations")
    submission = submission.model_copy(
        update={"limitations": list(dict.fromkeys(submission.limitations))}
    )
    from .investigation_phases import active

    if active(b) and b.phase_budget["phase"] in ("finished", "stopped"):
        raise WorkspaceError("phase_disallowed: registration after role stopped")
    if not set(submission.question_ids) <= await allowed_questions(b):
        raise WorkspaceError("invalid_question: questions outside current role/task")
    rows, proposals = [], []
    for rid in submission.ref_ids:
        row = await resolve(b, rid)
        p = {
            **row["proposal"],
            "local_id": rid,
            "limitations": list(
                dict.fromkeys(
                    row["proposal"].get("limitations", []) + submission.limitations
                )
            ),
        }
        check = await verify_observation(
            b, p, question_ids=submission.question_ids, owner=b.role
        )
        if "accepted" not in check:
            raise WorkspaceError("invalid_shape: " + rid + ": " + str(check))
        rows.append(row)
        proposals.append(p)
    key = (
        "refs-"
        + digest(
            [
                sorted(submission.ref_ids),
                sorted(submission.question_ids),
                sorted(submission.limitations),
                b.run_id,
                b.role,
                b.task_id,
                b.attempt_id,
            ]
        )[:24]
    )
    from .investigations import _existing_commit

    previous = await _existing_commit(b, b.role, key)
    if previous:
        result = {"accepted": True, "duplicate": True, "id_map": previous["id_map"]}
    else:
        opening = await active_checkpoint(b, b.role)
        result = await submit_observations(
            proposals,
            submission.question_ids,
            opening,
            client_commit_key=key,
            stage_goal="Persist model-selected exact references without assigning causal meaning",
            commit=partial(commit_stage, b),
        )
    if not result.get("accepted"):
        raise WorkspaceError("invalid_shape: reference stage rejected: " + str(result))
    observations = []
    for rid in submission.ref_ids:
        oid = result["id_map"][rid]
        formal = await b.repository.read(b.incident_id, oid, "observation")
        observations.append(
            {
                "ref_id": rid,
                "observation_id": oid,
                "exact_summary": {
                    "field": formal["field"],
                    "value": formal["field_value"],
                    "scope": formal["scope"],
                },
            }
        )
    return {
        "status": "accepted",
        "accepted": True,
        "observations": observations,
        "observation_ids": [r["observation_id"] for r in observations],
        "limitations": submission.limitations,
        "already_registered": bool(result.get("duplicate")),
    }


@contract_tool(args_schema=ReferenceRegistration)
async def record_observations(
    ref_ids: list[str], question_ids: list[str], limitations: list[str] = ()
) -> dict:
    """Register selected exact, previously delivered refs for current questions; no source fields to assemble."""
    return await register(
        current.get(),
        ReferenceRegistration(
            ref_ids=ref_ids, question_ids=question_ids, limitations=limitations
        ),
    )


@tool
async def read_evidence_reference(
    ref_id: str, char_offset: int = 0, max_chars: int = 0
) -> dict:
    """Redisplay an authorized stable reference exactly; no remote query or new version."""
    from .investigation_phases import guard

    await guard(current.get(), "read_evidence_reference")
    from .paging import resolve_max_chars, serialized_chars, fit_text
    import json

    if char_offset < 0:
        raise WorkspaceError("invalid_shape: negative reference character offset")
    from .receipts import new_receipt, publish_read

    b = current.get()
    row = await resolve(b, ref_id)
    info = descriptor(row)
    receipt = new_receipt(
        b,
        tool_name="read_evidence_reference",
        source_kind="evidence_reference",
        source_id=ref_id,
        source_hash=row["proposal"]["source_ref"].get("source_hash"),
        source_scope=info.get("scope"),
        source_limitations=info.get("source_limits"),
    )
    body = {"origin": "current", "read_receipt_id": receipt["id"], **info}
    cap = resolve_max_chars(max_chars)
    if not char_offset and serialized_chars(body) <= cap:
        receipt.update(
            returned_paths=[row["proposal"]["source_ref"].get("json_pointer")],
            truncated=False,
        )
        return await publish_read(b, receipt, body, max_chars=cap)
    text = json.dumps(body.pop("value"), ensure_ascii=False)

    def build(chunk):
        end = char_offset + len(chunk)
        return {
            **body,
            "text": chunk,
            "range": [char_offset, end],
            "range_unit": "serialized_value_chars",
            "truncated": end < len(text),
            "next_char_offset": end if end < len(text) else None,
        }

    if char_offset > len(text):
        raise WorkspaceError("invalid_shape: reference offset past the end")
    chunk, _, _ = fit_text(text, char_offset, cap, build)
    if chunk is None:
        raise WorkspaceError("invalid_shape: raise max_chars for reference metadata")
    payload = build(chunk)
    receipt.update(
        range=payload["range"],
        range_unit=payload["range_unit"],
        truncated=payload["truncated"],
        next=payload["next_char_offset"],
    )
    return await publish_read(b, receipt, payload, max_chars=cap)


@tool
async def find_evidence_references(
    evidence_id: str = "", query: str = "", offset: int = 0, limit: int = 10
) -> dict:
    """Find this role/task's already-delivered exact refs; clipped views do not revoke them."""
    b = current.get()
    from .investigation_phases import guard

    await guard(b, "find_evidence_references")
    if not 1 <= limit <= 50 or offset < 0:
        raise WorkspaceError("invalid_shape: bounded reference page required")
    from .evidence_references import reader_key, current_reader

    rows = [
        r
        for r in await b.repository.all(b.incident_id, "candidate")
        if r.get("run_id") == b.run_id
        and any(reader_key(p) == current_reader(b) for p in r["readers"])
    ]
    rows = [
        r
        for r in rows
        if (
            not evidence_id
            or r["proposal"]["source_ref"].get("evidence_id") == evidence_id
        )
        and query.lower() in str(r["proposal"]).lower()
    ]
    entries = [
        {
            "ref_id": r["id"],
            "source_ref": r["proposal"]["source_ref"],
            "aggregate": r["proposal"].get("aggregate"),
            "limitations": r["proposal"].get("limitations", []),
        }
        for r in rows
    ]
    from .paging import fit_list, resolve_max_chars

    def build(items):
        end = offset + len(items)
        return {
            "items": items,
            "total": len(entries),
            "next": end if end < len(entries) else None,
            "truncated": end < len(entries),
            "origin": "current",
            "note": "Existing read proofs only; search does not register observations.",
        }

    items, _, too_small = fit_list(
        entries, offset, min(len(entries), offset + limit), resolve_max_chars(0), build
    )
    if too_small:
        raise WorkspaceError("invalid_shape: narrow reference query to fit metadata")
    return build(items)
