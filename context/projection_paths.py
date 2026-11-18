"""Exact paths through known projection wrappers, never value-based search.

Mappings are derived from the source map in an actually visible card. Synthetic
counts/previews are not source facts. Representative rows use their original
index; ordinary list wrappers preserve order. Consumers still verify versions,
visibility and values against the immutable original.
"""


def pointer(tokens):
    return ''.join('/' + str(token).replace('~', '~0').replace('/', '~1') for token in tokens)


def _nodes(value, display, original):
    if isinstance(value, dict):
        if value.get('preview_incomplete'):
            return
        # These wrappers are produced by the fixed projectors. Their counters
        # and grouping metadata have no source field of their own.
        if 'shown' in value and 'total' in value:
            for key in ('items', 'candidates', 'traces', 'span_ids', 'spans'):
                rows = value.get(key)
                if isinstance(rows, list):
                    if value.get('shown') == value.get('total') == len(rows):
                        yield display + (key,), original, rows
                    for index, item in enumerate(rows):
                        yield from _nodes(item, display + (key, str(index)), original + (str(index),))
                    return
            return  # e.g. metric-series summaries need a raw detail read
        yield display, original, value
        for key, item in value.items():
            yield from _nodes(item, display + (key,), original + (key,))
    elif isinstance(value, list):
        yield display, original, value
        for index, item in enumerate(value):
            yield from _nodes(item, display + (str(index),), original + (str(index),))
    else:
        yield display, original, value


def _mapped(source, value, display, source_index=None):
    if isinstance(source, str):
        if '[]' in source:
            if source_index is None:
                return
            source = source.replace('[]', '.' + str(source_index))
        yield from _nodes(value, display, tuple(source.split('.')))
    elif isinstance(source, dict) and isinstance(value, dict):
        if isinstance(value.get('samples'), list):
            for index, item in enumerate(value['samples']):
                original_index = item.get('source_index')
                if type(original_index) is int and original_index >= 0:
                    yield from _mapped(source, item, display + ('samples', str(index)), original_index)
            return
        for key, item in value.items():
            if key != 'source_index':
                yield from _mapped(source.get(key), item, display + (key,), source_index)


def card_paths(card):
    """Yield (allowed aliases, original pointer, shown value) for mapped nodes."""
    origins = card.get('source') or {}
    for key, value in (card.get('observations') or {}).items():
        source = origins.get(key)
        for display, original, shown in _mapped(source, value, (key,)):
            canonical = pointer(original)
            aliases = {canonical, pointer(display), pointer(('observations',) + display)}
            # A model may combine the documented raw base with the displayed
            # suffix. This alias is generated only for this mapped block.
            if isinstance(source, str) and '[]' not in source:
                aliases.add(pointer(tuple(source.split('.')) + display[1:]))
            yield aliases, canonical, shown


def resolve_card_pointer(card, submitted):
    candidates = {canonical for aliases, canonical, _value in card_paths(card) if submitted in aliases}
    return next(iter(candidates)) if len(candidates) == 1 else None
