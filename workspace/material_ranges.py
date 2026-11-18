"""Exact range coverage; a tail page never proves the rest of a source."""
import json
from .repository import WorkspaceError


def checked_span(value):
    if (not isinstance(value, list) or len(value) != 2 or
            any(type(n) is not int for n in value) or not 0 <= value[0] <= value[1]):
        raise WorkspaceError('range must be [start,end] with nonnegative integer positions')
    return value


def slice_value(node, span, unit):
    start, end = checked_span(span)
    if unit == 'chars':
        text = json.dumps(node, ensure_ascii=False)
        if end > len(text):
            raise WorkspaceError('character range exceeds serialized field')
        return text[start:end], len(text)
    if unit != 'items':
        raise WorkspaceError('range_unit must be items or chars')
    rows = node if isinstance(node, list) else [node]
    if end > len(rows):
        raise WorkspaceError('item range exceeds field')
    return rows[start:end], len(rows)


def covers_spans(spans, target):
    """Union only actual intervals, retaining holes and half-open boundaries."""
    start, end = checked_span(target)
    cursor = start
    for left, right in sorted(spans):
        if left > cursor:
            break
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return cursor >= end


def pointer_field(pointer):
    from .observations import pointer_tokens
    tokens = pointer_tokens(pointer)
    # Dotted read paths cannot represent an empty key or a key containing a
    # dot. Never let either alias the root or a different nested field.
    return None if any(not token or '.' in token for token in tokens) else '.'.join(tokens)


def observation_covers_field(observation, source_id, version, field):
    ref = observation.get('source_ref') or {}
    return (source_id in (ref.get('evidence_id'), ref.get('artifact_id'))
            and ref.get('source_revision') == version
            and isinstance(ref.get('json_pointer'), str)
            and pointer_field(ref['json_pointer']) is not None
            and (field or '') == pointer_field(ref['json_pointer']))


def replacement_covers(observations, source_id, version, field, span=None, unit=None):
    matched = [o for o in observations if observation_covers_field(o, source_id, version, field)]
    if not matched:
        return False
    if any((o.get('source_ref') or {}).get('range') is None for o in matched):
        return True
    if span is None or unit not in ('items', 'chars'):
        return False
    intervals = [(o['source_ref']['range'][0], o['source_ref']['range'][1]) for o in matched
                 if o['source_ref'].get('range_unit') == unit]
    return covers_spans(intervals, span)


def disposition_covers(disposition, receipt):
    if (disposition.get('source_id'), disposition.get('source_version'), disposition.get('field') or '') != (
            receipt.get('source_id'), receipt.get('source_revision'), receipt.get('field') or ''):
        return False
    span = disposition.get('range')
    if span is None:
        return True  # validated full-field observation
    target = receipt.get('range')
    return (target is not None and disposition.get('range_unit') == receipt.get('range_unit')
            and covers_spans([span], target))
