"""Shared readonly v5 transport composition, independent of incident wording."""

from platforms.insights_v5.client import V5Client
from platforms.insights_v5.diagnostics import DiagnosticAccessPolicy


class V5DiagnosticClient(V5Client):
    def __init__(self, url, token):
        super().__init__(url, token, policy=DiagnosticAccessPolicy())
