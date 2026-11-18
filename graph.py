"""Product entry: RCAgent owns the default investigation loop."""
from workflow.investigation import build_investigation_graph


def build_parent_graph(*, runtime="rcagent", runtime_factory=None, **legacy_options):
    if runtime == "legacy":
        from workflow.legacy_investigation import (
            build_investigation_graph as build_legacy,
        )
        return build_legacy(**legacy_options)
    if runtime != "rcagent":
        raise ValueError("Unknown investigation runtime: " + str(runtime))
    if legacy_options:
        raise ValueError("Legacy model/catalog/client injection requires explicit runtime='legacy'; native MCP is configured by environment")
    return build_investigation_graph(runtime_factory=runtime_factory)


def evidence_gate(*args, **kwargs):
    """Explicit historical compatibility; never called by the default graph."""
    from workflow.gate import evidence_gate as legacy_gate
    return legacy_gate(*args, **kwargs)


parent_graph = build_parent_graph()
