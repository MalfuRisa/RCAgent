"""Deprecated name: connection arguments delegate to the single graph."""
def build_cpu_graph(client,target,identity_original,discovery_source,agent_id,model=None):
    from graph import build_parent_graph
    return build_parent_graph(client=client,target=target,identity_original=identity_original,
        discovery_source=discovery_source,agent_id=agent_id,model=model)
