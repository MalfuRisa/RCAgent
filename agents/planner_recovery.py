"""Bounded program plan for verified symptoms when LLM planning is unavailable."""
from models.schemas import RCATask
from workspace.investigations import save_question
from workspace.tasks import register_tasks
from workspace.recovery import record_issue
from workspace.repository import WorkspaceError
from config import settings


RECOVERABLE_PLANNING_ERRORS = frozenset({
    'model_timeout', 'model_connection_error', 'model_service_unavailable',
    'model_output_invalid', 'context_unsatisfiable', 'planner_output_invalid',
    'planner_incomplete', 'planner_empty_plan',
})


async def fallback_plan(binding, state, error):
    """Do not invent targets, evidence or causes; retain why planning degraded."""
    binding.remaining()
    resources = await binding.repository.all(binding.incident_id, 'resource')
    matches = {}
    for row in resources:
        if row.get('identity_status') != 'resolved':
            continue
        if state.get('target_cluster') and row.get('cluster') != state['target_cluster']:
            continue
        if state.get('target_namespace') and row.get('namespace') != state['target_namespace']:
            continue
        if state.get('target_resource') and row.get('name') != state['target_resource']:
            continue
        matches.setdefault((row.get('kind'), row.get('name')), []).append(row)
    resolved = {key: rows[0] for key, rows in matches.items() if len(rows) == 1}
    grouped = {}
    for symptom in state.get('symptoms', []):
        key = (symptom.resource_type, symptom.affected_resource)
        if key in resolved:
            grouped.setdefault(key, []).append(symptom)
    if not grouped:
        raise WorkspaceError('planner_fallback_unavailable: no verified symptom target')
    tasks = []
    for (kind, name), symptoms in list(grouped.items())[:settings.RCA_TASKS_PER_ITERATION]:
        resource = resolved[(kind, name)]
        description = '; '.join(s.potential_symptom for s in symptoms)
        goal = ('Program fallback plan: investigate the reported symptom on ' + name + ': ' + description +
                '. Collect support, opposition and missing data; do not assume a cause.')
        question = await save_question(binding, text=goal, resource_ids=[resource['id']],
            scope={'planning_source': 'program_fallback', 'limitations': ['LLM planning did not complete']},
            producer_role='program')
        tasks.append(RCATask(priority=len(tasks) + 1, investigation_goal=goal,
            resource_type=kind, target_resource=name, question_ids=[question['id']],
            acceptance_checks=['Verify the symptom using actual tool evidence',
                               'Seek opposing observations and retain scope limits',
                               'Report missing data and keep unsupported causes unresolved']))
    assigned = await register_tasks(binding, tasks, state.get('target_resource'), state.get('target_namespace'))
    await record_issue(binding, error.get('code') or 'planner_output_invalid',
                       'registered_program_fallback_tasks', detail=error)
    return {'rca_tasks': assigned, 'planning_source': 'program_fallback',
            'execution_status': 'incomplete', 'execution_error': None, 'stop_reason': None}
