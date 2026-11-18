"""Project task-relevant fields from the existing run's business catalog."""


def catalog_cards(outputs, spec):
    if spec.routing_family != 'business' or not spec.catalog_targets:
        return []
    source_ids = [sid for (family, tool, _), sid in outputs.preparation_cache.items()
                  if family == spec.routing_family and tool == 'insights_catalog']
    return [card for target in spec.catalog_targets
            if (card := find_dataset(outputs, reversed(source_ids), target)) is not None]


def find_dataset(outputs, source_ids, target):
    for sid in source_ids:
        source = outputs.sources.get(sid, {})
        if source.get('status') != 'ok' or source.get('mcp_source') != 'business':
            continue
        raw = outputs.materials.raw.get(sid) or {}
        data = (raw.get('structuredContent') or {}).get('data')
        if not isinstance(data, dict) or not isinstance(data.get('datasets'), list):
            continue
        for index, dataset in enumerate(data['datasets']):
            if isinstance(dataset, dict) and dataset.get('id') == target.dataset_id:
                return dataset_card(sid, index, data, dataset, target.metric_keys)
    return None


def dataset_card(sid, index, data, dataset, requested):
    metrics = [m for m in dataset.get('metrics', []) if isinstance(m, dict) and m.get('key')]
    selected = [m for m in metrics if m['key'] in requested] if requested else metrics[:6]
    return {
        'source_id': sid, 'pointer': f'/structuredContent/data/datasets/{index}',
        'dataset_id': dataset['id'], 'dataset_source': dataset.get('source'),
        'metrics': [{k: m.get(k) for k in ('key', 'unit', 'label')} for m in selected],
        'filters': [{k: f.get(k) for k in ('key', 'label')} for f in dataset.get('filters', []) if isinstance(f, dict)],
        'limits': data.get('limits'), 'metrics_received': len(metrics),
        'requested_missing': sorted(set(requested) - {m['key'] for m in selected}),
        'scope': 'Previously received catalog for this task. It does not establish live data availability.',
    }
