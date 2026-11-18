"""Read-only K8s Gateway parent runs; credentials stay in process memory."""
import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def kube(kind, name):
    result = subprocess.run(['kubectl', '--request-timeout=8s', '-n', 'kylin-insights', 'get', kind, name, '-o', 'json'],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('Kubernetes read failed: ' + kind + '/' + name)
    return json.loads(result.stdout)


async def run(output, preflight_only, core_only=False, supplied_key=None, model_probe_only=False, only_case=None, resume_report_from=None, acceptance_baseline=False, budget='on', scenario_file=None):
    from dotenv import load_dotenv
    load_dotenv(REPO / '.env')
    configured_key = os.environ.get('RCAGENT_LLM_API_KEY', '').strip() or os.environ.get('OPENAI_API_KEY', '').strip()
    key_matches_configuration = supplied_key == configured_key if supplied_key else None
    if supplied_key:
        os.environ['RCAGENT_LLM_API_KEY'] = supplied_key
    if acceptance_baseline:
        os.environ.update(RCAGENT_MODEL_TIMEOUT_SECONDS='60', RCAGENT_INVESTIGATION_TIMEOUT_SECONDS='600',
                          RCAGENT_MODEL_MAX_IN_FLIGHT='1', RCAGENT_MODEL_MIN_INTERVAL_SECONDS='2',
                          RCAGENT_MODEL_MAX_RETRIES='2', RCAGENT_SUPERVISOR_MAX_CORRECTIONS='1')
    secret = kube('secret', 'insights-mcp-gateway')
    token = base64.b64decode(secret['data']['GATEWAY_AUTH_TOKEN']).decode()
    del secret
    # The compact input is the thing under test: keep the character budget on by
    # default and record the choice; `--budget off` reproduces the old behaviour.
    os.environ.update(RCAGENT_MCP_MODE='kylin_gateway', MCP_GATEWAY_URL='http://10.44.56.107:30890/mcp',
                      MCP_GATEWAY_AUTH_TOKEN=token,
                      RCAGENT_CONTEXT_BUDGET_ENABLED='true' if budget == 'on' else 'false')
    from config import settings
    from dotenv import dotenv_values
    dotenv_key = (dotenv_values(REPO / '.env').get('RCAGENT_LLM_API_KEY') or '').strip()
    from tools import mcp_tools, observability_adapter
    from graph import build_parent_graph
    from workspace.repository import Repository
    from langchain_core.callbacks import BaseCallbackHandler
    from models.reducers import merge_rca_analyses
    secrets = [token, settings.LLM_API_KEY]
    def safe(value):
        text = json.dumps(value, ensure_ascii=False, indent=2, default=lambda v: v.model_dump() if hasattr(v, 'model_dump') else str(v))
        for credential in secrets:
            if credential:
                text = text.replace(credential, '[REDACTED]')
        return text
    def save(path, value):
        path.write_text(safe(value), encoding='utf-8')
    class NodeTimes(BaseCallbackHandler):
        def __init__(self):
            self.started, self.finished = {}, []
        def on_chain_start(self, serialized, inputs, *, run_id, **kwargs):
            name = kwargs.get('name') or (serialized or {}).get('name')
            if name in ('initialize_workspace', 'triage_agent', 'planner_agent', 'schedule_rca_tasks', 'rca_agent', 'supervisor_agent', 'evidence_gate'):
                self.started[run_id] = (name, time.monotonic(), datetime.now(timezone.utc).isoformat())
        def on_chain_end(self, outputs, *, run_id, **kwargs):
            if run_id in self.started:
                name, started, at = self.started.pop(run_id)
                self.finished.append({'node': name, 'started_at': at, 'finished_at': datetime.now(timezone.utc).isoformat(), 'duration_ms': (time.monotonic() - started) * 1000})
    output.mkdir(parents=True, exist_ok=True)
    deployment = kube('deployment', 'insights-mcp-gateway')
    config = kube('configmap', 'insights-mcp-gateway')['data']
    topology = {'observed_at': datetime.now(timezone.utc).isoformat(), 'route': os.environ['MCP_GATEWAY_URL'],
        'gateway_images': [c['image'] for c in deployment['spec']['template']['spec']['containers']],
        'routes': {k: config.get(k) for k in ('QUERIES_GRPC_ADDR', 'SERVER_GRPC_ADDR')},
        'model': settings.LLM_MODEL, 'structured_output_method': settings.LLM_STRUCTURED_OUTPUT_METHOD,
        'base_url': settings.LLM_BASE_URL, 'credential_present': bool(settings.LLM_API_KEY),
        'supplied_key_matches_configuration': key_matches_configuration,
        'effective_key_matches_project_dotenv': settings.LLM_API_KEY == dotenv_key if dotenv_key else None,
        'budget_enabled': settings.CONTEXT_BUDGET_ENABLED,
        'model_timeout_seconds': settings.MODEL_TIMEOUT_SECONDS,
        'investigation_timeout_seconds': settings.INVESTIGATION_TIMEOUT_SECONDS,
        'model_limits': {'in_flight': settings.MODEL_MAX_IN_FLIGHT, 'interval': settings.MODEL_MIN_INTERVAL_SECONDS, 'retries': settings.MODEL_MAX_RETRIES}}
    topology['supervisor_max_corrections'] = settings.SUPERVISOR_MAX_CORRECTIONS
    topology['acceptance_baseline'] = acceptance_baseline
    topology['budget_choice'] = budget
    topology['context_view_mode'] = settings.CONTEXT_VIEW_MODE
    topology['history_recent_batches'] = settings.CONTEXT_HISTORY_RECENT_BATCHES
    topology['read_max_chars'] = settings.CONTEXT_READ_MAX_CHARS
    for name in ('server-querier-mcp-grpc', 'server-internal-api', 'insights-mcp-gateway'):
        endpoints = kube('endpoints', name)
        topology[name] = [{'addresses': [a['ip'] for a in s.get('addresses', [])], 'ports': s.get('ports')} for s in endpoints.get('subsets', [])]
    save(output / 'environment.json', topology)
    if model_probe_only:
        from workspace.runtime import invoke_model
        from workspace.model_requests import ModelRequestError
        from langchain_core.messages import HumanMessage
        started = time.monotonic()
        try:
            result = await invoke_model(settings.get_chat_model(), [HumanMessage(content='Reply with OK.')])
            probe_result = {'status': 'completed', 'model': settings.LLM_MODEL,
                'usage': getattr(result, 'usage_metadata', None), 'key_matches_configuration': key_matches_configuration}
        except ModelRequestError as exc:
            probe_result = {'status': 'failed', 'model': settings.LLM_MODEL, 'error': exc.detail,
                'key_matches_configuration': key_matches_configuration}
        probe_result['elapsed_seconds'] = time.monotonic() - started
        save(output / 'model-probe.json', probe_result)
        print(safe(probe_result), flush=True)
        return
    if resume_report_from:
        import shutil
        from workspace.runtime import initialize_incident
        from graph import supervisor_node, evidence_gate
        folder = output / 'report-recovery'
        folder.mkdir(exist_ok=True)
        shutil.copytree(resume_report_from / 'workspace', folder / 'workspace', dirs_exist_ok=True)
        settings.WORKSPACE_ROOT = folder / 'workspace'
        state = json.loads((resume_report_from / 'final-state.json').read_text(encoding='utf-8'))
        repo = Repository(settings.WORKSPACE_ROOT)
        before = {kind: {r['id'] for r in await repo.all(state['incident_id'], kind)} for kind in ('evidence', 'context', 'model_attempt')}
        state.update(await initialize_incident(state))
        state.update(final_report={}, report_claims=[], report_items=[], stop_reason=None,
                     tasks_to_execute_ids=[], tasks_to_be_executed=[], workflow_stage='supervisor')
        save(folder / 'input.json', state)
        started = time.monotonic()
        timing = NodeTimes()
        state.update(await supervisor_node(state, {'callbacks': [timing]}))
        state.update(await evidence_gate(state))
        save(folder / 'final-state.json', state)
        save(folder / 'node-durations.json', timing.finished)
        records = {kind: await repo.all(state['incident_id'], kind) for kind in
            ('run', 'resource', 'task', 'attempt', 'evidence', 'context', 'model_call', 'model_attempt',
              'report_proposal', 'report', 'read_receipt')}
        save(folder / 'records.json', records)
        events = await repo.transact(lambda db: [dict(r) for r in db.execute('SELECT * FROM events WHERE incident=?', (state['incident_id'],))])
        save(folder / 'events.json', events)
        attempts = [r for r in records['model_attempt'] if r['id'] not in before['model_attempt']]
        contexts = [r for r in records['context'] if r['id'] not in before['context']]
        row = {'case': 'report-recovery', 'continued_incident': state['incident_id'], 'run_id': state['run_id'],
            'execution_status': state.get('execution_status'), 'answer_status': state.get('final_report', {}).get('answer_status'),
            'stop_reason': state.get('stop_reason'), 'execution_error': state.get('execution_error'),
            'remote_queries': sum(r['id'] not in before['evidence'] for r in records['evidence']),
            'model_attempts': len(attempts), 'peak_input_chars': max((c['input_chars'] for c in contexts), default=0),
            'elapsed_seconds': time.monotonic() - started}
        from local_testing.run_analysis import analyse_records, read_usage
        analysis = analyse_records(records, state, folder / 'records.json', events)
        analysis['read_usage'] = read_usage(records, settings.WORKSPACE_ROOT, state)
        row.update({k: analysis[k] for k in ('sent_peak_input_chars', 'candidate_peak_input_chars',
                                            'peak_input_chars_semantics', 'task_failures', 'stages', 'token_usage')})
        save(folder / 'run-analysis.json', analysis)
        save(output / 'summary.json', [row])
        print(safe(row), flush=True)
        return
    tools = await mcp_tools.get_mcp_tools(mcp_tools._mcp_client)
    mcp_tools._TOOLS = tools
    observability_adapter._TOOLS_BY_NAME = {t.name: t for t in tools}
    save(output / 'tool-schemas.json', [{'name': t.name, 'schema': t.args_schema} for t in tools])
    if len(tools) != 13:
        raise RuntimeError('Expected 13 Gateway tools')
    probes = []
    async def probe(name, args):
        started = time.monotonic()
        raw = await observability_adapter.call_gateway_tool(name, args)
        probes.append({'tool': name, 'arguments': args, 'result': raw, 'duration_ms': (time.monotonic() - started) * 1000})
        save(output / 'inventory-probes.json', probes)
        return raw
    pods = await probe('search_k8s_targets', {'cluster': 'rd2', 'namespace': 'kylin-insights', 'query': 'iam-server', 'kind': 'pod', 'limit': 20})
    from local_testing.live_checks import iam_inventory_check, model_diagnostics
    blocking, iam_pod = iam_inventory_check(pods)
    if blocking and not core_only and only_case in (None, 'iam-pod', 'wrong-namespace'):
        blocking.update(case=only_case or 'preflight', preflight_queries=len(probes))
        save(output / 'summary.json', [blocking])
        print(safe(blocking), flush=True)
        return
    pod_names = sorted(p['name'] for p in pods.get('candidates', []) if p.get('kind') == 'pod' and p.get('namespace') == 'kylin-insights')
    services = {}
    for query in ('iam-server', 'frontend', 'redis'):
        raw = await probe('search_k8s_targets', {'cluster': 'rd2', 'namespace': 'kylin-insights', 'query': query, 'kind': 'service', 'limit': 20})
        services[query] = sorted(p['name'] for p in raw.get('candidates', []) if p.get('kind') == 'service' and p.get('namespace') == 'kylin-insights')
    print(safe({'preflight': 'complete', 'tool_count': len(tools), 'model': settings.LLM_MODEL,
                'credential_present': bool(settings.LLM_API_KEY), 'iam_pods': pod_names, 'services': services}), flush=True)
    if preflight_only:
        return
    if not core_only and not pod_names and only_case in (None, 'iam-pod', 'wrong-namespace'):
        save(output / 'summary.json', [{'case': only_case or 'preflight', 'execution_status': 'blocked_environment',
            'stop_reason': 'iam_pod_inventory_unavailable', 'model_attempts': 0,
            'detail': 'No current IAM Pod identity was returned; inspect inventory-probes.json. No substitute target was generated.'}])
        return
    if core_only:
        from workspace.runtime import initialize_incident, from_state, bind, record_execution
        from workspace.resources import register, ensure_identity
        raw = await probe('search_k8s_targets', {'cluster': 'rd2', 'namespace': 'kube-system', 'query': 'coredns', 'kind': 'pod', 'limit': 20})
        candidates = sorted(raw.get('candidates', []), key=lambda p: p.get('name', ''))
        if not candidates or raw.get('truncated'):
            raise RuntimeError('CoreDNS inventory missing/incomplete; explicit identity cannot be selected')
        # Select a named replica for contract verification, without declaring it faulty.
        name = candidates[0]['name']
        settings.WORKSPACE_ROOT = output / 'workspace'
        state = await initialize_incident({'target_cluster': 'rd2', 'target_namespace': 'kube-system', 'target_resource': name})
        binding = from_state(state, 'worker')
        with bind(binding):
            resource = await register(binding, 'pod', name, 'kube-system')
            identity = await ensure_identity(binding, resource['id'])
            if identity['status'] != 'resolved':
                raise RuntimeError('CoreDNS exact identity unresolved')
            binding.resource_id = resource['id']
            args = {'cluster': 'rd2', 'namespace': 'kube-system', 'pod': name}
            first = await observability_adapter.call_gateway_tool('diagnose_pod', args)
            second = await observability_adapter.call_gateway_tool('diagnose_pod', args)
        await record_execution(binding, 'completed')
        records = {kind: await binding.repository.all(binding.incident_id, kind) for kind in ('run', 'resource', 'evidence')}
        events = await binding.repository.transact(lambda db: [dict(r) for r in db.execute('SELECT * FROM events')])
        save(output / 'core-diagnose.json', {'target': name, 'identity': identity, 'results': [first, second],
            'records': records, 'events': events, 'actual_calls': binding.mcp_calls, 'reuse_calls': binding.reuse_calls})
        print(safe({'core_target': name, 'actual_calls': binding.mcp_calls, 'reuse_calls': binding.reuse_calls,
            'modes': [r.get('_rcagent_query', {}).get('mode') for r in (first, second)],
            'errors': [r.get('error_type') for r in (first, second)]}), flush=True)
        return
    cases = [('iam-pod', pod_names[0] if pod_names else None, 'pod', 'kylin-insights'),
        ('iam-service', 'iam-server', 'service', 'kylin-insights'),
        ('frontend-service', 'frontend', 'service', 'kylin-insights'),
        ('redis-service', services['redis'][0] if len(services['redis']) == 1 else 'redis', 'service', 'kylin-insights'),
        ('missing', 'rcagent-does-not-exist-20261013', 'service', 'kylin-insights'),
        ('wrong-namespace', pod_names[0] if pod_names else None, 'pod', 'rcagent-wrong-namespace')]
    summary = []
    scenario = json.loads(scenario_file.read_text(encoding='utf-8')) if scenario_file else {}
    for label, name, kind, namespace in cases:
        if only_case and label != only_case:
            continue
        if name is None:
            continue
        folder = output / label
        folder.mkdir(exist_ok=True)
        settings.WORKSPACE_ROOT = folder / 'workspace'
        state = {'app_name': label, 'app_summary': '按实际资料核查最近十五分钟现状；没有预设根因，保留观测、候选解释及日志配置缺口。',
                 'target_cluster': 'rd2', 'target_namespace': namespace, 'prompts_config': {}, 'final_report': {},
                 'symptoms': [], 'rca_tasks': [], 'rca_analyses_list': []}
        if kind == 'pod':
            state['target_resource'] = name
        else:
            state['incident_text'] = f'cluster: rd2 namespace: {namespace} service: {name} 最近15分钟有错误，请按实际证据核查。'
        if scenario:
            from workspace.windows import normalize_window
            window = normalize_window(scenario['query_window'])
            state.update(query_window=window, app_summary=scenario['app_summary'],
                         incident_text=f'cluster: rd2 namespace: {namespace} {kind}: {name} '
                                       f"start_time: {window['start_time']} end_time: {window['end_time']} "
                                       + scenario['app_summary'])
        save(folder / 'input.json', state)
        started = time.monotonic()
        updates, final = [], dict(state)
        timing = NodeTimes()
        print('START ' + label, flush=True)
        try:
            async for update in build_parent_graph().astream(state, {'recursion_limit': 100, 'callbacks': [timing]}, stream_mode='updates'):
                updates.append({'elapsed_seconds': time.monotonic() - started, 'update': update})
                for node, value in update.items():
                    if isinstance(value, dict):
                        if value.get('rca_analyses_list'):
                            value = {**value, 'rca_analyses_list': merge_rca_analyses(final.get('rca_analyses_list', []), value['rca_analyses_list'])}
                        final.update(value)
                save(folder / 'node-updates.json', updates)
            save(folder / 'final-state.json', final)
            save(folder / 'node-durations.json', timing.finished)
            repo = Repository(settings.WORKSPACE_ROOT)
            records = {kind: await repo.all(final['incident_id'], kind) for kind in
                ('run', 'resource', 'task', 'attempt', 'evidence', 'context', 'model_call', 'model_attempt',
              'report_proposal', 'report', 'read_receipt')}
            save(folder / 'records.json', records)
            save(folder / 'model-diagnostics.json', model_diagnostics(records))
            events = await repo.transact(lambda db: [dict(r) for r in db.execute('SELECT * FROM events WHERE incident=?', (final['incident_id'],))])
            save(folder / 'events.json', events)
            row = {'case': label, 'execution_status': final.get('execution_status'), 'answer_status': final.get('final_report', {}).get('answer_status'),
                'triage_status': final.get('triage_status'), 'evidence_status': final.get('evidence_status'), 'stop_reason': final.get('stop_reason'),
                'execution_error': final.get('execution_error'), 'remote_queries': len(records['evidence']),
                'reuse_count': sum(e['action'] == 'query_reused' for e in events),
                'skipped_count': sum(e['action'] == 'query_skipped' for e in events),
                'rejected_count': sum(e['action'] == 'query_rejected' for e in events),
                'model_attempts': len(records['model_attempt']), 'peak_input_chars': max((c['input_chars'] for c in records['context']), default=0),
                'elapsed_seconds': time.monotonic() - started}
            from local_testing.run_analysis import analyse_records, read_usage
            analysis = analyse_records(records, final, folder / 'records.json', events)
            analysis['read_usage'] = read_usage(records, settings.WORKSPACE_ROOT, final)
            row.update({k: analysis[k] for k in ('sent_peak_input_chars', 'candidate_peak_input_chars',
                                                'peak_input_chars_semantics', 'task_failures', 'stages', 'token_usage')})
            save(folder / 'run-analysis.json', analysis)
        except Exception as exc:
            row = {'case': label, 'execution_status': 'harness_failed', 'error': type(exc).__name__ + ': ' + str(exc), 'elapsed_seconds': time.monotonic() - started}
        summary.append(row)
        save(output / 'summary.json', summary)
        print(safe(row), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preflight-only', action='store_true')
    parser.add_argument('--core-only', action='store_true')
    parser.add_argument('--use-key-stdin', action='store_true', help='Read a one-off model credential from stdin; never save it')
    parser.add_argument('--use-key-env', action='store_true', help='Consume RCAGENT_ONETIME_API_KEY from the process environment')
    parser.add_argument('--model-probe-only', action='store_true')
    parser.add_argument('--case', choices=['iam-pod', 'iam-service', 'frontend-service', 'redis-service', 'missing', 'wrong-namespace'])
    parser.add_argument('--resume-report-from', type=Path, help='Continue only Supervisor/gate using a saved case workspace in a new run')
    parser.add_argument('--scenario-file', type=Path, help='Local known-incident window and description; no remote modification')
    parser.add_argument('--acceptance-baseline', action='store_true', help='Use PRD 9 process-only 60/600-second verification baseline')
    parser.add_argument('--budget', choices=['on', 'off'], default='on',
                        help='Character budget for the compact input (default on: this batch tests the compact input)')
    args = parser.parse_args()
    supplied_key = os.environ.pop('RCAGENT_ONETIME_API_KEY', None) if args.use_key_env else None
    if args.use_key_stdin:
        supplied_key = sys.stdin.readline().strip()
    asyncio.run(run(args.output, args.preflight_only, args.core_only, supplied_key, args.model_probe_only,
                    args.case, args.resume_report_from, args.acceptance_baseline, args.budget, args.scenario_file))
