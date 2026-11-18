"""Existing Pod/network Gateway field contracts, kept separate from projection mechanics."""

from context.projection_values import (
    _text,
    _dig,
    _pick,
    _items,
    _listed,
    _representative,
    _extra_scalars,
    MAX_LIST_ITEMS,
    MAX_SAMPLE_ITEMS,
)


def _resolve_pod(raw):
    pods, total = _items(
        raw.get("pods"), ("pod_uid", "name", "namespace", "cluster", "node"), MAX_LIST_ITEMS
    )
    observations = {"ambiguous": raw.get("ambiguous")}
    if pods is not None:
        observations["candidates"] = {"candidates": pods, "shown": len(pods), "total": total}
        if len(pods) < total:
            observations["candidates"]["detail_required"] = True
    return (
        observations,
        {"ambiguous": "ambiguous", "candidates": "pods"},
        [],
        {"detail_fields": ["pods"]},
    )


def _search_targets(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("query", "query"),
            ("kind", "kind"),
            ("cluster_filter", "cluster_filter"),
            ("namespace_filter", "namespace_filter"),
            ("scanned", "scanned"),
            ("candidate_count", "candidates_by_kind"),
            ("notice", "notice"),
        ),
    )
    candidates, total = _items(
        raw.get("candidates"),
        ("kind", "name", "namespace", "cluster", "uid", "match"),
        MAX_LIST_ITEMS,
    )
    if candidates is not None:
        observations["candidates"] = {
            "candidates": candidates,
            "shown": len(candidates),
            "total": total,
        }
        if len(candidates) < total:
            observations["candidates"]["detail_required"] = True
        origin["candidates"] = "candidates"
    return observations, origin, missing, {"detail_fields": ["candidates"]}


def _service_backends(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("service", "service"),
            ("matching_basis", "matching_basis"),
            ("ready_status", "ready_status"),
            ("matching_note", "matching_note"),
            ("notice", "notice"),
        ),
    )
    pods, total = _items(
        raw.get("pods"), ("name", "namespace", "cluster", "uid", "state"), MAX_LIST_ITEMS
    )
    if pods is not None:
        observations["verified_backends"] = {"items": pods, "shown": len(pods), "total": total}
        if len(pods) < total:
            observations["verified_backends"]["detail_required"] = True
        origin["verified_backends"] = "pods"
    if isinstance(observations.get("service"), dict):
        # Identity and port summary stay brief; the full selector is detail.
        service = observations["service"]
        observations["service"] = {
            key: service.get(key)
            for key in ("name", "namespace", "cluster", "uid", "type", "cluster_ip")
            if key in service
        }
        origin["service"] = "service"
    return (
        observations,
        origin,
        missing,
        {"detail_fields": ["pods", "ports", "service.selector", "service.selector_raw"]},
    )


def _runtime_status(raw):
    observations, origin, missing = _pick(raw, (("phase", "status.phase"),))
    if "phase" not in observations and not missing:
        missing.append("status.phase")
    return observations, origin, missing, {"detail_fields": ["status", "containers", "events"]}


def _network_summary(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("rtt_avg_ms", "summary.rtt_avg_ms"),
            ("establish_fail_ratio", "summary.establish_fail_ratio"),
            ("retrans_ratio", "summary.retrans_ratio"),
            ("rst_count", "summary.rst_count"),
            ("zero_window_count", "summary.zero_window_count"),
        ),
    )
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    for key, path in (
        ("downstreams", "summary.downstreams"),
        ("upstreams", "summary.upstreams"),
        ("anomalies", "summary.anomalies"),
    ):
        block = _listed(summary.get(key), None, MAX_LIST_ITEMS)
        if block is not None:
            observations[key] = block
        elif key in summary:
            observations[key] = summary.get(key)  # explicit null stays null, not missing
        if key in summary:
            origin[key] = path
    extra, extra_origin = _extra_scalars(
        raw.get("summary"),
        {
            "rtt_avg_ms",
            "establish_fail_ratio",
            "retrans_ratio",
            "rst_count",
            "zero_window_count",
            "downstreams",
            "upstreams",
            "anomalies",
            "evidence",
        },
        "summary",
    )
    observations.update(extra)
    origin.update(extra_origin)
    return observations, origin, missing, {"detail_fields": ["summary", "samples"]}


def _application_summary(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("total_rps", "summary.total_rps"),
            ("error_ratio", "summary.error_ratio"),
            ("p95_ms", "summary.p95_ms"),
            ("p99_ms", "summary.p99_ms"),
            ("protocol_dist", "summary.protocol_dist"),
        ),
    )
    summary = raw.get("summary") if isinstance(raw.get("summary"), dict) else {}
    endpoints = summary.get("abnormal_endpoints")
    block = _listed(endpoints, None, MAX_LIST_ITEMS)
    if block is not None:
        observations["abnormal_endpoints"] = block
    elif "abnormal_endpoints" in summary:
        observations["abnormal_endpoints"] = endpoints
    if "abnormal_endpoints" in summary:
        origin["abnormal_endpoints"] = "summary.abnormal_endpoints"
    extra, extra_origin = _extra_scalars(
        raw.get("summary"),
        {
            "total_rps",
            "error_ratio",
            "p95_ms",
            "p99_ms",
            "protocol_dist",
            "abnormal_endpoints",
            "evidence",
        },
        "summary",
    )
    observations.update(extra)
    origin.update(extra_origin)
    return (
        observations,
        origin,
        missing,
        {"detail_fields": ["summary", "summary.abnormal_endpoints"]},
    )


#: Fields the real `get_abnormal_requests` return carries; a field only appears
#: when the stored row has it, and `status_code`/`error` are listed when the
#: return happens to use those names instead of `response_code`/`error_info`.
ABNORMAL_SAMPLE_FIELDS = (
    "protocol",
    "endpoint",
    "response_code",
    "response_status",
    "error_info",
    "duration_ms",
    "client",
    "server",
    "request_time",
    "trace_id",
    "status_code",
    "error",
)


def _abnormal_requests(raw):
    observations, origin, missing = {}, {}, []
    samples = raw.get("samples")
    block = _representative(
        samples, ("protocol", "response_code", "response_status"), ABNORMAL_SAMPLE_FIELDS
    )
    if block is not None:
        observations["samples"] = block
        origin["samples"] = {name: "samples[]." + name for name in ABNORMAL_SAMPLE_FIELDS}
        missing.extend(block.get("unknown_fields", []))
        if not any(isinstance(row, dict) and row.get("trace_id") for row in samples):
            observations["trace_id_gap"] = (
                "samples expose no trace_id; trace expansion is not available from these rows"
            )
            origin["trace_id_gap"] = "samples[].trace_id"
    return observations, origin, missing, {"detail_fields": ["samples"]}


def _trace_request(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("trace_id", "trace_id"),
            ("duration_ms", "duration_ms"),
            ("bottleneck_span", "bottleneck_span"),
            ("downstream_duration_ratio", "downstream_duration_ratio"),
        ),
    )
    block = _listed(raw.get("critical_path"), None, MAX_LIST_ITEMS)
    if block is not None:
        observations["critical_path"] = block
        origin["critical_path"] = "critical_path"
    return observations, origin, missing, {"detail_fields": ["critical_path", "spans"]}


def _compare_peers(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("single_pod_abnormal", "single_pod_abnormal"),
            ("scope", "scope"),
            ("metrics", "metrics"),
            ("target", "target"),
            ("peer_baseline", "peer_baseline"),
        ),
    )
    block = _listed(raw.get("diffs"), None, MAX_LIST_ITEMS)
    if block is not None:
        observations["diffs"] = block
        origin["diffs"] = "diffs"
    return observations, origin, missing, {"detail_fields": ["diffs", "peer_baseline"]}


def _diagnose_pod(raw):
    observations, origin, missing = _pick(
        raw,
        (("target", "target"), ("symptom", "symptom"), ("scope", "scope"), ("notice", "notice")),
    )
    inferences = None
    rivals = raw.get("root_cause_candidates")
    if isinstance(rivals, list):
        inferences, total = _items(rivals, ("cause", "confidence"), MAX_SAMPLE_ITEMS)
        inferences = {
            "candidates": inferences,
            "shown": len(inferences or []),
            "total": total,
            "kind": "tool_candidate_diagnosis_not_confirmed_fact",
        }
        if inferences["shown"] < total:
            inferences["detail_required"] = True
    excluded = raw.get("excluded")
    if isinstance(excluded, list):
        observations["excluded"] = _listed(excluded, None, MAX_LIST_ITEMS)
        origin["excluded"] = "excluded"
    extra, extra_origin = _extra_scalars(raw.get("summary"), set(), "summary")
    observations.update(extra)
    origin.update(extra_origin)
    return (
        observations,
        origin,
        missing,
        {
            "inferences": inferences,
            "detail_fields": ["root_cause_candidates", "recommendations", "excluded"],
        },
    )


def _series_brief(rows, limit=MAX_LIST_ITEMS):
    """Per-series brief: identity plus first/last value, never the whole series."""
    if not isinstance(rows, list):
        return None
    shown = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            shown.append(row)
            continue
        points = row.get("points") if isinstance(row.get("points"), list) else []
        entry = {"labels": row.get("labels"), "points": len(points)}
        if points:
            entry["first"] = points[0]
            entry["last"] = points[-1]
        shown.append(entry)
    block = {"series": shown, "shown": len(shown), "total": len(rows)}
    if len(shown) < len(rows):
        block["detail_required"] = True
    return block


def _query_metrics(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("metric_key", "metric_key"),
            ("unit", "unit"),
            ("series_count", "series_count"),
            ("point_count", "point_count"),
            ("notice", "notice"),
            ("window", "window"),
        ),
    )
    block = _series_brief(raw.get("series"))
    if block is not None:
        observations["series"] = block
        origin["series"] = "series"
        if not any(isinstance(row, dict) and row.get("summary") for row in raw["series"]):
            observations["series"]["aggregation"] = (
                "no_summary_returned; first/last are a marked sample"
            )
    return observations, origin, missing, {"detail_fields": ["series", "generated_query"]}


def _search_traces(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("service_tag", "service_tag"),
            ("trace_count", "trace_count"),
            ("tags_applied", "tags_applied"),
            ("window", "window"),
            ("notice", "notice"),
        ),
    )
    block = _listed(
        raw.get("traces"),
        ("trace_id", "status", "duration_ms", "start_time", "root_span"),
        MAX_LIST_ITEMS,
        "traces",
    )
    if block is not None:
        observations["traces"] = block
        origin["traces"] = "traces"
    return observations, origin, missing, {"detail_fields": ["traces"]}


def _get_trace(raw):
    observations, origin, missing = _pick(
        raw,
        (
            ("trace_id", "trace_id"),
            ("span_count", "span_count"),
            ("max_depth", "max_depth"),
            ("truncated_reason", "truncated_reason"),
        ),
    )
    path = _listed(raw.get("critical_path"), None, MAX_LIST_ITEMS, "span_ids")
    if path is not None:
        observations["critical_path"] = path
        origin["critical_path"] = "critical_path"
    spans = _listed(
        raw.get("spans"),
        ("span_id", "parent_span_id", "service", "operation", "duration_ms", "status", "depth"),
        MAX_LIST_ITEMS,
        "spans",
    )
    if spans is not None:
        observations["spans"] = spans
        origin["spans"] = "spans"
    return observations, origin, missing, {"detail_fields": ["spans", "tags"]}


PROJECTORS = {
    "resolve_pod": _resolve_pod,
    "search_k8s_targets": _search_targets,
    "get_service_backends": _service_backends,
    "get_pod_runtime_status": _runtime_status,
    "get_pod_network_summary": _network_summary,
    "get_pod_application_summary": _application_summary,
    "get_abnormal_requests": _abnormal_requests,
    "trace_request": _trace_request,
    "compare_pod_with_peers": _compare_peers,
    "diagnose_pod": _diagnose_pod,
    "query_metrics": _query_metrics,
    "search_traces": _search_traces,
    "get_trace": _get_trace,
}
