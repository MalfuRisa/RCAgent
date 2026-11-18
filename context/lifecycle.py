"""Roll out investigation inputs without changing observe/legacy model calls."""

from config import settings
from workspace.artifacts import write_artifact
from .investigations import select_investigation

INVESTIGATION_KINDS = (
    "question",
    "observation",
    "judgment",
    "stage_commit",
    "checkpoint",
    "material_usage",
    "report_packet",
)


def effective_mode(binding):
    """A bound investigation policy takes precedence over the rollout default."""
    return getattr(binding, "lifecycle_mode", None) or settings.CONTEXT_LIFECYCLE_MODE


async def input_snapshot(binding, snapshot, phase, manifest):
    mode = settings.CONTEXT_LIFECYCLE_MODE
    manifest["lifecycle_mode"] = mode
    if mode == "active":
        return snapshot
    if mode == "observe":
        candidate = select_investigation(binding, snapshot, phase)
        artifact = await write_artifact(
            binding,
            {
                "kind": "lifecycle_selection_candidate",
                "mode": mode,
                "phase": phase,
                "candidate": candidate,
            },
        )
        await binding.repository.save(binding.incident_id, "artifact", artifact)
        manifest["lifecycle_candidate_artifact_id"] = artifact["id"]
    return {key: value for key, value in snapshot.items() if key not in INVESTIGATION_KINDS}
