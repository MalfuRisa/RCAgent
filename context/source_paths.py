"""Concrete projection origins; templates alone never prove shown coverage."""


def source_values(source, value, index=None):
    """Return (path, actual shown value) leaves and whether every leaf is mapped.

    A representative list uses its stored source_index, not its display order.
    Group counts and preview annotations have no direct raw origin and therefore
    make the block unproven for deletion, even when individual leaves map.
    """
    if isinstance(source, str):
        if '[]' in source:
            if index is None:
                return [], False
            source = source.replace('[]', '.' + str(index))
        return [(source, value)], True
    if isinstance(source, dict) and isinstance(value, dict):
        if isinstance(value.get('samples'), list):
            leaves = []
            for row in value['samples']:
                mapped, _ = source_values(source, row, row.get('source_index'))
                leaves.extend(mapped)
            return leaves, False  # grouping/omitted categories remain protected
        leaves, complete = [], True
        for key, item in value.items():
            if key == 'source_index':
                continue
            mapped, known = source_values(source.get(key), item, index)
            leaves.extend(mapped)
            complete = complete and known
        return leaves, complete and bool(leaves)
    if isinstance(value, list):
        leaves, complete = [], True
        for row in value:
            mapped, known = source_values(source, row, row.get('source_index') if isinstance(row, dict) else None)
            leaves.extend(mapped)
            complete = complete and known
        return leaves, complete and bool(leaves)
    return [], False
