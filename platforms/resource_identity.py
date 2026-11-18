"""Only explicit typed run evidence may support integrated native resource IDs."""
from .insights_v5.native_task_scope import LocalRejection


def native_ids(source, raw, kind):
    data = (raw.get('structuredContent') or {}).get('data') or {}
    if not isinstance(data, dict):
        return set()
    value = data.get(kind)
    ids = {value.get('id')} if isinstance(value,dict) else set()
    tag = {'pod':'pod','node':'pod_node','cluster':'pod_cluster'}[kind]
    args = source.get('arguments') or {}
    if (args.get('body') or {}).get('tag_name') == tag:
        ids.update(r.get('value') for r in data.get('list',[]) if isinstance(r,dict))
    return {v for v in ids if isinstance(v,int) and not isinstance(v,bool) and v>0}


def validate_resource_ids(outputs, execution, name, arguments):
    if name != 'insights_k8s_resource':
        return
    for field, kind in (('pod_id','pod'),('node_id','node'),('cluster_id','cluster')):
        wanted = (arguments.get('query') or {}).get(field)
        if wanted is None:
            continue
        proven = set()
        for sid in execution.spec.context_source_ids + execution.source_ids:
            source = outputs.sources.get(sid)
            if not source or source['status'] != 'ok' or source.get('mcp_source') != 'native':
                continue
            proven |= native_ids(source, outputs.materials.raw.get(sid) or {}, kind)
        if wanted not in proven:
            raise LocalRejection('identity_evidence_required',
                f'{field} requires native typed resource/metadata evidence in this run; service IDs, same-name business records and UIDs are not interchangeable')
