"""Local stage-handoff tools: commit a stage and start the next segment (PRD §19.6).

`commit_investigation_stage` is a local tool (never an MCP call). It validates the
proposal through `investigations.commit_stage` and returns the ids a later stage
needs. A model batch that mixes the handoff with new queries or the final
submission is refused as a handoff; the other results stay intact, so no response
is lost and no half-switched segment exists.
"""

from langchain_core.tools import tool
from models.stage_proposals import StageProposal

from . import investigations
from .read_tools import binding
from .repository import WorkspaceError

#: A handoff must travel alone in its model step (PRD §19.6.2.1).
COMMIT_TOOL = "commit_investigation_stage"
MAX_ID_MAP_ENTRIES = 8
MAX_FIELD_ERRORS = 5


def commit_receipt(outcome):
    return bounded_receipt(
        {
            "status": "accepted" if outcome.get("accepted") else "rejected",
            **{
                key: outcome.get(key)
                for key in (
                    "reason",
                    "commit_id",
                    "checkpoint_id",
                    "next_stage_id",
                    "workspace_revision",
                    "proposal_artifact_id",
                    "retained_checkpoint_id",
                    "repair_pending_id",
                    "repair_error",
                )
                if outcome.get(key) is not None
            },
            "duplicate": bool(outcome.get("duplicate")),
            "id_map": compact_id_map(outcome.get("id_map") or {}),
            "field_errors": (outcome.get("field_errors") or [])[:MAX_FIELD_ERRORS],
            "field_errors_total": len(outcome.get("field_errors") or []),
        }
    )


from langchain_core.tools import StructuredTool


class StageCommitTool(StructuredTool):
    async def ainvoke(self, input, config=None, **kwargs):
        # Keep raw arguments even when the advertised shared schema rejects
        # them. The shared runtime validator archives errors before Repair.
        from config import settings
        from agents.stage_repair import submit_stage

        b = binding()
        raw = (
            input.get("args")
            if isinstance(input, dict) and input.get("type") == "tool_call"
            else input
        )
        if (
            isinstance(input, dict)
            and input.get("type") == "tool_call"
            and input.get("name") != self.name
        ):
            result = await investigations.reject(
                b,
                input,
                b.role,
                await phase_of(b),
                "stage_repair_not_applicable",
                [{"field": "tool.name", "reason": "unexpected stage tool name"}],
            )
            return commit_receipt(result)
        result = await submit_stage(
            b,
            getattr(b, "active_role_model", None) or settings.get_chat_model(),
            raw,
            phase=await phase_of(b),
            raw_output=input,
        )
        if result is None:
            return {"status": "rejected", "reason": "lifecycle_inactive"}
        return commit_receipt(result)


async def _commit(**proposal):
    """Shared runtime entry, including validation errors from raw Tool args."""
    return await commit_investigation_stage.ainvoke(proposal)


commit_investigation_stage = StageCommitTool(
    name=COMMIT_TOOL,
    description="Commit one investigation stage alone. All nested fields follow the shared StageProposal contract.",
    args_schema=StageProposal,
    coroutine=_commit,
)


def stage_tools():
    from config import settings

    return (
        [commit_investigation_stage]
        if settings.CONTEXT_LIFECYCLE_MODE == "active"
        else []
    )


def mixed_batch(calls):
    """The commit tool shares a batch with anything else -> refuse the handoff."""
    names = [call.get("name") for call in calls]
    return COMMIT_TOOL in names and len(names) > 1


async def phase_of(b, default="stage"):
    if not b.last_context_id:
        return default
    try:
        manifest = await b.repository.read(b.incident_id, b.last_context_id, "context")
    except WorkspaceError:
        return default
    return manifest.get("phase") or default


def compact_id_map(mapping):
    row = {"entries": dict(list(sorted(mapping.items()))[:MAX_ID_MAP_ENTRIES])}
    if len(mapping) > MAX_ID_MAP_ENTRIES:
        row["omitted"] = len(mapping) - MAX_ID_MAP_ENTRIES
        row["read"] = 'read_investigation(kind="stage_commit", record_id=<commit_id>)'
    return row


def bounded_receipt(result):
    from .paging import serialized_chars
    from config import settings

    cap = settings.CONTEXT_READ_MAX_CHARS
    if serialized_chars(result) <= cap:
        return result
    result = dict(result)
    if result.get("status") == "accepted":
        result.pop("id_map", None)
        result.pop("next_focus", None)
        result.pop("included_opposition_ids", None)
        result["read"] = (
            'read_investigation(kind="stage_commit", record_id=<commit_id>, field=<field>)'
        )
    else:
        result["field_errors"] = [
            {"field": row.get("field"), "reason": str(row.get("reason"))[:160]}
            for row in result.get("field_errors") or []
        ][:2]
        result["read"] = (
            "read_artifact(artifact_id=<proposal_artifact_id>, field=field_errors)"
        )
    if serialized_chars(result) > cap:
        result = {
            key: result[key]
            for key in (
                "status",
                "reason",
                "commit_id",
                "checkpoint_id",
                "next_stage_id",
                "proposal_artifact_id",
                "retained_checkpoint_id",
            )
            if key in result
        }
        result["detail_required"] = True
    return result


def segment_messages(rules_message, checkpoint, *, task_id=None, extra=None):
    """The fresh segment after an accepted handoff: rules + the next focus only."""
    import json
    from langchain_core.messages import HumanMessage

    payload = {
        "stage": "continued_after_handoff",
        "checkpoint_id": checkpoint.get("id"),
        "checkpoint_revision": checkpoint.get("revision"),
        "stage_commit_id": checkpoint.get("stage_commit_id"),
        "next_focus": checkpoint.get("next_focus") or [],
        "pending_items": checkpoint.get("pending_items") or [],
        "active_materials": checkpoint.get("active_materials") or [],
        "deferred_tool_calls": checkpoint.get("deferred_tool_calls") or [],
        "note": (
            "Earlier messages left the active input after this handoff. Facts, judgments and "
            "limits stay readable with read_investigation; read them again when a value matters."
        ),
    }
    if task_id:
        payload["task_id"] = task_id
    if extra:
        payload.update(extra)
    return [
        rules_message,
        HumanMessage(
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        ),
    ]


async def new_segment_after_commit(
    b, checkpoint_id, rules_message, *, task_id=None, extra=None, messages=()
):
    """Load the checkpoint that just became active and build its opening messages."""
    checkpoint = await b.repository.read(b.incident_id, checkpoint_id, "checkpoint")
    b.skill_segment = checkpoint_id
    from .investigation_phases import active, save

    if active(b):
        b.phase_budget["skill_segment"] = checkpoint_id
        await save(b)
    from .stage_segments import retire_messages

    retained = await retire_messages(b, checkpoint, list(messages))
    opening = segment_messages(rules_message, checkpoint, task_id=task_id, extra=extra)

    # Existing raw/unfinished batches are still paired and active. Rules already
    # supplied by the caller need not be duplicated in this retained history.
    def old_opening(message):
        import json

        if message.type != "human":
            return False
        try:
            return json.loads(message.content).get("stage") == "continued_after_handoff"
        except (ValueError, TypeError, AttributeError):
            return False

    return opening + [m for m in retained if m.type != "system" and not old_opening(m)]
