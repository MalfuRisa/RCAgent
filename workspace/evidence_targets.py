"""Scoped evidence attribution, shared by query validation and persistence."""


def query_matches(resource, name, args, kind):
    if resource.get('replaced_by') or resource.get('identity_status') != 'resolved':
        return False
    if resource.get('cluster') != args.get('cluster') or (kind and resource['kind'] != kind):
        return False
    namespace = args.get('namespace')
    if namespace and resource.get('namespace') != namespace:
        return False
    uid = args.get('pod_uid') or args.get('service_uid')
    if uid:
        return uid == resource.get('uid')
    target = args.get('pod') if name == 'diagnose_pod' else args.get('target_name') or args.get('service_name')
    return bool(target and namespace and target == resource['name'] and namespace == resource.get('namespace'))


def inventory_matches(resource, candidate, args, access):
    kind = str(candidate.get('kind', 'pod')).lower()
    cluster = candidate.get('cluster') or args.get('cluster')
    namespace = candidate.get('namespace')
    uid = candidate.get('pod_uid') or candidate.get('service_uid') or candidate.get('uid')
    if (resource.get('replaced_by') or cluster != access['cluster'] or not namespace or
            ('*' not in access['namespaces'] and namespace not in access['namespaces'])):
        return False
    if args.get('namespace') and namespace != args['namespace']:
        return False
    if resource['kind'] != kind or resource['name'] != (candidate.get('name') or candidate.get('pod_name')):
        return False
    if resource.get('cluster') and resource['cluster'] != cluster:
        return False
    if resource.get('namespace') and resource['namespace'] != namespace:
        return False
    if resource.get('uid') and resource['uid'] != uid:
        return False
    return True


def evidence_targets(resources, name, args, kind, inventory, access):
    matched = [r['id'] for r in resources if query_matches(r, name, args, kind)]
    if name in ('resolve_pod', 'search_k8s_targets', 'get_service_backends'):
        matched.extend(r['id'] for r in resources if any(inventory_matches(r, c, args, access) for c in inventory))
    return list(dict.fromkeys(matched))
