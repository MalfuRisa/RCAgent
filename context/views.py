"""Record-level projections: the model-facing brief for one workbench record.

The archive keeps every field (SQLite records plus raw artifacts). These
functions only select and rearrange; they never mutate stored records or become
a second source of truth for the scheduler (PRD §5.1).
"""
import json

from .tool_views import project as project_tool

CONTEXT_VIEW_VERSION = 1

RESOURCE_FIELDS = ('id', 'kind', 'name', 'cluster', 'namespace', 'uid', 'identity_status')
TASK_BRIEF_FIELDS = ('task_id', 'priority', 'target_resource_ids', 'question', 'question_conflict',
                     'execution_status', 'answer_status', 'latest_assessment_id', 'execution_error_code')
QUESTION_PREVIEW_LIMIT = 400


def _preview(text, limit=QUESTION_PREVIEW_LIMIT, marker='preview'):
    if not isinstance(text, str) or len(text) <= limit:
        return text, False
    return {marker: text[:limit], 'chars': len(text), 'preview_incomplete': True}, True


def resource_view(resource):
    """Identity fields, unknown markers and necessary sources; no storage noise."""
    view = {key: resource.get(key) for key in RESOURCE_FIELDS}
    view['unknown_fields'] = [key for key in ('cluster', 'namespace', 'uid') if not resource.get(key)]
    if resource.get('replaced_by'):
        view['replaced_by'] = resource['replaced_by']
        view['identity_status'] = resource.get('identity_status') or 'replaced'
    sources = [ref for ref in resource.get('source_refs', []) if ref]
    if sources:
        view['source_evidence_ids'] = sources[:5]
        if len(sources) > 5:
            view['source_evidence_ids_omitted'] = len(sources) - 5
    return view


def window_table(evidence_rows, requested_windows=None):
    """Central window table: full start/end and timezone, referenced elsewhere.

    Returns ``(windows, refs)`` where ``refs`` maps a window signature to its
    ``wN`` id. Requested windows (from query arguments) get their own entries so
    a returned window that differs from the request stays visible (PRD §6.2).
    """
    windows, refs = {}, {}
    def register(window):
        if not isinstance(window, dict):
            return None
        start, end = window.get('start_time'), window.get('end_time')
        if not start and not end:
            return None
        signature = (start, end)
        if signature not in refs:
            ref = 'w%d' % (len(refs) + 1)
            refs[signature] = ref
            entry = {'start_time': start, 'end_time': end}
            for key in ('step', 'timezone'):
                if window.get(key) is not None:
                    entry[key] = window[key]
            windows[ref] = entry
        return refs[signature]
    for row in evidence_rows:
        register(row.get('time_range'))
        register(row.get('returned_time_range'))
    for window in (requested_windows or {}).values():
        register(window)
    return windows, refs


def _signature(window):
    if not isinstance(window, dict):
        return None
    return (window.get('start_time'), window.get('end_time'))


def evidence_view(evidence, refs):
    """One evidence brief: identity, refs, filtered result, observations, limits.

    Never re-derives conclusions: tool candidates stay under ``inferences``.
    The requested window and the actually returned window are kept separately;
    a comparison that never ran keeps its absence instead of assuming equality.
    """
    projected = project_tool(evidence.get('tool_name'), evidence.get('observations') or {})
    view = {'evidence_id': evidence.get('id'), 'tool_name': evidence.get('tool_name'),
            'resource_revision_refs': evidence.get('resource_identities') or [
                {'id': rid, 'revision': evidence.get('resource_revisions', {}).get(rid)}
                for rid in evidence.get('resource_ids', [])],
            'effective_filters': effective_filters(evidence)}
    requested_ref = refs.get(_signature(evidence.get('time_range')))
    returned_ref = refs.get(_signature(evidence.get('returned_time_range')))
    if requested_ref:
        view['requested_window_ref'] = requested_ref
    if returned_ref:
        view['window_ref'] = returned_ref
    if requested_ref and returned_ref and requested_ref != returned_ref:
        view['window_mismatch'] = True
    if not requested_ref and not returned_ref:
        view['window_unknown'] = True
    for key in ('transport_status', 'source_status', 'truncated', 'freshness'):
        if evidence.get(key) is not None:
            view['result_' + key] = evidence.get(key)
    if projected.get('result'):
        # Transport/source status from storage wins; the response's own status is
        # kept separately and never upgraded to success.
        view['result_response'] = projected['result']
    view['observations'] = projected.get('observations', {})
    view['source'] = projected.get('source', {})
    if projected.get('unknown_fields'):
        view['unknown_fields'] = list(projected['unknown_fields'])
    if projected.get('inferences'):
        view['inferences'] = projected['inferences']
    limitations = limitation_entries(evidence, projected)
    if limitations:
        view['limitations'] = limitations
    if projected.get('unknown_shape'):
        view['detail_required'] = True
        view['detail_fields'] = ['<top-level keys>']
    else:
        if projected.get('detail_required') or any(
                isinstance(value, dict) and value.get('detail_required')
                for value in view['observations'].values()):
            view['detail_required'] = True
        view['detail_fields'] = projected.get('detail_fields', [])
    view['detail'] = 'read_evidence(evidence_id=%s, field=<one of detail_fields>)' % evidence.get('id')
    return view


def limitation_entries(evidence, projected):
    """Stored gaps/warnings plus projector findings, exactly deduplicated.

    A transport error can leave ``observations`` empty while the record still
    carries ``data_gaps``/``warnings``; those limits must survive in the short
    card with their source/reason/impact (PRD §17.4.6).
    """
    entries = {}
    for key in ('warnings', 'data_gaps'):
        seen, values = set(), []
        for item in list(projected.get(key) or []) + list(evidence.get(key) or []):
            signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if signature in seen:
                continue
            seen.add(signature)
            values.append(item)
        if values:
            entries[key] = values
    return entries


def effective_filters(evidence):
    """Filters that change how the result must be read (identity/window stay refs)."""
    arguments = evidence.get('arguments') or {}
    kept = {}
    for key in ('role', 'direction', 'query', 'kind', 'protocol', 'sample_size', 'top_n', 'limit',
                'abnormal_type', 'metric_key', 'target_kind', 'target_name', 'tags', 'step', 'min_duration'):
        if arguments.get(key) is not None:
            kept[key] = arguments[key]
    return kept


def task_view(task, *, role, main_task_id=None, assessment_ids_by_attempt=None):
    """Brief task record; the Worker's own task carries its full requirements.

    ``question``/``investigation_goal`` duplication is removed in presentation
    only: identical text is shown once, different text keeps the goal as the
    executing requirement plus ``question_conflict=true`` (PRD §5.2, §6.3).
    """
    goal, question = task.get('investigation_goal'), task.get('question')
    conflict = bool(goal and question and goal != question)
    view = {'task_id': task.get('id'), 'priority': task.get('priority'),
            'target_resource_ids': task.get('target_resource_ids') or ([task['resource_id']] if task.get('resource_id') else []),
            'execution_status': task.get('execution_status'), 'answer_status': task.get('answer_status'),
            'question_conflict': conflict}
    view = {key: value for key, value in view.items() if value is not None}
    assessment_id = (assessment_ids_by_attempt or {}).get(task.get('latest_attempt_id'))
    if assessment_id:
        view['latest_assessment_id'] = assessment_id
    error = (task.get('execution_error') or {}).get('code') if isinstance(task.get('execution_error'), dict) else None
    if error:
        view['execution_error_code'] = error
    requirement = goal or question
    own = role == 'worker' and (main_task_id is None or task.get('id') == main_task_id)
    if own:
        view['investigation_goal'] = requirement
        if conflict:
            view['question_supplement'] = _preview(question)[0]
            view['question_is_supplement_not_requirement'] = True
        for key in ('acceptance_checks', 'depends_on', 'suggested_tools'):
            if task.get(key):
                view[key] = task[key]
    else:
        value, truncated = _preview(requirement, marker='question_preview')
        view['question'] = value
        if truncated:
            view['detail_required'] = True
        view['detail'] = 'read_task(task_id=%s)' % task.get('id')
    if task.get('status') and task.get('status') != task.get('execution_status'):
        view['task_status'] = task.get('status')
    return view


def assessment_view(assessment, *, full_diagnosis=False):
    """Latest judgment with support/opposition/gaps; explicitly a model judgment."""
    raw_diagnosis = assessment.get('diagnosis') or ''
    if full_diagnosis:
        diagnosis, truncated = raw_diagnosis, False
    else:
        diagnosis, truncated = _preview(raw_diagnosis, marker='diagnosis_preview')
    view = {'assessment_id': assessment.get('id'), 'task_id': assessment.get('task_id'),
            'attempt_id': assessment.get('attempt_id'), 'answer_status': assessment.get('answer_status'),
            'producer': assessment.get('producer'), 'is_model_judgment': True,
            'diagnosis': diagnosis,
            'supporting_evidence_ids': assessment.get('supporting_evidence_ids') or [],
            'opposing_evidence_ids': assessment.get('opposing_evidence_ids') or [],
            'gaps': assessment.get('gaps') or [],
            'hypotheses': [hypothesis_view(item) for item in assessment.get('hypotheses') or []]}
    if truncated and not full_diagnosis:
        view['detail_required'] = True
        view['detail'] = 'read_assessment(assessment_id=%s)' % assessment.get('id')
    return view


def hypothesis_view(hypothesis):
    if not isinstance(hypothesis, dict):
        return hypothesis
    kept = {key: hypothesis.get(key) for key in
            ('content', 'status', 'mechanism', 'supporting_evidence_ids', 'opposing_evidence_ids')}
    return {key: value for key, value in kept.items() if value not in (None, [], '')}


def directory_row(evidence, refs):
    """Minimal catalog entry used when a brief does not fit the local page."""
    row = {'evidence_id': evidence.get('id'), 'tool_name': evidence.get('tool_name'),
           'transport_status': evidence.get('transport_status'),
           'source_status': evidence.get('source_status'),
           'resource_ids': evidence.get('resource_ids') or [],
           'gaps': bool(evidence.get('data_gaps') or evidence.get('warnings')),
           'detail': 'read_evidence(evidence_id=%s)' % evidence.get('id')}
    ref = refs.get(_signature(evidence.get('returned_time_range'))) or refs.get(_signature(evidence.get('time_range')))
    if ref:
        row['window_ref'] = ref
    else:
        row['window_unknown'] = True
    return row


def payload_chars(value):
    return len(json.dumps(value, ensure_ascii=False, default=str))