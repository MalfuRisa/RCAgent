"""Which investigation material each role sees, and what the manifest records.

Selection follows PRD §19.5: the role's own questions and the declarations it was
asked to check come first with their concrete facts, judgments, opposition and
limits; every other direction stays visible as a bounded overview with totals,
uncovered counts and a read entry, so a direction is never silently dropped.
Observation values travel with their sources, and opposition the model omitted is
pulled back in from the stored records.

The manifest fields of PRD §19.9 are produced here; the detailed audit stays in
storage instead of being injected recursively.
"""
import json
from workspace.observations import must_handle
from workspace.investigations import same_owner

from .views import _preview

#: How many records one question may inject before it becomes a read entry.
OBSERVATIONS_PER_QUESTION = 12
JUDGMENTS_PER_QUESTION = 8
OVERVIEW_LIMIT = 12


def _question_counts(question, observations, judgments):
    related = [row for row in observations if question['id'] in (row.get('question_ids') or [])]
    related_judgments = [row for row in judgments if row.get('question_id') == question['id']]
    return {'observations': len(related),
            'judgments': len(related_judgments),
            'opposition': len({ref for row in related_judgments
                               for ref in (row.get('opposing_observation_ids') or [])}),
            'unresolved_judgments': len([row for row in related_judgments
                                         if row.get('status') in ('proposed', 'unresolved')]),
            'gaps': len([gap for row in related_judgments for gap in (row.get('limitations') or [])])}


def _question_brief(question, observations, judgments):
    counts = _question_counts(question, observations, judgments)
    text, truncated = _preview(question.get('text') or '', marker='question_preview')
    brief = {'question_id': question['id'], 'status': question.get('status'), 'counts': counts,
             'resource_ids': question.get('resource_ids') or [],
             'read': 'read_investigation(question_id=%s)' % question['id']}
    if truncated:
        brief['text_preview'] = text
        brief['detail_required'] = True
    else:
        brief['text'] = text
    if question.get('parent_question_id'):
        brief['parent_question_id'] = question['parent_question_id']
    return brief


def _observation_row(observation):
    row = {'observation_id': observation['id'], 'question_ids': observation.get('question_ids') or [],
           'field': observation.get('field'), 'field_value': observation.get('field_value'),
           'producer': observation.get('producer'), 'kind': observation.get('kind'),
           'source_ref': observation.get('source_ref'), 'verification': observation.get('verification'),
           'limitations': observation.get('limitations') or []}
    # Committed page bodies remain exact in storage, but are not recursively
    # re-injected in every later stage. Values essential to an interpretation
    # should also be registered as small field observations, retained below.
    value_chars = len(json.dumps(row['field_value'], ensure_ascii=False))
    if observation.get('kind') == 'range' and value_chars > 512:
        row.pop('field_value')
        row['value_detail'] = {'chars': value_chars, 'value_not_injected': True,
            'read': 'read_investigation(kind="observation", record_id="%s", field="field_value", char_offset=0)' % observation['id']}
    scope = observation.get('scope') or {}
    if scope:
        row['scope'] = {'resource_refs': scope.get('resource_refs') or [],
                        'requested_window': scope.get('requested_window'),
                        'returned_window': scope.get('returned_window')}
    return row


def _judgment_row(judgment):
    row = {'judgment_id': judgment['id'], 'question_id': judgment.get('question_id'),
           'type': judgment.get('type'), 'content': judgment.get('content'), 'status': judgment.get('status'),
           'supporting_observation_ids': judgment.get('supporting_observation_ids') or [],
           'opposing_observation_ids': judgment.get('opposing_observation_ids') or [],
           'limitations': judgment.get('limitations') or [],
           'is_model_judgment': True}
    return row


def role_questions(binding, questions, material_usage, phase='request'):
    """The questions this role works on, and the number left for the overview."""
    if binding.role == 'worker':
        owned = {row['question_id'] for row in material_usage
                 if same_owner(binding, row, binding.role)
                 and row.get('state') in ('active', 'covered_by_commit', 'reopened')}
        if binding.task_id:
            owned |= {question['id'] for question in questions
                      if binding.task_id in (question.get('owner_task_ids') or [])}
        return [question for question in questions if question['id'] in owned]
    if binding.role == 'supervisor' and phase in ('review', 'report_prepare'):
        return list(questions)
    return list(questions)


def select_investigation(binding, snapshot, phase):
    """Assemble the investigation section plus its manifest bookkeeping."""
    def current(kind):
        return [row for row in snapshot.get(kind) or [] if row.get('run_id') == binding.run_id]
    questions, observations, judgments = current('question'), current('observation'), current('judgment')
    questions.sort(key=lambda row: (row.get('created_at') or '', row['id']))
    material_usage, commits = current('material_usage'), current('stage_commit')

    selected_questions = role_questions(binding, questions, material_usage, phase)
    focus = set(getattr(binding, 'question_focus_ids', []) or [])
    if focus:
        selected_questions = [q for q in selected_questions if q['id'] in focus]
    selected_questions = selected_questions[:OVERVIEW_LIMIT]
    selected_ids = {question['id'] for question in selected_questions}
    briefs = [_question_brief(question, observations, judgments) for question in selected_questions]
    # Directions this role is not working on stay visible as an overview row.
    overview_rows = [_question_brief(question, observations, judgments) for question in questions
                     if question['id'] not in selected_ids]

    injected_observations = []
    for question in selected_questions[:OVERVIEW_LIMIT]:
        related = [row for row in observations if question['id'] in (row.get('question_ids') or [])]
        fields = [row for row in related if row.get('kind') != 'range']
        injected_observations.extend(fields[:OBSERVATIONS_PER_QUESTION])
        # Page bodies/metadata are reading history, not the small factual
        # observations needed for reasoning. Keep their count and exact read
        # entry below; only an authoritative opposition reference forces a
        # particular page back into the active input.
    injected_judgment_rows = []
    for question in selected_questions[:OVERVIEW_LIMIT]:
        related = [row for row in judgments if row.get('question_id') == question['id']]
        injected_judgment_rows.extend(related[:JUDGMENTS_PER_QUESTION])

    closure = must_handle(selected_questions,
                          [row for row in judgments if row.get('question_id') in selected_ids])
    present_judgments = {row['id'] for row in injected_judgment_rows}
    injected_judgment_rows.extend(row for row in judgments
                                 if row['id'] in closure['judgment_ids'] and row['id'] not in present_judgments)
    required_observations = [row for row in observations
                             if row['id'] in set(closure.get('observation_ids') or [])]
    known = {row['observation_id'] for row in (_observation_row(item) for item in injected_observations)}
    for row in required_observations:
        if row['id'] not in known:
            injected_observations.append(row)

    # "Unassigned" only means something once the incident has started registering
    # questions or observations; before that every stored record is trivially
    # unassigned and the note would be pure noise (PRD §19.5.3).
    unassigned = []
    if questions or observations:
        linked_sources = {ref for row in observations
                          for ref in [(row.get('source_ref') or {}).get('evidence_id')]}
        unassigned = [row['id'] for row in snapshot.get('evidence') or []
                      if row.get('run_id') == binding.run_id and row['id'] not in linked_sources
                      and not any(row['id'] in (question.get('origin_refs') or []) for question in questions)]
    # One fact may answer several questions. Its question_ids preserve those
    # links; repeating its full provenance per question wastes context.
    injected_observations = list({row['id']: row for row in injected_observations}.values())
    projected_observations = [_observation_row(row) for row in injected_observations]
    projected_judgments = [_judgment_row(row) for row in injected_judgment_rows]
    section = {'questions': briefs[:OVERVIEW_LIMIT],
               'observations': projected_observations,
               'judgments': projected_judgments,
               'reading': {'question': 'read_investigation(question_id=<id>)',
                           'observation': 'read_investigation(kind="observation", record_id=<id>)',
                           'judgment': 'read_investigation(kind="judgment", record_id=<id>)',
                           'checkpoint': 'read_investigation(kind="checkpoint", record_id=<id>)',
                           'report_packet': 'read_investigation(kind="report_packet", record_id=<id>)'}}
    if closure.get('observation_ids') or closure.get('judgment_ids') or closure.get('question_ids'):
        section['must_handle'] = {'observation_ids': closure.get('observation_ids') or [],
                                  'judgment_ids': closure.get('judgment_ids') or [],
                                  'question_ids': closure.get('question_ids') or [],
                                  'note': 'stored opposition, limits and unresolved questions the next stage must still handle'}
    if overview_rows or len(briefs) > OVERVIEW_LIMIT:
        section['overview'] = {'total_questions': len(questions),
                               'listed': [row['question_id'] for row in overview_rows[:OVERVIEW_LIMIT]],
                               'listed_count': min(len(overview_rows), OVERVIEW_LIMIT),
                               'remaining_count': max(0, len(overview_rows) - OVERVIEW_LIMIT),
                               'read': 'read_investigation(kind="question", limit=5, cursor=<next_cursor>)',
                               'status_counts': _status_counts(questions),
                               'note': 'a direction missing from this list is not resolved; open it by id'}
    if unassigned:
        section['unassigned'] = {'count': len(unassigned), 'listed': sorted(unassigned)[:10],
                                 'read': 'query_evidence(tool_name=<tool>)'}
    range_total = sum(row.get('kind') == 'range' and bool(selected_ids.intersection(row.get('question_ids') or []))
                      for row in observations)
    shown_ranges = sum(row.get('kind') == 'range' for row in injected_observations)
    if range_total:
        section['committed_pages'] = {'total': range_total, 'listed': shown_ranges,
                                     'not_expanded': max(0, range_total - shown_ranges),
                                     'read': 'read_investigation(kind="observation", question_id=<id>, cursor=<next_cursor>)'}
    active = materials_summary(material_usage, binding, commits)
    if active:
        unfinished = [row for row in active if row['state'] != 'covered_by_commit']
        section['active_materials'] = unfinished[:OVERVIEW_LIMIT]
        section['material_counts'] = {'total': len(active), 'covered_by_commit': len(active) - len(unfinished),
            'unfinished': len(unfinished), 'unlisted_unfinished': max(0, len(unfinished) - OVERVIEW_LIMIT),
            'read': 'read_investigation(kind="material_usage", cursor=<next_cursor>)'}
    manifest = {'injected_question_ids': [row['question_id'] for row in briefs[:OVERVIEW_LIMIT]],
                'injected_observation_ids': [row['observation_id'] for row in projected_observations],
                'injected_judgment_ids': [row['judgment_id'] for row in projected_judgments],
                'must_handle_ids': {'observations': closure.get('observation_ids') or [],
                                    'judgments': closure.get('judgment_ids') or [],
                                    'questions': closure.get('question_ids') or []},
                'overview_question_ids': [row['question_id'] for row in overview_rows[:OVERVIEW_LIMIT]],
                'unassigned_evidence_ids': sorted(unassigned)[:10],
                'unassigned_evidence_total': len(unassigned),
                'material_usage': active[:OVERVIEW_LIMIT],
                'stage_commit_ids': [row['id'] for row in commits],
                'omitted_reasons': []}
    if section.get('overview'):
        manifest['omitted_reasons'].append(
            {'reason': 'overview lists %d of %d questions; the rest stay readable by id'
                       % (section['overview']['listed_count'], len(questions))})
    return {'payload': section, 'manifest': manifest}


def _status_counts(questions):
    counts = {}
    for question in questions:
        counts[question.get('status') or 'unknown'] = counts.get(question.get('status') or 'unknown', 0) + 1
    return counts


def materials_summary(material_usage, binding, commits):
    """Active material rows for this owner, with the commit that carried them."""
    rows = []
    commit_by_source = {}
    for commit in commits:
        for disposition in commit.get('material_dispositions') or []:
            if disposition.get('source_id'):
                commit_by_source[disposition['source_id']] = commit['id']
    for row in material_usage:
        if not same_owner(binding, row, binding.role):
            continue
        entry = {'source_id': row.get('source_id'), 'question_id': row.get('question_id'),
                 'field': row.get('field'), 'state': row.get('state'),
                 'source_version': row.get('source_version')}
        if row.get('state') == 'covered_by_commit' and row.get('source_id') in commit_by_source:
            entry['covered_by_commit'] = commit_by_source[row['source_id']]
        rows.append(entry)
    return sorted(rows, key=lambda row: (row.get('source_id') or '', row.get('field') or ''))


def coverage_three_ranges(binding, snapshot, receipts=None, commits=None, payload=None, messages=()):
    """fetched / shown / model-claimed ranges stay separate for each source (§19.9)."""
    receipts = receipts or []
    commits = commits or []
    fetched = {}
    for receipt in receipts:
        if not same_owner(binding, {**receipt, 'owner': receipt.get('role')}, binding.role):
            continue
        fetched.setdefault(receipt.get('source_id'), []).append(
            {'view': receipt.get('view'), 'field': receipt.get('field'), 'range': receipt.get('range'),
             'truncated': receipt.get('truncated')})
    claimed = {}
    for commit in commits:
        if not same_owner(binding, commit, binding.role):
            continue
        for disposition in commit.get('material_dispositions') or []:
            if disposition.get('source_id'):
                claimed.setdefault(disposition['source_id'], []).append(
                    {'field': disposition.get('field'), 'disposition': disposition.get('disposition'),
                     'commit_id': commit['id']})
    shown = {}
    from .source_paths import source_values
    for row in (payload or {}).get('evidence') or []:
        for key, value in (row.get('observations') or {}).items():
            leaves, complete = source_values((row.get('source') or {}).get(key), value)
            shown.setdefault(row['evidence_id'], []).extend(
                {'field': path, 'value_shown': True, 'projection_complete': complete} for path, _ in leaves)
    from .history import message_receipt_id
    visible_receipts = {message_receipt_id(message) for message in messages}
    visible_receipts.update(d.get('receipt_id') for d in (payload or {}).get('verified_read_details') or [])
    for receipt in receipts:
        if receipt['id'] in visible_receipts:
            shown.setdefault(receipt['source_id'], []).append(
                {'field': receipt.get('field'), 'range': receipt.get('range'), 'range_unit': receipt.get('range_unit'),
                 'receipt_id': receipt['id'], 'truncated': receipt.get('truncated')})
    sources = sorted(set(fetched) | set(claimed) | set(shown))
    return [{'source_id': source, 'fetched': fetched.get(source, []), 'shown': shown.get(source, []),
             'model_claimed': claimed.get(source, [])} for source in sources if source]
