"""One terminal execution result shared by state, report and run audit."""
import json

#: Internal stop reasons introduced by the lifecycle work (PRD §19.10). They map
#: onto the existing execution/answer states; task and attempt enums are not
#: extended, and an unknown code keeps the current failure semantics.
INTERNAL_REASON_STATES = {
    'context_unsatisfiable': {'execution_status': 'incomplete', 'answer_status': 'inconclusive',
                              'meaning': 'input preparation incomplete; the request was refused and stays unresolved'},
    'checkpoint_conflict': {'execution_status': 'incomplete', 'answer_status': 'inconclusive',
                            'meaning': 'stage commit revision conflict; the previous active segment is kept'},
    'checkpoint_invalid': {'execution_status': 'incomplete', 'answer_status': 'inconclusive',
                           'meaning': 'stage commit rejected and bounded correction was exhausted'},
    'packet_stale': {'execution_status': 'incomplete', 'answer_status': 'inconclusive',
                     'meaning': 'report packet invalidated by a new observation or source revision'},
}


def internal_reason_state(code):
    """Existing status values for one internal reason; unknown codes fail as before."""
    return INTERNAL_REASON_STATES.get(code, {'execution_status': 'failed', 'answer_status': 'inconclusive',
                                             'meaning': 'unclassified stop reason'})


def normalize_execution(status, error=None, stop_reason=None, analyses=(), secondary_reasons=()):
    failures = []
    if error is not None:
        failures.append({'source': 'parent', 'error': error})
    workers = [{'source': 'worker', 'task_id': a.get('task_id') or a.get('task', {}).get('task_id') or a.get('task', {}).get('id'),
                'attempt_id': a.get('attempt_id'), 'error': a['execution_error']} for a in analyses
               if a.get('execution_error') is not None]
    workers.sort(key=lambda f: (str(f['task_id'] or ''), str(f['attempt_id'] or ''), json.dumps(f['error'], sort_keys=True, default=str)))
    failures.extend(workers)
    selected = failures[0]['error'] if failures else None
    normalized = None
    reason = stop_reason
    extra = list(secondary_reasons or ())
    if failures:
        normalized = dict(selected) if isinstance(selected, dict) else {'raw_error': selected}
        code = normalized.get('code')
        code = code.strip() if isinstance(code, str) else None
        normalized['code'] = code or 'execution_failed'
        reason = normalized['code']
        if stop_reason and stop_reason != reason:
            extra.append(stop_reason)
        status = 'cancelled' if status == 'cancelled' else 'failed'
    return {'execution_status': status, 'execution_error': normalized, 'stop_reason': reason,
            'secondary_stop_reasons': list(dict.fromkeys(extra)), 'execution_errors': failures}
