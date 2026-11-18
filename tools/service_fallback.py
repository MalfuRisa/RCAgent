"""Bounded Pod summaries with honest Service coverage, no broader queries."""
from config import settings
from workspace.runtime import current
from workspace.resources import register, ensure_identity, incomplete


def error(raw):
    return not isinstance(raw, dict) or raw.get('error') or raw.get('error_type') or raw.get('status') in ('error', 'not_found')


async def coarse_pods(outcome, candidates, call, cluster, namespace):
    coverage = {'selected': [], 'checked': [], 'failed': [], 'unchecked': [],
                'inventory_complete': not incomplete(outcome.backends or {}),
                'ready_and_actual_traffic_unknown': True}
    binding = current.get()
    unique = {}
    for pod in candidates:
        ns = pod.get('namespace') or namespace
        pod_cluster = pod.get('cluster') or cluster
        name = pod.get('name') or pod.get('pod_name')
        uid = pod.get('pod_uid') or pod.get('uid')
        key = (ns, name, uid)
        if not name or not uid or pod_cluster != cluster or ns != namespace:
            coverage['failed'].append({'pod': pod, 'reason': 'missing UID/name or scope mismatch'})
            continue
        unique[key] = dict(pod, namespace=ns, cluster=pod_cluster, name=name, uid=uid)
    by_name = {}
    for key in unique:
        by_name.setdefault(key[:2], []).append(key)
    safe = [pod for key, pod in unique.items() if len(by_name[key[:2]]) == 1]
    coverage['failed'].extend({'pod': pod, 'reason': 'ambiguous same-name candidate UIDs'} for key, pod in unique.items() if len(by_name[key[:2]]) != 1)
    safe.sort(key=lambda p: (p['namespace'], p['name'], p['uid']))
    selected = safe[:settings.SERVICE_FALLBACK_MAX_PODS]
    coverage['selected'], coverage['unchecked'] = selected, safe[settings.SERVICE_FALLBACK_MAX_PODS:]
    for pod in selected:
        verified = pod
        if binding:
            resource = await register(binding, 'pod', pod['name'], pod['namespace'])
            identity = await ensure_identity(binding, resource['id'], force=not coverage['inventory_complete'])
            if identity['status'] != 'resolved' or identity['resource']['uid'] != pod['uid']:
                coverage['failed'].append({'pod': pod, 'reason': 'identity unresolved or inventory UID changed', 'identity': identity})
                continue
            verified = identity['resource']
        elif not coverage['inventory_complete']:
            raw = await call('resolve_pod', {'cluster': cluster, 'namespace': pod['namespace'], 'pod': pod['name']})
            actual = raw.get('pods') or []
            if error(raw) or incomplete(raw) or len(actual) != 1 or (actual[0].get('pod_uid') or actual[0].get('uid')) != pod['uid'] or actual[0].get('namespace') != pod['namespace'] or actual[0].get('cluster', cluster) != cluster or actual[0].get('name') != pod['name']:
                coverage['failed'].append({'pod': pod, 'reason': 'partial inventory candidate could not be independently verified'})
                continue
        observations, refs = {}, []
        for tool in ('get_pod_runtime_status', 'get_pod_network_summary', 'get_pod_application_summary'):
            outcome.tool_calls.append(tool)
            raw = await call(tool, {'cluster': cluster, 'pod_uid': verified['uid']})
            observations[tool] = raw
            if raw.get('_rcagent_query', {}).get('evidence_id'):
                refs.append(raw['_rcagent_query']['evidence_id'])
            if error(raw):
                outcome.add_gap('mcp_tool:' + tool, str(raw.get('error_type') or 'query_failed'),
                                f"{pod['namespace']}/{pod['name']}: Pod summary failed", severity='missing')
        item = {'pod': pod, 'resource_id': verified.get('id'), 'evidence_ids': refs, 'observations': observations}
        if any(not error(raw) for raw in observations.values()):
            coverage['checked'].append(item)
            outcome.add_fact('mcp_tool:pod_fallback', f"Summary observations for {pod['namespace']}/{pod['name']}; not Service-wide metrics", evidence=item)
        else:
            coverage['failed'].append(item)
    outcome.coverage = coverage
    if not candidates:
        outcome.add_gap('get_service_backends', 'empty_backend_inventory', 'No backend candidates; Service identity was found, observations unavailable')
    if coverage['unchecked'] or not coverage['inventory_complete'] or coverage['failed']:
        outcome.add_gap('pod_fallback', 'partial_service_coverage', 'Only checked Pods were observed; inventory/unchecked/failed Pods prevent Service-wide conclusions')
    outcome.evidence_status = 'partial' if coverage['checked'] else 'unavailable'
    outcome.status = 'partial'
    return outcome
