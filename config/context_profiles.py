"""Application composition for existing view providers; no dynamic plugin system."""

from functools import lru_cache


def context_preparers(profile):
    from context.investigation import common_preparers

    return common_preparers()


@lru_cache(maxsize=1)
def tool_projectors():
    from platforms.gateway_projections import PROJECTORS
    from platforms.insights_v5.cpu_projection import cpu_view

    return {**PROJECTORS, "insights_list_cpu_events": cpu_view}
