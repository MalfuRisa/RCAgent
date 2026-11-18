"""One native capability classification for model binding and send permission."""
from platforms.insights_v5.diagnostic_policy import NATIVE_READS
from platforms.business_policy import BUSINESS_READS, SUPPORT, is_business

SUPPORT_TOOLS = frozenset('insights_' + name for name in (
    'list_databases', 'list_tables', 'get_tags', 'meta_metrics', 'meta_tag_values',
    'describe_query', 'system_version'))
RESOURCE_TOOLS = frozenset({'insights_k8s_resource', 'insights_k8s_node_ids'})
ENTRY_SOURCES = {'dispatch_business': 'business', 'dispatch_native': 'native'}
SOURCE_ENTRIES = {source: entry for entry, source in ENTRY_SOURCES.items()}


def entry_name(source):
    return SOURCE_ENTRIES.get(source)


def source_reads(source):
    return BUSINESS_READS if source == 'business' else NATIVE_READS


def advertised_tools(source, discovered):
    """The exact tools this entry exposes and accepts in the current run."""
    return sorted(set(discovered) & source_reads(source))


def effective_tools(selected, discovered):
    source = task_source(selected, discovered)
    if source == 'business':
        return (SUPPORT | set(selected)) & set(discovered) & BUSINESS_READS
    if source == 'native':
        return (SUPPORT_TOOLS | set(selected)) & set(discovered) & NATIVE_READS
    return set()


def tool_source(name):
    if name in BUSINESS_READS:
        return 'business'
    if name in NATIVE_READS:
        return 'native'
    return None


def task_source(selected, discovered):
    selected_sources = {tool_source(n) for n in selected if tool_source(n)}
    if len(selected_sources)>1:
        raise ValueError('One source family per task; ask Planner for a new task to switch source')
    if selected_sources:
        return selected_sources.pop()
    available = {tool_source(n) for n in discovered if tool_source(n)}
    return available.pop() if len(available)==1 else None


def normalize_proposal(raw):
    """Only legacy proposals keep their old implicit resource capabilities."""
    if not isinstance(raw, dict):
        return raw
    result = dict(raw)
    if any(k in result for k in ('legacy_default','contract_version','routing_family')):
        raise ValueError("Internal contract fields cannot be supplied by the model")
    legacy = result.pop('business_tools', None)
    if legacy is not None:
        if 'tools' in result and set(result['tools']) != set(legacy):
            raise ValueError('/tools conflicts with /business_tools; use tools only')
        result['tools'] = legacy
        result['legacy_default'] = sorted(RESOURCE_TOOLS)
    if result.get('return_mode') == 'business_batch':
        result['return_mode'] = 'first_result'
    return result
