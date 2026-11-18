"""Public progress handoff references formal work; the legacy stage DTO stays internal."""

from pydantic import Field
from models.investigation import Contract
from workflow.tool_execution import contract_tool
from .runtime import current
from .reference_registration import allowed_questions
from .observation_selection import selected_observations
from .investigations import active_checkpoint, commit_stage, digest
from .repository import WorkspaceError
from .stage_tools import commit_receipt


class StageProgress(Contract):
    observation_ids: list[str] = Field(default_factory=list)
    opposing_observation_ids: list[str] = Field(default_factory=list)
    question_ids: list[str] = Field(min_length=1)
    interpretation: str = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)
    next_focus: list[str] = Field(default_factory=list)
    open_items: list[str] = Field(default_factory=list)


@contract_tool(args_schema=StageProgress)
async def commit_investigation_stage(
    question_ids: list[str],
    interpretation: str,
    observation_ids: list[str] = (),
    opposing_observation_ids: list[str] = (),
    limitations: list[str] = (),
    next_focus: list[str] = (),
    open_items: list[str] = (),
) -> dict:
    """Save formal observations, interpretation and next focus, then continue this same task."""
    b = current.get()
    from .investigation_phases import guard

    await guard(b, "commit_investigation_stage")
    # Public optional arrays may arrive through Python's omitted defaults.
    # The internal StageProposal contract requires JSON-style lists.
    observation_ids = list(observation_ids)
    opposing_observation_ids = list(opposing_observation_ids)
    limitations = list(limitations)
    next_focus = list(next_focus)
    open_items = list(open_items)
    if not set(question_ids) <= await allowed_questions(b):
        raise WorkspaceError("invalid_question: stage outside current questions")
    selected = await selected_observations(
        b, list(observation_ids) + list(opposing_observation_ids), question_ids
    )
    head = await active_checkpoint(b, b.role)
    from .investigations import _existing_commit

    key = (
        "progress-"
        + digest(
            [
                b.run_id,
                b.role,
                b.task_id,
                question_ids,
                observation_ids,
                opposing_observation_ids,
                interpretation,
                limitations,
                next_focus,
                open_items,
            ]
        )[:24]
    )
    previous = await _existing_commit(b, b.role, key)
    if previous:
        return commit_receipt(
            {
                "accepted": True,
                "duplicate": True,
                "commit_id": previous["id"],
                "checkpoint_id": (head or {}).get("id", previous["checkpoint_id"]),
                "id_map": previous["id_map"],
            }
        )
    proposal = {
        "client_commit_key": key,
        "stage_goal": "Save model-reviewed formal work and continue",
        "completion": "partial",
        "expected_checkpoint_id": (head or {}).get("id"),
        "expected_revision": (head or {}).get("revision"),
        "question_ids": question_ids,
        "judgments": [
            {
                "question_id": qid,
                "content": interpretation,
                "limitations": limitations,
                "supporting_observation_ids": observation_ids,
                "opposing_observation_ids": opposing_observation_ids,
            }
            for qid in question_ids
        ],
        "next_focus": next_focus,
        "open_items": open_items,
        "material_dispositions": [
            {
                "source_id": o["source_ref"]["evidence_id"],
                "source_version": o["source_ref"].get("source_revision"),
                "field": o["source_ref"]
                .get("json_pointer", "")
                .lstrip("/")
                .replace("/", "."),
                "range": o["source_ref"].get("range"),
                "range_unit": o["source_ref"].get("range_unit"),
                "disposition": "covered_by",
                "covered_by": [o["id"]],
            }
            for o in selected
            if o.get("kind") in ("field", "range")
        ],
    }
    return commit_receipt(await commit_stage(b, proposal))
