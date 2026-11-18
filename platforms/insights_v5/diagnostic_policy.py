"""Bounded readonly transport policy shared by all investigation inputs."""

NATIVE_READS = frozenset(
    {
        "insights_system_version",
        "insights_list_databases",
        "insights_list_tables",
        "insights_get_tags",
        "insights_meta_metrics",
        "insights_list_cpu_events",
        "insights_k8s_resource",
        "insights_k8s_node_ids",
        "insights_query_metric",
        "insights_describe_query",
        "insights_meta_tag_values",
        "insights_query_list",
        "insights_flow_log_detail",
        "insights_app_log_detail",
    }
)


class DiagnosticAccessPolicy:
    discovery = NATIVE_READS
    role_tools = {
        "preflight": NATIVE_READS,
        "worker": NATIVE_READS,
        "triage": frozenset(),
        "planner": frozenset(),
        "supervisor": frozenset(),
    }
    published_count = 54
    missing_prefix = "diagnostic MCP capabilities missing: "
    denied_message = "read-only diagnostic tool policy denied before MCP send"

    @staticmethod
    def validate(name, arguments):
        # Native expression-bearing methods are invoked only by the bounded
        # catalog builders below; they are never exposed as free native tools.
        return None
