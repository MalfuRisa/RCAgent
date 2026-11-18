"""Deprecated node aliases use the native product flow unless replay is explicit."""


async def invoke_role(state, role, *, runtime="rcagent"):
    if runtime == "legacy":
        from .legacy_role_compat import invoke_role as invoke_legacy
        return await invoke_legacy(state, role)
    if runtime != "rcagent":
        raise ValueError("Unknown runtime: " + str(runtime))
    if state.get("final_report", {}).get("runtime") in {"rcagent", "opencode"}:
        # Old composite callers may visit several deprecated node aliases.
        # Do not start another investigation after the same result was produced.
        return state
    from .investigation import build_investigation_graph
    return await build_investigation_graph().ainvoke(state)
