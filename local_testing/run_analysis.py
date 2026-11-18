"""One artifact-derived measurement for runner summaries and delivery reports.

This module never contacts a model, queries services or modifies raw records.
"""
from collections import Counter
import json
from pathlib import Path


def read_usage(records, workspace, final=None):
    """Payload novelty and observable later references, not a model quality score."""
    from workspace.receipts import source_hash
    workspace = Path(workspace).resolve()
    seen, rows = set(), []
    references = set()
    for statement in (final or {}).get('final_report', {}).get('statements', []):
        references.update(statement.get('supporting_evidence_ids') or [])
        references.update(statement.get('opposing_evidence_ids') or [])
    contexts = records.get('context', [])
    def leaves(value, prefix='', offset=0):
        if isinstance(value, dict):
            return [leaf for key, item in value.items() for leaf in leaves(item, prefix + '.' + key)]
        if isinstance(value, list):
            return [leaf for index, item in enumerate(value) for leaf in leaves(item, prefix + '.' + str(index + offset))]
        return [(prefix, source_hash(value))]
    for receipt in records.get('read_receipt', []):
        path = (workspace / 'artifacts' / receipt['incident_id'] / (receipt['result_artifact_id'] + '.json')).resolve()
        row = {key: receipt.get(key) for key in ('id', 'tool_name', 'source_kind', 'source_id', 'field', 'range', 'range_unit')}
        try:
            if not path.is_relative_to(workspace / 'artifacts'):
                raise ValueError('read body outside workspace')
            body = json.loads(path.read_text(encoding='utf-8'))
            if receipt.get('result_hash') and source_hash(body) != receipt['result_hash']:
                raise ValueError('receipt body hash mismatch')
        except (OSError, ValueError) as exc:
            row['storage_reference_error'] = str(exc)
            rows.append(row)
            continue
        span = body.get('range') or [0, 0]
        row['continued_page'] = bool(span[0])
        row['preview_or_pointer_only'] = not ('items' in body or 'text' in body)
        key = (receipt.get('source_kind'), receipt.get('source_id'), receipt.get('source_hash'),
               receipt.get('source_revision'), receipt.get('field'))
        if 'text' in body:
            payload_values = [(str(span), source_hash(body['text']))]
            row['novelty_unit'] = 'serialized_character_page; overlapping pages not merged'
        else:
            payload_values = leaves(body.get('items', body), offset=span[0] if 'items' in body else 0)
            row['novelty_unit'] = 'returned_payload_leaf; includes metadata and is not diagnostic fact count'
        keys = {key + leaf for leaf in payload_values}
        row['new_payload_values'] = len(keys - seen)
        row['repeated_payload_values'] = len(keys & seen)
        seen.update(keys)
        checks = [check for context in contexts for check in context.get('read_retention_checks', [])
                  if check.get('receipt_id') == receipt['id']]
        row['retained_in_later_input'] = any(c.get('required_values_retained') for c in checks) if checks else None
        row['source_referenced_in_final_report'] = receipt.get('source_id') in references
        rows.append(row)
    return {'method': 'saved returned payloads, context retention checks and final evidence references; no inference of model reasoning',
            'read_count': len(rows), 'rows': rows,
            'efficiency_conclusion': 'pending final proposal and manual field-level review' if not records.get('report_proposal')
            else 'counts available; fewer reads alone does not establish better investigation'}


def task_failures(records, evidence_path='records.json'):
    tasks = {t['id']: t for t in records.get('task', [])}
    failures = []
    for attempt in records.get('attempt', []):
        task = tasks.get(attempt.get('task_id'), {})
        if attempt.get('status') != 'failed':
            continue
        failures.append({'task_id': attempt.get('task_id'), 'attempt_id': attempt['id'],
                         'target': task.get('target_resource'),
                         'execution_status': 'failed', 'answer_status': task.get('answer_status'),
                         'stop_reason': attempt.get('stop_reason'),
                         'original_evidence_path': str(evidence_path) + '#attempt/' + attempt['id']})
    failed_tasks = {f['task_id'] for f in failures}
    for task in tasks.values():
        if task.get('execution_status') == 'failed' and task['id'] not in failed_tasks:
            failures.append({'task_id': task['id'], 'attempt_id': task.get('latest_attempt_id'),
                             'target': task.get('target_resource'), 'execution_status': 'failed',
                             'answer_status': task.get('answer_status'), 'stop_reason': task.get('stop_reason'),
                             'original_evidence_path': str(evidence_path) + '#task/' + task['id']})
    return failures


def analyse_records(records, final=None, evidence_path='records.json', events=None):
    final = final or {}
    attempts = records.get('model_attempt', [])
    contexts = records.get('context', [])
    calls = {a.get('logical_call_id') for a in attempts if a.get('send_started_at')}
    sent = [c for c in contexts if c.get('model_call_reference') in calls]
    sent_peak = max((c.get('input_chars') or 0 for c in sent), default=0)
    candidate_peak = max((c.get('input_chars') or 0 for c in contexts), default=0)
    usage = [a.get('token_usage') for a in attempts]
    known = [u for u in usage if isinstance(u, dict)]
    totals = {key: sum(u[key] for u in known if isinstance(u.get(key), (int, float)))
              if any(isinstance(u.get(key), (int, float)) for u in known) else None
              for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    tasks = records.get('task', [])
    phases = Counter(c.get('phase') for c in sent)
    roles = Counter(c.get('role') for c in sent)
    proposals = [p for p in records.get('report_proposal', [])
                 if (isinstance(p.get('proposal'), dict) and p['proposal'].get('final_report'))
                 or (p.get('packet_id') and (p.get('normalized') or {}).get('final_report'))]
    decisions = []
    for event in events or []:
        if event.get('action') != 'supervisor_structured_decision':
            continue
        body = event.get('body') or {}
        if isinstance(body, str):
            import json
            body = json.loads(body)
        decisions.append(body)
    return {'sent_peak_input_chars': sent_peak, 'candidate_peak_input_chars': candidate_peak,
            'sent_peak_request_chars_v2': max((c.get('request_chars_v2') or 0 for c in sent), default=0),
            'candidate_peak_request_chars_v2': max((c.get('request_chars_v2') or 0 for c in contexts), default=0),
            'stage_commit_count': len(records.get('stage_commit', [])),
            'report_packet_count': len(records.get('report_packet', [])),
            'peak_input_chars': candidate_peak, 'peak_input_chars_semantics': 'candidate_including_refused',
            'model_attempts': len(attempts),
            'completed_model_attempts': sum(a.get('status') == 'completed' for a in attempts),
            'token_usage': {**totals, 'reported_attempts': len(known),
                            'unreported_attempts': len(attempts) - len(known),
                            'complete_total_known': len(known) == len(attempts)},
            'model_failures': [{key: a.get(key) for key in
                               ('id', 'logical_call_id', 'error_type', 'http_status', 'request_ms', 'token_usage')}
                              for a in attempts if a.get('status') == 'failed'],
            'task_status_counts': dict(Counter((t.get('execution_status') or 'unknown') + '/' +
                                               (t.get('answer_status') or 'unknown') for t in tasks)),
            'task_failures': task_failures(records, evidence_path),
            'stages': {'worker_entered': bool(roles['worker'] or any(a.get('owner') == 'worker' for a in records.get('attempt', []))),
                       'supervisor_entered': any(c.get('role') == 'supervisor' for c in contexts),
                       'sent_phase_counts': dict(phases),
                       'structured_submission_sent': phases['initial_submission'] + phases['packet_submission'],
                       'structured_followup_decisions': sum(bool(d.get('tasks_to_execute_ids') or d.get('followup_task_count'))
                                                            for d in decisions) if events is not None and decisions else None,
                       'structured_decisions': decisions,
                       'final_report_proposals': len(proposals),
                       'report_proposal_records': len(records.get('report_proposal', [])),
                       'gate_report_present': bool(final.get('final_report')),
                       'gate_fallback_only': bool(final.get('final_report')) and not records.get('report_proposal')},
            'context_candidates': [{k: c.get(k) for k in
                                    ('id', 'role', 'phase', 'input_chars', 'projection', 'budget_steps',
                                     'history_replacements', 'verified_read_detail_count', 'disposition',
                                     'rejection_reason', 'protected_parts', 'read_reference_errors', 'read_retention_checks')}
                                   for c in contexts],
            'read_count': len(records.get('read_receipt', [])),
            'preflight_queries_included': False}
