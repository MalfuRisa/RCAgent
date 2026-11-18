"""Investigation records and the stage handoff transaction (PRD §19.4, §19.6).

Questions, observations, judgments, stage commits, checkpoints and report packets
are ordinary records in the existing SQLite store (no new database, no graph
engine). The stage commit is the one place where a validated handoff becomes
durable: it checks ids and revisions, verifies every proposed observation against
its original, merges existing opposition the model may have omitted, and writes
commit + checkpoint + active head + material usage in a single transaction.

Older originals, artifacts and read receipts are never deleted by a handoff: a
commit only changes which material is *active*, and only for the exact
owner + question + source version + field/range it has evidence for.
"""

import hashlib
import json
from typing import get_args
from models.stage_proposals import QuestionState, JudgmentState, DispositionState, Completion

from .observations import must_handle, verify_observation
from .repository import WorkspaceError, new_id, utcnow
from .stage_validation import (
    owner_scope,
    validate_local_ids,
    validate_judgments,
    validate_dispositions,
)

KINDS = ("question", "observation", "judgment", "stage_commit", "checkpoint", "report_packet")
QUESTION_STATES = get_args(QuestionState)
JUDGMENT_STATES = get_args(JudgmentState)
USAGE_STATES = ("unread", "active", "covered_by_commit", "reopened")
DISPOSITION_STATES = get_args(DispositionState)
COMPLETION_STATES = get_args(Completion)


def usage_key(owner, question_id, source_id, source_version, field):
    return "|".join(
        str(part) for part in (owner, question_id, source_id, source_version, field or "*")
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


async def save_question(
    binding,
    *,
    text,
    resource_ids=(),
    scope=None,
    origin_refs=(),
    owner_task_ids=(),
    parent_question_id=None,
    status="open",
    producer_role=None,
):
    if status not in QUESTION_STATES:
        raise WorkspaceError("question status must be one of %s" % ", ".join(QUESTION_STATES))
    if not isinstance(text, str) or not text.strip():
        raise WorkspaceError("question text is required")
    record = {
        "id": new_id("question"),
        "incident_id": binding.incident_id,
        "run_id": binding.run_id,
        "producer_role": producer_role or binding.role,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "created_at": utcnow(),
        "text": text,
        "parent_question_id": parent_question_id,
        "resource_ids": list(resource_ids),
        "scope": scope or {},
        "origin_refs": list(origin_refs),
        "status": status,
        "owner_task_ids": list(owner_task_ids),
    }
    return await binding.repository.save(
        binding.incident_id, "question", record, action="question_registered"
    )


async def set_question_status(binding, question_id, status):
    if status not in QUESTION_STATES:
        raise WorkspaceError("question status must be one of %s" % ", ".join(QUESTION_STATES))
    question = await binding.repository.read(binding.incident_id, question_id, "question")
    question["status"] = status
    return await binding.repository.save(
        binding.incident_id, "question", question, question["revision"], action="question_status"
    )


async def save_judgment(
    binding,
    *,
    question_id,
    type,
    content,
    supporting_observation_ids=(),
    opposing_observation_ids=(),
    evidence_refs=(),
    limitations=(),
    status="proposed",
    producer_role=None,
):
    if status not in JUDGMENT_STATES:
        raise WorkspaceError("judgment status must be one of %s" % ", ".join(JUDGMENT_STATES))
    record = {
        "id": new_id("judgment"),
        "incident_id": binding.incident_id,
        "run_id": binding.run_id,
        "producer_role": producer_role or binding.role,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "created_at": utcnow(),
        "question_id": question_id,
        "type": type,
        "content": content,
        "supporting_observation_ids": list(supporting_observation_ids),
        "opposing_observation_ids": list(opposing_observation_ids),
        "evidence_refs": list(evidence_refs),
        "limitations": list(limitations),
        "status": status,
        "is_model_interpretation": True,
    }
    return await binding.repository.save(
        binding.incident_id, "judgment", record, action="judgment_recorded"
    )


async def questions(binding, ids=None):
    rows = await binding.repository.all(binding.incident_id, "question")
    rows = [row for row in rows if row.get("run_id") == binding.run_id]
    if ids is None:
        return rows
    wanted = set(ids)
    return [row for row in rows if row["id"] in wanted]


async def judgments_for(binding, question_ids):
    wanted = set(question_ids)
    return [
        row
        for row in await binding.repository.all(binding.incident_id, "judgment")
        if row.get("question_id") in wanted and row.get("run_id") == binding.run_id
    ]


async def observations_for(binding, observation_ids):
    wanted = set(observation_ids)
    return [
        row
        for row in await binding.repository.all(binding.incident_id, "observation")
        if row["id"] in wanted
    ]


async def mark_material(
    binding, *, owner, question_id, source_id, source_version, field=None, state="active", note=None
):
    """Record material use on the exact (owner, question, version, field) relation."""
    if state not in USAGE_STATES:
        raise WorkspaceError("material state must be one of %s" % ", ".join(USAGE_STATES))
    record = {
        "id": usage_key(owner_scope(binding, owner), question_id, source_id, source_version, field),
        "incident_id": binding.incident_id,
        "run_id": binding.run_id,
        "producer_role": owner,
        "task_id": binding.task_id,
        "attempt_id": binding.attempt_id,
        "created_at": utcnow(),
        "owner": owner,
        "question_id": question_id,
        "source_id": source_id,
        "source_version": source_version,
        "field": field,
        "state": state,
        "note": note,
    }
    return await binding.repository.save(
        binding.incident_id, "material_usage", record, action="material_" + state
    )


async def materials(binding, *, owner=None, states=None):
    rows = await binding.repository.all(binding.incident_id, "material_usage")
    selected = [row for row in rows if row.get("run_id") == binding.run_id]
    if owner is not None:
        selected = [row for row in selected if same_owner(binding, row, owner)]
    if states is not None:
        wanted = set(states)
        selected = [row for row in selected if row.get("state") in wanted]
    return sorted(selected, key=lambda row: row["id"])


async def active_checkpoint(binding, owner):
    for row in await binding.repository.all(binding.incident_id, "checkpoint"):
        if same_owner(binding, row, owner) and row.get("is_head"):
            return row
    return None


def same_owner(binding, row, owner):
    return (row.get("owner"), row.get("run_id"), row.get("task_id"), row.get("attempt_id")) == (
        owner,
        binding.run_id,
        binding.task_id,
        binding.attempt_id,
    )


def _field_errors(proposal):
    errors = []
    key = proposal.get("client_commit_key")
    if not isinstance(key, str) or not key.strip():
        errors.append({"field": "client_commit_key", "reason": "required for idempotency"})
    if not isinstance(proposal.get("stage_goal"), str) or not proposal["stage_goal"].strip():
        errors.append({"field": "stage_goal", "reason": "required"})
    if proposal.get("completion") not in COMPLETION_STATES:
        errors.append({"field": "completion", "reason": "must be complete or partial"})
    question_ids = proposal.get("question_ids")
    if not isinstance(question_ids, list) or not question_ids:
        errors.append(
            {"field": "question_ids", "reason": "at least one registered question is required"}
        )
    elif any(not isinstance(ref, str) or not ref for ref in question_ids):
        errors.append({"field": "question_ids", "reason": "question IDs must be nonempty strings"})
    for name in (
        "question_proposals",
        "observation_proposals",
        "judgments",
        "material_dispositions",
    ):
        value = proposal.get(name) or []
        if not isinstance(value, list):
            errors.append({"field": name, "reason": "must be a list"})
    from config import settings
    from context.stage_budget import reserve
    from context.budget_policy import hard_limit

    available = hard_limit() - sum(reserve().values())
    measured = len(json.dumps(proposal, ensure_ascii=False, default=str))
    if measured > available:
        errors.append(
            {
                "field": "proposal",
                "reason": "handoff proposal exceeds measured control capacity",
                "proposal_chars": measured,
                "available_chars": max(0, available),
            }
        )
    return errors or validate_local_ids(proposal)


async def _existing_commit(binding, owner, client_commit_key):
    for row in await binding.repository.all(binding.incident_id, "stage_commit"):
        if same_owner(binding, row, owner) and row.get("client_commit_key") == client_commit_key:
            return row
    return None


async def _validate_references(binding, question_ids, judgments):
    errors = []
    registered = {row["id"] for row in await questions(binding)}
    for question_id in question_ids:
        if question_id not in registered:
            errors.append({"field": "question_ids", "reason": "unknown question %s" % question_id})
    for index, judgment in enumerate(judgments):
        if judgment.get("question_id") not in registered:
            errors.append(
                {
                    "field": "judgments[%d].question_id" % index,
                    "reason": "judgment must name a registered question",
                }
            )
        if judgment.get("status", "proposed") not in JUDGMENT_STATES:
            errors.append(
                {"field": "judgments[%d].status" % index, "reason": "unknown judgment status"}
            )
    return errors


def _temporary_map(proposal):
    mapping = {}
    for group in ("question_proposals", "observation_proposals", "judgments"):
        for item in proposal.get(group) or []:
            if not isinstance(item, dict):
                continue
            local = item.get("local_id")
            if local:
                prefix = {
                    "question_proposals": "question",
                    "observation_proposals": "observation",
                    "judgments": "judgment",
                }[group]
                mapping[local] = new_id(prefix)
    return mapping


def _resolve_ids(values, mapping):
    return [mapping.get(value, value) for value in values or []]


async def commit_stage(
    binding,
    proposal,
    *,
    owner=None,
    phase="stage",
    source_context_id=None,
    repair_guard=None,
    validate_only=False,
):
    """Validate and persist one stage handoff; nothing partial ever switches.

    Returns ``{'accepted': True, commit_id, checkpoint_id, id_map, next_stage_id,
    workspace_revision, included_opposition}`` or a rejection that keeps the
    previous active head and records the proposal for audit.
    """
    owner = owner or binding.role
    from pydantic import ValidationError
    from models.stage_proposals import StageProposal, field_errors

    proposal = normalize_proposal(proposal)
    try:
        validated = StageProposal.model_validate(proposal)
    except ValidationError as exc:
        return await reject(
            binding, proposal, owner, phase, "checkpoint_invalid", field_errors(exc)
        )
    # The shared schema advertises partial as the default. Apply that mechanical
    # default before the legacy business check; preserve nested fields_set so
    # missing expected_value never becomes an invented explicit null.
    if "completion" not in proposal:
        proposal = {**proposal, "completion": validated.completion}
    from config import settings

    if (getattr(binding, "lifecycle_mode", None) or settings.CONTEXT_LIFECYCLE_MODE) != "active":
        return await reject(
            binding,
            proposal,
            owner,
            phase,
            "lifecycle_inactive",
            [{"field": "mode", "reason": "stage commits require explicit active mode"}],
        )
    errors = _field_errors(proposal)
    if errors:
        return await reject(binding, proposal, owner, phase, "checkpoint_invalid", errors)
    existing = await _existing_commit(binding, owner, proposal["client_commit_key"])
    if existing is not None:
        if existing.get("proposal_digest") == digest(proposal):
            return {
                "accepted": True,
                "duplicate": True,
                "commit_id": existing["id"],
                "checkpoint_id": existing.get("checkpoint_id"),
                "id_map": existing.get("id_map") or {},
                "next_stage_id": existing.get("next_stage_id"),
                "workspace_revision": existing.get("workspace_revision"),
            }
        return await reject(
            binding,
            proposal,
            owner,
            phase,
            "checkpoint_conflict",
            [
                {
                    "field": "client_commit_key",
                    "reason": "the same key was already used with different content",
                }
            ],
            retained=existing.get("checkpoint_id"),
        )
    head = await active_checkpoint(binding, owner)
    expected = proposal.get("expected_checkpoint_id")
    expected_revision = proposal.get("expected_revision")
    if (
        (head is not None and expected != head["id"])
        or (head is None and expected not in (None, 0, ""))
        or (head is not None and expected_revision != head.get("revision"))
        or (head is None and expected_revision not in (None, 0))
    ):
        return await reject(
            binding,
            proposal,
            owner,
            phase,
            "checkpoint_conflict",
            [
                {
                    "field": "expected_checkpoint_id",
                    "reason": "does not match the active head",
                    "expected": expected,
                    "actual": (head or {}).get("id"),
                    "expected_revision": expected_revision,
                    "actual_revision": (head or {}).get("revision"),
                }
            ],
            retained=(head or {}).get("id"),
        )
    errors = []
    mapping = _temporary_map(proposal)
    question_ids = _resolve_ids(proposal["question_ids"], mapping)
    for index, item in enumerate(proposal.get("question_proposals") or []):
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            errors.append(
                {
                    "field": "question_proposals[%d].text" % index,
                    "reason": "question text is required",
                }
            )
    if errors:
        return await reject(binding, proposal, owner, phase, "checkpoint_invalid", errors)
    registered_questions = await questions(binding)
    known_questions = {row["id"] for row in registered_questions} | {
        mapping[item["local_id"]]
        for item in proposal.get("question_proposals") or []
        if isinstance(item, dict) and item.get("local_id")
    }
    for question_id in question_ids:
        if question_id not in known_questions:
            errors.append({"field": "question_ids", "reason": "unknown question %s" % question_id})
        registered = next((row for row in registered_questions if row["id"] == question_id), None)
        if (
            registered
            and binding.role == "worker"
            and binding.task_id
            and registered.get("owner_task_ids")
            and binding.task_id not in registered["owner_task_ids"]
        ):
            errors.append({"field": "question_ids", "reason": "question belongs to another task"})
    for index, item in enumerate(proposal.get("question_proposals") or []):
        parent = mapping.get(item.get("parent_question_id"), item.get("parent_question_id"))
        if parent and parent not in known_questions:
            errors.append(
                {
                    "field": "question_proposals[%d].parent_question_id" % index,
                    "reason": "unknown parent question",
                }
            )
    for index, item in enumerate(proposal.get("judgments") or []):
        resolved = mapping.get(item.get("question_id"), item.get("question_id"))
        if (
            resolved not in set(question_ids)
            or item.get("status", "proposed") not in JUDGMENT_STATES
        ):
            errors.append(
                {
                    "field": "judgments[%d].question_id" % index,
                    "reason": "judgment must name a registered or proposed question",
                }
            )
    if errors:
        return await reject(binding, proposal, owner, phase, "checkpoint_invalid", errors)
    accepted_observations, rejected = [], []
    for index, item in enumerate(proposal.get("observation_proposals") or []):
        related = (
            _resolve_ids(item.get("question_ids"), mapping)
            if item.get("question_ids")
            else question_ids
        )
        if not set(related).issubset(question_ids):
            rejected.append(
                {
                    "field": "observation_proposals[%d].question_ids" % index,
                    "reason": "observation must belong to this stage questions",
                }
            )
            continue
        outcome = await verify_observation(
            binding, item, question_ids=related, owner=owner, source_context_id=source_context_id
        )
        if "accepted" in outcome:
            record = outcome["accepted"]
            record["id"] = mapping.get(item.get("local_id"), record["id"])
            accepted_observations.append(record)
        else:
            rejected.append(dict(outcome["rejected"], field="observation_proposals[%d]" % index))
    if rejected:
        return await reject(binding, proposal, owner, phase, "checkpoint_invalid", rejected)

    stored_judgments = await judgments_for(binding, question_ids)
    closure = must_handle(
        [row for row in await questions(binding) if row["id"] in set(question_ids)],
        stored_judgments,
    )
    proposed_opposition = {
        ref
        for item in proposal.get("judgments") or []
        for ref in _resolve_ids(item.get("opposing_observation_ids"), mapping)
    }
    included_opposition = sorted(set(closure["observation_ids"]) - proposed_opposition)

    judgments = []
    for index, item in enumerate(proposal.get("judgments") or []):
        judgments.append(
            {
                "id": mapping.get(item.get("local_id"), new_id("judgment")),
                "incident_id": binding.incident_id,
                "run_id": binding.run_id,
                "producer_role": owner,
                "task_id": binding.task_id,
                "attempt_id": binding.attempt_id,
                "created_at": utcnow(),
                "question_id": mapping.get(item["question_id"], item["question_id"]),
                "type": item.get("type") or "interpretation",
                "content": item.get("content") or "",
                "supporting_observation_ids": _resolve_ids(
                    item.get("supporting_observation_ids"), mapping
                ),
                "opposing_observation_ids": _resolve_ids(
                    item.get("opposing_observation_ids"), mapping
                ),
                "evidence_refs": list(item.get("evidence_refs") or []),
                "limitations": list(item.get("limitations") or []),
                "status": item.get("status") or "proposed",
                "is_model_interpretation": True,
            }
        )
    dispositions = []
    for index, item in enumerate(proposal.get("material_dispositions") or []):
        state = item.get("disposition")
        if state not in DISPOSITION_STATES:
            return await reject(
                binding,
                proposal,
                owner,
                phase,
                "checkpoint_invalid",
                [
                    {
                        "field": "material_dispositions[%d].disposition" % index,
                        "reason": "must be one of %s" % ", ".join(DISPOSITION_STATES),
                    }
                ],
            )
        dispositions.append(
            {
                "source_id": item.get("source_id"),
                "field": item.get("field"),
                "source_version": item.get("source_version"),
                "range": item.get("range"),
                "disposition": state,
                "range_unit": item.get("range_unit"),
                "reason": item.get("reason"),
                "covered_by": _resolve_ids(item.get("covered_by"), mapping),
            }
        )

    errors = await validate_judgments(binding, judgments, accepted_observations)
    errors += await validate_dispositions(
        binding, dispositions, accepted_observations, question_ids
    )
    if errors:
        return await reject(binding, proposal, owner, phase, "checkpoint_invalid", errors)

    if validate_only:
        return {"accepted": True, "verified_observations": accepted_observations}
    commit_id, checkpoint_id, stage_id = (
        new_id("stage_commit"),
        new_id("checkpoint"),
        new_id("segment"),
    )
    revision = None

    from .artifacts import write_artifact

    archived_proposal = await write_artifact(
        binding,
        {
            "kind": "stage_proposal",
            "proposal": proposal,
            "source_context_id": binding.last_context_id,
        },
    )
    await binding.repository.save(binding.incident_id, "artifact", archived_proposal)

    new_questions = []
    for index, item in enumerate(proposal.get("question_proposals") or []):
        question_id = mapping.get(item.get("local_id")) or new_id("question")
        new_questions.append(
            {
                "id": question_id,
                "incident_id": binding.incident_id,
                "run_id": binding.run_id,
                "producer_role": owner,
                "task_id": binding.task_id,
                "attempt_id": binding.attempt_id,
                "created_at": utcnow(),
                "text": item["text"],
                "parent_question_id": mapping.get(
                    item.get("parent_question_id"), item.get("parent_question_id")
                ),
                "resource_ids": list(item.get("resource_ids") or []),
                "scope": item.get("scope") or {},
                "origin_refs": list(item.get("origin_refs") or []),
                "status": item.get("status") or "open",
                "owner_task_ids": list(item.get("owner_task_ids") or []),
            }
        )

    def write(db):
        if repair_guard is not None:
            from .stage_pending import check_guard

            check_guard(binding, db, repair_guard)
        current = next(
            (
                r
                for r in binding.repository.list(db, binding.incident_id, "checkpoint")
                if same_owner(binding, r, owner) and r.get("is_head")
            ),
            None,
        )
        if (current or {}).get("id") != (head or {}).get("id"):
            raise WorkspaceError("checkpoint_conflict: active head changed during verification")
        for record in accepted_observations:
            ref = record["source_ref"]
            if not ref.get("evidence_id"):
                continue
            source = binding.repository.get(db, binding.incident_id, ref["evidence_id"], "evidence")
            if (
                ref.get("source_revision") is not None
                and ref["source_revision"] != source["revision"]
            ):
                raise WorkspaceError(
                    "checkpoint_conflict: observation source changed during verification"
                )
        for row in dispositions:
            source = binding.repository.get(db, binding.incident_id, row["source_id"])
            if source["revision"] != row["source_version"]:
                raise WorkspaceError(
                    "checkpoint_conflict: material source changed during verification"
                )
        for record in new_questions:
            binding.repository.put(
                db, binding.incident_id, "question", record, action="question_registered"
            )
        for record in accepted_observations:
            binding.repository.put(
                db, binding.incident_id, "observation", record, action="observation_verified"
            )
        for record in judgments:
            binding.repository.put(
                db, binding.incident_id, "judgment", record, action="judgment_recorded"
            )
        if head is not None:
            head["is_head"] = False
            binding.repository.put(
                db,
                binding.incident_id,
                "checkpoint",
                head,
                head["revision"],
                "checkpoint_superseded",
            )
        commit = {
            "id": commit_id,
            "incident_id": binding.incident_id,
            "run_id": binding.run_id,
            "task_id": binding.task_id,
            "attempt_id": binding.attempt_id,
            "owner": owner,
            "phase": phase,
            "created_at": utcnow(),
            "client_commit_key": proposal["client_commit_key"],
            "proposal_digest": digest(proposal),
            "proposal_artifact_id": archived_proposal["id"],
            "source_context_id": source_context_id or binding.last_context_id,
            "repair_proposal_id": (repair_guard or {}).get("id"),
            "stage_goal": proposal["stage_goal"],
            "completion": proposal["completion"],
            "question_ids": question_ids,
            "observation_ids": [record["id"] for record in accepted_observations],
            "judgment_ids": [record["id"] for record in judgments],
            "included_opposition_ids": included_opposition,
            "coverage": proposal.get("coverage") or {},
            "open_items": list(proposal.get("open_items") or []),
            "next_focus": list(proposal.get("next_focus") or []),
            "material_dispositions": dispositions,
            "stage_id": stage_id,
            "id_map": mapping,
        }
        commit["checkpoint_id"] = checkpoint_id
        commit["next_stage_id"] = stage_id
        binding.repository.put(
            db, binding.incident_id, "stage_commit", commit, action="stage_committed"
        )
        checkpoint = {
            "id": checkpoint_id,
            "incident_id": binding.incident_id,
            "run_id": binding.run_id,
            "task_id": binding.task_id,
            "attempt_id": binding.attempt_id,
            "owner": owner,
            "created_at": utcnow(),
            "is_head": True,
            "previous_checkpoint_id": (head or {}).get("id"),
            "stage_commit_id": commit_id,
            "stage_id": stage_id,
            "next_focus": list(proposal.get("next_focus") or []),
            "selected_refs": [record["id"] for record in accepted_observations],
            "active_materials": [row for row in dispositions if row["disposition"] == "active"],
            "pending_items": list(proposal.get("open_items") or []),
            "budget_counters_ref": {
                "model_steps": binding.model_steps,
                "mcp_calls": binding.mcp_calls,
                "reads": binding.reads,
            },
        }
        binding.repository.put(
            db, binding.incident_id, "checkpoint", checkpoint, action="checkpoint_opened"
        )
        for row in dispositions:
            if not row.get("source_id"):
                continue
            binding.repository.put(
                db,
                binding.incident_id,
                "material_usage",
                {
                    "id": usage_key(
                        owner_scope(binding, owner),
                        question_ids[0],
                        row["source_id"],
                        row.get("source_version") or 1,
                        str((row.get("field"), row.get("range"), row.get("range_unit"))),
                    ),
                    "incident_id": binding.incident_id,
                    "run_id": binding.run_id,
                    "task_id": binding.task_id,
                    "attempt_id": binding.attempt_id,
                    "producer_role": owner,
                    "created_at": utcnow(),
                    "owner": owner,
                    "question_id": question_ids[0],
                    "source_id": row["source_id"],
                    "source_version": row.get("source_version") or 1,
                    "field": row.get("field"),
                    "range": row.get("range"),
                    "range_unit": row.get("range_unit"),
                    "state": "covered_by_commit"
                    if row["disposition"] == "covered_by"
                    else row["disposition"],
                    "note": row.get("reason"),
                },
                action="material_" + row["disposition"],
            )
        binding.repository.event(
            db,
            binding.incident_id,
            commit_id,
            "stage_committed",
            {
                "commit_id": commit_id,
                "checkpoint_id": checkpoint_id,
                "owner": owner,
                "completion": proposal["completion"],
                "stage_id": stage_id,
                "included_opposition_ids": included_opposition,
            },
        )
        return db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE incident=?", (binding.incident_id,)
        ).fetchone()[0]

    try:
        revision = await binding.repository.transact(write)
    except WorkspaceError as exc:
        if "checkpoint_conflict" not in str(exc):
            raise
        # Verification deliberately happens outside SQLite's write lock. A rival
        # may have committed the identical proposal while it was in progress.
        replay = await _existing_commit(binding, owner, proposal["client_commit_key"])
        if replay and replay.get("proposal_digest") == digest(proposal):
            return {
                "accepted": True,
                "duplicate": True,
                "commit_id": replay["id"],
                "checkpoint_id": replay["checkpoint_id"],
                "id_map": replay["id_map"],
                "next_stage_id": replay["next_stage_id"],
            }
        current = await active_checkpoint(binding, owner)
        return await reject(
            binding,
            proposal,
            owner,
            phase,
            "checkpoint_conflict",
            [{"field": "expected_checkpoint_id", "reason": str(exc)}],
            retained=(current or {}).get("id"),
        )
    return {
        "accepted": True,
        "commit_id": commit_id,
        "checkpoint_id": checkpoint_id,
        "id_map": mapping,
        "next_stage_id": stage_id,
        "workspace_revision": revision,
        "question_ids": [record["id"] for record in new_questions],
        "included_opposition": included_opposition,
        "observation_ids": [record["id"] for record in accepted_observations],
        "judgment_ids": [record["id"] for record in judgments],
    }


async def reject(binding, proposal, owner, phase, reason, field_errors, retained=None):
    """Record an invalid handoff without switching anything (PRD §19.6.1)."""
    from .artifacts import write_artifact
    from .stage_errors import normalized_errors

    field_errors = normalized_errors(field_errors)
    artifact = await write_artifact(
        binding,
        {
            "kind": "stage_proposal_rejected",
            "owner": owner,
            "phase": phase,
            "reason": reason,
            "field_errors": field_errors,
            "proposal": proposal,
        },
    )
    await binding.repository.save(binding.incident_id, "artifact", artifact)
    await binding.repository.transact(
        lambda db: binding.repository.event(
            db,
            binding.incident_id,
            binding.run_id,
            "stage_commit_rejected",
            {
                "reason": reason,
                "owner": owner,
                "field_errors": field_errors,
                "proposal_artifact_id": artifact["id"],
                "retained_checkpoint_id": retained,
            },
        )
    )
    return {
        "accepted": False,
        "reason": reason,
        "field_errors": field_errors,
        "retained_checkpoint_id": retained,
        "proposal_artifact_id": artifact["id"],
    }


def normalize_proposal(proposal):
    """Accept a pydantic stage proposal or a plain dict; keep only the contract."""
    if proposal is None:
        return None
    if hasattr(proposal, "model_dump"):
        proposal = proposal.model_dump(exclude_unset=True)
    if not isinstance(proposal, dict):
        return None
    return proposal


async def apply_stage_proposal(binding, proposal, *, owner=None, phase="stage"):
    """Persist one role's stage proposal; a rejection is audited and never fatal.

    The role's own output stays valid: an invalid handoff keeps the previous
    active segment and records why, so the parent flow can continue or correct.
    """
    proposal = normalize_proposal(proposal)
    if not proposal:
        return None
    from config import settings

    if (getattr(binding, "lifecycle_mode", None) or settings.CONTEXT_LIFECYCLE_MODE) != "active":
        if (
            getattr(binding, "lifecycle_mode", None) or settings.CONTEXT_LIFECYCLE_MODE
        ) == "observe":
            from .artifacts import write_artifact

            artifact = await write_artifact(
                binding,
                {
                    "kind": "stage_proposal_candidate",
                    "phase": phase,
                    "owner": owner or binding.role,
                    "proposal": proposal,
                },
            )
            await binding.repository.save(binding.incident_id, "artifact", artifact)
        return None
    outcome = await commit_stage(binding, proposal, owner=owner, phase=phase)
    event = {
        "owner": owner or binding.role,
        "phase": phase,
        "commit_id": outcome.get("commit_id"),
        "checkpoint_id": outcome.get("checkpoint_id"),
        "reason": outcome.get("reason"),
        "field_errors": outcome.get("field_errors"),
    }
    await binding.repository.transact(
        lambda db: binding.repository.event(
            db,
            binding.incident_id,
            binding.run_id,
            "stage_proposal_accepted" if outcome.get("accepted") else "stage_proposal_rejected",
            event,
        )
    )
    return outcome
