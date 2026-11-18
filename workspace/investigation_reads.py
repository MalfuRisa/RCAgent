"""Bounded investigation reads with immutable, owner-scoped pagination."""
import base64
import json

from .artifacts import read_artifact_raw, write_artifact
from .repository import WorkspaceError

KINDS = ('question', 'observation', 'judgment', 'stage_commit', 'checkpoint', 'material_usage', 'report_packet')


def scope(b):
    return [b.incident_id, b.run_id, b.role, b.task_id, b.attempt_id]


async def snapshot_rows(b, kind, question_id, owner, cursor):
    query = [kind, question_id, owner]
    if cursor:
        try:
            token = json.loads(base64.urlsafe_b64decode(cursor))
            page = await read_artifact_raw(b, token['snapshot'])
            start = token['offset']
        except (KeyError, ValueError, TypeError, WorkspaceError) as exc:
            raise WorkspaceError('invalid investigation cursor') from exc
        if page.get('scope') != scope(b) or page.get('query') != query:
            raise WorkspaceError('investigation cursor scope does not match')
        if type(start) is not int or start < 0 or start > len(page['rows']):
            raise WorkspaceError('invalid investigation cursor offset')
        return page['rows'], token['snapshot'], start
    rows = await b.repository.all(b.incident_id, kind)
    rows = [r for r in rows if r.get('run_id') == b.run_id]
    if question_id:
        rows = [r for r in rows if question_id in (r.get('question_ids') or [])
                or question_id in (r.get('id'), r.get('question_id'))]
    if owner:
        rows = [r for r in rows if r.get('owner') == owner and r.get('task_id') == b.task_id
                and r.get('attempt_id') == b.attempt_id]
    rows.sort(key=lambda row: (row.get('created_at') or '', row['id']))
    artifact = await write_artifact(b, {'kind': 'investigation_listing', 'scope': scope(b), 'query': query,
                                       'rows': rows})
    await b.repository.save(b.incident_id, 'artifact', artifact)
    return rows, artifact['id'], 0


def cursor_for(snapshot_id, offset):
    return base64.urlsafe_b64encode(json.dumps({'snapshot': snapshot_id, 'offset': offset}).encode()).decode()


async def read(b, *, kind, record_id, question_id, owner, limit, cursor, max_chars,
               field='', offset=0, char_offset=None):
    from .read_tools import (limit_setting, new_receipt, content_page, publish_read, fit_list)
    if kind and kind not in KINDS:
        raise WorkspaceError('unknown investigation kind')
    if not 1 <= limit <= 100:
        raise WorkspaceError('invalid investigation page limit')
    cap = limit_setting(max_chars)
    receipt = new_receipt(b, tool_name='read_investigation', source_kind='investigation',
                          source_id=record_id or question_id or kind, view='field' if field else 'auto',
                          field=field or None, complete_paths=[], returned_paths=[])
    if record_id:
        record = await b.repository.read(b.incident_id, record_id, kind or None)
        target = next((k for k in KINDS if record_id.startswith(k + '_')), None)
        if kind == 'material_usage':
            target = kind  # composite owner/question/source keys have no id prefix
        if target is None or record.get('run_id') != b.run_id:
            raise WorkspaceError('record is not an investigation record in this run')
        if target == 'question':
            b.question_focus_ids = [record_id]
        receipt['source_revision'] = record['revision']
        return await content_page(b, tool_name='read_investigation', reference=record_id,
                                  identity={'record_id': record_id}, raw=record, field=field, offset=offset,
                                  limit=limit, char_offset=char_offset, limit_chars=cap, view='field',
                                  source_marker='investigation_record', receipt=receipt, extra={'kind': target})
    if field or char_offset is not None:
        raise WorkspaceError('field reads need an official record_id')
    kind = kind or 'question'
    if question_id:
        question = await b.repository.read(b.incident_id, question_id, 'question')
        if question.get('run_id') != b.run_id:
            raise WorkspaceError('question belongs to another run')
        b.question_focus_ids = [question_id]
    rows, snapshot_id, start = await snapshot_rows(b, kind, question_id, owner, cursor)
    def build(items):
        end = start + len(items)
        more = end < len(rows)
        return {'kind': kind, 'items': items, 'range': [start, end], 'total': len(rows), 'has_more': more,
                'next_cursor': cursor_for(snapshot_id, end) if more else None,
                'read_receipt_id': receipt['id']}
    # Listing is a directory, never an implicit full-text observation.
    briefs = [{'record_id': row['id'], 'revision': row['revision'], 'status': row.get('status'),
               'is_head': row.get('is_head')} for row in rows]
    items, end, too_small = fit_list(briefs, start, min(start + limit, len(rows)), cap, build)
    result = build(items)
    if too_small:
        result.update(page_limit_too_small=True, next_cursor=None,
                      detail_entries={'record_id': rows[start]['id'], 'read': 'read_investigation(record_id=<id>, field=<field>)'})
    receipt.update(range=[start, end], has_more=end < len(rows), next_position=end if end < len(rows) else None,
                   returned_paths=[r['record_id'] for r in items], complete_paths=[])
    return await publish_read(b, receipt, result, max_chars=cap)
