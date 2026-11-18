"""Run-level stop signals: a rate limit stops the whole run (PRD §19.10).

One 429 ends the run's model use: the request is not retried, every role shares
the stop, and a request that was still queued is terminated before it is sent.
The in-process registry is the fast path consulted before each send; the run
record and the ``run_stopped`` event are the persisted, auditable authority, so
a restart or another reader can still see why the run ended.

The signal is scoped to one (workspace, incident, run); another incident's run
is unaffected. Connection/503 retries keep their existing contract.
"""
_stopped = {}


def key(binding):
    return (str(binding.repository.root), binding.incident_id, binding.run_id)


def reason(binding):
    """The stop reason recorded for this run, or None."""
    if binding is None:
        return None
    return _stopped.get(key(binding))


async def persisted_reason(binding):
    """Read the durable authority too, including stops written by another process."""
    cached = reason(binding)
    if cached or binding is None:
        return cached
    run = await binding.repository.read(binding.incident_id, binding.run_id, 'run')
    if run.get('stop_reason'):
        return stop(binding, run['stop_reason'])
    return None


def stop(binding, reason_code):
    """Mark this run stopped; the first cause is kept."""
    if binding is None:
        return reason_code
    _stopped.setdefault(key(binding), reason_code)
    return _stopped[key(binding)]


def clear(binding):
    """Forget the in-process marker (used by tests and by a fresh run)."""
    if binding is not None:
        _stopped.pop(key(binding), None)


async def stop_run(binding, reason_code, detail=None):
    """Persist the stop marker and its audit event, then return the reason.

    The run's first stop reason is not overwritten, so a later cancellation or
    deadline cannot hide the 429 that really ended the run.
    """
    reason_code = stop(binding, reason_code)
    event = {'reason': reason_code, 'detail': detail or {}, 'rate_limit_policy': _policy()}
    def update(db):
        run = binding.repository.get(db, binding.incident_id, binding.run_id, 'run')
        if not run.get('stop_reason'):
            run['stop_reason'] = reason_code
        run['rate_limit_policy'] = _policy()
        errors = list(run.get('execution_errors') or [])
        if not any(item.get('code') == reason_code for item in errors if isinstance(item, dict)):
            errors.append({'source': 'run_stop', 'code': reason_code, 'detail': detail or {}})
        run['execution_errors'] = errors
        run['stopped_at'] = _now()
        return binding.repository.put(db, binding.incident_id, 'run', run, run['revision'], 'run_stop_marked')
    await binding.repository.transact(update)
    await binding.repository.transact(lambda db: binding.repository.event(
        db, binding.incident_id, binding.run_id, 'run_stopped', event))
    return reason_code


def _policy():
    from config import settings
    return settings.RATE_LIMIT_POLICY


def _now():
    from .repository import utcnow
    return utcnow()
