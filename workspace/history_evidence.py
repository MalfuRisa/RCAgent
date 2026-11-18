"""Align returned history samples without filling gaps or mixing query scopes."""

from collections import defaultdict
from dataclasses import dataclass
from math import isfinite


@dataclass
class Series:
    source_id: str
    path: tuple[str, str]
    points: dict
    coverage: dict


def path_of(meta):
    endpoints = {}
    for target in (meta.get('requested') or {}).get('filters') or []:
        key, values = target.get('field'), target.get('values')
        if key in {'auto_service_0', 'auto_service_1'} and target.get('op') == 'eq' and len(values or []) == 1:
            endpoints[key] = values[0]
    client, server = endpoints.get('auto_service_0'), endpoints.get('auto_service_1')
    return (client, server) if isinstance(client, str) and isinstance(server, str) else None


def scope_of(source, data):
    meta = data.get('queryMeta') or {}
    time = data.get('time') or {}
    start, end = time.get('start'), time.get('end')
    requested = meta.get('requested') or {}
    dataset = requested.get('datasetId')
    path = path_of(meta)
    requests = (meta.get('constructed') or {}).get('requests') or []
    if len(requests) != 1 or not path or not dataset or type(start) is not int or type(end) is not int:
        return None
    if requested.get('time') != {'start': start, 'end': end}:
        return None
    request = requests[0]
    interval = request.get('interval')
    table = request.get('source') or {}
    context = source.get('source_context') or {}
    name = (context.get('server_identity') or {}).get('serverInfo') or {}
    route = (source.get('mcp_source'), context.get('mcp_url'), context.get('configured_backend'),
             name.get('name'), name.get('version'))
    if (not any(route[1:3]) or not table.get('database') or not table.get('table') or
            type(interval) is not int or interval <= 0 or 'toi' not in (request.get('groupBy') or [])):
        return None
    key = (route, start, end, dataset, table['database'], table['table'], request.get('dataSource'), interval)
    return key, path, meta


def observed_points(raw, pointer, start, end):
    points = {}
    ambiguous = set()
    for index, point in enumerate(raw):
        if not isinstance(point, list) or len(point) != 2 or type(point[0]) is not int:
            continue
        timestamp, value = point
        if not start * 1000 <= timestamp <= end * 1000:
            continue
        if timestamp in points:
            ambiguous.add(timestamp)
        finite = type(value) in (int, float) and isfinite(value)
        points[timestamp] = {'value': value if finite else None, 'pointer': f'{pointer}/{index}'}
    for timestamp in ambiguous:
        # A timestamp with more than one candidate has no unambiguous value.
        points[timestamp] = {'value': None, 'pointer': None}
    return points


def result_series(source, data):
    scope = scope_of(source, data)
    if not scope:
        return []
    key, path, meta = scope
    semantics = {m.get('metricKey'): m for m in meta.get('metricSemantics') or []}
    results = []
    for idx, chart in enumerate(data.get('charts') or []):
        metric, unit = chart.get('metricKey'), chart.get('unit')
        chart_series = chart.get('series') or []
        if not metric or not isinstance(unit, str) or len(chart_series) != 1:
            continue
        expression = (semantics.get(metric) or {}).get('expression')
        pointer = f'/structuredContent/data/charts/{idx}/series/0/points'
        points = observed_points(chart_series[0].get('points') or [], pointer, key[1], key[2])
        results.append(((*key, metric, unit, expression), Series(source['source_id'], path, points, meta.get('coverage'))))
    return results


def change(series, timestamp, step):
    previous, current, following = (series.points.get(t) for t in (timestamp-step, timestamp, timestamp+step))
    if not previous or not current or previous['value'] is None or current['value'] is None:
        return None
    return {'path': {'client': series.path[0], 'server': series.path[1]}, 'source_id': series.source_id,
            'same_named_endpoints': series.path[0] == series.path[1],
            'before': previous, 'current': current, 'after': following,
            'delta': current['value'] - previous['value']}


def align_group(key, paths):
    route, start, end, dataset, database, table, data_source, seconds, metric, unit, expression = key
    timestamps = sorted({t for series in paths for t in series.points})
    step = seconds*1000
    singles, simultaneous = [], []
    same_time_values, comparable = 0, 0
    for t in timestamps:
        if sum(series.points.get(t, {}).get('value') is not None for series in paths) >= 2:
            same_time_values += 1
        changes = [entry for series in paths if (entry := change(series, t, step)) is not None]
        positives = sorted((entry for entry in changes if entry['delta'] > 0), key=lambda entry: -entry['delta'])
        if positives:
            singles.append({'timestamp_ms': t, **positives[0]})
        if len(changes) >= 2:
            comparable += 1
        if len(positives) >= 2:
            simultaneous.append({'timestamp_ms': t, 'rising': positives[:5], 'rising_count': len(positives),
                                 'compared': len(changes), 'missing': len(paths)-len(changes)})
    simultaneous.sort(key=lambda item: -item['rising'][1]['delta'])
    singles.sort(key=lambda item: -item['delta'])
    return {'source_family': route[0], 'server_name': route[3], 'server_version': route[4],
            'dataset': dataset, 'table': {'database': database, 'name': table},
            'window': {'start': start, 'end': end}, 'sampling_seconds': seconds, 'data_source': data_source,
            'metric': metric, 'unit': unit, 'expression': expression,
            'paths': [{'path': series.path, 'source_id': series.source_id, 'coverage': series.coverage,
                       'finite_points': sum(point['value'] is not None for point in series.points.values())}
                      for series in paths],
            'observed_timestamps': len(timestamps), 'timestamps_with_values_on_two_or_more_paths': same_time_values,
            'timestamps_with_adjacent_deltas_on_two_or_more_paths': comparable,
            'single_path_candidates': singles[:3], 'same_time_candidates': simultaneous[:3]}


def history_candidates(outputs, touched=None):
    groups = defaultdict(dict)
    ungrouped = []
    for sid, source in outputs.sources.items():
        if source['tool_name'] != 'insights_metric_history' or source['status'] != 'ok':
            continue
        raw = outputs.materials.raw.get(sid) or {}
        data = (raw.get('structuredContent') or {}).get('data') or {}
        parsed = result_series(source, data) if isinstance(data, dict) else []
        if not parsed:
            ungrouped.append(sid)
        for key, series in parsed:
            # The same directional path is one participant even if queried twice.
            groups[key][series.path] = series
    matching = [(key, list(paths.values())) for key, paths in groups.items()
                if touched is None or any(series.source_id in touched for series in paths.values())]
    if not matching and not ungrouped:
        return None
    return {'groups': [align_group(key, paths) for key, paths in matching[:12]],
            'received_group_count': len(matching), 'unconfirmed_dimensions': ungrouped[:20],
            'limitations': ['Ranked positive-change candidates, not proof of complete temporal coverage or common cause.',
                            '同刻有值仅要求当前点；涨幅可比要求至少两条路径各有前一采样点和当前点。涨幅可比为0表示证据不足。',
                            'Aligned exact timestamps at the requested scale, only where previous and current values exist.',
                            'Absent, null and ambiguous values remain unknown; zero is finite.',
                            'Observation point is unconfirmed. Only endpoint names were filtered.',
                            'Window rates are background; per-minute request count requires comparable samples.']}
