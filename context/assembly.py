"""Role- and phase-aware assembly of the model-facing context view.

The assembler reads one snapshot, selects records per role/phase (PRD §7),
projects them with the fixed field views (`views.py`, `tool_views.py`) and
records what was omitted. It never writes workbench state and never decides
scheduling: `coverage` is presentation bookkeeping only (PRD §5.1, §7.3).
"""
from .investigations import coverage_three_ranges, select_investigation
from .views import (CONTEXT_VIEW_VERSION, assessment_view, directory_row, evidence_view, resource_view,
                    task_view, window_table)

#: How many evidence briefs stay in the default input, per role.
BRIEF_LIMITS = {'triage': 12, 'planner': 12, 'worker': 16, 'supervisor': 10}
#: Directory rows kept before the catalog degrades to counts plus entries.
DIRECTORY_LIMIT = 40
#: At the deepest level the catalog itself is capped; coverage keeps the totals
#: and the omission audit names every record that left the list (PRD §10.2).
DIRECTORY_LIMIT_LEVEL2 = 12


def select(binding, snapshot):
    """Select tasks, resources, assessments and evidence for one role/phase.

    Selection follows the role tables in PRD §7.2 and the closure rules in
    §7.3: referenced support/opposition evidence is always included, failures
    and gaps on the focused targets are not filtered out, and nothing hidden
    behind another role's task is silently dropped from coverage.
    """
    resources = {r['id']: r for r in snapshot['resource']}
    tasks = snapshot['task']
    evidence = snapshot['evidence']
    assessment_by_attempt = {a.get('attempt_id'): a for a in snapshot['assessment'] if a.get('attempt_id')}
    latest_by_task = {}
    for task in tasks:
        assessment = assessment_by_attempt.get(task.get('latest_attempt_id'))
        if assessment:
            latest_by_task[task['id']] = assessment
    history_count = {}
    for assessment in snapshot['assessment']:
        history_count[assessment.get('task_id')] = history_count.get(assessment.get('task_id'), 0) + 1

    focus = set(binding.focus_resource_ids or ([binding.resource_id] if binding.resource_id else []))
    own_task = next((t for t in tasks if t['id'] == binding.task_id), None) if binding.task_id else None
    if own_task:
        focus.update(own_task.get('target_resource_ids') or [])
    if binding.role == 'supervisor':
        # The Supervisor overview covers every task's targets of this incident.
        focus.update(rid for task in tasks for rid in task.get('target_resource_ids') or [])
    selected_tasks = _select_tasks(binding, tasks, focus, own_task)
    if not focus:
        focus = _run_target_focus(binding, snapshot)
    related = [e for e in evidence if focus.intersection(e.get('resource_ids', []))]
    # An inventory/backends response may legitimately connect the queried Service
    # to its backend Pods; that link is evidence, not name matching.
    focus.update(rid for e in related if e.get('tool_name') == 'get_service_backends'
                 for rid in e.get('resource_ids', []))

    assessments = _select_assessments(binding, selected_tasks, latest_by_task, assessment_by_attempt)
    referenced = {ref for a in assessments for ref in
                  (a.get('supporting_evidence_ids') or []) + (a.get('opposing_evidence_ids') or []) +
                  [rid for h in a.get('hypotheses') or [] for rid in
                   (h.get('supporting_evidence_ids') or []) + (h.get('opposing_evidence_ids') or [])]}
    produced = {e['id'] for e in evidence if binding.attempt_id and e.get('attempt_id') == binding.attempt_id}
    if focus:
        selected_evidence = [e for e in evidence
                             if focus.intersection(e.get('resource_ids', [])) or e['id'] in referenced or e['id'] in produced]
    else:
        # No verified target yet: this run's own observations stay visible; the
        # whole-database snapshot is never used as a fallback (§7.3.7).
        selected_evidence = [e for e in evidence if e.get('run_id') == binding.run_id]
    # The Supervisor overview must be able to find the incident's own observations
    # (including identity candidates outside the task targets); those extra records
    # stay directory rows, never full briefs (§7.2, §7.3.6). It is scoped to this
    # run and the task targets: another run's unrelated leftover is counted in
    # coverage instead of being pulled in as a snapshot fallback (§7.3.5, §7.3.7).
    directory_extras = []
    if binding.role == 'supervisor':
        listed = {row['id'] for row in selected_evidence}
        directory_extras = [e for e in evidence if e['id'] not in listed
                            and (e.get('run_id') == binding.run_id
                                 or focus.intersection(e.get('resource_ids') or []))]
        focus.update(rid for e in directory_extras for rid in e.get('resource_ids', []))
    focus.update(rid for e in selected_evidence for rid in e.get('resource_ids', []))
    return {'resources': [resources[rid] for rid in resources if rid in focus],
            'questions': [q for q in snapshot.get('question') or [] if q.get('run_id') == binding.run_id
                          and (binding.role != 'worker' or focus.intersection(q.get('resource_ids') or [])
                               or binding.task_id in (q.get('owner_task_ids') or []))],
            'tasks': selected_tasks, 'evidence': selected_evidence, 'directory_extras': directory_extras,
            'assessments': assessments,
            'focus': focus, 'referenced': referenced, 'history_count': history_count}


def _select_tasks(binding, tasks, focus, own_task):
    if binding.role == 'worker':
        selected = list([own_task] if own_task else [])
        dependency_ids = {dep for task in selected for dep in task.get('depends_on', [])}
        selected += [t for t in tasks if t['id'] in dependency_ids and t['id'] != binding.task_id]
        return selected
    if binding.role == 'supervisor':
        # Global progress table over the current run's incident (§7.2).
        return list(tasks)
    return [t for t in tasks if focus.intersection(t.get('target_resource_ids', [])) or t['id'] == binding.task_id]


def _select_assessments(binding, tasks, latest_by_task, assessment_by_attempt):
    if binding.role == 'worker' and binding.task_id:
        own = assessment_by_attempt.get(binding.attempt_id)
        if own:
            return [own]
        previous = latest_by_task.get(binding.task_id)
        return [previous] if previous else []
    return [latest_by_task[t['id']] for t in tasks if t['id'] in latest_by_task]


def _run_target_focus(binding, snapshot):
    """Targets observed by this run's discovery steps, or none (§7.3.7)."""
    if binding.role not in ('triage', 'planner'):
        return set()
    return {rid for e in snapshot['evidence'] if e.get('run_id') == binding.run_id and not e.get('producer_task_id')
            for rid in e.get('resource_ids', [])}


def build_payload(binding, snapshot, phase, *, level=0, verified_details=None, catalog_limit=None,
                  investigation=None):
    """Assemble the context-view payload for one model call.

    ``level`` walks the reduction ladder of PRD §10.2 by changing only what is
    *shown*: 0 keeps briefs, 1 externalizes briefs that no submitted judgment
    references, 2 keeps directory rows plus counts. The archive is untouched.
    ``verified_details`` carries what this role's own reading phase returned
    (PRD §17.7); they are protected and are never part of the ladder.
    """
    if phase == 'packet_submission':
        from .report_packets import packet_view
        return packet_view(binding, snapshot)
    selected = select(binding, snapshot)
    evidence = selected['evidence']
    windows, refs = window_table(evidence, {row['id']: row.get('time_range') for row in evidence})
    payload = {'context_view_version': CONTEXT_VIEW_VERSION, 'incident_id': binding.incident_id,
               'run_id': binding.run_id, 'role': binding.role, 'phase': phase,
               'workspace_revision': snapshot['workspace_revision'], 'scope': snapshot['scope'],
               'query_window': snapshot.get('query_window') or binding.query_window,
               'target_focus': {'resource_ids': sorted(selected['focus'])} if selected['focus'] else {
                   'status': 'undetermined',
                   'reason': ('discovery evidence from this run is listed below'
                              if evidence else 'no target verified yet; discover the target first')},
               'resources': [resource_view(r) for r in sorted(selected['resources'], key=lambda r: r['id'])],
               'windows': windows}
    assessment_ids = {a.get('attempt_id'): a['id'] for a in selected['assessments']}
    payload['tasks'] = [task_view(t, role=binding.role, main_task_id=binding.task_id,
                                  assessment_ids_by_attempt=assessment_ids)
                        for t in sorted(selected['tasks'], key=lambda t: (t.get('priority', 0), t['id']))]
    payload['assessments'] = [assessment_view(a) for a in selected['assessments']]
    if payload['assessments']:
        payload['assessment_label'] = 'Model judgments, not inventory facts; inspect support/opposition and gaps.'
    if binding.role == 'worker':
        payload['budget'] = {'mcp_calls_used': binding.mcp_calls, 'mcp_calls_budget': snapshot.get('mcp_budget')}
        payload['reusable_queries'] = reusable_queries(selected['evidence'], binding, level,
                                                      cap_override=0 if catalog_limit == 0 else None)
    briefs, directory = evidence_sections(binding, selected, refs, level)
    directory.extend(directory_row(e, refs) for e in selected.get('directory_extras', []))
    if level >= 2:
        directory = directory[:DIRECTORY_LIMIT_LEVEL2 if catalog_limit is None else catalog_limit]
    payload['evidence'] = briefs
    if directory:
        payload['evidence_directory'] = directory
    section = (investigation or select_investigation(binding, snapshot, phase))['payload']
    if any(section.get(key) for key in ('questions', 'observations', 'judgments', 'must_handle',
                                       'overview', 'unassigned', 'active_materials')):
        payload['investigation'] = section
    if verified_details:
        payload['verified_read_details'] = verified_details
        payload['verified_read_details_note'] = ('Content this role already read this run; each entry names its '
                                                'receipt, source revision, scope, window and coverage.')
    payload['coverage'] = coverage(snapshot, selected, briefs, directory)
    payload['reading_notes'] = ('Assessments are model judgments, not inventory facts. Detail is readable via '
                                'read_evidence / read_task / read_assessment; unknown stays unknown and 0 is not missing.')
    recovered = [row for row in snapshot.get('recovery_issue', []) if row.get('run_id') == binding.run_id]
    if recovered:
        payload['operational_issues'] = [{key: row.get(key) for key in
            ('id', 'role', 'task_id', 'code', 'action', 'detail', 'proposal_artifact_id')} for row in recovered]
        payload['operational_issues_note'] = ('Planning or optional handoff degraded; these are execution gaps, '
                                             'not accepted facts or proof of a cause. Preserve them in the report.')
    return payload


def evidence_sections(binding, selected, refs, level):
    """Briefs for the most relevant evidence, directory rows for the rest.

    Referenced (support/opposition) evidence and the current attempt's own
    results are briefed first so a submitted judgment can always be audited
    against what the model was shown (PRD §6.5, §7.3.2).
    """
    referenced = selected['referenced']
    ordered = sorted(selected['evidence'],
                     key=lambda e: (e['id'] not in referenced, e.get('finished_at') or '', e['id']))
    from config import settings
    if settings.CONTEXT_BUDGET_POLICY == 'soft' and binding.role in ('triage', 'planner'):
        # These roles currently use structured output, not a local-read tool
        # loop. A directory-only fallback would promise reads they cannot do.
        return [evidence_view(e, refs) for e in ordered], []
    if level >= 2:
        mandatory = set()
        if settings.CONTEXT_LIFECYCLE_MODE == 'active':
            mandatory.update(selected['referenced'])
            # A registered direction's source is a necessary fact. Replacing all
            # briefs with directory IDs would lose that fact under the v2 meter.
            for question in selected.get('questions') or []:
                mandatory.update(question.get('origin_refs') or [])
        briefs = [evidence_view(e, refs) for e in ordered if e['id'] in mandatory]
        directory = [directory_row(e, refs) for e in ordered if e['id'] not in mandatory]
        return briefs, directory[:DIRECTORY_LIMIT_LEVEL2]
    limit = BRIEF_LIMITS.get(binding.role, 10)
    if level == 1:
        limit = max(limit // 2, 4)
    briefs = [evidence_view(e, refs) for e in ordered[:limit]]
    return briefs, [directory_row(e, refs) for e in ordered[limit:limit + DIRECTORY_LIMIT]]


#: Reuse entries per projection level: full list, then a short list, then a count.
REUSE_LIMITS = (10, 4, 2)


def reusable_queries(evidence, binding, level=0, *, cap_override=None):
    """Same-run successful queries the Worker may reuse instead of re-querying.

    The list is itself a catalog, so the reduction ladder caps it and keeps a
    total plus the workbench's query entry (PRD §10.2).
    """
    rows = []
    for e in evidence:
        if e.get('run_id') != binding.run_id or e.get('transport_status') != 'ok':
            continue
        rows.append({'evidence_id': e['id'], 'tool_name': e.get('tool_name'), 'reuse_key': e.get('reuse_key')})
    cap = REUSE_LIMITS[min(level, len(REUSE_LIMITS) - 1)]
    if cap_override is not None:
        cap = min(cap, cap_override)
    capped = rows[:cap]
    if len(rows) > len(capped):
        capped.append({'reusable_queries_total': len(rows),
                       'detail': 'query_evidence lists every stored query for this incident'})
    return capped


def coverage(snapshot, selected, briefs, directory):
    """Counts and entries for what the default input does not show (§7.3.6)."""
    totals = {kind: len(snapshot[kind]) for kind in ('resource', 'task', 'evidence', 'assessment')}
    shown = {'resource': len(selected['resources']), 'task': len(selected['tasks']),
             'evidence': len(briefs) + len(directory), 'assessment': len(selected['assessments'])}
    retries = {task_id: count for task_id, count in selected['history_count'].items() if count > 1}
    result = {'totals': totals, 'shown': shown,
              'omitted': {kind: totals[kind] - shown[kind] for kind in totals if totals[kind] > shown[kind]},
              'evidence_with_detail_required': sum(1 for row in briefs if row.get('detail_required')),
              'filters': {'query_evidence': ['resource_id', 'task_id', 'tool_name', 'source_status',
                                             'start_time+end_time', 'cursor', 'max_chars'],
                          'read_evidence': ['field', 'view=metadata|raw'], 'read_task': ['task_id'],
                          'read_assessment': ['assessment_id']},
              'detail_entry': 'local read tools (query_evidence / read_evidence / read_task / read_assessment)',
              'note': 'a page without a match does not mean the whole workbench has none'}
    if retries:
        result['retry_history_by_task'] = retries
    return result
