"""Tools and input normalization used by the investigation workflow."""


__all__ = ["TOOLS", "get_mcp_tools"]


def __getattr__(name):
    if name in __all__:
        from . import mcp_tools

        return getattr(mcp_tools, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
