"""Domain-neutral brief projection and unknown-result behavior."""
from config.context_profiles import tool_projectors
from .projection_values import (_gaps, _result, MAX_LIST_ITEMS, MAX_SAMPLE_ITEMS,
    GROUP_SUMMARY_LIMIT, TEXT_LIMIT, PREVIEW_CHARS, EXTRA_SCALAR_LIMIT)


def project(tool_name, raw):
    """Return the brief projection of one tool response.

    Shape: ``observations`` (values), ``source`` (path per value),
    ``unknown_fields``, ``inferences`` (Tool-supplied candidates, explicitly not
    confirmed facts), ``result``, ``limitations`` and ``detail_fields``.
    """
    if not isinstance(raw, dict):
        warnings, gaps = _gaps({})
        return {'unknown_shape': True, 'detail_required': True, 'observations': {}, 'source': {},
                'unknown_fields': ['<non-object response>'], 'detail_fields': [],
                'warnings': warnings, 'data_gaps': gaps, 'result': _result({})}
    warnings, gaps = _gaps(raw)
    result = _result(raw)
    projector = tool_projectors().get(tool_name)
    if projector is None:
        # Unrecognised shape: keep status/limitations/top-level directory only.
        return {'unknown_shape': True, 'detail_required': True, 'observations': {}, 'source': {},
                'unknown_fields': [], 'detail_fields': sorted(raw.keys()),
                'warnings': warnings, 'data_gaps': gaps, 'result': result}
    observations, origin, missing, extra = projector(raw)
    if not observations:
        # The tool is known but none of its anchors are present: treat the shape
        # as unrecognised rather than inventing an empty success (§6.4.5).
        return {'unknown_shape': True, 'detail_required': True, 'observations': {}, 'source': {},
                'unknown_fields': list(missing), 'detail_fields': sorted(raw.keys()),
                'warnings': warnings, 'data_gaps': gaps, 'result': result}
    projection = {'observations': observations, 'source': origin, 'unknown_fields': missing,
                  'result': result, 'warnings': warnings, 'data_gaps': gaps,
                  'detail_fields': extra.pop('detail_fields', [])}
    projection.update(extra)
    details = [key for key in ('status', 'truncated', 'freshness') if result.get(key) is None]
    if details:
        # Transport/source status is authoritative elsewhere; only record that the
        # response itself did not state it.
        projection['unknown_fields'].extend('result.' + key for key in details)
    return projection



def __getattr__(name):
    if name == 'PROJECTORS':
        return tool_projectors()
    raise AttributeError(name)
