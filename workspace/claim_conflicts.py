"""Authoritative assessment conflict checks, independent of Supervisor omissions."""
import json


def related_judgment(judgment, task, claim):
    if claim['resource_id'] not in task.get('target_resource_ids', []):
        return False
    if set(judgment.get('supporting_evidence_ids', [])).intersection(claim['supporting_evidence_ids']):
        return True
    if claim.get('task_id') == task['id'] or claim.get('question') == task.get('question') and claim.get('question'):
        return True
    mechanism = judgment.get('mechanism')
    if mechanism is None:
        mechanism = judgment.get('submission', {}).get('mechanism')
    # Older judgments don't encode mechanisms. For this target a missing scope
    # cannot establish that its counter evidence/gaps concern a different cause.
    return mechanism is None or mechanism == claim['mechanism']


async def unresolved_conflicts(binding, claim):
    def read(db):
        repo, incident = binding.repository, binding.incident_id
        tasks = {t['id']: t for t in repo.list(db, incident, 'task')}
        return tasks, repo.list(db, incident, 'assessment')
    tasks, assessments = await binding.repository.transact(read)
    conflicts = []
    for assessment in assessments:
        task = tasks.get(assessment['task_id'])
        if not task:
            continue
        for judgment in [assessment] + assessment.get('hypotheses', []):
            if not related_judgment(judgment, task, claim):
                continue
            opposing, gaps = judgment.get('opposing_evidence_ids', []), judgment.get('gaps', [])
            if opposing or gaps or judgment.get('status') == 'refuted':
                conflicts.append({'assessment_id': assessment['id'], 'task_id': task['id'],
                                  'opposing_evidence_ids': opposing, 'gaps': gaps,
                                  'hypothesis_status': judgment.get('status')})
    return json.dumps(conflicts, ensure_ascii=False) if conflicts else None
