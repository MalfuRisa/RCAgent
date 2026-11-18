"""Persisted role budgets shared by exploration, local consolidation and repair."""

from config import settings
from .investigations import digest
from .repository import WorkspaceError

LOCAL_TOOLS = {
    "inspect_capability",
    "read_evidence",
    "read_artifact",
    "read_investigation",
    "query_evidence",
    "record_observations",
    "find_evidence_references",
    "read_evidence_reference",
    "search_investigation_materials",
    "read_investigation_material",
    "read_skill",
    "commit_investigation_stage",
    "complete_task",
    "submit_report",
    "begin_consolidation",
}


def active(b):
    return getattr(b, "phase_budget", None) is not None


async def guard(b, operation):
    """Execution-side guard also applies to direct service/tool invocations."""
    b.remaining()
    from .run_stops import persisted_reason

    stopped = await persisted_reason(b)
    if stopped:
        raise WorkspaceError("execution_stopped: " + str(stopped))
    if not active(b):
        return
    phase = b.phase_budget["phase"]
    if phase in ("finished", "stopped") or (
        phase != "investigating" and operation not in LOCAL_TOOLS
    ):
        raise WorkspaceError("phase_disallowed: " + operation + " in " + phase)


async def save(b):
    b.phase_budget = await b.repository.save(
        b.incident_id, "role_budget", b.phase_budget
    )


async def initialize(b, *, review_round=0):
    key = "role_budget_" + digest([b.run_id, b.role, b.task_id, review_round])[:24]
    rows = await b.repository.all(b.incident_id, "role_budget")
    b.phase_budget = next((r for r in rows if r["id"] == key), None)
    if b.phase_budget is None:
        limits = {
            "investigating": getattr(settings, "WORKER_EXPLORATION_STEPS", 14)
            if b.role == "worker"
            else getattr(settings, "SUPERVISOR_EXPLORATION_STEPS", 8),
            "consolidating": getattr(settings, "INVESTIGATION_CONSOLIDATION_STEPS", 4),
            "repairing": getattr(settings, "INVESTIGATION_REPAIR_STEPS", 2),
            "skill": settings.SKILL_READ_EXTRA_ROUNDS
            if settings.INVESTIGATION_SKILLS_ENABLED
            else 0,
        }
        b.phase_budget = {
            "id": key,
            "run_id": b.run_id,
            "role": b.role,
            "task_id": b.task_id,
            "review_round": review_round,
            "phase": "investigating",
            "limits": limits,
            "used": {k: 0 for k in limits},
            "logical_steps": 0,
            "tight_budget": b.local_model_limit is not None
            and b.local_model_limit < sum(limits.values()),
            "explicit_total_limit": b.local_model_limit,
        }
        await save(b)
    await refresh(b)
    b.skill_segment = b.phase_budget.get("skill_segment", "initial")


def remaining(b, phase=None):
    row = b.phase_budget
    phase = phase or row["phase"]
    return max(0, row["limits"].get(phase, 0) - row["used"].get(phase, 0))


async def refresh(b):
    b.remaining()
    row = b.phase_budget
    previous = row["phase"]
    if previous == "investigating" and (not remaining(b) or b.checkpoint_required):
        row["phase"] = "consolidating"
    elif previous == "repairing" and not remaining(b):
        row.update(phase="stopped", stop_reason="repair_budget_exhausted")
    if row["phase"] != previous:
        await save(b)
    return row["phase"]


def can_send(b):
    if not active(b):
        return
    if b.phase_budget["phase"] in ("finished", "stopped"):
        raise WorkspaceError("phase_budget_exhausted: " + b.phase_budget["phase"])
    if (
        b.local_model_limit is not None
        and b.phase_budget["logical_steps"] >= b.local_model_limit
    ):
        raise WorkspaceError("phase_budget_exhausted: explicit tight total limit")
    if b.phase_budget["phase"] == "investigating" and not remaining(b):
        raise WorkspaceError("phase_transition_required")
    if not remaining(b) and not remaining(b, "skill"):
        if b.phase_budget["phase"] == "investigating":
            raise WorkspaceError("phase_transition_required")
        raise WorkspaceError("phase_budget_exhausted: no local or knowledge steps")


async def reserve(b):
    if not active(b):
        return
    can_send(b)
    phase = b.phase_budget["phase"]
    if not remaining(b):
        phase = "skill"
    b.phase_budget["used"][phase] += 1
    b.phase_budget["logical_steps"] += 1
    b.phase_step_category = phase
    await save(b)


async def settle(b, calls, outcomes):
    if not active(b):
        return
    category = getattr(b, "phase_step_category", b.phase_budget["phase"])
    pure_skill = len(calls) == 1 and calls[0].get("name") == "read_skill"
    if pure_skill and category != "skill" and remaining(b, "skill"):
        b.phase_budget["used"][category] -= 1
        b.phase_budget["used"]["skill"] += 1
    rejected = next(
        (
            (name, result)
            for name, result in outcomes
            if result.get("status") in ("rejected", "error")
        ),
        None,
    )
    submission = rejected and (
        rejected[0]
        in {
            "record_observations",
            "complete_task",
            "submit_report",
            "commit_investigation_stage",
            "PacketSelection",
        }
        or b.phase_budget["phase"] != "investigating"
    )
    if submission and remaining(b, "repairing"):
        b.phase_budget.update(
            phase="repairing", repair_operation=rejected[0], last_rejection=rejected[1]
        )
    elif b.phase_budget["phase"] == "repairing":
        fixed = any(
            name == b.phase_budget.get("repair_operation")
            and result.get("status") == "accepted"
            for name, result in outcomes
        )
        if fixed and remaining(b, "consolidating"):
            b.phase_budget["phase"] = "consolidating"
    if any(
        name == "complete_task" and result.get("status") == "accepted"
        for name, result in outcomes
    ):
        b.phase_budget["phase"] = "finished"
    await save(b)


def tools_for(b, tools):
    if not active(b):
        return tools
    if b.phase_budget["phase"] in ("finished", "stopped"):
        return []
    if not remaining(b):
        return (
            [t for t in tools if t.name == "read_skill"]
            if remaining(b, "skill")
            else []
        )
    if b.phase_budget["phase"] != "investigating":
        return [t for t in tools if t.name in LOCAL_TOOLS]
    return tools


async def consolidate(b):
    if b.phase_budget["phase"] == "investigating":
        b.phase_budget["phase"] = "consolidating"
        await save(b)
    return {"status": "accepted", **view(b)}


def view(b):
    if not active(b):
        return {}
    return {
        "phase": b.phase_budget["phase"],
        "remaining": {k: remaining(b, k) for k in b.phase_budget["limits"]},
        "logical_steps": b.phase_budget["logical_steps"],
        "tight_budget": b.phase_budget["tight_budget"],
        "last_rejection": b.phase_budget.get("last_rejection"),
    }
