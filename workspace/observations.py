"""Verify model-proposed observations against the stored original (PRD §19.4.3).

The model proposes *where* a value lives and what it expects; the program reads
the stored source, resolves the JSON Pointer, compares the value and records the
scope. A mismatch is rejected with the real difference instead of being silently
rewritten into a pass, and every observation carries the split verification flags
``source_valid / value_matches / scope_known / coverage_status`` rather than one
vague ``verified=true``.

Nothing here mutates stored originals: a source that changed produces a new
observation or an explicit ``source_changed`` marker.
"""
import json

from .artifacts import read_artifact_raw
from .repository import WorkspaceError, new_id, utcnow

#: Fields the program itself derives; a model proposal never sets them.
PROGRAM_ONLY = ('id', 'incident_id', 'run_id', 'revision', 'created_at', 'verification')


def pointer_tokens(pointer):
    """Split an RFC 6901 pointer into tokens (``~1`` -> ``/``, ``~0`` -> ``~``)."""
    if pointer == '':
        return []
    if not isinstance(pointer, str) or not pointer.startswith('/'):
        raise WorkspaceError('json_pointer must be empty or start with "/"')
    return [token.replace('~1', '/').replace('~0', '~') for token in pointer.split('/')[1:]]


def resolve_pointer(document, pointer):
    """Resolve a JSON Pointer; returns ``(value, found, container)``."""
    node = document
    for token in pointer_tokens(pointer):
        if isinstance(node, list):
            if token == '-':
                return None, False, node  # the end-of-array token is not a readable value
            if not token.isdigit() or (len(token) > 1 and token.startswith('0')):
                return None, False, node
            index = int(token)
            if not 0 <= index < len(node):
                return None, False, node
            node = node[index]
            continue
        if not isinstance(node, dict) or token not in node:
            return None, False, node
        node = node[token]
    return node, True, node


def values_match(expected, actual):
    """Strict comparison: ``3`` is not ``"3"`` and ``True`` is not ``1``."""
    if isinstance(expected, bool) or isinstance(actual, bool):
        return isinstance(expected, bool) and isinstance(actual, bool) and expected == actual
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
        return expected == actual
    if type(expected) is not type(actual):
        return False
    if isinstance(actual, dict):
        return expected.keys() == actual.keys() and all(values_match(expected[key], value) for key, value in actual.items())
    if isinstance(actual, list):
        return len(expected) == len(actual) and all(values_match(left, right) for left, right in zip(expected, actual))
    return expected == actual


def document_for(evidence, artifact, which='artifact'):
    if which == 'evidence':
        return evidence
    return artifact


async def load_source(binding, source_ref):
    """Read the stored source (evidence record + its archived raw) for one reference."""
    evidence_id = source_ref.get('evidence_id')
    artifact_id = source_ref.get('artifact_id')
    evidence, artifact = None, None
    if evidence_id:
        evidence = await binding.repository.read(binding.incident_id, evidence_id, 'evidence')
        if evidence.get('run_id') != binding.run_id:
            raise WorkspaceError('source belongs to another run; explicit historical policy required')
        if artifact_id and artifact_id != evidence.get('artifact_id'):
            raise WorkspaceError('artifact_id is not the evidence original')
        artifact_id = artifact_id or evidence.get('artifact_id')
    if artifact_id:
        record = await binding.repository.read(binding.incident_id, artifact_id, 'artifact')
        if not evidence and record.get('run_id') != binding.run_id:
            raise WorkspaceError('artifact belongs to another run or has unknown run provenance')
        artifact = await read_artifact_raw(binding, artifact_id)
        return evidence, artifact, record
    if evidence is None:
        raise WorkspaceError('source_ref needs an evidence_id or an artifact_id')
    return evidence, None, None


def source_problems(evidence, artifact_record, source_ref):
    """Check existing source revision; legacy digest fields are informational."""
    problems = []
    if evidence is not None:
        expected_revision = source_ref.get('source_revision')
        if expected_revision is not None and evidence.get('revision') != expected_revision:
            problems.append({'field': 'source_revision', 'reason': 'source_changed',
                             'expected': expected_revision, 'actual': evidence.get('revision')})
    return problems


def scope_of(evidence):
    identities = [identity for identity in (evidence or {}).get('resource_identities') or [] if identity.get('id')]
    return {'resource_refs': identities,
            'requested_window': (evidence or {}).get('time_range') or None,
            'returned_window': (evidence or {}).get('returned_time_range') or None,
            'scope_known': bool(identities),
            'coverage_status': 'partial' if (evidence or {}).get('truncated') else 'complete'}


async def shown_to_model(binding, evidence_id, source_kind='evidence'):
    """Is this source provably visible in the model's current input or reads?

    A model-claimed observation must be traceable to the context manifest it was
    asked in or to a read receipt of the current stage; the program can register
    its own observations separately with ``producer='program'``.
    """
    if binding.last_context_id:
        try:
            manifest = await binding.repository.read(binding.incident_id, binding.last_context_id, 'context')
        except WorkspaceError:
            manifest = None
        if manifest:
            if evidence_id in (manifest.get('injected_evidence_ids') or []):
                return True
            for event in manifest.get('read_events') or []:
                if event.get('reference') == evidence_id or evidence_id in (event.get('evidence_ids') or []):
                    return True
    receipts = await binding.repository.all(binding.incident_id, 'read_receipt')
    return any(receipt.get('source_id') == evidence_id and receipt.get('source_kind') == source_kind
               for receipt in receipts)


async def verify_observation(binding, proposal, *, question_ids=(), owner=None, producer='model', source_context_id=None):
    """Verify one proposed observation; returns ``{'accepted': record}`` or ``{'rejected': {...}}``.

    ``proposal`` carries ``source_ref`` (evidence/artifact + revision/hash +
    json_pointer) and ``expected_value``; a free-text proposal carries
    ``text_ref`` (source_ref + char range) instead. Requests for aggregates use
    ``aggregate`` (operation, source range) and are computed by the program.
    """
    source_ref = proposal.get('source_ref') or {}
    try:
        evidence, artifact, artifact_record = await load_source(binding, source_ref)
    except WorkspaceError as exc:
        return rejection('source_unavailable', str(exc), source_ref)
    problems = source_problems(evidence, artifact_record, source_ref)
    if problems:
        return {'rejected': {'reason': 'source_changed', 'problems': problems, 'source_ref': source_ref}}
    source_ref = dict(source_ref, evidence_id=(evidence or {}).get('id'),
                      artifact_id=(artifact_record or {}).get('id'),
                      source_revision=(evidence or artifact_record or {}).get('revision'),
                      source_hash=(artifact_record or {}).get('sha256'))
    proposal = {**proposal, 'source_ref': source_ref}
    if proposal.get('aggregate'):
        return await program_aggregate(binding, proposal, evidence, artifact, question_ids, owner)
    if proposal.get('text_ref'):
        return await verify_fragment(binding, proposal, evidence, artifact, question_ids, owner, source_context_id=source_context_id)
    document_name = proposal.get('source_document') or 'artifact'
    document = document_for(evidence, artifact, document_name)
    if document is None:
        return rejection('source_unavailable', 'the archived source is not readable', source_ref)
    pointer = source_ref.get('json_pointer', '')
    resolution = None
    try:
        actual, found, _container = resolve_pointer(document, pointer)
    except WorkspaceError as exc:
        return rejection('invalid_pointer', str(exc), source_ref)
    if not found:
        from .observation_visibility import projection_pointer
        canonical = (await projection_pointer(binding, (evidence or {}).get('id'), pointer, source_context_id=source_context_id)
                     if document_name == 'artifact' and evidence and source_ref.get('range') is None else None)
        if canonical and canonical != pointer:
            resolution = {'submitted_pointer': pointer, 'resolved_pointer': canonical,
                          'context_id': source_context_id or binding.last_context_id, 'method': 'visible_projection_mapping'}
            pointer = canonical
            source_ref = dict(source_ref, json_pointer=pointer)
            actual, found, _container = resolve_pointer(document, pointer)
    if not found:
        return rejection('pointer_not_found',
                         'json_pointer does not resolve in the %s document' % document_name,
                         source_ref, pointer=pointer)
    if source_ref.get('range') is not None:
        from .material_ranges import slice_value
        try:
            actual, source_total = slice_value(actual, source_ref['range'], source_ref.get('range_unit'))
        except WorkspaceError as exc:
            return rejection('invalid_range', str(exc), source_ref)
    if producer == 'model':
        from .observation_visibility import field_shown
        if not await field_shown(binding, source_ref, actual, source_context_id=source_context_id):
            return rejection('not_shown_to_model', 'read this exact field in the current owner segment first', source_ref)
    if 'expected_value' not in proposal:
        return rejection('expected_value_required', 'missing expected_value is distinct from explicit null', source_ref)
    expected = proposal['expected_value']
    if not values_match(expected, actual):
        return {'rejected': {'reason': 'value_mismatch', 'source_ref': source_ref, 'json_pointer': pointer,
                             'expected': expected, 'actual': actual}}
    scope = scope_of(evidence)
    if source_ref.get('range') is not None:
        scope['coverage_status'] = ('complete' if source_ref['range'] == [0, source_total]
                                    and scope['coverage_status'] == 'complete' else 'partial')
    record = observation_record(binding, owner=owner, question_ids=question_ids,
                               source_ref=dict(source_ref, evidence_id=(evidence or {}).get('id'),
                                               artifact_id=(artifact_record or {}).get('id'),
                                               source_revision=(evidence or artifact_record or {}).get('revision'),
                                               source_hash=(artifact_record or {}).get('sha256')),
                               field='json_pointer:' + (pointer or '/'),
                               field_value=actual, kind='range' if source_ref.get('range') is not None else 'field', scope=scope,
                               limitations=list(proposal.get('limitations') or []),
                               verification={'source_valid': True, 'value_matches': True,
                                             'scope_known': scope['scope_known'],
                                             'coverage_status': scope['coverage_status']},
                               producer=producer)
    if resolution:
        record['source_pointer_resolution'] = resolution
    return {'accepted': record}


async def verify_fragment(binding, proposal, evidence, artifact, question_ids, owner, *, source_context_id=None):
    """A free-text fragment is verified by exact characters and a stated range."""
    text_ref = proposal.get('text_ref') or {}
    range_ = text_ref.get('char_range')
    if not (isinstance(range_, list) and len(range_) == 2 and all(isinstance(v, int) for v in range_)):
        return rejection('fragment_range_required', 'text_ref needs an explicit [start, end] char range',
                         text_ref.get('source_ref') or {})
    start, end = range_
    if start < 0 or end <= start:
        return rejection('invalid_range', 'char range must satisfy 0 <= start < end', {})
    fragments = proposal.get('fragments')
    if not isinstance(fragments, list) or not fragments:
        return rejection('fragment_required', 'text_ref needs at least one exact fragment', {})
    document = artifact if artifact is not None else evidence
    text = json.dumps(document, ensure_ascii=False)
    if end > len(text):
        return rejection('fragment_out_of_range', 'char range exceeds the stored text length', {})
    window = text[start:end]
    for fragment in fragments:
        if not isinstance(fragment, str) or fragment not in window:
            return {'rejected': {'reason': 'fragment_mismatch', 'expected': fragment, 'window_chars': len(window)}}
    from .observation_visibility import fragment_shown
    if not await fragment_shown(binding, proposal.get('source_ref') or {}, window, source_context_id=source_context_id):
        return rejection('not_shown_to_model', 'read the exact fragment before claiming it', proposal.get('source_ref') or {})
    scope = scope_of(evidence)
    record = observation_record(binding, owner=owner, question_ids=question_ids,
                               source_ref=dict(proposal.get('source_ref') or {}),
                               field='chars:%d-%d' % (start, end), field_value=window, kind='fragment',
                               scope=scope, limitations=list(proposal.get('limitations') or []),
                               verification={'source_valid': True, 'value_matches': True,
                                             'scope_known': scope['scope_known'],
                                             'coverage_status': 'partial'},
                               producer='model')
    record['interpretation'] = 'fragment_match_does_not_prove_meaning; the explanation stays a judgment'
    return {'accepted': record}


#: Program-side aggregates: name -> function over a bounded list of scalars.
AGGREGATES = {
    'count': lambda values: len(values),
    'distinct_count': lambda values: len({json.dumps(value, sort_keys=True, ensure_ascii=False) for value in values}),
}


async def program_aggregate(binding, proposal, evidence, artifact, question_ids, owner):
    """A fixed, recorded aggregation over one bounded range (never a free formula)."""
    aggregate = proposal['aggregate']
    operation = aggregate.get('operation')
    if operation not in AGGREGATES:
        return rejection('aggregate_not_allowed', 'aggregate operation must be one of %s'
                         % ', '.join(sorted(AGGREGATES)), {})
    document = artifact if artifact is not None else evidence
    pointer = aggregate.get('json_pointer', '')
    node, found, _ = resolve_pointer(document, pointer)
    if not found or not isinstance(node, list):
        return rejection('aggregate_source_invalid', 'aggregate needs a list at its json_pointer', {})
    start = int(aggregate.get('start_index') or 0)
    end = aggregate.get('end_index')
    end = len(node) if end is None else int(end)
    if start < 0 or end > len(node) or end <= start:
        return rejection('aggregate_range_invalid', 'aggregate range must stay inside the stored list', {})
    values = node[start:end]
    result = AGGREGATES[operation](values)
    scope = scope_of(evidence)
    record = observation_record(binding, owner=owner, question_ids=question_ids,
                               source_ref=dict(proposal.get('source_ref') or {}),
                               field='aggregate:%s%s[%d:%d]' % (operation, pointer, start, end),
                               field_value=result, kind='aggregate', scope=scope,
                               limitations=list(proposal.get('limitations') or []),
                               verification={'source_valid': True, 'value_matches': True,
                                             'scope_known': scope['scope_known'],
                                             'coverage_status': 'complete' if start == 0 and end == len(node)
                                             and scope['coverage_status'] == 'complete' else 'partial'},
                               producer='program')
    record['aggregate'] = {'operation': operation, 'json_pointer': pointer, 'start_index': start,
                           'end_index': end, 'input_total': len(node),
                           'note': 'a partial range counts only that range'}
    return {'accepted': record}


def observation_record(binding, *, owner, question_ids, source_ref, field, field_value, kind, scope,
                       limitations, verification, producer):
    """Build the record body; the program owns ids, revisions and timestamps."""
    return {'id': new_id('observation'), 'incident_id': binding.incident_id, 'run_id': binding.run_id,
            'producer_role': owner or binding.role, 'task_id': binding.task_id,
            'attempt_id': binding.attempt_id, 'created_at': utcnow(),
            'producer': producer, 'kind': kind, 'question_ids': list(question_ids),
            'source_ref': source_ref, 'field': field, 'field_value': field_value,
            'scope': scope, 'limitations': list(limitations), 'verification': verification}


def rejection(reason, message, source_ref, pointer=None):
    row = {'reason': reason, 'message': message, 'source_ref': source_ref}
    if pointer is not None:
        row['json_pointer'] = pointer
    return {'rejected': row}


def must_handle(questions, judgments):
    """Opposition and open gaps that stay in the input even if the model omits them.

    ``questions`` are the selected question records, ``judgments`` their stored
    judgments; the result lists judgment ids plus the opposition observations
    that a later stage must still see (PRD §19.4.4).
    """
    required = {'judgment_ids': [], 'observation_ids': [], 'question_ids': []}
    for question in questions:
        if question.get('status') in ('open', 'partially_answered', 'blocked'):
            required['question_ids'].append(question['id'])
    for judgment in judgments:
        if judgment.get('status') in ('withdrawn',):
            continue
        opposition = list(judgment.get('opposing_observation_ids') or [])
        gaps = [gap for gap in judgment.get('limitations') or []]
        if opposition or gaps:
            required['judgment_ids'].append(judgment['id'])
            required['observation_ids'].extend(opposition)
    required['observation_ids'] = sorted(set(required['observation_ids']))
    required['question_ids'] = sorted(set(required['question_ids']))
    return required
