"""Offline evidence accounting for a saved fresh CPU run; never calls services."""
import json
import sys
from pathlib import Path
from local_testing.preflight_v5_cpu import save


def analyse(folder):
    folder = Path(folder)
    records = json.loads((folder/'records.json').read_text())
    result = json.loads((folder/'run-analysis.json').read_text())
    events = json.loads((folder/'events.json').read_text())
    input_state = json.loads((folder/'input.json').read_text())
    environment = json.loads((folder/'environment.json').read_text())
    calls = json.loads((folder/'mcp-calls.json').read_text())
    role_durations = []
    for e in events:
        body = json.loads(e['body']) if isinstance(e.get('body'), str) else e.get('body', {})
        if e.get('action') == 'cpu_role_duration':
            role_durations.append(body)
    model_calls = records.get('model_call', [])
    calls_by_id = {row['id']:row for row in model_calls}
    attempts = records.get('model_attempt', [])
    tokens = [r['token_usage'] for r in attempts if isinstance(r.get('token_usage'),dict)]
    # The live runner exports the main record types. Receipts remain authoritative
    # in the original workspace; read them without rewriting the saved export.
    import sqlite3
    with sqlite3.connect('file:'+str(folder/'workspace/workspace.sqlite3')+'?mode=ro',uri=True) as db:
        receipts = [json.loads(row[0]) for row in db.execute("SELECT body FROM records WHERE kind='read_receipt'")]
    save(folder/'read-receipts-accounting.json',receipts)
    result.update(record_counts={k: len(v) for k,v in records.items()},
        scope={'target_cluster': input_state['target_cluster'], 'target_resource': input_state['target_resource'],
               'query_window': input_state['query_window']},
        actual_versions=environment['images'], protocol=environment['protocol_identity']['protocolVersion'],
        effective_budget=environment['effective'], role_durations=role_durations,
        model_timing=[{**{k:row.get(k) for k in ('id','status','queue_ms','request_ms','error_type',
            'queued_at','send_started_at','finished_at')},
            **{k:calls_by_id.get(row['logical_call_id'],{}).get(k) for k in
                ('role','task_id','preparation_ms','request_timeout_seconds')}} for row in attempts],
        read_receipt_count=len(receipts),
        reported_token_usage={'available_attempts':len(tokens), 'unavailable_attempts':len(attempts)-len(tokens),
            'known_input_tokens':sum(t.get('input_tokens',0) for t in tokens),
            'known_output_tokens':sum(t.get('output_tokens',0) for t in tokens),
            'known_total_tokens':sum(t.get('total_tokens',0) for t in tokens),
            'boundary':'Sums only provider-reported usage. Missing/timeout usage is unknown, not zero.'},
        request_chars_v2=[r.get('request_chars_v2') for r in records.get('context', [])],
        CPU_backend_calls=[{'arguments': c['arguments'], 'status': c.get('status'),
            'duration_ms': c.get('duration_ms')} for c in calls if c['tool']=='insights_list_cpu_events'],
        preflight_backend_calls_in_this_run=sum(c['role']=='preflight' for c in calls),
        nonempty_CPU_evidence_count=sum(bool(e.get('observations',{}).get('data',{}).get('list'))
            for e in records.get('evidence', []) if e['tool_name']=='insights_list_cpu_events'),
        recovery_issues=records.get('recovery_issue', []),
        first_response_ms=None, generation_ms=None,
        stage_note='Each model call/attempt is from this fresh run; separate identity preflight is not a run observation.')
    save(folder/'evidence-accounting.json', result)
    if (folder/'final-state.json').exists():
        state = json.loads((folder/'final-state.json').read_text())
        report = state.get('final_report') or {}
        if report:
            scope = f"调查对象：{input_state['target_cluster']} Node {input_state['target_resource']}（平台Node身份，非Pod/UID）。\n\n"
            scope += '固定绝对窗口：'+json.dumps(input_state['query_window'],ensure_ascii=False)+'。\n\n'
            scope += 'latency_ns/latency_threshold_ns 单位为纳秒，timestamp 为 Unix 纳秒；事件采样不能证明业务请求因果。\n\n'
            scope += '运行状态：'+str(state.get('execution_status'))+'；停止/收尾原因：'+str(state.get('stop_reason'))+'。\n\n'
            if result['recovery_issues']:
                scope += '执行恢复缺口（未被报告包成功掩盖）：\n\n'+json.dumps([
                    {k:r.get(k) for k in ('role','task_id','code','action','detail')}
                    for r in result['recovery_issues']],ensure_ascii=False,indent=2)+'\n\n'
            text = '# rd2 CPU调查报告\n\n'+scope+report['root_cause']+'\n\n## 原文字段核验的观测\n\n'+report['evidence_summary']
            text += '\n\n## 模型审查的局限与下一步\n\n以下是模型审查陈述，不增加新的已验证事实。'
            text += '关于阈值筛选位置、样本统计和时间聚集的解释未被当作额外正式观测；本次未验证平台的全部采样/过滤机制。\n\n'+report['investigation_summary']
            (folder/'report.md').write_text(text,encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('software_flow_completed','evidence_backed_report',
        'root_cause_confirmed','elapsed_seconds','record_counts')},ensure_ascii=False))


if __name__=='__main__':
    analyse(sys.argv[1])
