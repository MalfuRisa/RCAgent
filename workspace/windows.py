"""Fixed run windows, preserving the actual backend window independently."""
from datetime import datetime, timezone
from .repository import WorkspaceError


def normalize_window(window):
    try:
        start, end = (datetime.fromisoformat(window[key].replace('Z', '+00:00')) for key in ('start_time', 'end_time'))
        if not start.tzinfo or not end.tzinfo or start >= end or (end - start).total_seconds() > 86400:
            raise ValueError('window requires timezone and 0 < duration <= 24h')
        return {key: at.astimezone(timezone.utc).isoformat(timespec='microseconds') for key, at in zip(('start_time', 'end_time'), (start, end))}
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise WorkspaceError('invalid_query_window: ' + str(exc)) from exc


def returned_window(raw):
    meta = raw.get('meta') or {}
    window = raw.get('time_range') or meta.get('time_range') or raw.get('window')
    if not isinstance(window, dict):
        return None
    window = {'start_time': window.get('start_time') or window.get('start'), 'end_time': window.get('end_time') or window.get('end')}
    try:
        return normalize_window(window)
    except WorkspaceError:
        return None


async def request_window(binding, name, args):
    from tools.contracts import HAS_WINDOW
    result = dict(args)
    if name not in HAS_WINDOW:
        return result
    if not result.get('start_time') and not result.get('end_time'):
        if binding.query_window:
            result.update(binding.query_window)
    else:
        if not result.get('start_time') or not result.get('end_time'):
            raise WorkspaceError('start_time and end_time must be supplied together')
    if result.get('start_time'):
        result.update(normalize_window(result))
    return result
