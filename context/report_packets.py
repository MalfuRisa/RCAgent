"""The final packet view contains registered facts, never the earlier read history."""
from workspace.repository import WorkspaceError
from .investigations import _observation_row, _judgment_row


def packet_view(binding, snapshot):
    packet_id = getattr(binding, 'active_packet_id', None)
    packet = next((r for r in snapshot.get('report_packet') or [] if r['id'] == packet_id), None)
    if packet is None:
        raise WorkspaceError('packet_invalid: active packet is missing from the input snapshot')
    state = packet['source_state']
    required = {ref for statement in packet['statements'] for ref in statement['observation_ids']}
    required.update(ref for judgment in state['judgment'] for ref in judgment.get('opposing_observation_ids') or [])
    observations = [_observation_row(r) for r in state['observation']
                    if r.get('kind') != 'range' or r['id'] in required]
    judgments = [_judgment_row(r) for r in state['judgment']]
    return {'context_view_version': 'packet-v1', 'incident_id': binding.incident_id, 'run_id': binding.run_id,
            'scope': snapshot['scope'], 'query_window': binding.query_window,
            'report_packet': {'packet_id': packet_id, 'statements': packet['statements'],
                              'unresolved_question_ids': packet['unresolved_question_ids'],
                              'question_dispositions': packet['question_dispositions']},
            'investigation': {'observations': observations, 'judgments': judgments,
                'committed_pages': {'total': sum(r.get('kind') == 'range' for r in state['observation']),
                    'read': 'read_investigation(kind="observation", question_id=<id>, cursor=<next_cursor>)'}},
            'tasks': [{key: r.get(key) for key in ('id', 'execution_status', 'answer_status',
                                                  'latest_attempt_id', 'question_ids')} for r in state['task']],
            'resources': [{key: r.get(key) for key in ('id', 'kind', 'name', 'namespace', 'cluster', 'uid', 'revision')}
                          for r in state['resource']],
            'evidence': [], 'coverage': {'packet_source_fingerprint': packet['source_fingerprint']}}
