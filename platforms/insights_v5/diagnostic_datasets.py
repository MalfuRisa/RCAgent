"""Bounded metric/list requests over discovered fields and verified target identities."""

import re
import uuid
from datetime import datetime
from tools.diagnostic_catalog import Capability, QueryResult
from workspace.repository import WorkspaceError
from .diagnostic_contracts import object_schema, identifier
from .diagnostic_identity import resolved


def make_dataset_capabilities(client, metadata, fresh_identity):
    capabilities = []
    common = {
        "database": {"type": "string"},
        "table": {"type": "string"},
        "resource_id": {"type": "string"},
        "page": {"type": "integer", "minimum": 1, "maximum": 20},
    }
    metric_schema = object_schema(
        {
            **common,
            "metric": {"type": "string"},
            "aggregation": {"type": "string", "enum": ["Avg", "Min", "Max", "Sum", "Count"]},
            "data_source": {"type": "string"},
        },
        ("database", "table", "resource_id", "metric", "aggregation"),
    )
    rows_schema = object_schema(
        {
            **common,
            "fields": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
        },
        ("database", "table", "resource_id", "fields"),
    )

    async def normalize_dataset(b, args):
        await fresh_identity(b, args.get("resource_id"))
        identifier(args["database"])
        identifier(args["table"])
        if "data_source" in args and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args["data_source"]):
            raise WorkspaceError("invalid data source identifier")
        await resolved(b, args["resource_id"])
        entry = metadata.get((b.incident_id, b.run_id, args["database"], args["table"]))
        if not entry:
            raise WorkspaceError(
                "describe this actual dataset with insights_get_tags / insights_meta_metrics first"
            )
        r = (await resolved(b, args["resource_id"]))[0]
        possible = {
            "node": ("pod_node_id", "pod_node_id_0", "pod_node_id_1"),
            "pod": ("pod_id", "pod_id_0", "pod_id_1"),
        }.get(r["kind"], ())
        field = next((f for f in possible if f in entry["fields"]), None)
        if field is None or not r.get("platform_id"):
            raise WorkspaceError("dataset has no verified target filter; scope cannot be dropped")
        if "metric" in args and identifier(args["metric"]) not in entry["metrics"]:
            raise WorkspaceError("metric is not present in current dataset metadata")
        if "fields" in args and any(identifier(f) not in entry["fields"] for f in args["fields"]):
            raise WorkspaceError("selected field is not present in dataset metadata")
        args.setdefault("page", 1)
        args["_scope_field"], args["_scope_id"] = field, r["platform_id"]
        return args

    def native_body(b, args, metric):
        times = [
            int(datetime.fromisoformat(b.query_window[k].replace("Z", "+00:00")).timestamp())
            for k in ("start_time", "end_time")
        ]
        selection = (
            (args["aggregation"] + "(" + args["metric"] + ")")
            if metric
            else ", ".join("`" + f + "`" for f in args["fields"])
        )
        body = {
            "database": args["database"],
            "table": args["table"],
            "time_start": times[0],
            "time_end": times[1],
            "debug": False,
            "queries": [
                {
                    "query_id": str(uuid.uuid4()),
                    "select": selection,
                    "where": "`" + args["_scope_field"] + "` = " + str(args["_scope_id"]),
                    "group_by": [],
                    "metrics": [selection] if metric else [],
                }
            ],
        }
        if "data_source" in args:
            body["data_source"] = args["data_source"]
        if metric:
            # Native metric endpoint has no list pagination. Use the bounded
            # aggregation options verified against the current v5 service.
            body.update(top=20, interval=60, window_size=60, include_history=True)
        else:
            body.update(page_index=args["page"], page_size=20, total=True)
            body["sort"] = {"order_by": "`time`", "sorted_by": "DESC"}
        return {"body": body}

    for name, is_metric, schema, description in (
        (
            "metric_series",
            True,
            metric_schema,
            "Generic bounded metric query for CPU/network/memory/storage fields actually discovered in metadata. No fabricated metric availability.",
        ),
        (
            "query_rows",
            False,
            rows_schema,
            "Read a bounded page from a discovered dataset (including network/application logs); selects verified fields, fixed target filter and run window. No free SQL.",
        ),
    ):

        async def query(b, args, _metric=is_metric):
            tool = "insights_query_metric" if _metric else "insights_query_list"
            raw = await client.call("worker", tool, native_body(b, args, _metric))
            return QueryResult(
                raw,
                [args["resource_id"]],
                None,
                truncated=True,
                data_gaps=[
                    "Backend does not echo actual window; request is bounded by contract. Results are a page, not a complete population"
                ],
            )

        capabilities.append(
            Capability(
                name,
                description,
                schema,
                normalize_dataset,
                query,
                "v5-bounded-query-v2",
                True,
                "static contract; memory/storage and successful metric response require actual platform metadata/data; errors remain gaps",
            )
        )
    return capabilities
