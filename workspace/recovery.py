"""Audited local degradation; rejected stage drafts never become accepted facts."""
from .repository import new_id, utcnow


async def record_issue(binding, code, action, *, detail=None, proposal_artifact_id=None):
    row = {'id': new_id('recovery_issue'), 'run_id': binding.run_id,
           'role': binding.role, 'task_id': binding.task_id, 'attempt_id': binding.attempt_id,
           'created_at': utcnow(), 'code': code, 'action': action,
           'detail': detail, 'proposal_artifact_id': proposal_artifact_id,
           'note': 'Operational gap retained; no rejected observation or material coverage was accepted.'}
    return await binding.repository.save(binding.incident_id, 'recovery_issue', row, action='local_recovery')


async def issues(binding):
    return [row for row in await binding.repository.all(binding.incident_id, 'recovery_issue')
            if row.get('run_id') == binding.run_id]
