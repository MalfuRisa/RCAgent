"""Offline replay of archived incidents through the context assembler.

No network, no model: it loads saved workspace records (the 2026-10-12 and
2026-10-13 dumps), rebuilds the snapshot in a throwaway directory - including
the archived artifact files, so local reads can still fetch real detail - and
reports what the model-facing input would contain per role/phase/mode/budget.

The requested window comes from the run record the incident actually used, or
from the evidence windows when the run has none; it is never invented. Every
report names the snapshot it replayed (version-88 rebuild vs final records) and
exports the exact visible input, read receipts and refusal candidate before the
temporary workspace is removed.
"""
import argparse
import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from workspace.repository import Repository  # noqa: E402
from workspace.runtime import Binding  # noqa: E402
from context.builder import prepare, visible_chars  # noqa: E402
from context.selection import message_chars  # noqa: E402


def load_history(path):
    """Rebuild replay history: the previously saved workbench is dropped because
    the assembler regenerates it; everything else keeps its type and pairing."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    raw = json.loads(Path(path).read_text(encoding='utf-8'))
    messages = []
    for row in raw:
        kind = row.get('type')
        content = row.get('content', '')
        if kind == 'system':
            if isinstance(content, str) and content.startswith('Incident workbench'):
                continue
            messages.append(SystemMessage(content=content))
        elif kind == 'human':
            messages.append(HumanMessage(content=content))
        elif kind == 'ai':
            messages.append(AIMessage(content=content, tool_calls=row.get('tool_calls') or []))
        elif kind == 'tool':
            messages.append(ToolMessage(content=content, name=row.get('name') or 'tool',
                                        tool_call_id=row.get('tool_call_id') or row_id(row)))
    return messages


def row_id(row):
    return 'replay-' + str(abs(hash(json.dumps(row, sort_keys=True, default=str))) % (10 ** 10))


def load_dump(path):
    """Accept a records list, the ``kind -> rows`` dump, or a fixture shape."""
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if isinstance(data, dict) and 'records' in data:
        return data['records'], data.get('incidents')
    if isinstance(data, dict) and 'samples' in data:
        return None, data
    if isinstance(data, dict):
        # kind -> [body, ...] dump: rebuild the records list the repository uses.
        rows = []
        for kind, values in data.items():
            for body in values:
                if not isinstance(body, dict) or 'id' not in body:
                    continue
                rows.append({'id': body['id'], 'incident': body.get('incident_id'), 'kind': kind,
                             'revision': body.get('revision', 1), 'body': body})
        return rows, None
    return data, None


def incident_records(records, incident, kind):
    return [row for row in records if row.get('incident') == incident and row.get('kind') == kind]


def replay_window(records, incident):
    """The window the incident actually used, or the evidence windows as stated."""
    for row in incident_records(records, incident, 'run'):
        window = (row.get('body') or {}).get('query_window')
        if isinstance(window, dict) and window.get('start_time') and window.get('end_time'):
            return window, 'run_record'
    starts, ends = [], []
    for row in incident_records(records, incident, 'evidence'):
        body = row.get('body') or {}
        window = body.get('returned_time_range') or body.get('time_range') or {}
        if window.get('start_time') and window.get('end_time'):
            starts.append(window['start_time'])
            ends.append(window['end_time'])
    if starts:
        return {'start_time': min(starts), 'end_time': max(ends)}, 'evidence_windows'
    return {}, 'unknown'


def artifact_dirs(args):
    """Directories that may hold this incident's artifact files, in order."""
    dump = Path(args.records)
    candidates = []
    if args.artifacts:
        candidates.append(Path(args.artifacts))
    candidates += [dump.parent / 'workspaces' / 'artifacts' / args.incident,
                   dump.parent / 'workspace' / 'artifacts' / args.incident,
                   dump.parent.parent / 'workspaces' / 'artifacts' / args.incident]
    return [path for path in candidates if path.is_dir()]


def copy_artifacts(args, directory):
    """Copy archived artifact files so local reads of the replay can succeed."""
    copied = 0
    target = Path(directory) / 'artifacts' / args.incident
    for source in artifact_dirs(args):
        target.mkdir(parents=True, exist_ok=True)
        for path in source.glob('*.json'):
            shutil.copyfile(path, target / path.name)
            copied += 1
        if copied:
            break
    return copied


def stage_records(records, incidents, directory, incident):
    """Write archived records into a fresh repository; never touches the source."""
    repo = Repository(directory)
    def seed(db):
        selected = [row for row in records if row.get('incident') == incident]
        if not selected:
            raise SystemExit('incident %s has no records in this dump' % incident)
        scope = {'cluster': 'rd2', 'namespaces': ['*']}
        if incidents:
            scope = next((item['scope'] for item in incidents if item['id'] == incident), scope)
        db.execute("INSERT OR REPLACE INTO incidents VALUES (?,?)", (incident, json.dumps(scope)))
        for row in selected:
            db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)",
                       (row['id'], row['incident'], row['kind'], row.get('revision', 1),
                        json.dumps(row['body'], ensure_ascii=False, default=str)))
    return repo, seed


async def replay(args):
    records, incidents = load_dump(args.records)
    if records is None:
        raise SystemExit('this dump is a fixture, not a records list')
    window, window_source = replay_window(records, args.incident)
    with tempfile.TemporaryDirectory(prefix='rcagent-replay-') as directory:
        repo = Repository(directory)
        await repo.initialize()
        await repo.transact(stage_records(records, incidents, directory, args.incident)[1])
        artifacts = copy_artifacts(args, directory)
        def ids(db, kind):
            return [json.loads(row['body'])['id'] for row in
                    db.execute("SELECT body FROM records WHERE incident=? AND kind=?", (args.incident, kind))]
        task_ids = await repo.transact(lambda db: ids(db, 'task'))
        attempt_ids = await repo.transact(lambda db: ids(db, 'attempt'))
        run_ids = await repo.transact(lambda db: ids(db, 'run'))
        binding = Binding(repo, args.incident, args.run_id or (run_ids[0] if run_ids else 'replay-run'),
                          args.role, time.time() + 300,
                          task_id=(args.task_id or (task_ids[0] if task_ids else None)),
                          attempt_id=(args.attempt_id or (attempt_ids[0] if attempt_ids else None)))
        binding.query_window = window
        messages = []
        if args.history:
            messages = load_history(args.history)
        budget_enabled = args.budget == 'on'
        with_patch = dict(CONTEXT_VIEW_MODE=args.mode, CONTEXT_BUDGET_ENABLED=budget_enabled,
                          CONTEXT_MAX_CHARS=args.max_chars)
        original = {name: getattr(settings, name) for name in with_patch}
        try:
            for name, value in with_patch.items():
                setattr(settings, name, value)
            started = time.monotonic()
            try:
                visible, manifest = await prepare(binding, messages, args.phase)
                status = 'assembled'
            except Exception as exc:  # context_incomplete and friends: still report
                status, visible = 'refused', []
                stored = await repo.transact(lambda db: [
                    json.loads(row['body']) for row in
                    db.execute("SELECT body FROM records WHERE incident=? AND kind='context' ORDER BY rowid",
                               (args.incident,))])
                manifest = stored[-1] if stored else {}
                manifest['rejection_detail'] = str(exc)
            duration_ms = (time.monotonic() - started) * 1000
            receipts = await repo.transact(lambda db: [
                json.loads(row['body']) for row in
                db.execute("SELECT body FROM records WHERE incident=? AND kind='read_receipt'", (args.incident,))])
            stored_messages = [{'type': m.type, 'name': getattr(m, 'name', None),
                                'tool_call_id': getattr(m, 'tool_call_id', None), 'content': m.content}
                               for m in visible]
        finally:
            for name, value in original.items():
                setattr(settings, name, value)
        report = summarize(args, visible, manifest, status, duration_ms)
        report.update(query_window=window, query_window_source=window_source,
                      snapshot_label=args.snapshot_label, artifact_files=artifacts,
                      read_receipt_count=len(receipts))
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            name = '%s-%s-%s-%s-budget-%s' % (args.incident[:14], args.role, args.phase,
                                              args.mode, args.budget)
            (out / (name + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
            candidate = manifest.get('candidate_messages_artifact_id')
            (out / (name + '-visible-input.json')).write_text(json.dumps(
                {'status': status, 'messages': stored_messages, 'manifest': manifest,
                 'refusal_candidate_artifact_id': candidate, 'read_receipts': receipts},
                ensure_ascii=False, indent=1), encoding='utf-8')
        print(json.dumps({k: report[k] for k in ('incident', 'role', 'phase', 'mode', 'status',
                                                 'input_chars', 'limit', 'would_exceed_budget',
                                                 'coverage', 'discoverable')},
                         ensure_ascii=False))
        return report


def summarize(args, visible, manifest, status, duration_ms):
    text = '\n'.join(str(m.content) for m in visible)
    payload = {}
    if visible:
        head = visible[0].content
        if head.startswith('Incident workbench'):
            payload = json.loads(head.split('\n', 1)[1])
    discoverable = {
        'tasks': sorted(row['task_id'] for row in payload.get('tasks', []))[:10],
        'task_count': len(payload.get('tasks', [])),
        'assessments': len(payload.get('assessments', [])),
        'evidence_briefs': len(payload.get('evidence', [])),
        'evidence_directory': len(payload.get('evidence_directory', [])),
        'resources': len(payload.get('resources', [])),
        'verified_read_details': len(payload.get('verified_read_details', [])),
    }
    return {'incident': args.incident, 'role': args.role, 'phase': args.phase, 'mode': args.mode,
            'budget': args.budget, 'limit': args.max_chars, 'status': status,
            'input_chars': manifest.get('input_chars', 0),
            'workbench_chars': manifest.get('part_chars', {}).get('workbench'),
            'message_chars': manifest.get('part_chars', {}).get('messages'),
            'would_exceed_budget': manifest.get('would_exceed_budget'),
            'view_version': manifest.get('view_version'),
            'projection': manifest.get('projection'),
            'budget_steps': manifest.get('budget_steps'),
            'history_replacements': manifest.get('history_replacements'),
            'protected_parts_exceed_budget': manifest.get('protected_parts_exceed_budget'),
            'protected_parts': manifest.get('protected_parts'),
            'context_incomplete': manifest.get('context_incomplete'),
            'rejection_detail': manifest.get('rejection_detail'),
            'assembly_duration_ms': round(duration_ms, 2),
            'coverage': payload.get('coverage'),
            'discoverable': discoverable,
            'markers': {marker: (marker in text) for marker in ('OBSERVED_FAILURE', 'tcp_errors', 'not_found')}}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Replay archived incidents through context.prepare')
    parser.add_argument('--records', required=True, help='records dump (JSON list)')
    parser.add_argument('--incident', required=True)
    parser.add_argument('--role', default='supervisor', choices=('triage', 'planner', 'worker', 'supervisor'))
    parser.add_argument('--phase', default='initial_submission')
    parser.add_argument('--mode', default='compact', choices=('compact', 'legacy'))
    parser.add_argument('--budget', default='on', choices=('on', 'off'))
    parser.add_argument('--max-chars', type=int, default=settings.CONTEXT_MAX_CHARS)
    parser.add_argument('--task-id', default=None)
    parser.add_argument('--attempt-id', default=None)
    parser.add_argument('--run-id', default=None)
    parser.add_argument('--history', default=None, help='JSON message list to replay as history')
    parser.add_argument('--artifacts', default=None, help='directory holding this incident\'s artifact files')
    parser.add_argument('--snapshot-label', default=None,
                        help='which snapshot this dump is (for example: version-88 rebuild, final records)')
    parser.add_argument('--out', default=None, help='directory for the JSON report')
    return asyncio.run(replay(parser.parse_args(argv)))


if __name__ == '__main__':
    main()