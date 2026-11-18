"""Read-only live verification boundaries and model-attempt diagnostics."""


def iam_inventory_check(raw, cluster='rd2', namespace='kylin-insights'):
    failure = {'execution_status': 'blocked_environment', 'blocking_step': 'iam_pod_inventory',
               'model_attempts': 0, 'remote_queries': 0, 'inventory_result': raw}
    if raw.get('error') or raw.get('error_type') or raw.get('status') == 'error':
        return {**failure, 'classification': 'inventory_error', 'stop_reason': 'iam_inventory_error'}, None
    if raw.get('truncated') or raw.get('meta', {}).get('truncated') or raw.get('status') == 'partial':
        return {**failure, 'classification': 'inventory_partial', 'stop_reason': 'iam_inventory_incomplete'}, None
    candidates = raw.get('candidates', [])
    if not candidates:
        return {**failure, 'classification': 'inventory_not_found', 'stop_reason': 'iam_inventory_not_found'}, None
    valid = [p for p in candidates if p.get('kind') == 'pod' and p.get('cluster') == cluster and
             p.get('namespace') == namespace and p.get('name') and (p.get('uid') or p.get('pod_uid'))]
    if len(valid) != 1 or len(candidates) != 1:
        return {**failure, 'classification': 'inventory_identity_unresolved', 'stop_reason': 'iam_identity_unresolved'}, None
    return None, valid[0]


def model_diagnostics(records):
    calls = {c['id']: c for c in records.get('model_call', [])}
    contexts = {c.get('model_call_reference'): c for c in records.get('context', [])}
    return [{'attempt_id': a['id'], 'logical_call_id': a['logical_call_id'],
             'role': calls.get(a['logical_call_id'], {}).get('role'),
             'context_id': contexts.get(a['logical_call_id'], {}).get('id'),
             'phase': contexts.get(a['logical_call_id'], {}).get('model_phase'),
             'injected_evidence_ids': contexts.get(a['logical_call_id'], {}).get('injected_evidence_ids'),
             'input_chars': contexts.get(a['logical_call_id'], {}).get('input_chars'),
             'queue_ms': a.get('queue_ms'), 'request_ms': a.get('request_ms'),
             'status': a.get('status'), 'error_type': a.get('error_type'),
             'token_usage': a.get('token_usage'), 'usage_status': a.get('usage_status'),
             'visible_messages_artifact_id': contexts.get(a['logical_call_id'], {}).get('visible_messages_artifact_id')}
            for a in records.get('model_attempt', [])]
