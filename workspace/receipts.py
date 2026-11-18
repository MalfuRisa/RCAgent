"""Read receipts: what one local read actually returned (PRD §17.6).

A receipt answers "which materials did this message hand to the model" with
program-verified fields: the source record and revision, the paths that were
returned completely, the page range, the windows, the limits and the archived
body. Later projections deduplicate facts only from receipts that are still
visible in the current input, so an earlier read never silently deletes a value.

Records live in the same SQLite workspace and artifact directory as everything
else; no new service or table is introduced.
"""
import hashlib
import json

from .artifacts import write_artifact
from .repository import new_id


def new_receipt(binding, *, tool_name, source_kind, source_id, source_revision=None, source_hash=None,
                view=None, field=None, page_range=None, range_unit=None, returned_paths=(),
                complete_paths=(), source_scope=None, requested_window=None, returned_window=None,
                source_limitations=None, truncated=None, has_more=None, next_position=None):
    """Build the receipt body for one successful read (id is assigned here)."""
    return {"id": new_id('read_receipt'), "incident_id": binding.incident_id, "run_id": binding.run_id,
            "task_id": binding.task_id, "attempt_id": binding.attempt_id, "role": binding.role,
            "phase": None, "context_id": binding.last_context_id, "tool_call_id": None,
            "tool_name": tool_name, "source_kind": source_kind, "source_id": source_id,
            "source_revision": source_revision, "source_hash": source_hash,
            "view": view, "field": field, "range": list(page_range) if page_range is not None else None,
            "range_unit": range_unit, "returned_paths": list(returned_paths),
            "complete_paths": list(complete_paths), "source_scope": source_scope,
            "requested_window": requested_window, "returned_window": returned_window,
            "source_limitations": source_limitations, "truncated": truncated,
            "has_more": has_more, "next": next_position,
            "result_artifact_id": None, "result_chars": None}


async def publish_read(binding, receipt, payload, *, kind='local_read', evidence_ids=None, max_chars=0):
    """Archive the returned body, store the receipt, audit the read, return payload.

    The payload already carries its ``read_receipt_id``; the receipt stores the
    artifact id and the final serialized size, so the returned page can be
    reproduced exactly. The artifact body never references its own artifact id;
    it is written before the receipt so a failed write cannot publish a receipt.
    """
    from .paging import serialized_chars, resolve_max_chars
    from .read_tools import CURRENT_TOOL_CALL
    receipt = dict(receipt)
    payload, receipt = bounded_read_result(payload, receipt, resolve_max_chars(max_chars))
    if payload.get('read_page_limit_too_small') or payload.get('page_limit_too_small'):
        receipt.update(returned_paths=[], complete_paths=[])
        await record_failed_read(binding, receipt['tool_name'], receipt['source_id'], 'read_page_limit_too_small')
    receipt['tool_call_id'] = receipt.get('tool_call_id') or CURRENT_TOOL_CALL.get()
    artifact = await write_artifact(binding, payload)
    receipt['result_artifact_id'] = artifact['id']
    receipt['result_chars'] = serialized_chars(payload)
    event = {"reference": receipt['source_id'], "role": binding.role, "run_id": binding.run_id,
             "task_id": binding.task_id, "context_id": binding.last_context_id,
             "tool_call_id": receipt['tool_call_id'], "range": receipt.get('range'),
             "field": receipt.get('field'), "chars": receipt['result_chars'], "kind": kind,
             "read_receipt_id": receipt['id']}
    if evidence_ids is not None:
        event["evidence_ids"] = list(evidence_ids)

    def commit(db):
        manifest = None
        if binding.last_context_id:
            manifest = binding.repository.get(db, binding.incident_id, binding.last_context_id, "context")
            receipt['phase'] = receipt.get('phase') or manifest.get('phase')
        binding.repository.put(db, binding.incident_id, "artifact", artifact)
        binding.repository.put(db, binding.incident_id, "read_receipt", receipt, action="read_recorded")
        binding.repository.event(db, binding.incident_id, receipt['source_id'], kind, event)
        if manifest is not None:
            manifest['read_events'].append(event)
            binding.repository.put(db, binding.incident_id, "context", manifest, manifest["revision"], "context_read")
    await binding.repository.transact(commit)
    return payload


def bounded_read_result(payload, receipt, limit):
    """Final whole-envelope guard, before either body or coverage is published.

    A directory is deliberately not a successful observation page. Its field
    entry uses the same tool and metadata view as the original request.
    """
    from .paging import serialized_chars
    if serialized_chars(payload) <= limit:
        return payload, receipt
    receipt.update(returned_paths=[], complete_paths=[], truncated=True, has_more=True,
                   range=None, next=None)
    tool = receipt['tool_name']
    kind = receipt['source_kind']
    id_key = {'evidence_metadata': 'evidence_id', 'evidence': 'evidence_id',
              'task': 'task_id', 'assessment': 'assessment_id',
              'resource': 'resource_id', 'artifact': 'artifact_id'}.get(kind)
    field_view = ', view=metadata' if kind == 'evidence_metadata' else ''
    body = {'read_receipt_id': receipt['id'], 'truncated': True,
            'view': 'bounded_directory', 'source_kind': kind,
            'detail': tool + '(' + (id_key + '=' + receipt['source_id'] + ', ' if id_key else '')
                      + 'char_offset=0' + field_view + ')'}
    if id_key:
        body[id_key] = receipt['source_id']
    nested = next((payload[k] for k in ('metadata', 'task', 'assessment', 'brief')
                   if isinstance(payload.get(k), dict)), {})
    for key in ('unknown_fields', 'limitations', 'data_gaps'):
        if key in nested and key not in payload:
            payload = {**payload, key: nested[key]}
    directory = {'fields': sorted(nested)} if nested else {}
    if directory and serialized_chars({**body, **directory}) <= limit:
        body.update(directory)
    # Preserve only complete, bounded identity/limit values, never sliced JSON.
    for key in ('unknown_fields', 'kind', 'name', 'cluster', 'namespace', 'uid',
                'limitations', 'data_gaps', 'windows', 'read_entries'):
        if key in payload and serialized_chars({**body, key: payload[key]}) <= limit:
            body[key] = payload[key]
    if any(payload.get(key) and key not in body for key in ('limitations', 'data_gaps', 'unknown_fields')):
        if serialized_chars({**body, 'omitted_details_require_read': True}) <= limit:
            body['omitted_details_require_read'] = True
    if serialized_chars(body) > limit or payload.get('page_limit_too_small'):
        body = {'read_receipt_id': receipt['id'], 'read_page_limit_too_small': True,
                'page_limit_too_small': True,
                'retry_hint': 'raise max_chars or select a shorter field'}
    return body, receipt


async def record_failed_read(binding, tool_name, reference, error):
    """A failed read is auditable but never claims that facts were covered."""
    from .read_tools import CURRENT_TOOL_CALL
    event = {"reference": reference, "role": binding.role, "run_id": binding.run_id,
             "task_id": binding.task_id, "context_id": binding.last_context_id,
             "tool_call_id": CURRENT_TOOL_CALL.get(), "kind": 'local_read_error',
             "tool_name": tool_name, "error": str(error)}
    def commit(db):
        binding.repository.event(db, binding.incident_id, reference or binding.incident_id,
                                 'local_read_error', event)
        if binding.last_context_id:
            manifest = binding.repository.get(db, binding.incident_id, binding.last_context_id, "context")
            manifest['read_events'].append(event)
            binding.repository.put(db, binding.incident_id, "context", manifest, manifest["revision"], "context_read")
    await binding.repository.transact(commit)


def source_hash(value):
    """Stable hash of a stored record body, so a receipt pins its exact source."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode('utf-8')).hexdigest()


def matches_verified_read(receipt, body):
    """Compare the actual stored read body, independent of legacy hash fields.

    The transient body is populated by the async verifier, never persisted in
    receipts. Without it, retain material rather than claiming read coverage.
    """
    from .observations import values_match

    return "_verified_body" in receipt and values_match(receipt["_verified_body"], body)


async def matches_read(binding, receipt, body):
    from .artifacts import read_artifact_raw
    from .repository import WorkspaceError

    try:
        stored = await read_artifact_raw(binding, receipt["result_artifact_id"])
    except (WorkspaceError, OSError, ValueError, KeyError):
        return False
    return matches_verified_read({"_verified_body": stored}, body)
