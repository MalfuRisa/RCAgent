"""Complete the archived long Worker investigation with a visible-only model.

Re-executes original source reads in bounded pages, using a fresh workspace.
The old end-of-run snapshot and its rejected candidate are baseline evidence,
not a claim to reproduce call-time state. No network/model service is used.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(source, output):
    from config import settings
    from models.schemas import RCATask
    from workspace.runtime import initialize_incident, from_state, bind
    from workspace.repository import Repository, new_id, utcnow
    from workspace.artifacts import read_artifact_raw, write_artifact
    from workspace.queries import save_query
    from workspace.tasks import register_tasks, start
    from workspace import investigations
    from agents.rca_agent import rcaAgent
    from local_testing.long_investigation_model import LongInvestigator
    from local_testing.run_analysis import analyse_records, read_usage
    from tools import mcp_tools
    output.mkdir(parents=True, exist_ok=False)
    def save(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                             default=lambda v: v.model_dump() if hasattr(v, 'model_dump') else str(v)), encoding='utf-8')
    records = json.loads((source / 'records.json').read_text())
    refused = next(c for c in records['context'] if c['role'] == 'worker' and c.get('context_incomplete'))
    original = from_state({'workspace_root': str(source / 'workspace'), 'incident_id': refused['incident_id'],
                          'run_id': refused['run_id'], 'deadline': time.time() + 600}, 'worker')
    original.repository = Repository(source / 'workspace')
    target = next(r for r in records['task'] if r['id'] == refused['task_id'])
    identity = next(r for r in records['resource'] if r['id'] == target['resource_id'])
    receipts = [r for r in records['read_receipt'] if r.get('task_id') == target['id'] and r.get('role') == 'worker']
    model = LongInvestigator()
    with patch.object(settings, 'WORKSPACE_ROOT', output / 'workspace'), \
         patch.object(settings, 'CONTEXT_LIFECYCLE_MODE', 'active'), \
         patch.object(settings, 'MODEL_MIN_INTERVAL_SECONDS', 0), \
         patch.object(settings, 'get_chat_model', return_value=model), patch.object(mcp_tools, '_TOOLS', []):
        state = await initialize_incident({'target_cluster': identity['cluster'], 'target_namespace': identity['namespace'],
                                           'target_resource': identity['name']})
        b = from_state(state, 'worker')
        for resource in records['resource']:
            await b.repository.save(b.incident_id, 'resource', {**resource, 'run_id': b.run_id, 'verified_at': utcnow()})
        source_map = {}
        for evidence in records['evidence']:
            raw = await read_artifact_raw(original, evidence['artifact_id'])
            eid = new_id('evidence')
            await save_query(b, eid, evidence['tool_name'], evidence['arguments'], raw, 'recorded-replay', 0,
                             evidence['transport_status'], reuse_key=None)
            source_map[evidence['id']] = eid
        plan, copied_sources, artifact_copies = [], [], {}
        seen = set()
        for receipt in receipts:
            sid = receipt['source_id']
            if sid in seen or receipt['source_kind'] not in ('evidence', 'artifact'):
                continue
            seen.add(sid)
            marker, name = ('evidence_id', 'read_evidence') if receipt['source_kind'] == 'evidence' else ('artifact_id', 'read_artifact')
            if marker == 'artifact_id':
                raw = await read_artifact_raw(original, sid)
                from workspace.receipts import source_hash
                digest = source_hash(raw)
                stored = artifact_copies.get(digest)
                if stored is None:
                    stored = await write_artifact(b, raw)
                    await b.repository.save(b.incident_id, 'artifact', stored)
                    artifact_copies[digest] = stored
                source_map[sid] = stored['id']
            else:
                original_evidence = next(r for r in records['evidence'] if r['id'] == sid)
                raw = await read_artifact_raw(original, original_evidence['artifact_id'])
            if not any(r['args'].get(marker) == source_map[sid] for r in plan):
                plan.append({'name': name, 'source_tool': original_evidence['tool_name'] if marker == 'evidence_id' else 'archived_message',
                    'args': {marker: source_map[sid], 'view': 'raw', 'char_offset': 0, 'max_chars': 8000}})
            copied_sources.append({'original_source_id': sid, 'new_source_id': source_map[sid],
                                   'original_serialized_chars': len(json.dumps(raw, ensure_ascii=False))})
        task = (await register_tasks(b, [RCATask(priority=1, investigation_goal='Review every archived Worker source range; '
            'recheck recorded reset count, preserve DNS/database alternatives and source gaps; conclude only what evidence proves.',
            target_resource=identity['name'], resource_type='pod',
            acceptance_checks=[('%s:%s:%s' % (next(k for k in r['args'] if k.endswith('_id')),
                next(v for k, v in r['args'].items() if k.endswith('_id')), r['source_tool'])) for r in plan])], identity['name'], identity['namespace']))[0]
        question = await investigations.save_question(b, text='Do recorded resets explain request failures? '
            'DNS configuration and database causation remain unknown.', owner_task_ids=[task.task_id],
            resource_ids=[task.resource_id])
        task.question_ids = [question['id']]
        # Registration of the owner relation is already durable on the question.
        attempt = await start(b, task.task_id)
        b.task_id, b.attempt_id, b.resource_id = task.task_id, attempt['id'], task.resource_id
        input_state = {**state, 'rca_task': task, 'messages': [], 'rca_prompts_config': {}}
        started, error, result = time.monotonic(), None, None
        try:
            with bind(b):
                result = await rcaAgent(input_state)
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        review, gate, reviewer = None, None, None
        worker_attempt = await b.repository.read(b.incident_id, b.attempt_id, 'attempt')
        if not worker_attempt.get('stop_reason') and not error:
            from agents.supervisor_agent import supervisor_agent
            from graph import evidence_gate
            from local_testing.long_review_model import LongReviewer
            reviewer = LongReviewer()
            supervisor_binding = from_state(state, 'supervisor')
            with patch.object(settings, 'get_chat_model', return_value=reviewer), bind(supervisor_binding):
                review = await supervisor_agent({**state, **(result or {}), 'app_name': identity['name']})
            gate = await evidence_gate({**state, **(result or {}), **review})
        kinds = ('resource', 'task', 'attempt', 'evidence', 'assessment', 'context', 'read_receipt', 'model_call',
                 'model_attempt', 'question', 'observation', 'judgment', 'stage_commit', 'checkpoint', 'material_usage',
                 'report_packet', 'report_proposal', 'report', 'run')
        rows = {kind: await b.repository.all(b.incident_id, kind) for kind in kinds}
        events = await b.repository.transact(lambda db: [dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq')])
        save('records.json', rows)
        save('events.json', events)
        save('model-visible-inputs.json', model.calls)
        save('final-state.json', result)
        save('supervisor-state.json', review)
        save('gate-state.json', gate)
        save('supervisor-visible-inputs.json', reviewer.calls if reviewer else [])
        coverage = []
        from workspace.material_ranges import covers_spans
        for copied in copied_sources:
            observations = [o for o in rows['observation'] if copied['new_source_id'] in (
                o['source_ref'].get('evidence_id'), o['source_ref'].get('artifact_id'))]
            spans = [o['source_ref']['range'] for o in observations if o['source_ref'].get('range_unit') == 'chars'
                     and o['source_ref'].get('json_pointer') == '']
            coverage.append({**copied, 'committed_ranges': spans,
                             'all_characters_reviewed': covers_spans(spans, [0, copied['original_serialized_chars']])})
        save('source-range-coverage.json', coverage)
        analysis = analyse_records(rows, {}, output / 'records.json', events)
        analysis.update(snapshot='fresh investigation over sources from archived end-of-run workspace; not call-time reconstruction',
            source_candidate_id=refused['id'], source_historical_message_chars=refused['input_chars'],
            runner_error=error, elapsed_seconds=time.monotonic() - started, real_model=False, network_used=False,
            real_model_requests=0, offline_model_adapter_invocations=len(model.calls) + len(reviewer.calls if reviewer else []),
            model_timeout_seconds=settings.MODEL_TIMEOUT_SECONDS,
            investigation_timeout_seconds=settings.INVESTIGATION_TIMEOUT_SECONDS,
            all_original_worker_sources_reviewed=all(row['all_characters_reviewed'] for row in coverage),
            local_source_count=len(copied_sources), original_receipt_count=len(receipts),
            distinct_read_sources=len(plan), identical_artifact_aliases=len(copied_sources) - len(plan),
            exact_reread=any(r.get('field') == 'summary.rst_count' for r in rows['read_receipt']),
            final_task_status=rows['task'][-1]['execution_status'],
            worker_model_steps=b.model_steps, model_step_limit=settings.WORKER_MAX_MODEL_STEPS,
            deadline_unchanged=b.deadline == state['deadline'], mcp_calls=b.mcp_calls)
        analysis['coverage_semantics'] = ('Exact source ranges were present in actual inputs and accepted by stage verification; '
            'this does not measure model comprehension or prove an investigation conclusion. Identical artifact bodies '
            'are copied once with explicit aliases; evidence provenance is not merged.')
        analysis['worker_stage_commit_count'] = sum(r['owner'] == 'worker' for r in rows['stage_commit'])
        analysis['supervisor_stage_commit_count'] = sum(r['owner'] == 'supervisor' for r in rows['stage_commit'])
        analysis['worker_read_count'] = sum(r['role'] == 'worker' for r in rows['read_receipt'])
        analysis['supervisor_read_count'] = sum(r['role'] == 'supervisor' for r in rows['read_receipt'])
        analysis['all_original_source_ranges_shown_and_committed'] = analysis['all_original_worker_sources_reviewed']
        analysis['supervisor_and_gate_completed'] = bool(gate and gate.get('execution_status') == 'completed'
            and rows['report_packet'] and rows['report_proposal'] and not review.get('stop_reason'))
        analysis['read_usage'] = read_usage(rows, output / 'workspace')
        analysis['multistage_completion_verified'] = (analysis['worker_stage_commit_count'] >= 2 and
            analysis['all_original_worker_sources_reviewed'] and analysis['exact_reread'] and
            analysis['final_task_status'] == 'finished' and not rows['attempt'][-1].get('stop_reason') and not error)
        save('run-analysis.json', analysis)
        print(json.dumps({k: analysis[k] for k in ('multistage_completion_verified', 'final_task_status',
            'worker_model_steps', 'all_original_worker_sources_reviewed', 'supervisor_and_gate_completed', 'runner_error')}))
        return analysis


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.source, args.output))
    if not result['multistage_completion_verified'] or not result['supervisor_and_gate_completed']:
        raise SystemExit(1)
