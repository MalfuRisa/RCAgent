"""Exact, bounded CPU projection with original paths; omitted rows stay readable."""

FIELDS = (
    "time",
    "timestamp",
    "latency_ns",
    "latency_threshold_ns",
    "cpu",
    "pid",
    "comm",
    "agent_id",
    "agent",
    "pod_id",
    "pod_id_0_reason",
    "pod",
    "pod_node_id",
    "pod_node",
    "pod_cluster_id",
    "pod_cluster",
    "pod_ns",
)


def cpu_view(raw):
    data, meta = raw.get("data"), raw.get("meta", {})
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        return {}, {}, ["data.list"], {}
    values, paths = {}, {}
    for i, row in enumerate(data["list"][:3]):
        for field in FIELDS:
            if field in row:
                key = f"row_{i}_{field}"
                values[key], paths[key] = row[field], f"data.list.{i}.{field}"
    if "total" in data:
        values["total"], paths["total"] = data["total"], "data.total"
    for key in ("effective_start_time", "effective_end_time", "page", "page_size"):
        if key in meta:
            values[key], paths[key] = meta[key], "meta." + key
    values["projection_note"] = {
        "shown_rows": min(3, len(data["list"])),
        "page_rows": len(data["list"]),
        "omitted_rows": max(0, len(data["list"]) - 3),
        "units": {
            "latency_ns": "nanoseconds",
            "timestamp": "Unix nanoseconds",
            "time": "backend display timezone",
        },
        "scope": "sample page, not complete population or business-latency evidence",
    }
    return values, paths, [], {"detail_fields": ["data.list", "data.total", "meta"]}
