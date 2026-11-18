"""One real-model parent run against an explicitly local MCP endpoint.

Local fixture/service results are labeled as such, never intranet acceptance.
No deployment, source alteration, model change or repeat-on-429 is performed.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from time import perf_counter
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(args):
    route = urlparse(args.gateway_url)
    if route.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('this runner accepts only explicitly local MCP endpoints')
    args.output.mkdir(parents=True, exist_ok=False)
    workspace = args.output / 'workspace'
    os.environ.update(MCP_GATEWAY_URL=args.gateway_url, RCAGENT_MCP_MODE='kylin_gateway',
        RCAGENT_WORKSPACE_ROOT=str(workspace), RCAGENT_CONTEXT_LIFECYCLE_MODE='active',
        RCAGENT_CONTEXT_BUDGET_ENABLED='true', RCAGENT_REQUEST_BUDGET_BASIS='v2',
        RCAGENT_MODEL_MAX_IN_FLIGHT='1', RCAGENT_MODEL_MIN_INTERVAL_SECONDS='2',
        RCAGENT_MODEL_MAX_RETRIES='2', RCAGENT_RATE_LIMIT_POLICY='stop_run')
    from config import settings
    from tools import observability_adapter as gateway
    from tools.mcp_tools import get_mcp_tools, _mcp_client
    from workspace.repository import Repository
    from local_testing.run_analysis import analyse_records, read_usage
    from graph import build_parent_graph
    secrets = [settings.LLM_API_KEY, os.environ.get('MCP_GATEWAY_AUTH_TOKEN', '')]
    def save(name, value):
        raw = json.dumps(value, ensure_ascii=False, indent=2,
                         default=lambda item: item.model_dump() if hasattr(item, 'model_dump') else str(item))
        for secret in secrets:
            if secret:
                raw = raw.replace(secret, '[REDACTED]')
        (args.output / name).write_text(raw, encoding='utf-8')
    save('environment.json', {'route': args.gateway_url, 'data_source_label': args.data_source_label,
        'model': settings.LLM_MODEL, 'model_timeout_seconds': settings.MODEL_TIMEOUT_SECONDS,
        'planner_timeout_seconds': settings.PLANNER_MODEL_TIMEOUT_SECONDS,
        'supervisor_timeout_seconds': settings.SUPERVISOR_MODEL_TIMEOUT_SECONDS,
        'total_timeout_seconds': settings.INVESTIGATION_TIMEOUT_SECONDS, 'lifecycle_mode': settings.CONTEXT_LIFECYCLE_MODE,
        'context_limit': settings.CONTEXT_MAX_CHARS, 'rate_limit_policy': settings.RATE_LIMIT_POLICY,
        'budget_policy': settings.CONTEXT_BUDGET_POLICY, 'hard_context_limit': settings.CONTEXT_HARD_MAX_CHARS,
        'remote_changed': False, 'intranet_acceptance': False, 'preflight_only': args.preflight_only})
    state = {'target_cluster': args.cluster, 'target_namespace': args.namespace, 'target_resource': args.pod,
             'app_name': 'local-acceptance', 'app_summary': args.incident,
             'incident_text': args.incident, 'rca_tasks': [], 'rca_analyses_list': [], 'symptoms': [],
             'final_report': {}, 'query_window': {'start_time': args.start, 'end_time': args.end}}
    save('input.json', state)
    tools = await get_mcp_tools(_mcp_client)
    result = await gateway.call_gateway_tool('resolve_pod', {
        'cluster': args.cluster, 'namespace': args.namespace, 'pod': args.pod})
    save('preflight.json', {'published_tools': [tool.name for tool in tools], 'resolve_pod': result})
    if args.preflight_only:
        return
    # Do not consume a model request when local inventory cannot verify a target.
    from workspace.resources import candidates as inventory_candidates
    decoded = gateway._decode(result)
    rows = inventory_candidates(decoded)
    valid = [row for row in rows if (row.get('uid') or row.get('pod_uid'))
             and (row.get('name') or row.get('pod')) == args.pod]
    if (decoded.get('error_type') or decoded.get('status') in ('error', 'not_found', 'partial')
            or decoded.get('ambiguous') or len(valid) != 1):
        save('run-analysis.json', {'status': 'blocked_local_environment', 'model_attempts': 0})
        return
    graph = build_parent_graph().with_config({'recursion_limit': 100})
    # Stream final states too, so a local exception still leaves the run identity.
    latest, failure, timeline = dict(state), None, []
    started = perf_counter()
    try:
        async with asyncio.timeout(settings.INVESTIGATION_TIMEOUT_SECONDS + 10):
            async for current in graph.astream(state, stream_mode='values'):
                timeline.append({'elapsed_seconds': perf_counter() - started,
                    'changed_fields': [key for key in current if current[key] != latest.get(key)],
                    'execution_status': current.get('execution_status'), 'stop_reason': current.get('stop_reason'),
                    'task_count': len(current.get('rca_tasks') or []),
                    'worker_reports': len(current.get('rca_analyses_list') or [])})
                latest.update(current)
                save('node-timeline.json', timeline)
    except Exception as exc:
        failure = type(exc).__name__ + ': ' + str(exc)
    save('final-state.json', latest)
    repo = Repository(settings.WORKSPACE_ROOT)
    if not latest.get('incident_id'):
        save('run-analysis.json', {'status': 'failed_before_workspace', 'error': failure, 'model_attempts': 0})
        return
    kinds = ('resource', 'task', 'attempt', 'evidence', 'assessment', 'context', 'read_receipt', 'model_call',
             'model_attempt', 'report_proposal', 'run', 'question', 'observation', 'judgment', 'stage_commit',
             'checkpoint', 'material_usage', 'report_packet', 'report', 'recovery_issue')
    records = {kind: await repo.all(latest['incident_id'], kind) for kind in kinds}
    events = await repo.transact(lambda db: [dict(row) for row in db.execute(
        'SELECT * FROM events WHERE incident=? ORDER BY seq', (latest['incident_id'],))])
    save('records.json', records)
    save('events.json', events)
    analysis = analyse_records(records, latest, args.output / 'records.json', events)
    analysis.update(data_source_label=args.data_source_label, intranet_acceptance=False,
                    total_elapsed_seconds=perf_counter() - started, runner_error=failure,
                    stage_commit_count=len(records['stage_commit']), report_packet_count=len(records['report_packet']))
    analysis['read_usage'] = read_usage(records, workspace, latest)
    save('run-analysis.json', analysis)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gateway-url', required=True)
    parser.add_argument('--data-source-label', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cluster', required=True)
    parser.add_argument('--namespace', required=True)
    parser.add_argument('--pod', required=True)
    parser.add_argument('--incident', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end', required=True)
    parser.add_argument('--preflight-only', action='store_true')
    asyncio.run(run(parser.parse_args()))
