"""Source-aware read-only context, not a cross-source entity resolver."""


def model_source_context(source):
    """Describe the observed route and server without replaying its handshake."""
    if source is None:
        return None
    result = {key: source[key] for key in ('family', 'mcp_url', 'configured_backend', 'route_basis') if key in source}
    identity = source.get('server_identity') or {}
    server = identity.get('serverInfo') or {}
    if server:
        result['server_identity'] = {'serverInfo': {key: server[key] for key in ('name', 'title', 'version')
                                                    if server.get(key) is not None}}
    return result


def model_query_context(context):
    if context is None:
        return None
    return {**context, 'source': model_source_context(context.get('source'))}


def query_context(outcome):
    raw = outcome.get('raw_result') or {}
    structured = raw.get('structuredContent') or {}
    data = structured.get('data') or {}
    if not isinstance(data, dict):
        data = {}
    meta = structured.get('meta') or {}
    return {'source':outcome.get('source_context'), 'requested':outcome.get('arguments'),
        'resolved_time':outcome.get('resolved_time') or data.get('time') or (data.get('dashboard') or {}).get('time'),
        'queryMeta':data.get('queryMeta'),
        'result_query_meta':[r.get('queryMeta') for r in (data.get('results') or []) if isinstance(r,dict)],
        'checked_query_meta':[r.get('queryMeta') for r in (data.get('checked') or []) if isinstance(r,dict)],
        'native_query_scope':meta.get('query_scope'), 'native_pagination':meta.get('pagination'),
        'resourceRefs':data.get('resourceRefs') or [],
        'paths':[{k:p.get(k) for k in ('client','server','observationPoint','resourceRefs','sourcePointer')}
                 for p in (data.get('paths') or [])],
        'limitations':['Names and numeric service IDs are not Pod identity; configured routes are not proof of shared storage.']}
