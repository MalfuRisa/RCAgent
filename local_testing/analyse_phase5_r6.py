"""Summarize one completed Phase5 product run; no model or MCP requests."""
import argparse
import collections
import json
import re
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def union_seconds(intervals):
    merged=[]
    for start,end in sorted(intervals):
        if merged and start<=merged[-1][1]: merged[-1][1]=max(merged[-1][1],end)
        else: merged.append([start,end])
    return sum(end-start for start,end in merged)


def analyze(root):
    run=root/'live-01'
    result=load(run/'result.json')
    events=[json.loads(line) for line in (run/'events.jsonl').read_text().splitlines()]
    sources=result['sources']
    started={e['call_id']:e for e in events if e['type']=='tool_started'}
    ended={e['call_id']:e for e in events if e['type']=='tool_result'}
    model_intervals=[]; pending={}
    for e in events:
        role=e['role']
        if e['type']=='model_started': pending[role]=e['elapsed_seconds']
        elif e['type'] in ('model_response','execution_error') and role in pending:
            model_intervals.append((pending.pop(role),e['elapsed_seconds']))
    tool_intervals=[(e['elapsed_seconds'],ended[c]['elapsed_seconds']) for c,e in started.items() if c in ended]
    roles={role:{'model_started':sum(e['type']=='model_started' and e['role']==role for e in events),
        'complete_model_responses':sum(e['type']=='model_response' and e['role']==role and not e['payload'].get('partial') for e in events),
        'tool_outcomes':sum(s['role']==role for s in sources)} for role in ('planner','worker','review')}
    usage=result['usage_if_available']
    totals={k:sum(u.get(k,0) or 0 for u in usage) for k in ('input_tokens','output_tokens','total_tokens')}
    for group in ('input_token_details','output_token_details'):
        keys={key for u in usage for key in (u.get(group) or {})}
        totals[group]={key:sum((u.get(group) or {}).get(key,0) or 0 for u in usage) for key in keys}
    target=load(root/'task.json')['target_resource']
    candidates=[]; raw_codes=collections.Counter(); bodies=[]
    for source in sources:
        path=Path(source['raw_path']) if source.get('raw_path') else None
        if not path or not path.exists(): continue
        raw=load(path)
        structured=raw.get('structuredContent') or {}
        data=structured.get('data') or {}
        args=source.get('arguments') or {}
        tool=source['tool_name']
        event=ended.get(source['call_id'],{})
        elapsed=event.get('elapsed_seconds')
        if source['status']!='ok':
            code=(raw.get('error') or {}).get('code')
            for block in raw.get('content',[]):
                try: code=json.loads(block.get('text','')).get('code',code)
                except (ValueError,AttributeError): pass
            raw_codes[code or 'unclassified']+=1
        rows=data.get('list') if isinstance(data,dict) else None
        if isinstance(rows,list) and source.get('returned_rows') and tool in ('insights_query_list','insights_query_metric'):
            body=args.get('body') or {}
            target_in_request=target in json.dumps(args,ensure_ascii=False)
            target_in_rows=any(any(row.get(k)==target for k in ('pod','pod_0','pod_1')) for row in rows if isinstance(row,dict))
            business_fields=source.get('returned_fields') or []
            positive=any(any(isinstance(row.get(k),(int,float)) and row[k]>0 for k in ('byte','packet','request','response','req','resp'))
                for row in rows if isinstance(row,dict))
            candidates.append({'elapsed_seconds':elapsed,'source_id':source['source_id'],'tool':tool,
                'dataset':[body.get('database'),body.get('table')],'returned_rows':len(rows),
                'fields':business_fields,'target_in_request':target_in_request,'target_name_in_rows':target_in_rows,
                'positive_metric_in_rows':positive})
        if tool=='insights_describe_query':
            bodies.append({'elapsed_seconds':elapsed,'arguments':args,'status':source['status'],'source_id':source['source_id']})
    confirmed=[c for c in candidates if c['target_name_in_rows'] and c['positive_metric_in_rows']]
    first=confirmed[0] if confirmed else None
    cutoff=first['elapsed_seconds'] if first else None
    first_counts={'worker_model_responses':sum(e['role']=='worker' and e['type']=='model_response' and e['elapsed_seconds']<=cutoff for e in events),
        'tool_outcomes_all_roles':sum(e['type']=='tool_result' and e['elapsed_seconds']<=cutoff for e in events)} if cutoff is not None else None
    ids=set(re.findall(r'source_[a-z0-9]+',result['body']))
    unknown=sorted(ids-{s['source_id'] for s in sources})
    summary={'runtime':result['runtime'],'run_id':result['run_id'],'conversation_id':result['conversation_id'],
        'execution_status':result['execution_status'],'review_status':result['review_status'],'elapsed_seconds':result['elapsed_seconds'],
        'role_counts':roles,'tool_status_counts':dict(collections.Counter(s['status'] for s in sources)),
        'tool_name_counts':dict(collections.Counter(s['tool_name'] for s in sources)),'error_codes':dict(raw_codes),
        'mcp_valid_send_attempts':sum(e['payload']['tool'].startswith('insights_') for e in started.values()),
        'local_valid_call_attempts':sum(not e['payload']['tool'].startswith('insights_') for e in started.values()),
        'model_wait_receive_seconds':sum(b-a for a,b in model_intervals),'tool_interval_union_seconds':union_seconds(tool_intervals),
        'answer_times':{e['role']:e['elapsed_seconds'] for e in events if e['type']=='answer_saved'},
        'first_target_positive_business_return':first,'counts_before_first_target_business':first_counts,
        'business_return_candidates':candidates,'help_outcomes':bodies,'usage':totals,'cost':'unknown; cache/reasoning are included subsets',
        'execution_errors':[{'role':e['role'],'elapsed_seconds':e['elapsed_seconds'],**e['payload']} for e in events if e['type']=='execution_error'],
        'unknown_cited_source_ids':unknown,'archive_errors':load(run/'archive-status.json')['errors'],
        'gateway_cleanup':load(run/'gateway/cleanup.json')}
    (root/'run-analysis.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in summary.items() if k not in ('business_return_candidates','help_outcomes')},ensure_ascii=False,indent=2))
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('root',type=Path)
    analyze(parser.parse_args().root)
