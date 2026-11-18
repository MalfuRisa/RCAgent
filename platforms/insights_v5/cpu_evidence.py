"""v5-specific provenance and Node identity; never reuses old Pod/UID contracts."""
from workspace.repository import WorkspaceError, new_id, utcnow
from workspace.windows import normalize_window


def node_identity(raw, sample):
    node, cluster = raw['data']['node'], raw['data']['cluster']
    if (cluster['name'] != 'rd2' or cluster['id'] != sample['pod_cluster_id'] or
        node['id'] != sample['pod_node_id'] or node['pod_cluster_id'] != cluster['id'] or
        node['name'] != sample['pod_node'] or node.get('deleted') or cluster.get('deleted')):
        raise WorkspaceError('v5 Node/rd2 identity mismatch')
    return {'id': new_id('resource'), 'kind': 'node', 'name': node['name'], 'namespace': None,
        'cluster': cluster['name'], 'uid': None, 'platform_id': node['id'],
        'platform_lcuuid': node['lcuuid'], 'platform_cluster_id': cluster['id'],
        'identity_status': 'resolved', 'verified_at': utcnow(), 'source_refs': [],
        'candidate_resource_ids': [], 'lookup_scope': {},
        'identity_note': 'platform Node identity; lcuuid is not a Kubernetes UID'}


def validate_cpu_page(raw, arguments, target):
    data, meta = raw['data'], raw['meta']
    if not isinstance(data, dict) or not isinstance(data.get('list'), list) or type(data.get('total')) is not int:
        raise WorkspaceError('CPU list/total shape changed')
    rows = data['list']
    actual = normalize_window({'start_time': meta['effective_start_time'], 'end_time': meta['effective_end_time']})
    requested = normalize_window({k: arguments[k] for k in ('start_time', 'end_time')})
    if actual != requested or meta['page'] != arguments['page'] or meta['page_size'] != 20 or len(rows)>20:
        raise WorkspaceError('CPU window/page mismatch')
    start, end = (int(__import__('datetime').datetime.fromisoformat(actual[k]).timestamp())*10**9
                  for k in ('start_time', 'end_time'))
    for row in rows:
        if (row.get('pod_cluster') != 'rd2' or row.get('pod_cluster_id') != target['platform_cluster_id'] or
            row.get('pod_node_id') != target['platform_id']):
            raise WorkspaceError('CPU result contains foreign or unknown target')
        if any(str(row.get(k)) != arguments[k] for k in ('agent_id','pid','pod_id') if k in arguments):
            raise WorkspaceError('CPU result does not match requested platform IDs')
        for key in ('timestamp','latency_ns','latency_threshold_ns','pid','cpu'):
            if type(row.get(key)) is not int or row[key] < 0:
                raise WorkspaceError('CPU integer field invalid: '+key)
        if not start <= row['timestamp'] <= end:
            raise WorkspaceError('CPU event outside actual window')
    return actual, data['total'] > len(rows) or arguments['page'] > 1


async def save_cpu_query(b, client, args, target, tool_call_id=None):
    """Deprecated writer name; execution requires the shared configured catalog."""
    from workspace.query_execution import execute_query
    catalog = b.capability_catalog
    if catalog is None:
        raise WorkspaceError('save_cpu_query requires the unified capability catalog on the binding')
    known = await b.repository.read(b.incident_id, target['id'], 'resource')
    if any(known.get(key) != target.get(key) for key in ('kind','name','cluster','platform_id','platform_lcuuid')):
        raise WorkspaceError('CPU target does not match the registered identity')
    result = await execute_query(b, catalog, 'insights_list_cpu_events', args, tool_call_id)
    return await b.repository.read(b.incident_id, result['_rcagent_query']['evidence_id'], 'evidence')
