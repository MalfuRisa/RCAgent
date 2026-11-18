"""Historical CPU client entry point; binds the domain policy to v5 transport."""

from platforms.insights_v5.diagnostics import DiagnosticAccessPolicy

DISCOVERY = DiagnosticAccessPolicy.discovery
ROLE_TOOLS = DiagnosticAccessPolicy.role_tools
from platforms.insights_v5.cpu_contract import CONTRACT, cpu_arguments
from platforms.insights_v5.client import decode_result
from tools.diagnostic_client import V5DiagnosticClient


V5CPUClient = V5DiagnosticClient
