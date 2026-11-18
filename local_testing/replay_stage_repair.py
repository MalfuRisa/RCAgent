"""Original 48k failure: strict negative and explicitly adapted local Repair replay."""
import argparse
import asyncio
import copy
import json
import re
import shutil
import sqlite3
import time
import sys
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class SemanticPatchModel:
    """Only actual Repair messages influence the choice; no storage access."""
    def __init__(self):
        self.inputs = []
        self.outputs = []

    def with_structured_output(self, schema):
        async def answer(messages):
            from models.stage_repair import StageRepair
            assert schema == StageRepair
            payload = json.loads(messages[-1].content)
            self.inputs.append([m.model_dump(mode='json') for m in messages])
            changes = []
            for item in payload['judgments']:
                allowed = payload['allowed'][item['path']]
                words = set(re.findall(r'[a-z0-9_-]+', item['judgment']['content'].lower()))
                candidates = [r for r in payload['candidate_questions'] if r['id'] in allowed]
                # Rank by actual semantic overlap, with stable visible order.
                candidate = max(candidates, key=lambda r: len(words.intersection(
                    re.findall(r'[a-z0-9_-]+', r['text'].lower()))))
                changes.append({'path': item['path'], 'value': candidate['id']})
            result = {'proposal_id': payload['proposal_id'], 'proposal_version': payload['proposal_version'],
                      'status': 'patched', 'changes': changes, 'reason': None}
            self.outputs.append(result)
            return result
        return RunnableLambda(answer)


def adapt(raw):
    proposal, changes = copy.deepcopy(raw), []
    dispositions = proposal['material_dispositions']
    for index, row in enumerate(dispositions):
        assert set(row) == {'task_id', 'disposition', 'reason'}
        statement = 'Original draft task unresolved statement (not a task state update): ' + json.dumps(row, ensure_ascii=False)
        proposal['open_items'].append(statement)
        changes.append({'from': '/material_dispositions/%d' % index, 'original': row,
                        'to': '/open_items/%d' % (len(proposal['open_items']) - 1), 'value': statement})
    proposal['material_dispositions'] = []
    assert proposal.pop('observations') == []
    changes.append({'from': '/observations', 'original': [], 'to': '/observation_proposals', 'value': []})
    proposal['observation_proposals'] = []
    for index, row in enumerate(proposal['judgments']):
        assert row.pop('is_model_judgment') is True
        changes.append({'from': '/judgments/%d/is_model_judgment' % index, 'original': True,
                        'to': 'adaptation audit only; program assigns is_model_interpretation'})
        assert 'question_id' not in row
    return proposal, changes


async def run(source, output, runtime_budget=False):
    from config import settings
    from workspace.repository import Repository
    from workspace.runtime import Binding, bind, invoke_model
    from workspace.artifacts import read_artifact_raw
    from workspace.stage_tools import commit_investigation_stage, new_segment_after_commit
    from workspace.stage_messages import archive_stage_pairs
    from models.stage_proposals import StageProposal, field_errors
    from pydantic import ValidationError
    from agents.stage_requests import stage_request
    from context.request_budget import tool_schemas, measure
    from workspace.read_tools import READ_TOOLS
    from workspace.report_packets import prepare_report_packet
    from local_testing.run_analysis import analyse_records
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / 'workspace'
    workspace.mkdir()
    # SQLite backup is consistent and leaves the historical database read-only.
    original_db = sqlite3.connect('file:' + str(source / 'workspace/workspace.sqlite3') + '?mode=ro', uri=True)
    target_db = sqlite3.connect(workspace / 'workspace.sqlite3')
    original_db.backup(target_db)
    original_db.close()
    target_db.close()
    index_db = sqlite3.connect(workspace / 'workspace.sqlite3')
    wanted = set()
    for (body,) in index_db.execute("SELECT body FROM records WHERE kind IN ('evidence','read_receipt','stage_commit','checkpoint')"):
        row = json.loads(body)
        wanted.update(row[key] for key in ('artifact_id', 'result_artifact_id', 'proposal_artifact_id',
                                          'segment_archive_artifact_id') if row.get(key))
    wanted.update(['artifact_aea9900aab1247a3aa23c45ca2afd5ee', 'artifact_74ca50240b6a420c9f9dcc9ce8583741'])
    for (body,) in index_db.execute("SELECT body FROM records WHERE kind='artifact'"):
        row = json.loads(body)
        if row['id'] in wanted:
            destination = workspace / row['storage_reference']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / 'workspace' / row['storage_reference'], destination)
    index_db.close()
    def save(name, value):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2,
            default=lambda v: v.model_dump(mode='json') if hasattr(v, 'model_dump') else str(v)), encoding='utf-8')
    records = json.loads((source / 'records.json').read_text())
    refused = next(r for r in records['context'] if r['id'] == 'context_7e53b6857fdc4764a1bf3d804bdc82da')
    original = next(r for r in records['context'] if r['id'] == 'context_fbc7a654d8a441508bde64dbf1120957')
    run_record = next(r for r in records['run'] if r['id'] == original['run_id'])
    soft_limit = settings.CONTEXT_MAX_CHARS if runtime_budget else 24000
    ceiling = settings.CONTEXT_HARD_MAX_CHARS if runtime_budget else 48000
    b = Binding(Repository(workspace), original['incident_id'], original['run_id'], 'supervisor', time.time() + settings.INVESTIGATION_TIMEOUT_SECONDS,
                reads=4, model_steps=2, last_context_id=original['id'], local_model_limit=9,
                query_window=run_record.get('query_window') or {})
    # Replay starts at the recorded response boundary, before the historical
    # budget stop. Restore ONLY the copied run's then-current record from its
    # real event, rather than disabling the production persisted-stop guard.
    def restore_run(db):
        for row in db.execute('SELECT body FROM events WHERE incident=? AND record_id=? AND seq<=? ORDER BY seq DESC',
                              (b.incident_id, b.run_id, original['workspace_revision'])):
            body = json.loads(row[0])
            if body.get('id') == b.run_id and body.get('revision'):
                if body.get('stop_reason'):
                    raise RuntimeError('Recorded boundary was already stopped; replay cannot bypass it')
                return b.repository.put(db, b.incident_id, 'run', body, action='offline_recorded_boundary_restored')
        raise RuntimeError('Original run event missing')
    restored_run = await b.repository.transact(restore_run)
    save('recorded-run-boundary.json', restored_run)
    before = await b.repository.transact(lambda db: {k: b.repository.list(db, b.incident_id, k)
        for k in ('context', 'model_call', 'model_attempt', 'stage_commit', 'checkpoint', 'task', 'material_usage', 'read_receipt')})
    candidate = await read_artifact_raw(b, refused['candidate_messages_artifact_id'])
    failed_ai = next(r for r in candidate if any(c['name'] == 'commit_investigation_stage' for c in r.get('tool_calls') or []))
    raw_call = next(c for c in failed_ai['tool_calls'] if c['name'] == 'commit_investigation_stage')
    raw = raw_call['args']
    assembled = next(r['proposal']['proposal'] for r in reversed(json.loads((source / 'stage-rejections.json').read_text()))
                     if r.get('owner') == 'supervisor')
    save('original-tool-arguments.json', raw)
    save('old-assembled-proposal.json', assembled)
    derived, adaptations = adapt(raw)
    save('adapted-proposal.json', derived)
    save('adaptations.json', adaptations)
    try:
        StageProposal.model_validate(derived)
        errors = []
    except ValidationError as exc:
        errors = field_errors(exc)
    save('adapted-schema-errors.json', errors)
    assert {r['path'] for r in errors} == {'/judgments/0/question_id', '/judgments/1/question_id'}, errors
    model = SemanticPatchModel()
    from agents.stage_repair import submit_stage
    with patch.object(settings, 'CONTEXT_LIFECYCLE_MODE', 'active'), \
         patch.object(settings, 'CONTEXT_BUDGET_POLICY', 'soft'), \
         patch.object(settings, 'CONTEXT_MAX_CHARS', soft_limit), \
         patch.object(settings, 'CONTEXT_HARD_MAX_CHARS', ceiling), \
         patch.object(settings, 'REQUEST_BUDGET_BASIS', 'v2'), \
         patch.object(settings, 'MODEL_MIN_INTERVAL_SECONDS', 0), \
         patch.object(settings, 'get_chat_model', return_value=model), bind(b):
        negatives = []
        for label, draft in [('raw', raw), ('old_assembled', assembled)]:
            from dataclasses import replace
            # Independent negative-case role bindings start with the same
            # recorded counters; they do not spend the positive case's reads.
            with bind(replace(b)):
                result = await commit_investigation_stage.ainvoke(draft)
            negatives.append({'label': label, 'result': result})
        save('negative-results.json', negatives)
        assert not model.inputs
        assert not await b.repository.all(b.incident_id, 'stage_repair_slot')
        assert all(r['result']['reason'] == 'stage_repair_not_applicable' for r in negatives)
        accepted = await commit_investigation_stage.ainvoke(derived)
        save('accepted-receipt.json', accepted)
        save('repair-visible-inputs.json', model.inputs)
        save('repair-outputs.json', model.outputs)
        continued = None
        if accepted.get('status') == 'accepted':
            # Keep original read batches. Archive the old failed pair; the
            # currently adapted submission gets exactly one final ToolMessage.
            history = []
            for row in candidate[1:]:
                if row['type'] == 'system':
                    continue
                if row['type'] == 'human':
                    if 'checkpoint_required:' in str(row['content']):
                        continue
                    history.append(HumanMessage(content=row['content']))
                elif row['type'] == 'ai':
                    history.append(AIMessage(content=row['content'], tool_calls=row.get('tool_calls') or []))
                elif row['type'] == 'tool':
                    history.append(ToolMessage(content=row['content'], tool_call_id=row['tool_call_id'],
                        name='commit_investigation_stage' if row['tool_call_id'] == raw_call['id'] else 'read_task'))
            history = await archive_stage_pairs(b, history)
            history += [AIMessage(content='', tool_calls=[{**raw_call, 'args': derived, 'id': 'adapted-stage'}]),
                        ToolMessage(content=json.dumps(accepted), tool_call_id='adapted-stage', name='commit_investigation_stage')]
            rules = next(r['content'] for r in candidate[1:] if r['type'] == 'system')
            segment = await new_segment_after_commit(b, accepted['checkpoint_id'], SystemMessage(content=rules), messages=history)
            save('continued-segment.json', [m.model_dump(mode='json') for m in segment])
            class Continue:
                def bind_tools(self, tools, **kwargs):
                    async def answer(messages):
                        save('continued-visible-input.json', [m.model_dump(mode='json') for m in messages])
                        if runtime_budget:
                            opening = next(json.loads(m.content) for m in messages if m.type == 'human'
                                and str(m.content).startswith('{') and 'continued_after_handoff' in m.content)
                            return AIMessage(content='', tool_calls=[{'name': 'read_investigation',
                                'args': {'kind': 'checkpoint', 'record_id': opening['checkpoint_id']},
                                'id': 'runtime-read-checkpoint'}])
                        return AIMessage(content='Continue investigating original IAM/MySQL scope; no causal conclusion.')
                    return RunnableLambda(answer)
            try:
                response = await stage_request(b, Continue(), segment, READ_TOOLS + [commit_investigation_stage, prepare_report_packet],
                    [commit_investigation_stage, prepare_report_packet], phase='local_read')
                continued = {'sent': True}
                if runtime_budget:
                    from workspace.read_tools import CURRENT_TOOL_CALL
                    actions = {tool.name: tool for tool in READ_TOOLS}
                    for call in response.tool_calls:
                        token = CURRENT_TOOL_CALL.set(call['id'])
                        try:
                            receipt = await actions[call['name']].ainvoke(call['args'])
                        finally:
                            CURRENT_TOOL_CALL.reset(token)
                        save('continued-read-result.json', receipt)
                        continued['action_executed'] = bool(receipt.get('read_receipt_id'))
                    from local_testing.runtime_review_model import RuntimeReviewer
                    from agents.supervisor_agent import supervisor_agent
                    from graph import evidence_gate
                    state = {'incident_id': b.incident_id, 'run_id': b.run_id, 'workspace_root': str(workspace),
                        'deadline': b.deadline, 'query_window': b.query_window, 'query_policy': {},
                        'app_name': 'local-archived-runtime-recovery', 'symptoms': [], 'rca_analyses_list': [],
                        'supervisor_rounds': 0, 'final_report': {}, 'execution_error': None}
                    reviewer = RuntimeReviewer()
                    with patch.object(settings, 'get_chat_model', return_value=reviewer):
                        state.update(await supervisor_agent(state))
                        state.update(await evidence_gate(state))
                    save('final-state.json', state)
                    continued['final_report_present'] = bool(state.get('final_report'))
                    continued['workflow_finished'] = state.get('workflow_stage') == 'finished'
                    continued['report_packet_id'] = state.get('report_packet_id')
            except Exception as exc:
                continued = {'sent': False, 'error': str(exc)}
        kinds = ('stage_pending', 'stage_repair_slot', 'stage_commit', 'checkpoint', 'task', 'material_usage',
                 'observation', 'judgment', 'question', 'read_receipt', 'model_call', 'model_attempt', 'context', 'run')
        rows = {k: await b.repository.all(b.incident_id, k) for k in kinds}
        prior_ids = {r['id'] for r in before['context']}
        fresh_contexts = [r for r in rows['context'] if r['id'] not in prior_ids]
        def added(kind):
            prior = {r['id'] for r in before[kind]}
            return [r for r in rows[kind] if r['id'] not in prior]
        analysis = analyse_records({**rows, 'context': fresh_contexts,
            'model_call': [r for r in rows['model_call'] if r['id'] not in {x['id'] for x in before['model_call']}],
            'model_attempt': [r for r in rows['model_attempt'] if r['id'] not in {x['id'] for x in before['model_attempt']}]}, {}, output/'records.json')
        analysis.update(real_model=False, real_model_requests=0, original_context_id=original['id'],
            original_refused_context_id=refused['id'], snapshot='recorded response boundary over copied end-of-run workspace',
            raw_negative=negatives, repair_calls=len(model.inputs), adapted_result=accepted, continued=continued,
            original_proposal_chars=len(json.dumps(raw, ensure_ascii=False)), adapted_proposal_chars=len(json.dumps(derived, ensure_ascii=False)),
            task_states_unchanged=rows['task'] == before['task'], material_usage_unchanged=rows['material_usage'] == before['material_usage'],
            reads_after=b.reads, model_steps_after=b.model_steps, timeout_seconds=settings.MODEL_TIMEOUT_SECONDS,
            total_timeout_seconds=settings.INVESTIGATION_TIMEOUT_SECONDS, soft_limit=soft_limit, hard_limit=ceiling,
            budget_profile='runtime' if runtime_budget else 'pressure_48k',
            repair_v2=[r['request_chars_v2'] for r in fresh_contexts if r['phase'] == 'stage_repair'])
        analysis['new_record_counts'] = {kind: len(added(kind)) for kind in
            ('stage_commit', 'checkpoint', 'model_call', 'model_attempt', 'read_receipt')}
        analysis['historical_record_counts'] = {kind: len(before[kind]) for kind in
            ('stage_commit', 'checkpoint', 'model_call', 'model_attempt', 'read_receipt')}
        analysis['fresh_contexts'] = [{k: row.get(k) for k in ('id', 'phase', 'request_chars_v2', 'disposition',
            'part_chars', 'representation_savings', 'exact_representation', 'exact_task_read_duplicates')}
            for row in fresh_contexts]
        analysis['fresh_sent_v2'] = [row['request_chars_v2'] for row in fresh_contexts if row.get('model_call_reference')]
        pending_rows = rows['stage_pending']
        if pending_rows and pending_rows[-1].get('derived_artifact_id'):
            merged = (await read_artifact_raw(b, pending_rows[-1]['derived_artifact_id']))['proposal']
            def differences(left, right, path=''):
                if isinstance(left, dict) and isinstance(right, dict):
                    return [p for key in sorted(left.keys() | right.keys()) for p in
                        (differences(left[key], right[key], path+'/'+key) if key in left and key in right else [path+'/'+key])]
                if isinstance(left, list) and isinstance(right, list) and len(left) == len(right):
                    return [p for index, (a, z) in enumerate(zip(left, right)) for p in differences(a, z, path+'/'+str(index))]
                return [] if left == right else [path]
            analysis['repair_changed_paths'] = differences(derived, merged)
            save('merged-proposal.json', merged)
            assert analysis['repair_changed_paths'] == ['/judgments/0/question_id', '/judgments/1/question_id']
        if accepted.get('status') == 'accepted':
            commit = next(r for r in rows['stage_commit'] if r['id'] == accepted['commit_id'])
            analysis['included_opposition_ids'] = commit.get('included_opposition_ids')
            analysis['new_commit_material_dispositions'] = commit['material_dispositions']
            analysis['active_receipts_retired'] = next(r['retired_receipt_ids'] for r in rows['checkpoint']
                                                      if r['id'] == accepted['checkpoint_id'])
        analysis['passed'] = (accepted.get('status') == 'accepted' and bool(continued and continued.get('sent'))
            and len(model.inputs) == 1 and analysis['task_states_unchanged'] and analysis['material_usage_unchanged']
            and max(analysis['repair_v2'], default=99999) <= 12000)
        if runtime_budget:
            analysis['passed'] = analysis['passed'] and all(continued.get(key) for key in
                ('action_executed', 'final_report_present', 'workflow_finished', 'report_packet_id'))
        save('records.json', rows)
        save('events.json', await b.repository.transact(lambda db: [dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq')]))
        save('run-analysis.json', analysis)
        print(json.dumps({k: analysis[k] for k in ('passed', 'repair_calls', 'repair_v2', 'continued', 'adapted_result')}))
        return analysis


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--runtime-budget', action='store_true', help='Use configured runtime budget and continue through report gate')
    args = parser.parse_args()
    if not asyncio.run(run(args.source, args.output, args.runtime_budget))['passed']:
        raise SystemExit(1)
