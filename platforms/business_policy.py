"""Business profile contracts, independent from the native14 query API."""

BUSINESS_READS = frozenset('insights_' + n for n in (
    'status', 'catalog', 'find_anomalies', 'query_metrics', 'metric_history',
    'drill_system', 'generate_dashboard', 'kb_search', 'list_briefs', 'get_brief',
    'list_reports', 'get_report', 'conclude', 'list_paths'))
DATA_READS = frozenset('insights_' + n for n in (
    'find_anomalies', 'query_metrics', 'metric_history', 'drill_system', 'generate_dashboard', 'list_paths'))
SUPPORT = frozenset({'insights_status', 'insights_catalog'})
HISTORICAL = frozenset('insights_' + n for n in ('list_briefs', 'get_brief', 'list_reports', 'get_report'))


def is_business(discovered):
    return bool(discovered) and set(discovered) <= BUSINESS_READS


def resolved_time(raw):
    data = (raw.get('structuredContent') or {}).get('data')
    if not isinstance(data, dict):
        return None
    return data.get('time') or (data.get('dashboard') or {}).get('time')


def assigned_time(context, name, arguments, schema):
    """Fill only an omitted business time that the discovered contract accepts."""
    if not context or not context.spec.window or name not in DATA_READS or 'time' in arguments:
        return arguments
    fields = schema.get('properties', {}).get('time', {}).get('properties', {})
    if any(fields.get(k, {}).get('type') != 'integer' for k in ('start', 'end')):
        return arguments
    return {**arguments, 'time': {'start': context.spec.window.start, 'end': context.spec.window.end}}


def check_business_scope(context, name, arguments):
    from platforms.insights_v5.native_task_scope import LocalRejection
    def reject(message):
        raise LocalRejection('business_scope_required', message)
    if name not in context.spec.effective_tools(context.discovered_tools):
        reject('Tool outside assigned business capability')
    if name not in DATA_READS:
        return
    if name == 'insights_generate_dashboard' and arguments.get('scopePolicy') != 'require-target':
        reject('RCA business calls require scopePolicy=require-target; never fall back to global panels')
    def targeted(filters):
        return any(f.get('op') in {'eq', 'in'} and f.get('values') for f in filters)
    if name == 'insights_query_metrics' and any(not targeted(p.get('filters', [])) for p in arguments.get('plans', [])):
        reject('Each business metric plan needs explicit target filters')
    if name in {'insights_find_anomalies','insights_list_paths'} and not targeted(arguments.get('filters', [])):
        reject(f'{name} requires a positive eq/in filter for the user-specified or observed target')
    if name in {'insights_metric_history', 'insights_drill_system'} and not any(arguments.get(k) for k in ('node', 'client', 'server')):
        reject('History/drill needs an explicit node or path target')
    if context.spec.window:
        requested = arguments.get('time') or {}
        if (requested.get('start'), requested.get('end')) != (context.spec.window.start, context.spec.window.end):
            reject(f'Use assigned absolute time={{start:{context.spec.window.start},end:{context.spec.window.end}}}')
