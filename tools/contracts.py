"""Manual semantic capabilities verified against Gateway c7c6b3e, not LLM guesses."""
import hashlib
import json

VERSION = 'gateway-c7c6b3e-v1'
SCHEMA_FINGERPRINTS = {}
POD_UID_TOOLS = {'get_pod_runtime_status', 'get_pod_network_summary', 'get_pod_application_summary', 'get_abnormal_requests', 'compare_pod_with_peers'}
REUSABLE = POD_UID_TOOLS | {'diagnose_pod'}
HAS_WINDOW = REUSABLE | {'query_metrics', 'search_traces', 'get_trace'}
REQUIRED = {name: {'cluster': 'string', 'pod_uid': 'string'} for name in POD_UID_TOOLS}
REQUIRED.update(diagnose_pod={'cluster': 'string', 'namespace': 'string', 'pod': 'string'},
    resolve_pod={'cluster': 'string', 'namespace': 'string'}, search_k8s_targets={'query': 'string'},
    get_service_backends={'cluster': 'string', 'namespace': 'string'},
    query_metrics={'metric_key': 'string', 'target_name': 'string', 'namespace': 'string'},
    search_traces={'service_name': 'string'}, get_trace={'trace_id': 'string'}, trace_request={'trace_id': 'string'})
DEFAULTS = {'get_pod_runtime_status': {}, 'get_pod_network_summary': {'direction': 'both', 'top_n': 20},
    'get_pod_application_summary': {'role': 'server', 'top_n': 20},
    'get_abnormal_requests': {'abnormal_type': '', 'sample_size': 10},
    'compare_pod_with_peers': {'metrics': ['rrt', 'error_ratio', 'retrans', 'rtt']},
    'diagnose_pod': {'symptom': '', 'scope': ['kubernetes', 'network', 'application', 'trace'], 'compare_with_peers': False}}


def fingerprint(schema):
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_schema(name, schema):
    required, properties = set(schema.get('required', [])), schema.get('properties', {})
    if name in REQUIRED and required != set(REQUIRED[name]):
        return name + ': required fields changed; disable this route until its contract is verified'
    for field, kind in REQUIRED.get(name, {}).items():
        types = properties.get(field, {}).get('type')
        if field not in required or types != kind:
            return f'{name}.{field}: expected required {kind}; tool contract incompatible'
    if name in HAS_WINDOW and any(properties.get(key, {}).get('type') != 'string' for key in ('start_time', 'end_time')):
        return name + ': missing compatible explicit window fields'
    return None


def unsupported_scope(name, args, access=None):
    required_cluster = args.get('cluster') or (access or {}).get('cluster')
    if name == 'query_metrics' and required_cluster:
        return ['cluster']
    if name == 'search_traces' and (required_cluster or args.get('namespace')):
        return [key for key, present in [('cluster', required_cluster), ('namespace', args.get('namespace'))] if present]
    return []


def effective_parameters(name, args):
    result = dict(args)
    for field, default in DEFAULTS.get(name, {}).items():
        if result.get(field) is None or result.get(field) == '' or field in ('top_n', 'sample_size') and result.get(field) == 0:
            result[field] = default
    return result
