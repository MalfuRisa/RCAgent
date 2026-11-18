"""Verified v5 CPU native arguments; no model execution or task policy."""
from datetime import datetime
CONTRACT = 'insights-v5-http-cpu-v1'


def cpu_arguments(window, *, page=1, pod_id=None, agent_id=None, pid=None):
    start, end = (datetime.fromisoformat(window[k].replace('Z', '+00:00'))
                  for k in ('start_time', 'end_time'))
    if not start.tzinfo or not end.tzinfo or not 0 < (end-start).total_seconds() <= 86400:
        raise ValueError('CPU window requires explicit timezone and duration <=24h')
    if type(page) is not int or not 1 <= page <= 20:
        raise ValueError('bounded CPU page must be 1..20')
    args = dict(event_type='runqlat', **window, page=page, page_size=20)
    for key, value in (('pod_id', pod_id), ('agent_id', agent_id), ('pid', pid)):
        if value is None:
            continue
        if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or int(value) <= 0:
            raise ValueError('CPU platform IDs must be positive decimal integers')
        args[key] = str(value)
    return args


