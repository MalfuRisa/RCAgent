"""Mechanical business-result values; aggregate API output is not raw DB rows."""
import math

from platforms.business_policy import BUSINESS_READS, DATA_READS, HISTORICAL


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def observation(tool, raw):
    if tool not in BUSINESS_READS:
        return {}
    data = (raw.get('structuredContent') or {}).get('data')
    data = data if isinstance(data, dict) else {}
    values = []
    rows_count = 0
    for i,path in enumerate(data.get('paths') or []):
        metric=path.get('metric') or {}
        if number(metric.get('value')):
            values.append({'metric':metric.get('key'),'value':metric['value'],'unit':metric.get('unit'),
                'client':path.get('client'),'server':path.get('server'),'observationPoint':path.get('observationPoint'),
                'pointer':f'/structuredContent/data/paths/{i}/metric/value'})
    for i, result in enumerate(data.get('results') or []):
        units = {m['key']: m.get('unit') for m in result.get('metrics', [])}
        rows = result.get('rows', [])
        rows_count += len(rows)
        for j, row in enumerate(rows):
            for key, stats in row.get('values', {}).items():
                if isinstance(stats, dict) and any(number(stats.get(s)) for s in ('last', 'avg', 'max', 'min')):
                    values.append({'dataset': result.get('datasetId'), 'subject': row.get('label'),
                        'metric': key, 'unit': units.get(key), 'stats': stats,
                        'pointer': f'/structuredContent/data/results/{i}/rows/{j}/values/' + key.replace('~', '~0').replace('/', '~1')})
    for i, metric in enumerate(data.get('metrics') or []):
        if isinstance(metric, dict) and any(number(v) for v in (metric.get('stats') or {}).values()):
            values.append({'metric': metric.get('key'), 'unit': metric.get('unit'), 'stats': metric['stats'],
                           'pointer': f'/structuredContent/data/metrics/{i}/stats'})
    for field in ('anomalies', 'culprits'):
        for i, item in enumerate(data.get(field) or []):
            if number(item.get('value')):
                values.append({'metric': item.get('metricKey', item.get('metric')), 'value': item['value'],
                    'unit': item.get('unit', 'unknown'), 'subject': item.get('subject'),
                    'pointer': f'/structuredContent/data/{field}/{i}/value'})
    if tool not in DATA_READS:
        values = []
    return {'material_kind': 'historical' if tool in HISTORICAL else 'business' if tool in DATA_READS else 'support',
            'business_value_count': len(values), 'original_values_preview': values[:12],
            'values_preview_truncated': len(values) > 12, 'returned_aggregate_rows': rows_count,
            'resolved_time': data.get('time') or (data.get('dashboard') or {}).get('time'),
            'scope_status': data.get('scopeStatus', 'not_verified'),
            'returned_paths':len(data.get('paths') or []),
            'limitations': ['Business API aggregate only; underlying database full result and applied filters not independently verified',
                            'Original stats/unit preserved; display conversions are separate']}
