"""Read-only archived analysis and candidate replay in temporary workspaces."""
import argparse
import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from config import settings
from context.builder import prepare
from context.selection import message_chars
from local_testing.run_analysis import analyse_records, read_usage
from workspace.artifacts import read_artifact_raw
from workspace.repository import Repository, WorkspaceError
from workspace.runtime import Binding
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


def lifecycle_request(role):
    """Archived tool schemas plus the current local tool definitions; no network."""
    from context.request_budget import tool_schemas
    from context.stage_budget import worker_tool_set
    from workspace.read_tools import READ_TOOLS
    from workspace.stage_tools import stage_tools
    from workspace.report_packets import prepare_report_packet
    from tools.rca_tools import submit_final_diagnosis
    schemas = json.loads((Path(__file__).parent / 'fixtures/workspace_gateway_schemas.json').read_text())
    if role == 'worker':
        allowed = {'get_pod_runtime_status', 'get_pod_network_summary', 'get_pod_application_summary',
                   'get_abnormal_requests', 'trace_request', 'compare_pod_with_peers', 'diagnose_pod'}
        remote = [{'type': 'function', 'function': {'name': row['name'],
                   'description': 'Archived Gateway schema; live descriptions unavailable in this fixture',
                   'parameters': row['inputSchema']}} for row in schemas if row['name'] in allowed]
        return {'tools': remote + tool_schemas(READ_TOOLS + stage_tools() + [submit_final_diagnosis]),
                'parallel_tool_calls': True}
    return {'tools': tool_schemas(READ_TOOLS + stage_tools() + [prepare_report_packet])}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


def messages_from(rows):
    result = []
    for row in rows:
        kind, text = row['type'], row.get('content', '')
        if kind == 'system' and str(text).startswith('Incident workbench'):
            continue
        if kind == 'system':
            result.append(SystemMessage(content=text))
        elif kind == 'human':
            result.append(HumanMessage(content=text))
        elif kind == 'ai':
            result.append(AIMessage(content=text, tool_calls=row.get('tool_calls') or []))
        elif kind == 'tool':
            result.append(ToolMessage(content=text, name=row.get('name') or 'tool',
                                      tool_call_id=row.get('tool_call_id') or 'replay'))
    return result


async def replay(source, output):
    for case in ('iam-pod', 'iam-service', 'frontend-service'):
        folder = source / case
        if not folder.exists():
            continue
        records = json.loads((folder / 'records.json').read_text())
        final = json.loads((folder / 'final-state.json').read_text())
        analysis = analyse_records(records, final, folder / 'records.json')
        analysis['read_usage'] = read_usage(records, folder / 'workspace', final)
        save(output / case / 'run-analysis.json', analysis)
        if case != 'iam-pod':
            continue
        refused = [c for c in records['context'] if c.get('context_incomplete')]
        for row in refused:
            name = row['role'] + '-' + row['id']
            with tempfile.TemporaryDirectory(prefix='rcagent-round2-') as temp:
                workspace = Path(temp) / 'workspace'
                shutil.copytree(folder / 'workspace', workspace)
                repo = Repository(workspace)
                binding = Binding(repo, row['incident_id'], row['run_id'], row['role'], time.time() + 300,
                                  task_id=row.get('task_id'), attempt_id=row.get('attempt_id'))
                original = await read_artifact_raw(binding, row['candidate_messages_artifact_id'])
                messages = messages_from(original)
                restored = []
                receipts = {r['id']: r for r in records.get('read_receipt', [])}
                for position, message in enumerate(messages):
                    if message.type != 'tool':
                        continue
                    try:
                        body = json.loads(message.content)
                    except ValueError:
                        continue
                    receipt = receipts.get(body.get('read_receipt_id')) if isinstance(body, dict) else None
                    if not receipt or receipt.get('tool_call_id') != message.tool_call_id:
                        continue
                    exact = await read_artifact_raw(binding, receipt['result_artifact_id'])
                    if exact != body:
                        messages[position] = message.model_copy(update={'content': json.dumps(exact, ensure_ascii=False)})
                        restored.append(receipt['id'])
                with patch.object(settings, 'CONTEXT_BUDGET_ENABLED', True), patch.object(settings, 'CONTEXT_MAX_CHARS', 24000):
                    try:
                        request = lifecycle_request(row['role']) if settings.CONTEXT_LIFECYCLE_MODE == 'active' else None
                        visible, manifest = await prepare(binding, messages, row['phase'], request=request)
                        disposition = 'assembled'
                    except WorkspaceError:
                        manifest = (await repo.all(binding.incident_id, 'context'))[-1]
                        visible_rows = await read_artifact_raw(binding, manifest['candidate_messages_artifact_id'])
                        visible = messages_from(visible_rows)
                        # Keep workbench for facts when reconstructing refused content.
                        visible.insert(0, SystemMessage(content=visible_rows[0]['content']))
                        disposition = 'refused'
                after = [{'type': m.type, 'content': m.content, 'tool_call_id': getattr(m, 'tool_call_id', None),
                          'tool_calls': getattr(m, 'tool_calls', [])} for m in visible]
                before_requirements = [(m.type, m.content) for m in messages if m.type in ('system', 'human', 'ai')]
                after_requirements = [(m.type, m.content) for m in visible if m.type in ('system', 'human', 'ai')
                                      and not str(m.content).startswith('Incident workbench')]
                assert before_requirements == after_requirements
                # Each exact local read body must remain visible or in protected detail.
                transformed = []
                payload = json.loads(visible[0].content.split('\n', 1)[1])
                details = {d.get('receipt_id'): d for d in payload.get('verified_read_details', [])}
                for message in messages:
                    if message.type != 'tool':
                        continue
                    try:
                        body = json.loads(message.content)
                    except ValueError:
                        continue
                    rid = body.get('read_receipt_id') if isinstance(body, dict) else None
                    if not rid:
                        continue
                    same = any(m.type == 'tool' and m.tool_call_id == message.tool_call_id and m.content == message.content
                               for m in visible)
                    content = {k: body[k] for k in ('items', 'text', 'range', 'truncated', 'next', 'next_char_offset') if k in body}
                    if 'items' not in body and 'text' not in body:
                        content = body
                    retained = same or details.get(rid, {}).get('content') == content
                    transformed.append({'receipt_id': rid, 'exact_message_kept': same, 'exact_page_in_detail':
                                        details.get(rid, {}).get('content') == content, 'retained': retained})
                report = {'historical_candidate_chars': row['input_chars'], 'snapshot': 'end-of-run workspace; not call-time snapshot',
                          'original_messages_from': row['candidate_messages_artifact_id'], 'source_head': '15f32ce plus uncommitted changes',
                          'restored_original_read_receipts': restored,
                          'status': disposition, 'input_chars': manifest['input_chars'],
                          'lifecycle_mode': settings.CONTEXT_LIFECYCLE_MODE,
                          'request_parts_note': 'remote schemas from archive; remote description text unavailable',
                          'multistage_completion_verified': False,
                          'required_messages_unchanged': True, 'read_pages': transformed,
                          'all_read_pages_retained': all(p['retained'] for p in transformed), 'manifest': manifest}
                save(output / 'capacity-replay' / name / 'result.json', report)
                save(output / 'capacity-replay' / name / 'visible-input.json', after)
                print(json.dumps({k: report[k] for k in ('historical_candidate_chars', 'status', 'input_chars', 'all_read_pages_retained')}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(replay(args.source, args.output))
