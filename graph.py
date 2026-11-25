"""RCAgent product graph."""
from workflow.investigation import build_investigation_graph


def build_parent_graph(*, runtime="rcagent", runtime_factory=None):
    if runtime != "rcagent":
        raise ValueError(f"Unknown investigation runtime: {runtime}")
    return build_investigation_graph(runtime_factory=runtime_factory)


parent_graph = build_parent_graph()
