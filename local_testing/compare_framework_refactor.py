"""Offline, same-input comparison of existing Pod/network and CPU parent paths."""
import asyncio
import copy
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from config import settings
from workspace.repository import Repository
from local_testing.preflight_v5_cpu import save


def summary(state, rows, events, calls):
    resources = {r['id']: r['name'] for r in rows['resource']}
    evidence = {e['id']: e for e in rows['evidence']}
    report = state.get('final_report') or {}
    return {'execution_status':state.get('execution_status'), 'stop_reason':state.get('stop_reason'),
        'answer_status':report.get('answer_status'), 'fallback':report.get('fallback'),
        'report_errors':report.get('report_errors',[]),
        'tasks':sorted((t['target_resource'],t['investigation_goal'],t['execution_status'],t['answer_status']) for t in rows['task']),
        'evidence':sorted((e['tool_name'],json.dumps(e['arguments'],sort_keys=True),e['transport_status'],e['source_status'],
            sorted(resources.get(r,r) for r in e['resource_ids']),e.get('contract_version')) for e in rows['evidence']),
        'observations':sorted((o['field'],json.dumps(o['field_value'],sort_keys=True),
            evidence[o['source_ref']['evidence_id']]['tool_name'], tuple(o['limitations'])) for o in rows['observation']),
        'assessments':sorted((a['diagnosis'],a['answer_status'],tuple(a['gaps'])) for a in rows['assessment']),
        'counts':{k:len(v) for k,v in rows.items()},
        'events':dict(Counter(e['action'] for e in events)),
        'remote_calls':sorted((name,json.dumps(args,sort_keys=True)) for name,args in calls)}


async def run(output, cpu_archive):
    from test_workspace_graph import ParentGraphTests, ScriptedModel, FakeGateway, FIXTURE
    pod_input = copy.deepcopy(FIXTURE['input'])
    pod_input['query_window']={'start_time':'2026-10-28T06:00:00+00:00','end_time':'2026-10-28T06:15:00+00:00'}
    state,rows,events,gateway,_ = await ParentGraphTests().run_graph(
        model=ScriptedModel(),gateway=FakeGateway(),input_state=pod_input)
    selected = {k:rows[k] for k in ('resource','task','attempt','evidence','observation',
        'judgment','stage_commit','checkpoint','report_packet','report_proposal','report','model_call','model_attempt','read_receipt')}
    selected['assessment']=state['rca_analyses_list']
    result={'pod_network':summary(state,selected,events,gateway.calls)}
    from test_v5_cpu import ScriptModel
    from graph import build_parent_graph
    archived=json.loads((Path(cpu_archive)/'records.json').read_text(encoding='utf-8-sig'))
    target=copy.deepcopy(archived['resource'][0])
    source=next(e for e in archived['evidence'] if e['arguments']['page']==1)
    original=next(a for a in archived['artifact'] if a['id']==source['artifact_id'])
    raw=json.loads((Path(cpu_archive)/'workspace'/original['storage_reference']).read_text())
    calls=[]
    async def cpu_call(role,name,args):
        calls.append((name,args))
        return copy.deepcopy(raw)
    with tempfile.TemporaryDirectory() as tmp, patch.object(settings,'WORKSPACE_ROOT',Path(tmp)), \
            patch.object(settings,'CONTEXT_LIFECYCLE_MODE','active'), patch.object(settings,'MODEL_MIN_INTERVAL_SECONDS',0):
        client=SimpleNamespace(schemas={'insights_list_cpu_events':{'inputSchema':{}}},call=cpu_call)
        graph=build_parent_graph(profile='v5_cpu',client=client,target=target,identity_original={},
            discovery_source={'archive':str(cpu_archive)},agent_id=int(source['arguments']['agent_id']),model=ScriptModel())
        state=await graph.ainvoke({'target_cluster':'rd2','incident_text':'CPU runqlat on rd2',
            'query_window':{k:source['arguments'][k] for k in ('start_time','end_time')},
            'app_name':'cpu','app_summary':'cpu','prompts_config':{}})
        repo=Repository(tmp)
        rows={k:await repo.all(state['incident_id'],k) for k in selected}
        events=await repo.transact(lambda db:[dict(r) for r in db.execute('SELECT * FROM events')])
        result['cpu_archive']=summary(state,rows,events,calls)
    save(output,result)
    print('Saved deterministic semantic comparison: '+str(output))


if __name__=='__main__':
    asyncio.run(run(sys.argv[1],sys.argv[2]))
