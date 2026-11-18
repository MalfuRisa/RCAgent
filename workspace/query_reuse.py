"""Incident-scoped result reuse and cancellable in-process operation sharing."""
import asyncio
import hashlib
import json
import weakref
from tools.contracts import REUSABLE, VERSION, SCHEMA_FINGERPRINTS, effective_parameters
from .windows import normalize_window, returned_window
from .repository import WorkspaceError

_loops = weakref.WeakKeyDictionary()


def reuse_key(binding, name, args, resource):
    if name not in REUSABLE or not args.get('start_time') or not resource:
        return None
    identity = {k: resource.get(k) for k in ('kind', 'cluster', 'namespace', 'uid')}
    if not all(identity.values()):
        return None
    params = effective_parameters(name, args)
    params.update(normalize_window(args))
    return hashlib.sha256(json.dumps({'contract': VERSION, 'schema_fingerprint': SCHEMA_FINGERPRINTS.get(name), 'tool': name, 'identity': identity, 'params': params},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def usable(evidence, args, raw, allow_partial):
    if evidence['transport_status'] != 'ok' or evidence['source_status'] not in ('ok', 'partial'):
        return False
    if returned_window(raw) != normalize_window(args):
        return False
    partial = evidence.get('truncated') or evidence['source_status'] == 'partial' or evidence.get('data_gaps')
    return not partial or allow_partial


async def find(binding, key, args, allow_partial):
    if key is None:
        return None
    from .queries import read_raw
    for evidence in reversed(await binding.repository.all(binding.incident_id, 'evidence')):
        if evidence.get('reuse_key') != key:
            continue
        try:
            raw = await read_raw(binding, evidence['id'])
        except (WorkspaceError, OSError, ValueError):
            continue
        if usable(evidence, args, raw, allow_partial):
            return raw, evidence
    return None


async def shared(binding, key, operation):
    """Cancelling one observer leaves an independently timed operation alive."""
    registry = _loops.setdefault(asyncio.get_running_loop(), {})
    full_key = (str(binding.repository.root), binding.incident_id, key)
    entry = registry.get(full_key)
    joined = entry is not None
    if entry is None:
        entry = {'task': asyncio.create_task(operation()), 'waiters': 0}
        registry[full_key] = entry
    entry['waiters'] += 1
    try:
        return await asyncio.wait_for(asyncio.shield(entry['task']), binding.remaining()), joined
    finally:
        entry['waiters'] -= 1
        if entry['waiters'] == 0:
            if registry.get(full_key) is entry:
                registry.pop(full_key)
            if not entry['task'].done():
                entry['task'].cancel()
                # Its cancellation commits an unknown-result evidence record.
                await asyncio.shield(asyncio.gather(entry['task'], return_exceptions=True))


async def annotate(binding, raw, evidence, mode, tool_call_id):
    if mode != 'new':
        binding.reuse_calls += 1
        await binding.repository.transact(lambda db: binding.repository.event(db, binding.incident_id,
            binding.task_id, 'query_reused', {'mode': mode, 'evidence_id': evidence['id'],
                'run_id': binding.run_id, 'requested_by_task_id': binding.task_id, 'tool_call_id': tool_call_id}))
    return {**raw, '_rcagent_query': {'mode': mode, 'evidence_id': evidence['id'],
        'original_run_id': evidence['run_id'], 'requested_by_task_id': binding.task_id,
        'reason': 'same identity, actual window and effective parameters' if mode != 'new' else 'actual remote operation',
        'time_range': evidence.get('time_range'), 'source_status': evidence['source_status']}}
