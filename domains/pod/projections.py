"""Historical imports of Gateway result field mappings; no domain workflow."""

from platforms import gateway_projections as _implementation

PROJECTORS = _implementation.PROJECTORS


def __getattr__(name):
    return getattr(_implementation, name)
