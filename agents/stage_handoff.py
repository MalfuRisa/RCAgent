"""Preserve the role payload; Repair is only a shared field-patch submission."""
from workspace.repository import WorkspaceError
from workspace.model_requests import ModelRequestError
from config import settings
from workspace.recovery import record_issue
from .stage_repair import submit_stage


async def accept_role_stage(binding, model, proposal, messages=(), *, owner, phase):
    if binding is None:
        return None
    if owner != binding.role:
        raise WorkspaceError('stage_repair_conflict: role mismatch')
    try:
        outcome = await submit_stage(binding, model, proposal, phase=phase)
    except ModelRequestError as exc:
        if settings.STAGE_HANDOFF_REQUIRED or exc.detail['code'] != 'model_timeout':
            raise
        outcome = {'accepted': False, 'reason': exc.detail['code'], 'repair_error': exc.detail['message']}
    if outcome is not None and not outcome.get('accepted'):
        if settings.STAGE_HANDOFF_REQUIRED:
            raise WorkspaceError(outcome.get('repair_error') or outcome.get('reason') or 'checkpoint_invalid')
        await record_issue(binding, outcome.get('reason') or 'checkpoint_invalid',
            'continue_valid_outer_payload_without_accepting_stage',
            detail=outcome.get('repair_error') or outcome.get('field_errors'),
            proposal_artifact_id=outcome.get('proposal_artifact_id'))
    return outcome
