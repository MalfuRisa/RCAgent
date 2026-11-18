"""Real-model report recovery on a copied pre-terminal branch; original run stays failed."""
import argparse
import asyncio
import json
import shutil
import time
import sys
from pathlib import Path
from langchain_core.messages import SystemMessage

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def run(source, output):
    from config import settings
    from workspace.repository import Repository
    from workspace.runtime import Binding, bind
    from workspace.recovery import record_issue
    from agents.report_recovery import recover_report
    from graph import evidence_gate
    from local_testing.run_analysis import analyse_records
    source_records = json.loads((source / 'records.json').read_text(encoding='utf-8'))
    failed = next(row for row in reversed(source_records['model_call'])
                  if row.get('role') == 'supervisor' and (row.get('error') or {}).get('code') == 'model_timeout')
    context = next(row for row in source_records['context'] if row['id'] == failed['context_id'])
    output.mkdir(parents=True, exist_ok=False)
    shutil.copytree(source / 'workspace', output / 'workspace')
    repo = Repository(output / 'workspace')
    state = json.loads((source / 'final-state.json').read_text(encoding='utf-8'))
    def restore(db):
        for event in db.execute('SELECT body FROM events WHERE incident=? AND record_id=? AND seq<=? ORDER BY seq DESC',
                               (state['incident_id'], state['run_id'], context['workspace_revision'])):
            original = json.loads(event[0])
            if original.get('id') == state['run_id'] and original.get('revision'):
                if original.get('stop_reason'):
                    raise RuntimeError('Original boundary already stopped; cannot replay it')
                return repo.put(db, state['incident_id'], 'run', original,
                                action='copied_report_boundary_restored')
        raise RuntimeError('No recorded pre-terminal run state')
    await repo.transact(restore)
    before = await repo.transact(lambda db: {kind: {row['id'] for row in repo.list(db, state['incident_id'], kind)}
        for kind in ('context', 'model_call', 'model_attempt')})
    elapsed = json.loads((source / 'run-analysis.json').read_text())['total_elapsed_seconds']
    deadline = time.time() + max(1, settings.INVESTIGATION_TIMEOUT_SECONDS - elapsed)
    state.update(workspace_root=str(output / 'workspace'), deadline=deadline,
                 execution_error=None, execution_status='incomplete', stop_reason=None,
                 final_report={}, report_claims=[], report_items=[], tasks_to_execute_ids=[])
    binding = Binding(repo, state['incident_id'], state['run_id'], 'supervisor', deadline,
        model_steps=sum(row['role'] == 'supervisor' for row in source_records['model_call']),
        query_window=state['query_window'], last_context_id=context['id'])
    secret = settings.LLM_API_KEY
    def save(name, value):
        text = json.dumps(value, ensure_ascii=False, indent=2,
                          default=lambda item: item.model_dump() if hasattr(item, 'model_dump') else str(item))
        (output / name).write_text(text.replace(secret, '[REDACTED]') if secret else text, encoding='utf-8')
    save('branch.json', {'source': str(source), 'source_call': failed['id'], 'source_context': context['id'],
                        'semantics': 'Copied recorded pre-terminal run state; original terminal run unchanged',
                        'remaining_seconds': deadline - time.time(), 'real_model': True,
                        'supervisor_timeout_seconds': settings.SUPERVISOR_MODEL_TIMEOUT_SECONDS})
    settings.CONTEXT_LIFECYCLE_MODE = 'active'
    started = time.monotonic()
    failure = None
    with bind(binding):
        await record_issue(binding, 'model_timeout', 'bounded_active_report_finalization', detail=failed['error'])
        try:
            state.update(await recover_report(binding, settings.get_chat_model(),
                SystemMessage(content='Review the archived IAM/MySQL investigation. Preserve every observation, '
                    'counterevidence and unknown direction. Only exact source fields establish observations; '
                    'only the supported OOM rule can establish a mechanism. Finish via a verified report packet.')))
            state.update(execution_status='incomplete', stop_reason='completed_with_recovery_gaps')
            state.update(await evidence_gate(state))
        except Exception as exc:
            failure = type(exc).__name__ + ': ' + str(exc)
    kinds = ('run', 'resource', 'task', 'attempt', 'evidence', 'assessment', 'context', 'model_call', 'model_attempt',
             'read_receipt', 'question', 'observation', 'judgment', 'stage_commit', 'checkpoint', 'report_packet',
             'report_proposal', 'report', 'recovery_issue')
    records = {kind: await repo.all(binding.incident_id, kind) for kind in kinds}
    fresh = {**records, **{kind: [row for row in records[kind] if row['id'] not in ids] for kind, ids in before.items()}}
    analysis = analyse_records(fresh, state, output / 'records.json')
    analysis.update(real_model=True, data_source='copied exact historical MCP observations; NOT live backend',
        runner_error=failure, elapsed_seconds=time.monotonic() - started,
        original_run_changed=False, report_recovery=True,
        passed=bool(state.get('report_packet_id') and state.get('workflow_stage') == 'finished' and not failure))
    save('final-state.json', state)
    save('records.json', records)
    save('run-analysis.json', analysis)
    print(json.dumps({key: analysis.get(key) for key in ('passed', 'runner_error', 'elapsed_seconds', 'model_attempts')}))
    return analysis


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if not asyncio.run(run(args.source, args.output))['passed']:
        raise SystemExit(1)
