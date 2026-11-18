"""Minimal semantic Repair input, never the normal workbench/history builder."""
import json
from langchain_core.messages import SystemMessage, HumanMessage
from config import settings
from .budget_policy import hard_limit
from .request_budget import measure
from .publication import publish, refuse_context
from workspace.artifacts import read_artifact_raw


async def prepare_repair(b, pending, request):
    original = await read_artifact_raw(b, pending['original_artifact_id'])
    proposal = original['proposal']
    observations = {r.get('local_id'): r for r in proposal.get('observation_proposals', []) if r.get('local_id')}
    judgments, needed = [], set()
    for path in pending['allowed']:
        row = proposal['judgments'][int(path.split('/')[2])]
        judgments.append({'path': path, 'judgment': row})
        needed.update(row.get('supporting_observation_ids', []) + row.get('opposing_observation_ids', []))
    authoritative_limits = []
    for record_id in pending.get('related_judgment_ids', []):
        row = await b.repository.read(b.incident_id, record_id, 'judgment')
        needed.update(row.get('opposing_observation_ids') or [])
        authoritative_limits.extend(row.get('limitations') or [])
    facts = []
    for ref in sorted(needed):
        if ref in observations:
            facts.append({'id': ref, 'proposal_observation': observations[ref],
                          'verification': 'full preflight verified against original visible input and stored source'})
        else:
            row = await b.repository.read(b.incident_id, ref, 'observation')
            facts.append({k: row.get(k) for k in ('id', 'question_ids', 'field', 'field_value', 'source_ref',
                                                  'scope', 'verification', 'limitations')})
    resources = [await b.repository.read(b.incident_id, ref['id'], 'resource')
                 for ref in pending['versions'] if ref['kind'] == 'resource']
    payload = {'proposal_id': pending['id'], 'proposal_version': pending['proposal_version'],
        'role': b.role, 'stage_goal': proposal['stage_goal'], 'errors': pending['errors'],
        'allowed': pending['allowed'], 'judgments': judgments, 'candidate_questions': pending['candidates'],
        'observations': facts,
        'authoritative_limits': list(dict.fromkeys(authoritative_limits)),
        'resource_scope': [{k: row.get(k) for k in ('id', 'kind', 'name', 'cluster', 'namespace', 'uid', 'identity_status')}
                           for row in resources],
        'instructions': 'Only assign the allowed question_id fields using semantic content and concrete observations. '
                        'Preserve all other fields. No queries, reads, new questions or full proposal. '
                        'A limited normal observation is not a proven absence/cause. If ambiguous return cannot_repair.'}
    messages = [SystemMessage(content='Repair only the specified question assignments for this investigation. '
                               'A field patch does not establish causation.'),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False, separators=(',', ':')))]
    total, components = measure(messages, request)
    limit = min(settings.STAGE_REPAIR_MAX_CHARS, hard_limit())
    manifest = {'id': __import__('workspace.repository', fromlist=['new_id']).new_id('context'),
        'run_id': b.run_id, 'role': b.role, 'task_id': b.task_id, 'attempt_id': b.attempt_id,
        'phase': 'stage_repair', 'pending_id': pending['id'], 'proposal_version': pending['proposal_version'],
        'source_context_id': pending['source_context_id'], 'request_chars_v2': total,
        'component_chars': components, 'repair_limit': limit, 'input_chars': total,
        'injected_evidence_ids': [], 'read_events': [], 'coverage_note': 'only listed semantic fragments shown; not original workbench'}
    if total > limit:
        await refuse_context(b, manifest, {}, messages, 'stage_repair',
                             reason='repair_context_unsatisfiable: required semantic fragments exceed Repair limit', visible=messages)
    return await publish(b, manifest, messages)
