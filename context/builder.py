"""Model input assembly: full archive stays, the visible copy is projected.

`prepare` dispatches on `RCAGENT_CONTEXT_VIEW_MODE`:

* ``compact`` (default) builds the per-role, per-phase field projection
  (`assembly.py`) and walks the reduction ladder of PRD §10.2;
* ``legacy`` keeps the previous assembly for replay comparison and rollback.

Both paths persist exactly the visible messages before the model call and save
a candidate input when the request is refused, so a failed call is explainable
instead of reconstructed (PRD §10.3).
"""
import json

from langchain_core.messages import SystemMessage
from config import settings
from workspace.repository import WorkspaceError, new_id
from workspace.artifacts import write_artifact, read_artifact_raw
from .selection import message_chars, judgment_view, group_identical_evidence
from .materials import select_materials, key_observations, summarized_message
from .assembly import build_payload
from .investigations import coverage_three_ranges, select_investigation
from . import history, parts
from . import budget_policy
from .snapshot import read_snapshot
from .publication import (WORKBENCH_PREFIX, workbench_message, visible_chars,
    component_chars, budget_basis, request_chars, base_manifest, publish, refuse_context)
from .read_details import verified_read_details, verify_receipts, transferred_pages, retention_audit

async def prepare(binding, messages, phase='request', request=None):
    """Assemble the visible input for one model call.

    ``request`` carries what the caller will actually send besides the messages
    (bound tool schemas and the structured-output schema), so the engineering
    budget can be measured on the canonical request object (PRD §19.8.1).
    """
    provider = getattr(binding, 'context_preparers', {}).get(phase)
    if provider is not None:
        return await provider(binding, messages, phase, request)
    if settings.CONTEXT_VIEW_MODE == 'legacy':
        if settings.CONTEXT_LIFECYCLE_MODE == 'active':
            raise WorkspaceError('context_unsatisfiable: active lifecycle requires the compact canonical request path')
        return await prepare_legacy(binding, messages, phase)
    return await prepare_compact(binding, messages, phase, request=request)


async def prepare_compact(binding, messages, phase, request=None):
    snapshot = await read_snapshot(binding)
    enabled = settings.CONTEXT_BUDGET_ENABLED or settings.CONTEXT_LIFECYCLE_MODE == 'active'
    ceiling = settings.CONTEXT_MAX_CHARS
    reserve_cost = 0
    limit = ceiling
    if settings.CONTEXT_LIFECYCLE_MODE == 'active' and phase in ('worker_step', 'local_read'):
        from .stage_budget import reserve
        reserved = reserve()
        names = [(row.get('function') or {}).get('name') for row in (request or {}).get('tools') or []]
        reserve_cost = sum(reserved.values()) - (reserved['commit_schema'] if 'commit_investigation_stage' in names else 0)
        limit -= reserve_cost
    manifest = base_manifest(binding, snapshot, phase)
    manifest['expansion_target_chars'] = limit
    manifest['bound_tool_names'] = [(row.get('function') or {}).get('name') for row in (request or {}).get('tools') or []]
    from .lifecycle import input_snapshot
    snapshot = await input_snapshot(binding, snapshot, phase, manifest)
    receipts = {row['id']: row for row in snapshot.get('read_receipt') or []}
    receipt_bodies = await verify_receipts(binding, messages, receipts, phase)
    manifest['read_reference_errors'] = [{'receipt_id': r['id'], 'source_id': r.get('source_id'),
                                          'error': r['storage_error']} for r in receipts.values() if r.get('storage_error')]
    revisions = {row['id']: row.get('revision') for row in snapshot['evidence']}
    messages = list(messages)
    original_messages = list(messages)
    # History first: earlier batches keep their pairing but their long results
    # become cards carrying the receipt that proves what they showed (§9, §17.6.3).
    batches, consumed = history.plan_batches(messages)
    recent = history.history_settings()
    recent_positions = {position for batch in batches[-recent:] for position in batch.reply_indices} if recent else set()
    messages, decisions = await history.externalize_earlier(binding, messages, batches, recent,
                                                            history.evidence_index(snapshot), receipts)
    if decisions:
        manifest['history_replacements'] = decisions
        manifest['history_recent_batches'] = recent
    # Typed parts: protected content is never replaced by a generic sentence (§17.8).
    kinds = parts.classify(messages, phase=phase, recent_positions=recent_positions)
    details = await verified_read_details(binding, snapshot, receipts, phase)
    investigation = select_investigation(binding, snapshot, phase)
    details.extend(transferred_pages(binding, original_messages, messages, receipts, receipt_bodies, details))
    source_messages = list(messages)
    steps = []
    level = 0
    catalog_limit = None
    while True:
        messages = list(source_messages)
        payload = build_payload(binding, snapshot, phase, level=level, verified_details=details,
                                catalog_limit=catalog_limit)
        if manifest.get('soft_target_expanded') and binding.role in ('worker', 'supervisor'):
            payload['budget_notice'] = {
                'soft_target_chars': settings.CONTEXT_MAX_CHARS, 'blocks_necessary_reads': False,
                'action': 'At the next natural checkpoint, commit a concise verified stage. Necessary reads remain allowed; '
                          'do not discard active facts, opposition or gaps just to meet the soft target.'}
        payload_text = json.dumps(payload, ensure_ascii=False)
        messages, _ = await parts.shrink_optional(binding, messages, kinds, manifest, payload_text, limit, enabled)
        payload.setdefault('verified_read_details', []).extend(transferred_pages(
            binding, source_messages, messages, receipts, receipt_bodies, details, payload_text))
        # Coverage is evaluated only after all transformations of this level.
        payload = history.deduplicate(payload, history.visible_coverage(binding, messages, receipts), revisions)
        from .exact_representation import enabled as sharing_enabled
        if sharing_enabled(binding, phase):
            from .duplicate_task_views import share_task_questions
            payload, task_duplicates = share_task_questions(binding, payload, messages, receipts, snapshot)
            manifest['exact_task_read_duplicates'] = task_duplicates
        from .exact_representation import serialize
        payload_text, shared_decisions = serialize(binding, payload, phase)
        manifest['exact_representation'] = shared_decisions
        manifest['representation_savings'] = {'original_workbench_chars': len(json.dumps(payload, ensure_ascii=False)),
                                             'visible_workbench_chars': len(payload_text)}
        visible = [SystemMessage(content=workbench_message(payload_text))] + list(messages)
        manifest['read_retention_checks'] = retention_audit(original_messages, messages, payload)
        step = 'projection level %d' % level
        if catalog_limit is not None:
            step += ' optional catalog limit %d' % catalog_limit
        basis, v1_chars, v2_chars, basis_name = budget_basis(payload_text, messages, request)
        from .request_budget import measure
        _, request_components = measure(visible, request)
        steps.append({'step': step, 'chars': message_chars(visible), 'request_chars_v1': v1_chars,
                      'request_chars_v2': v2_chars, 'budget_basis': basis, 'basis_name': basis_name,
                      'target_limit': limit, 'ceiling': ceiling})
        if not enabled or basis <= limit:
            break
        if level >= 2:
            # A directory is optional discovery material, not a protected fact.
            # Try two finite smaller catalogs before refusing protected content.
            # Counts, read entry points and per-record omission audit remain.
            if catalog_limit != 0:
                catalog_limit = 6 if catalog_limit is None else 0
                continue
            if budget_policy.can_expand() and not manifest.get('soft_target_expanded'):
                # Rebuild from the original messages and the richest view. The
                # small target failed; do not discard useful facts merely to
                # advertise compliance with it. This second ladder is finite.
                ceiling = budget_policy.hard_limit()
                limit = ceiling - reserve_cost
                manifest.update(soft_target_expanded=True, expansion_target_chars=limit)
                level, catalog_limit = 0, None
                continue
            if limit < ceiling:
                if not receipt_bodies and not details and not manifest.get('minimum_expansion_reserve'):
                    # An initial/fresh stage has no page to hand off yet. Try
                    # a smaller, still useful read allowance before requiring
                    # a checkpoint; optional catalogs have already gone.
                    limit = ceiling - 1536 - reserved['control_prompt'] - reserved['error_receipt']
                    manifest['minimum_expansion_reserve'] = True
                    continue
                # A fitting current request can ask for a handoff, but cannot
                # acquire more material. The dispatcher enforces this flag.
                limit = ceiling
                binding.checkpoint_required = True
                manifest['checkpoint_required'] = True
                # The caller must rebind the control-only tools before a send;
                # returning this normal-tool request would advertise reads the
                # dispatcher immediately refuses.
                return await refuse_context(binding, manifest, payload, messages, phase,
                    reason='expansion_reserve_exhausted: rebind checkpoint controls', visible=visible,
                    extra={'budget_steps': steps, 'request_chars_v2': v2_chars,
                           'message_chars_v1': v1_chars, 'component_chars': request_components})
            report = parts.protected_parts(messages, kinds)
            return await refuse_context(binding, manifest, payload, messages, phase,
                                        reason='protected_parts_exceed_budget: necessary protected content '
                                               'exceeds the character budget',
                                        visible=visible, protected=report,
                                        extra={'projection': {'level': level,
                                                              'evidence_briefs': len(payload.get('evidence', [])),
                                                              'evidence_directory': len(payload.get('evidence_directory', []))},
                                               'budget_steps': steps,
                                               'message_chars_v1': v1_chars, 'request_chars_v2': v2_chars,
                                               'request_basis': basis_name, 'component_chars': request_components,
                                               'part_chars': {'workbench': len(payload_text),
                                                              'messages': message_chars(messages)},
                                               'verified_read_detail_count': len(payload.get('verified_read_details') or []),
                                               'view_version': payload.get('context_view_version')})
        level += 1
        manifest['omitted_references_and_reasons'].append(
            {'reason': 'reduction level %d: briefs and catalogs capped; full detail remains readable' % level,
             'projection_level': level})
    from .exact_representation import serialize
    payload_text, _ = serialize(binding, payload, phase)
    # In soft mode disabling efficiency reductions never disables the separate
    # safety ceiling. Count the complete canonical request, including schemas.
    if settings.CONTEXT_BUDGET_POLICY == 'soft' and v2_chars > budget_policy.hard_limit():
        return await refuse_context(binding, manifest, payload, messages, phase,
            reason='engineering_ceiling_exceeded: complete request exceeds the independent character ceiling',
            visible=visible, extra={**budget_policy.audit(v2_chars), 'budget_steps': steps,
                                   'request_chars_v2': v2_chars, 'component_chars': request_components})
    manifest.update(view_version=payload['context_view_version'],
                    projection={'level': level, 'evidence_briefs': len(payload.get('evidence', [])),
                                'evidence_directory': len(payload.get('evidence_directory', []))},
                    injected_evidence_ids=visible_evidence_ids(payload),
                    verified_read_detail_count=len(payload.get('verified_read_details') or []),
                    coverage_three_ranges=coverage_three_ranges(binding, snapshot, list(receipts.values()),
                                                                snapshot.get('stage_commit') or [], payload, messages),
                    **investigation['manifest'],
                    budget_steps=steps,
                    omitted_references_and_reasons=manifest['omitted_references_and_reasons'] +
                    omission_rows(binding, snapshot, payload, level))
    manifest.update(input_chars=message_chars(visible), would_exceed_budget=message_chars(visible) > limit,
                    # Legacy names kept for existing readers; `budget` carries the new detail.
                    budget_enabled=enabled, nominal_limit=settings.CONTEXT_MAX_CHARS,
                    part_chars={'workbench': len(payload_text),
                                'messages': message_chars(messages)},
                    budget={'enabled': enabled, 'limit': limit, 'view_mode': 'compact',
                            'history_recent_batches': settings.CONTEXT_HISTORY_RECENT_BATCHES,
                            'read_max_chars': settings.CONTEXT_READ_MAX_CHARS},
                    message_chars_v1=(steps[-1].get('request_chars_v1') if steps else None),
                    request_chars_v2=(steps[-1].get('request_chars_v2') if steps else None),
                    request_basis=(steps[-1].get('basis_name') if steps else None),
                    request_parts_available={'tools': bool(request and request.get('tools')),
                                             'response_schema': bool(request and request.get('response_schema'))},
                    component_chars=request_components,
                    canonical_request_note='SDK/server hidden templates and provider tokens are not observable here',
                    assembly_duration_ms=None, **budget_policy.audit(v2_chars))
    return await publish(binding, manifest, visible)


def visible_evidence_ids(payload):
    ids = [row.get('evidence_id') for row in payload.get('evidence', [])]
    ids.extend(row.get('evidence_id') for row in payload.get('evidence_directory', []))
    return [value for value in ids if value]


def omission_rows(binding, snapshot, payload, level=0):
    """Audit entries explaining records the default view did not show (§7.3.6).

    The reason must state the true cause: a record left out by a reduction level
    is capped, not out of scope.
    """
    rows = []
    reason = ('outside current role/target context; available via local workbench reads'
              if level == 0 else
              'not listed at reduction level %d (catalog capped); readable via local workbench reads' % level)
    included = {row['evidence_id'] for row in payload.get('evidence', []) + payload.get('evidence_directory', [])}
    for evidence in snapshot['evidence']:
        if evidence['id'] not in included:
            rows.append({'id': evidence['id'], 'kind': 'evidence', 'reason': reason})
    # Packet views keep the authoritative record ID; regular task views expose
    # task_id. Both projections identify the same registered task for auditing.
    task_ids = {row.get('task_id') or row['id'] for row in payload.get('tasks', [])}
    for task in snapshot['task']:
        if task['id'] not in task_ids:
            rows.append({'id': task['id'], 'kind': 'task', 'reason': reason})
    return rows


async def prepare_legacy(binding, messages, phase='request'):
    repo, incident = binding.repository, binding.incident_id
    snapshot = await read_snapshot(binding)
    snapshot.pop('mcp_budget', None)
    snapshot.pop('query_window', None)
    for extra in ('read_receipt', 'question', 'observation', 'judgment', 'stage_commit',
                  'checkpoint', 'material_usage', 'report_packet', 'recovery_issue'):
        snapshot.pop(extra, None)
    snapshot, omitted = select_materials(snapshot, binding)
    original_evidence = snapshot.pop("evidence")
    evidence = group_identical_evidence(original_evidence)
    compact = [{k: e.get(k) for k in ("id", "equivalent_evidence_ids", "resource_ids", "resource_revisions", "resource_identities", "tool_name", "arguments", "transport_status",
               "source_status", "warnings", "data_gaps", "truncated", "freshness", "data_source", "time_range", "artifact_id")} for e in evidence]
    for e, row in zip(evidence, compact):
        row['key_observations'] = key_observations(e.get('observations', {}))
    snapshot["evidence"] = compact
    snapshot["assessment"] = [judgment_view(a) for a in snapshot["assessment"]]
    snapshot["assessment_label"] = "Model judgments, not inventory facts; inspect support/opposition and gaps."
    manifest = {"id": new_id("context"), "run_id": binding.run_id, "task_id": binding.task_id,
                "attempt_id": binding.attempt_id, "role": binding.role, "phase": phase, "mode": 'legacy',
                "workspace_revision": snapshot["workspace_revision"],
                "resource_revisions": {r["id"]: r["revision"] for r in snapshot["resource"]},
                "injected_evidence_ids": [e["id"] for e in original_evidence],
                "omitted_references_and_reasons": omitted, "read_events": []}
    for e in evidence:
        for reference in e["equivalent_evidence_ids"][1:]:
            manifest["omitted_references_and_reasons"].append({"id": reference, "represented_by": e["id"],
                "reason": "identical visible result/window/provenance grouped; original query and artifact preserved"})
    payload = json.dumps(snapshot, ensure_ascii=False)
    enabled = settings.CONTEXT_BUDGET_ENABLED
    manifest.update(budget_enabled=enabled, nominal_limit=settings.CONTEXT_MAX_CHARS)
    if enabled and visible_chars(payload, []) > settings.CONTEXT_MAX_CHARS:
        await repo.save(incident, "context", {**manifest, 'context_incomplete': True, 'disposition': 'rejected_before_send'}, action="context_incomplete")
        raise WorkspaceError("context_incomplete: necessary identities and limitations exceed character budget")
    messages = list(messages)
    for index in sorted(range(len(messages)), key=lambda i: len(str(messages[i].content)), reverse=True):
        if not enabled or visible_chars(payload, messages) <= settings.CONTEXT_MAX_CHARS:
            break
        message = messages[index]
        if message.type in ("system", "ai", "human") or len(str(message.content)) < 1000:
            continue  # human requirements and corrections are protected (PRD 17.8)
        artifact = await write_artifact(binding, {"content": message.content})
        await repo.save(incident, "artifact", artifact)
        summary = summarized_message(message.content)
        if summary is None and binding.role in ('triage', 'planner'):
            manifest['context_incomplete'] = True
            await repo.save(incident, 'context', manifest, action='context_incomplete')
            raise WorkspaceError('context_incomplete: cannot preserve required unstructured input')
        replacement = (summary or 'Key observations and limitations are in workbench.') + '\nOriginal content externalized; read artifact ' + artifact['id']
        messages[index] = message.model_copy(update={"content": replacement})
        manifest["omitted_references_and_reasons"].append({"artifact_id": artifact["id"], "reason": "large message externalized; message type and tool_call_id preserved"})
    if enabled and visible_chars(payload, messages) > settings.CONTEXT_MAX_CHARS:
        manifest["context_incomplete"] = True
        await repo.save(incident, "context", manifest, action="context_incomplete")
        raise WorkspaceError("context_incomplete: required messages exceed character budget")
    for e, row in zip(evidence, compact):
        if not enabled:
            break
        observation = e.get("observations", {})
        if observation == row['key_observations']:
            continue
        row['observations'] = observation
        candidate_payload = json.dumps(snapshot, ensure_ascii=False)
        if visible_chars(candidate_payload, messages) <= settings.CONTEXT_MAX_CHARS:
            payload = candidate_payload
        else:
            del row['observations']
            manifest["omitted_references_and_reasons"].append({"id": e["id"], "reason": "raw observation externalized; limitations retained"})
    visible = [SystemMessage(content=workbench_message(payload))] + list(messages)
    manifest.update(input_chars=message_chars(visible), would_exceed_budget=message_chars(visible) > settings.CONTEXT_MAX_CHARS)
    if enabled and message_chars(visible) > settings.CONTEXT_MAX_CHARS:
        manifest['context_incomplete'] = True
        await repo.save(incident, 'context', manifest, action='context_incomplete')
        raise WorkspaceError('context_incomplete: serialized visible input exceeds character budget')
    return await publish(binding, manifest, visible)
