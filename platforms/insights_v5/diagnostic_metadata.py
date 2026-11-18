"""Discover actual dataset fields and metrics for the shared bounded query adapter."""

from tools.diagnostic_catalog import Capability, QueryResult
from .diagnostic_contracts import metadata_names


async def passthrough(b, args):
    return args


def make_metadata_capabilities(client, metadata):
    capabilities = []
    for name, description in (
        (
            "insights_list_databases",
            "Discover actual dataset databases; metadata, not fault evidence.",
        ),
        ("insights_list_tables", "Discover tables for a database; metadata, not fault evidence."),
        ("insights_get_tags", "Discover field names, units and operators for any actual table."),
        (
            "insights_meta_metrics",
            "Discover actual metric names for a dataset; use before metric_series.",
        ),
    ):
        if name not in client.schemas:
            continue

        async def invoke(b, args, _name=name):
            raw = await client.call("worker", _name, args)
            if _name in ("insights_get_tags", "insights_meta_metrics"):
                body = args if _name == "insights_get_tags" else args.get("body", {})
                db = body.get("database", body.get("db"))
                table = body.get("table")
                entry = metadata.setdefault(
                    (b.incident_id, b.run_id, db, table), {"fields": set(), "metrics": set()}
                )
                entry["fields" if _name == "insights_get_tags" else "metrics"].update(
                    metadata_names(raw)
                )
            return QueryResult(raw)

        capabilities.append(
            Capability(
                name,
                description,
                client.schemas[name]["inputSchema"],
                passthrough,
                invoke,
                "v5-native-read-v2",
            )
        )

    return capabilities
