"""CPU event argument and source validation; uses the common evidence executor."""

from datetime import datetime
from tools.diagnostic_catalog import Capability, QueryResult, DiagnosticResultError
from workspace.repository import WorkspaceError
from workspace.windows import normalize_window
from .diagnostic_identity import resolved


def resource_matches_event(resource, event):
    id_field = {"node": "pod_node_id", "pod": "pod_id"}.get(resource.get("kind"))
    return bool(
        id_field
        and event.get(id_field)
        and event.get("pod_cluster") == resource.get("cluster")
        and event.get("pod_cluster_id") == resource.get("platform_cluster_id")
        and event[id_field] == resource.get("platform_id")
    )


def make_cpu_capability(client, fresh_identity):
    async def normalize_cpu(b, args):
        await fresh_identity(b)
        explicit = {k: args.get(k) for k in ("start_time", "end_time")}
        if any(explicit.values()) and normalize_window(explicit) != normalize_window(
            b.query_window
        ):
            raise WorkspaceError("query window outside current run")
        args.update(b.query_window)
        args.setdefault("page", 1)
        args.setdefault("page_size", 20)
        if type(args["page"]) is not int or not 1 <= args["page"] <= 20 or args["page_size"] != 20:
            raise WorkspaceError("bounded pages require page1..20/page_size20")
        known = await resolved(b)
        if not any(args.get(k) for k in ("pod_id", "agent_id")):
            agents = {str(v) for r in known for v in r.get("proven_agent_ids", [])}
            if len(agents) != 1:
                raise WorkspaceError("CPU query requires a verified scoped agent_id or pod_id")
            args["agent_id"] = next(iter(agents))
        for key in ("pod_id", "agent_id", "pid"):
            if key not in args:
                continue
            if (
                not isinstance(args[key], str)
                or not args[key].isascii()
                or not args[key].isdigit()
                or int(args[key]) <= 0
            ):
                raise WorkspaceError("platform IDs must be positive decimal strings")
            allowed = set()
            for r in known:
                if key == "pod_id" and r["kind"] == "pod":
                    allowed.add(str(r.get("platform_id")))
                allowed.update(str(v) for v in r.get("proven_" + key + "s", []))
            if args[key] not in allowed:
                raise WorkspaceError("platform identity has no scoped source: " + key)
        return args

    async def cpu(b, args):
        raw = await client.call("worker", "insights_list_cpu_events", args)
        try:
            return await cpu_result(b, args, raw)
        except (WorkspaceError, ValueError, KeyError, TypeError) as exc:
            raise DiagnosticResultError(raw, "CPU result validation failed: " + str(exc)) from exc

    async def cpu_result(b, args, raw):
        known = await resolved(b)
        rows = raw.get("data", {}).get("list")
        if not isinstance(rows, list) or len(rows) > 20:
            raise WorkspaceError("CPU result list shape/size changed")
        meta = raw["meta"]
        actual = normalize_window(
            {"start_time": meta["effective_start_time"], "end_time": meta["effective_end_time"]}
        )
        start, end = [
            int(datetime.fromisoformat(actual[k]).timestamp()) * 10**9
            for k in ("start_time", "end_time")
        ]
        ids = set()
        for row in rows:
            matching = [r for r in known if resource_matches_event(r, row)]
            if not matching:
                raise WorkspaceError("CPU response contains unverified/out-of-scope target")
            ids.update(r["id"] for r in matching)
            if any(str(row.get(k)) != args[k] for k in ("pod_id", "agent_id", "pid") if k in args):
                raise WorkspaceError("CPU result filter mismatch")
            for field in ("timestamp", "latency_ns", "latency_threshold_ns", "pid", "cpu"):
                if type(row.get(field)) is not int or row[field] < 0:
                    raise WorkspaceError("CPU integer precision/type invalid")
            if not start <= row["timestamp"] <= end:
                raise WorkspaceError("CPU event outside actual window")
        if (
            actual != normalize_window(b.query_window)
            or meta["page"] != args["page"]
            or meta["page_size"] != args["page_size"]
        ):
            raise WorkspaceError("CPU actual window/page mismatch")
        if not ids:
            ids = {r["id"] for r in known}
        total = raw["data"].get("total")
        if type(total) is not int:
            raise WorkspaceError("CPU total is not an integer")
        return QueryResult(
            raw,
            sorted(ids),
            actual,
            truncated=total > len(rows) or args["page"] > 1,
            data_gaps=[
                "CPU scheduling events do not measure business latency or establish causality"
            ],
        )

    return Capability(
        "insights_list_cpu_events",
        "Read CPU scheduling events, with exact nanosecond values. No utilization/business impact proof. Fixed run window; page_size20.",
        client.schemas["insights_list_cpu_events"]["inputSchema"],
        normalize_cpu,
        cpu,
        "v5-cpu-native-v2",
        True,
    )
