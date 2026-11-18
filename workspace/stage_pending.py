"""Immutable pending proposals, relevant versions and one atomic stage allowance."""
import copy
import hashlib
import json
from .repository import WorkspaceError, new_id
from .artifacts import read_artifact_raw, write_artifact
from .investigations import digest, same_owner


def identity(b):
    return {'run_id': b.run_id, 'owner': b.role, 'task_id': b.task_id, 'attempt_id': b.attempt_id}


def check_guard(b, db, pending):
    saved = b.repository.get(db, b.incident_id, pending['id'], 'stage_pending')
    if (any(saved.get(k) != v for k, v in identity(b).items()) or saved['proposal_version'] != pending['proposal_version']
            or saved['proposal_digest'] != pending['proposal_digest']):
        raise WorkspaceError('stage_repair_conflict: pending identity/version changed')
    head = next((r for r in b.repository.list(db, b.incident_id, 'checkpoint')
                 if same_owner(b, r, b.role) and r.get('is_head')), {})
    if (head.get('id'), head.get('revision')) != (pending['opening_id'], pending['opening_revision']):
        raise WorkspaceError('stage_repair_conflict: opening checkpoint changed')
    for ref in pending['versions']:
        row = b.repository.get(db, b.incident_id, ref['id'], ref['kind'])
        if row['revision'] != ref['revision'] or digest(row) != ref['digest']:
            raise WorkspaceError('stage_repair_conflict: relevant ' + ref['kind'] + ' changed')
    if 'related_judgment_ids' in pending:
        current_ids = {r['id'] for r in b.repository.list(db, b.incident_id, 'judgment')
                       if r.get('run_id') == b.run_id and r.get('question_id') in pending['question_scope']}
        if current_ids != set(pending['related_judgment_ids']):
            raise WorkspaceError('stage_repair_conflict: relevant judgments added or removed')


async def original_record(b, context, record_id, kind):
    """Read what existed before original preparation, never guess from latest."""
    def read(db):
        rows = db.execute('SELECT body FROM events WHERE incident=? AND record_id=? AND seq<=? ORDER BY seq DESC',
                          (b.incident_id, record_id, context['workspace_revision'])).fetchall()
        for row in rows:
            body = json.loads(row[0])
            if body.get('id') == record_id and body.get('revision'):
                return body
        raise WorkspaceError('stage_repair_cannot_repair: original version unavailable: ' + record_id)
    row = await b.repository.transact(read)
    current = await b.repository.read(b.incident_id, record_id, kind)
    if row != current:
        raise WorkspaceError('stage_repair_conflict: source/semantic record changed since original input')
    return row


async def create(b, proposal, outcome, allowed, candidates, *, raw_output=None):
    if not b.last_context_id:
        raise WorkspaceError('stage_repair_cannot_repair: original model input unavailable')
    context = await b.repository.read(b.incident_id, b.last_context_id, 'context')
    if any(context.get(k) != v for k, v in {'run_id': b.run_id, 'role': b.role,
                                           'task_id': b.task_id, 'attempt_id': b.attempt_id}.items()):
        raise WorkspaceError('stage_repair_conflict: original context owner mismatch')
    if not context.get('visible_messages_artifact_id'):
        raise WorkspaceError('stage_repair_cannot_repair: original input was not sent')
    from .investigations import active_checkpoint
    head = await active_checkpoint(b, b.role) or {}
    if (proposal.get('expected_checkpoint_id'), proposal.get('expected_revision')) != (
            head.get('id'), head.get('revision')) and not (not head and proposal.get('expected_revision') in (0, None)):
        raise WorkspaceError('stage_repair_conflict: proposal opening does not match')
    ids = {(r['id'], 'question') for r in candidates if r.get('registered')}
    candidate_ids = {r['id'] for r in candidates}
    for row in await b.repository.all(b.incident_id, 'judgment'):
        if row.get('run_id') == b.run_id and row.get('question_id') in candidate_ids:
            ids.add((row['id'], 'judgment'))
            ids.update((ref, 'observation') for ref in row.get('opposing_observation_ids', []) +
                       row.get('supporting_observation_ids', []))
    for judgment in proposal.get('judgments', []):
        ids.update((ref, 'observation') for ref in judgment.get('supporting_observation_ids', []) +
                   judgment.get('opposing_observation_ids', []) if ref not in {
                       r.get('local_id') for r in proposal.get('observation_proposals', [])})
    for candidate in candidates:
        ids.update((ref, 'resource') for ref in candidate.get('resource_ids', []))
    if b.task_id:
        ids.add((b.task_id, 'task'))
    for obs in proposal.get('observation_proposals', []):
        ref = obs.get('source_ref') or {}
        ids.update((ref[k], kind) for k, kind in [('evidence_id', 'evidence'), ('artifact_id', 'artifact')] if ref.get(k))
    # Stored facts bring all their source originals into the version guard.
    semantic = {}
    for record_id, kind in list(ids):
        row = await original_record(b, context, record_id, kind)
        semantic[record_id] = row
        if kind == 'observation':
            ref = row.get('source_ref') or {}
            ids.update((ref[k], source_kind) for k, source_kind in [('evidence_id', 'evidence'), ('artifact_id', 'artifact')]
                       if ref.get(k))
    for record_id, kind in list(ids):
        row = semantic.get(record_id) or await original_record(b, context, record_id, kind)
        semantic[record_id] = row
        if kind == 'evidence' and row.get('artifact_id'):
            aid = row['artifact_id']
            semantic[aid] = await original_record(b, context, aid, 'artifact')
            ids.add((aid, 'artifact'))
    for record_id, kind in ids:
        if kind == 'artifact':
            await read_artifact_raw(b, record_id)
    artifact_id = outcome['proposal_artifact_id']
    raw_artifact_id = None
    if raw_output is not None and raw_output != proposal:
        raw_artifact = await write_artifact(b, {'raw_output': raw_output})
        await b.repository.save(b.incident_id, 'artifact', raw_artifact)
        raw_artifact_id = raw_artifact['id']
    pending = {'id': new_id('pending'), **identity(b), 'proposal_version': 1, 'proposal_digest': digest(proposal),
        'original_artifact_id': artifact_id, 'raw_output_artifact_id': raw_artifact_id,
        'rejection_artifact_id': outcome.get('proposal_artifact_id'),
        'source_context_id': context['id'], 'source_messages_artifact_id': context['visible_messages_artifact_id'],
        'opening_id': head.get('id'), 'opening_revision': head.get('revision'),
        'versions': [{'id': rid, 'kind': kind, 'revision': semantic[rid]['revision'], 'digest': digest(semantic[rid])}
                     for rid, kind in sorted(ids)],
        'allowed': allowed, 'candidates': candidates, 'errors': outcome.get('field_errors') or [],
        'question_scope': sorted(candidate_ids),
        'related_judgment_ids': sorted(rid for rid, kind in ids if kind == 'judgment'),
        'status': 'pending', 'repair_used': False}
    return await b.repository.save(b.incident_id, 'stage_pending', pending)


async def reserve(b, pending):
    stable = '|'.join(str(v or '') for v in [b.incident_id, b.run_id, b.role, b.task_id, b.attempt_id,
                                            pending['opening_id'] or 'initial'])
    key = 'repair_slot_' + hashlib.sha256(stable.encode()).hexdigest()
    def write(db):
        check_guard(b, db, pending)
        if any(r['id'] == key for r in b.repository.list(db, b.incident_id, 'stage_repair_slot')):
            raise WorkspaceError('stage_repair_budget_exhausted: this real stage already used Repair')
        b.repository.put(db, b.incident_id, 'stage_repair_slot',
                         {'id': key, **identity(b), 'pending_id': pending['id'], 'opening_id': pending['opening_id']})
        row = b.repository.get(db, b.incident_id, pending['id'], 'stage_pending')
        row.update(repair_used=True, status='reserved')
        return b.repository.put(db, b.incident_id, 'stage_pending', row, row['revision'])
    return await b.repository.transact(write)


async def stage_slot_used(b):
    from .investigations import active_checkpoint
    head = await active_checkpoint(b, b.role) or {}
    return any(all(r.get(k) == v for k, v in identity(b).items()) and r.get('opening_id') == head.get('id')
               for r in await b.repository.all(b.incident_id, 'stage_repair_slot'))


def merge(pending, proposal, patch):
    from models.stage_repair import StageRepair
    patch = StageRepair.model_validate(patch)
    if patch.proposal_id != pending['id'] or patch.proposal_version != pending['proposal_version']:
        raise WorkspaceError('stage_repair_invalid_patch: proposal identity/version mismatch')
    if patch.status == 'cannot_repair':
        raise WorkspaceError('stage_repair_cannot_repair: ' + patch.reason)
    paths = [r.path for r in patch.changes]
    if len(set(paths)) != len(paths) or set(paths) != set(pending['allowed']):
        raise WorkspaceError('stage_repair_invalid_patch: paths must exactly match allowed errors')
    result = copy.deepcopy(proposal)
    for change in patch.changes:
        if change.value not in pending['allowed'][change.path]:
            raise WorkspaceError('stage_repair_invalid_patch: value outside this path candidates')
        result['judgments'][int(change.path.split('/')[2])]['question_id'] = change.value
    return result
