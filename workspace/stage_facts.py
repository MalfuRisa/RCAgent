"""Mechanical handoff for known business responses already archived by the run."""

from .tool_projection import content_view

COLLECTIONS = {
    'insights_list_paths': 'paths',
    'insights_query_metrics': 'results',
    'insights_generate_dashboard': 'results',
    'insights_metric_history': 'metrics',
    'insights_find_anomalies': 'checked',
}
DATA = '/structuredContent/data'


def collection(data, field):
    items = data.get(field)
    return items if isinstance(items, list) and all(isinstance(item, dict) for item in items) else None


def meta_view(meta):
    meta = meta if isinstance(meta, dict) else {}
    constructed = meta.get('constructed') or {}
    return {key: meta.get(key) for key in ('coverage', 'comparedStat', 'unknowns', 'warnings', 'backendReported')} | {
        'constructed_requests': [{key: request.get(key) for key in ('source', 'interval', 'dataSource', 'orderBy')}
                                 for request in (constructed.get('requests') or [])[:3]],
        'constructed_filters': constructed.get('appliedFilters'),
    }


def fact_source(source, raw):
    tool = source['tool_name']
    field = COLLECTIONS.get(tool)
    if field is None:
        return None
    sid = source['source_id']
    fact = {'source_id': sid, 'tool': tool, 'status': source['status'], 'scope': source.get('arguments'),
            'window': source.get('resolved_time'), 'source_family': source.get('mcp_source'),
            'error': source.get('error'), 'read_entry': {'source_id': sid, 'pointer': ''}}
    raw = raw or {}
    structured = raw.get('structuredContent')
    omissions = []
    independent = content_view(raw, structured, omissions)
    if independent:
        fact['independent_content'] = independent
    if omissions:
        fact['content_omissions'] = omissions
    if source['status'] != 'ok':
        return fact
    data = structured.get('data') if isinstance(structured, dict) else None
    if not isinstance(data, dict) or (items := collection(data, field)) is None:
        return None
    fact.update(subject=data.get('subject'), scope_status=data.get('scopeStatus'), upstream_gap=data.get('error'),
                gaps=data.get('gaps'), result_warnings=data.get('warnings'), **meta_view(data.get('queryMeta')))
    if field == 'results':
        panels = query_panels(items)
        if panels is None:
            return None
        fact['panels'] = panels
    elif field == 'checked':
        anomalies = collection(data, 'anomalies')
        if anomalies is None:
            return None
        fact['checked'] = [{k: check.get(k) for k in ('datasetId', 'metricKey', 'rows', 'comparedStat', 'status')}
                           for check in items[:16]]
        fact['anomalies'] = anomalies[:6]
        fact['anomalies_received'] = len(anomalies)
    elif field == 'paths':
        if any(not isinstance(path.get('metric'), dict) or 'client' not in path or 'server' not in path for path in items):
            return None
        fact['paths'] = [{k: path.get(k) for k in ('client', 'server', 'metric', 'observationPoint', 'resourceRefs')}
                         | {'pointer': f'{DATA}/paths/{index}'} for index, path in enumerate(items[:10])]
    else:
        if any(not metric.get('key') or not isinstance(metric.get('stats'), dict) for metric in items):
            return None
        fact['metrics'] = [{k: metric.get(k) for k in ('key', 'unit', 'stats', 'peakAt')}
                           | {'pointer': f'{DATA}/metrics/{index}'} for index, metric in enumerate(items[:4])]
        fact['observation_point'] = 'unknown; history filters the two names, not observation points'
        fact['points_pointer'] = f'{DATA}/charts' if isinstance(data.get('charts'), list) else None
    fact.update(received_items=len(items), collection_pointer=f'{DATA}/{field}')
    return fact


def query_panels(results):
    panels = []
    for index, panel in enumerate(results[:4]):
        rows = collection(panel, 'rows')
        if rows is None or collection(panel, 'metrics') is None or any(not isinstance(row.get('values'), dict) for row in rows):
            return None
        pointer = f'{DATA}/results/{index}'
        panels.append({
            'dataset_id': panel.get('datasetId'), 'metrics': [{k: m.get(k) for k in ('key', 'unit')} for m in panel['metrics']],
            'rows': [{'subject': row.get('label'), 'values': row.get('values'), 'pointer': f'{pointer}/rows/{j}'}
                     for j, row in enumerate(rows[:10])],
            'rows_received': len(rows), 'pointer': pointer, **meta_view(panel.get('queryMeta')),
            'chart_entries': chart_entries(panel.get('charts'), pointer),
        })
    return panels


def chart_entries(charts, panel_pointer):
    # Record availability rather than copying the chart's time series into handoff.
    return [{'metric': chart.get('metricKey'), 'unit': chart.get('unit'),
             'series_count': len(chart.get('series') or []), 'pointer': f'{panel_pointer}/charts/{index}'}
            for index, chart in enumerate((charts or [])[:4]) if isinstance(chart, dict)]


def program_handoff(outputs, execution):
    if execution.spec.return_mode != 'first_result':
        return None, 'answer_uses_worker_note'
    if execution.spec.handoff_mode == 'worker_note':
        return None, 'worker_note_requested'
    scope = execution.spec.target_tools
    facts, unrecognized = [], []
    for sid in dict.fromkeys(execution.source_ids):
        source = outputs.sources.get(sid)
        if not source or source['tool_name'] not in scope:
            continue
        fact = fact_source(source, outputs.materials.raw.get(sid))
        if fact is None:
            unrecognized.append({
                'source_id': sid, 'tool': source['tool_name'], 'status': source['status'],
                'error': source.get('error'), 'read_entry': {'source_id': sid, 'pointer': ''},
            })
            continue
        facts.append(fact)
    if not facts:
        return None, 'unrecognized_or_semantic_target'
    reason = 'known_structured_results' if not unrecognized else 'partial_structured_results'
    return {
        'window_kind': execution.window_kind,
        'window': execution.spec.window.card() if execution.spec.window else None,
        'results': facts[:8], 'result_count': len(facts),
        'unrecognized_sources': unrecognized[:6], 'unrecognized_count': len(unrecognized),
        'notice': 'Mechanical facts sampled from archived responses; omitted rows, points and results remain readable by source_id.',
    }, reason
