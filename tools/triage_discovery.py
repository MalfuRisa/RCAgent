"""Triage target discovery for natural-language incidents.

Given a Service name extracted from an operator's message, this module performs
the PRD §7 discovery sequence through the MCP Gateway only:

    1. ``search_k8s_targets``   -- who did the operator mean?
    2. ``get_service_backends`` -- which Pods does that Service claim?
    3. ``query_metrics``        -- is the anomaly service-wide or Pod-scoped?
    4. ``search_traces``        -- which requests are actually failing?
    5. ``get_trace``            -- optional: expand one failing trace.

Hard rules enforced here:

* Multiple equally valid matches end in ``ambiguous`` -- the first candidate is
  **never** silently selected, and no Pod is investigated.
* Missing observations are reported as :mod:`data_gaps`. An empty metric set or
  an empty trace set is never reported as "the service is healthy".
* Facts returned by the Gateway are recorded separately from statements this
  module derives (thresholding, attribution), so a report can tell them apart.

The module is deliberately import-light so it can be unit-tested with an
injected ``call_tool`` and no Gateway, credentials or model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional

from pydantic import BaseModel, Field

from .incident_text import (
    SYMPTOM_LATENCY,
    SYMPTOM_TIMEOUT,
    UNKNOWN,
    IncidentScope,
    resolve_incident_scope,
)

#: A Pod series is called out as a suspected fault domain at or above this
#: error ratio. This is an *agent* rule, not something the Gateway reported.
DEFAULT_ANOMALY_THRESHOLD = 0.1
#: ...or when it is at least this many times the median of its peer Pods.
DEFAULT_PEER_RATIO = 3.0

#: Metrics Triage asks for. ``query_metrics`` only accepts known templates;
#: the agent never sends PromQL.
BASE_METRIC_KEYS = ("error_rate",)
LATENCY_METRIC_KEYS = ("latency_p95",)

DISCOVERY_OK = "ok"
DISCOVERY_AMBIGUOUS = "ambiguous"
DISCOVERY_NOT_FOUND = "not_found"
DISCOVERY_PARTIAL = "partial"
DISCOVERY_INSUFFICIENT = "insufficient"
DISCOVERY_ERROR = "error"

ToolCaller = Callable[[str, Mapping[str, Any]], Awaitable[dict[str, Any]]]


def _default_caller() -> ToolCaller:
    """Resolve the adapter lazily so tests need no Gateway and no credentials."""
    from .observability_adapter import call_gateway_tool

    return call_gateway_tool


class DiscoveryOutcome(BaseModel):
    """Everything Triage learned, split by epistemic status."""

    status: str = DISCOVERY_INSUFFICIENT
    evidence_status: str = 'unavailable'
    coverage: dict = Field(default_factory=dict)
    skipped_tools: list[dict] = Field(default_factory=list)
    scope: IncidentScope = Field(default_factory=IncidentScope)
    requires_confirmation: bool = False
    clarification: dict[str, Any] = Field(default_factory=dict)

    #: Statements taken from Gateway responses (Mock facts in local testing).
    facts: list[dict[str, Any]] = Field(default_factory=list)
    #: Statements derived here from those facts (agent inference).
    inferences: list[dict[str, Any]] = Field(default_factory=list)
    #: Observations that could not be made, with the reason and impact.
    data_gaps: list[dict[str, Any]] = Field(default_factory=list)

    candidates: list[dict[str, Any]] = Field(default_factory=list)
    service: Optional[dict[str, Any]] = None
    backends: Optional[dict[str, Any]] = None
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    traces: Optional[dict[str, Any]] = None
    trace_detail: Optional[dict[str, Any]] = None
    #: Pods this module considers the likely fault domain, with the rule used.
    attributed_pods: list[dict[str, Any]] = Field(default_factory=list)
    #: Ordered list of Gateway tools actually invoked.
    tool_calls: list[str] = Field(default_factory=list)

    def add_fact(self, source: str, statement: str, evidence: Any = None) -> None:
        self.facts.append({"source": source, "statement": statement, "evidence": evidence})

    def add_inference(self, statement: str, basis: Iterable[str], confidence: str = "medium") -> None:
        self.inferences.append(
            {"statement": statement, "basis": list(basis), "confidence": confidence}
        )

    def add_gap(self, source: str, reason: str, impact: str, severity: str = "missing") -> None:
        """Record an evidence gap.

        ``severity="missing"`` means the observation could not be made at all, so
        the discovery result is incomplete. ``severity="info"`` records a caveat
        about an observation that *was* made (e.g. ``ready_status`` is unknown by
        design) and must not downgrade the result.
        """
        self.data_gaps.append(
            {"source": source, "reason": reason, "impact": impact, "severity": severity}
        )

    def as_summary(self) -> dict[str, Any]:
        return {
            'evidence_status': self.evidence_status, 'coverage': self.coverage, 'skipped_tools': self.skipped_tools,
            "status": self.status,
            "requires_confirmation": self.requires_confirmation,
            "scope": self.scope.as_summary(),
            "tool_calls": list(self.tool_calls),
            "candidates": self.candidates,
            "service": self.service,
            "attributed_pods": self.attributed_pods,
            "facts": self.facts,
            "inferences": self.inferences,
            "data_gaps": self.data_gaps,
            "clarification": self.clarification,
        }


def _is_error(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    if result.get("error_type") or result.get("error"):
        return True
    return str(result.get("status", "")).lower() == "error"


def _absorb_error(outcome: DiscoveryOutcome, tool: str, result: Any) -> None:
    gaps = result.get("data_gaps") if isinstance(result, dict) else None
    if gaps:
        for gap in gaps:
            outcome.add_gap(
                str(gap.get("source", f"mcp_tool:{tool}")),
                str(gap.get("reason", "error")),
                str(gap.get("impact", "evidence unavailable")),
                severity="missing",
            )
    else:
        outcome.add_gap(
            f"mcp_tool:{tool}",
            str(result.get("error_type", "error")) if isinstance(result, dict) else "error",
            f"{tool} could not be completed; its evidence is missing",
            severity="missing",
        )


def _absorb_warnings(outcome: DiscoveryOutcome, tool: str, result: Any) -> None:
    """Upstream warnings are caveats on data that *was* returned."""
    if not isinstance(result, dict):
        return
    for warning in result.get("warnings") or []:
        outcome.add_gap(
            f"mcp_tool:{tool}", "upstream_warning", str(warning), severity="info"
        )
    if result.get("truncated"):
        outcome.add_gap(
            f"mcp_tool:{tool}",
            "truncated",
            str(result.get("truncated_reason") or
                f"{tool} truncated its result; the list is not exhaustive"),
            severity="missing",
        )


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    where = f"{candidate.get('cluster', UNKNOWN)}/{candidate.get('namespace', UNKNOWN)}"
    return {
        "kind": candidate.get("kind", UNKNOWN),
        "name": candidate.get("name", UNKNOWN),
        "uid": candidate.get("uid", UNKNOWN),
        "id": candidate.get("id"),
        "cluster": candidate.get("cluster", UNKNOWN),
        "namespace": candidate.get("namespace", UNKNOWN),
        "match": candidate.get("match", UNKNOWN),
        "location": where,
    }


def _series_peak(series: Mapping[str, Any]) -> Optional[float]:
    values: list[float] = []
    for point in series.get("points") or []:
        if isinstance(point, Mapping) and point.get("v") is not None:
            try:
                values.append(float(point["v"]))
            except (TypeError, ValueError):
                continue
    return max(values) if values else None


def _series_last(series: Mapping[str, Any]) -> Optional[float]:
    points = series.get("points") or []
    for point in reversed(points):
        if isinstance(point, Mapping) and point.get("v") is not None:
            try:
                return float(point["v"])
            except (TypeError, ValueError):
                continue
    return None


def _series_label(series: Mapping[str, Any]) -> str:
    labels = series.get("labels") or {}
    for key in ("pod", "pod_name", "instance", "service"):
        if labels.get(key):
            return str(labels[key])
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items())) or "aggregate"


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _attribute_pods(
    outcome: DiscoveryOutcome,
    metric_key: str,
    result: Mapping[str, Any],
    *,
    anomaly_threshold: float,
    peer_ratio: float,
    target_kind: str,
) -> None:
    """Decide which Pod series look anomalous -- explicitly an inference."""
    series = result.get("series") or []
    if target_kind != "service" or not series:
        return

    peaks: list[tuple[str, float]] = []
    for item in series:
        if not isinstance(item, Mapping):
            continue
        peak = _series_peak(item)
        if peak is None:
            continue
        peaks.append((_series_label(item), peak))

    if not peaks:
        return

    values = [value for _, value in peaks]
    baseline = _median(values)
    for label, peak in peaks:
        last = _series_last(next(
            (item for item in series if _series_label(item) == label), {}
        ))
        reasons: list[str] = []
        if peak >= anomaly_threshold:
            reasons.append(f"{metric_key} peak {peak:g} >= threshold {anomaly_threshold:g}")
        if baseline is not None and baseline > 0 and peak / baseline >= peer_ratio:
            reasons.append(
                f"{metric_key} peak {peak:g} is {peak / baseline:.1f}x the peer median {baseline:g}"
            )
        if not reasons:
            continue

        outcome.attributed_pods.append(
            {
                "pod": label,
                "metric_key": metric_key,
                "peak": peak,
                "last": last,
                "peer_median": baseline,
                "rule": "; ".join(reasons),
                "epistemic_status": "agent_inference",
                "note": (
                    "derived from Gateway-returned series; the threshold rule is "
                    "RCAgent's, not a Gateway fact"
                ),
            }
        )
        outcome.add_inference(
            f"Pod {label} is a suspected fault domain for this incident ({metric_key} peak {peak:g})",
            basis=[f"mcp_tool:query_metrics[{metric_key}]", "agent threshold rule"],
            confidence="medium",
        )


async def discover_service_target(
    scope: IncidentScope,
    *,
    call_tool: Optional[ToolCaller] = None,
    now: Optional[datetime] = None,
    fetch_trace_detail: bool = True,
    anomaly_threshold: float = DEFAULT_ANOMALY_THRESHOLD,
    peer_ratio: float = DEFAULT_PEER_RATIO,
) -> DiscoveryOutcome:
    """Run target discovery for an ``incident_text`` scope.

    Raises nothing on Gateway failure: every problem becomes a recorded data gap
    so that Triage can report "evidence missing" instead of inventing a cause.
    """
    call = call_tool or _default_caller()
    outcome = DiscoveryOutcome(scope=scope)

    if scope.is_insufficient:
        outcome.status = DISCOVERY_INSUFFICIENT
        outcome.requires_confirmation = True
        outcome.clarification = {
            "reason": "insufficient_information",
            "question": (
                "请提供服务名（例如 “payment 服务最近 15 分钟报 500”）"
                "或明确的目标 Pod（target_cluster + target_namespace + target_resource）。"
            ),
            "missing": list(scope.reasons) or ["service"],
            "next_step": "Re-run with a service name or an explicit Pod target.",
        }
        outcome.add_gap(
            "incident_input",
            "insufficient_information",
            "no service or Pod target could be identified from the input; "
            "nothing was queried and no cause may be inferred",
            severity="missing",
        )
        return outcome

    # ---- 1. search_k8s_targets -------------------------------------------
    search_args: dict[str, Any] = {"query": scope.service, "kind": "auto", "limit": 10}
    if scope.cluster != UNKNOWN:
        search_args["cluster"] = scope.cluster
    if scope.namespace != UNKNOWN:
        search_args["namespace"] = scope.namespace

    outcome.tool_calls.append("search_k8s_targets")
    found = await call("search_k8s_targets", search_args)

    if _is_error(found):
        outcome.status = DISCOVERY_ERROR
        _absorb_error(outcome, "search_k8s_targets", found)
        return outcome

    _absorb_warnings(outcome, "search_k8s_targets", found)

    candidates = [c for c in (found.get("candidates") or []) if isinstance(c, Mapping)]
    service_candidates = [c for c in candidates if c.get("kind") == "service"]
    pod_candidates = [c for c in candidates if c.get("kind") == "pod"]

    outcome.add_fact(
        "mcp_tool:search_k8s_targets",
        f"Gateway returned {len(service_candidates)} Service and "
        f"{len(pod_candidates)} Pod candidate(s) for '{scope.service}'"
        + (f" in {scope.cluster}/{scope.namespace}"
           if scope.cluster != UNKNOWN or scope.namespace != UNKNOWN else " with no scope filter"),
        evidence={
            "query": found.get("query", scope.service),
            "status": found.get("status"),
            "scanned": found.get("scanned"),
            "candidates_by_kind": found.get("candidates_by_kind"),
        },
    )

    # 补充 PRD §4 P0-3 第 1 条：**列表不完整时不得断言唯一**。
    #
    # 这一条必须排在"唯一候选可以继续"之前：即使 `truncated` 时只看见 1 个候选，
    # 也不能当作唯一匹配 —— 未扫描到的部分里可能还有同名目标。
    # Gateway 侧已保证截断时不会返回 ok / not_found（见其
    # decideSearchStatus），这里负责把它转成"需要确认"。
    upstream_status = str(found.get("status", "")).lower()
    candidate_count = len(service_candidates) + len(pod_candidates)
    truncated = bool(found.get("truncated")) or upstream_status == DISCOVERY_PARTIAL
    if truncated:
        visible = [_candidate_identity(c) for c in (service_candidates or pod_candidates)]
        outcome.status = DISCOVERY_PARTIAL
        outcome.requires_confirmation = True
        outcome.candidates = visible
        outcome.clarification = {
            "reason": "incomplete_target_scan",
            "question": (
                f"'{scope.service}' 的候选列表不完整（扫描或返回条数已达上限），"
                f"当前只看到 {len(visible)} 个候选，**不能**当作唯一目标。"
                "请缩小集群/命名空间范围，或确认要调查哪一个。"
            ),
            "candidates": visible,
            "truncated": True,
            "truncated_reason": found.get("truncated_reason")
            or "the upstream list is not exhaustive",
            "missing": [
                name for name in ("cluster", "namespace")
                if scope.cluster == UNKNOWN or scope.namespace == UNKNOWN
            ],
            "next_step": (
                "Re-run with a narrower target_cluster/target_namespace, or confirm exactly "
                "one candidate; no Service, metric, Trace or Pod will be investigated until then."
            ),
        }
        outcome.add_gap(
            "mcp_tool:search_k8s_targets",
            "incomplete_target_scan",
            "the candidate list is not exhaustive, so a single visible candidate cannot be "
            "treated as the only match; no Service, Pod or metric was investigated",
            severity="missing",
        )
        return outcome

    upstream_ambiguous = upstream_status == DISCOVERY_AMBIGUOUS

    if upstream_ambiguous or len(service_candidates) > 1 or (
        not service_candidates and len(pod_candidates) > 1
    ):
        outcome.status = DISCOVERY_AMBIGUOUS
        outcome.requires_confirmation = True
        outcome.candidates = [_candidate_identity(c) for c in (service_candidates or pod_candidates)]
        outcome.clarification = {
            "reason": "ambiguous_target_candidates",
            "question": (
                f"'{scope.service}' 匹配到多个候选目标，请补充集群与命名空间，"
                "或确认要调查哪一个。RCAgent 不会默认选择第一个候选。"
            ),
            "candidates": outcome.candidates,
            "missing": [
                name for name in ("cluster", "namespace")
                if scope.cluster == UNKNOWN or scope.namespace == UNKNOWN
            ],
            "next_step": (
                "Re-run with target_cluster and target_namespace, or confirm exactly "
                "one candidate; no Pod will be investigated until then."
            ),
        }
        outcome.add_gap(
            "mcp_tool:search_k8s_targets",
            "ambiguous",
            "the operator's target is not unique, so no Service, Pod or metric was "
            "investigated; this is not a finding about service health",
            severity="missing",
        )
        return outcome

    if not service_candidates and not pod_candidates:
        outcome.status = DISCOVERY_NOT_FOUND
        outcome.clarification = {
            "reason": "no_matching_target",
            "question": f"没有找到名为 '{scope.service}' 的 Service 或 Pod，请确认名称与范围。",
            "next_step": "Confirm the service name, or supply target_cluster/target_namespace.",
        }
        outcome.add_gap(
            "mcp_tool:search_k8s_targets",
            "not_found",
            f"no Kubernetes target matched '{scope.service}'; this says nothing about "
            "whether the service is healthy",
            severity="missing",
        )
        return outcome

    # ---- 2. resolve the single Service (or a single Pod) ------------------
    resolved_service: Optional[Mapping[str, Any]] = None
    if service_candidates:
        resolved_service = service_candidates[0]
    direct_pod: Optional[Mapping[str, Any]] = None
    if resolved_service is None and len(pod_candidates) == 1:
        direct_pod = pod_candidates[0]

    cluster = str(
        (resolved_service or direct_pod or {}).get("cluster") or scope.cluster
    )
    namespace = str(
        (resolved_service or direct_pod or {}).get("namespace") or scope.namespace
    )

    if direct_pod is not None:
        # The operator named a Pod directly; skip Service expansion entirely.
        outcome.service = None
        outcome.add_fact(
            "mcp_tool:search_k8s_targets",
            f"exactly one Pod candidate '{direct_pod.get('name')}' matched",
            evidence=_candidate_identity(direct_pod),
        )
        outcome.status = DISCOVERY_OK
        outcome.attributed_pods = [
            {
                "pod": direct_pod.get("name"),
                "uid": direct_pod.get("uid"),
                "rule": "only matching Pod candidate",
                "epistemic_status": "tool_result",
            }
        ]
        from .service_fallback import coarse_pods
        return await coarse_pods(outcome, [direct_pod], call, cluster, namespace)

    assert resolved_service is not None
    service_uid = str(resolved_service.get("uid") or "")
    service_name = str(resolved_service.get("name") or scope.service)
    outcome.service = {
        **_candidate_identity(resolved_service),
        "type": resolved_service.get("type", UNKNOWN),
        "cluster_ip": resolved_service.get("cluster_ip", UNKNOWN),
        "resolved_from": "search_k8s_targets",
    }
    if scope.cluster == UNKNOWN:
        scope.cluster = cluster or UNKNOWN
    if scope.namespace == UNKNOWN:
        scope.namespace = namespace or UNKNOWN

    # ---- 3. get_service_backends -----------------------------------------
    backend_args: dict[str, Any] = {"pod_scan_limit": 500}
    if service_uid:
        backend_args["service_uid"] = service_uid
    backend_args["service_name"] = service_name
    if cluster:
        backend_args["cluster"] = cluster
    if namespace:
        backend_args["namespace"] = namespace

    outcome.tool_calls.append("get_service_backends")
    backends = await call("get_service_backends", backend_args)
    outcome.backends = backends

    if _is_error(backends):
        _absorb_error(outcome, "get_service_backends", backends)
    else:
        _absorb_warnings(outcome, "get_service_backends", backends)
        pods = [p for p in (backends.get("pods") or []) if isinstance(p, Mapping)]
        outcome.add_fact(
            "mcp_tool:get_service_backends",
            f"Service {service_name} has {len(pods)} candidate Pod(s) by inventory selector "
            f"in {cluster or UNKNOWN}/{namespace or UNKNOWN}",
            evidence={
                "matching_basis": backends.get("matching_basis"),
                "ready_status": backends.get("ready_status", UNKNOWN),
                "selector_raw": (backends.get("service") or {}).get("selector_raw"),
                "ports": backends.get("ports"),
                "pods": [p.get("name") for p in pods],
            },
        )
        if not pods:
            outcome.add_gap(
                "mcp_tool:get_service_backends",
                "no_selector_match",
                str(
                    backends.get("matching_note")
                    or "无法按 selector 映射 Pod；这不等于该 Service 没有后端，也不代表服务已宕机"
                ),
                severity="info",
            )
        outcome.add_gap(
            "mcp_tool:get_service_backends",
            "ready_status_unknown",
            "candidate Pods come from the resource inventory, not from a live "
            "EndpointSlice; whether they are Ready and serving traffic is unknown",
            severity="info",
        )

    # Program capabilities reflect source semantics, even when schema accepts fields.
    from .contracts import unsupported_scope
    blocked = [(tool, unsupported_scope(tool, {'cluster': cluster, 'namespace': namespace})) for tool in ('query_metrics', 'search_traces')]
    if any(fields for _, fields in blocked):
        for tool, fields in blocked:
            if fields:
                record = {'tool': tool, 'required_scope': {'cluster': cluster, 'namespace': namespace},
                          'unsupported_fields': fields, 'reason': 'Gateway cannot filter this required scope', 'fallback': 'verified Pod summaries'}
                outcome.skipped_tools.append(record)
                outcome.add_gap(tool, 'unsupported_scope', str(record), severity='missing')
                from workspace.runtime import current
                binding = current.get()
                if binding:
                    await binding.repository.transact(lambda db: binding.repository.event(db, binding.incident_id, None, 'query_skipped', record))
        from .service_fallback import coarse_pods
        return await coarse_pods(outcome, (backends.get('pods') or []) if not _is_error(backends) else [], call, cluster, namespace)

    # ---- 4. query_metrics -------------------------------------------------
    metric_keys = list(BASE_METRIC_KEYS)
    if scope.symptom in (SYMPTOM_LATENCY, SYMPTOM_TIMEOUT):
        metric_keys.extend(LATENCY_METRIC_KEYS)

    start_time, end_time = scope.facts.window(now)
    for metric_key in metric_keys:
        metric_args: dict[str, Any] = {
            "metric_key": metric_key,
            "target_kind": "service",
            "target_name": service_name,
        }
        if cluster:
            metric_args["cluster"] = cluster
        if namespace:
            metric_args["namespace"] = namespace
        metric_args["start_time"] = start_time
        metric_args["end_time"] = end_time

        outcome.tool_calls.append(f"query_metrics:{metric_key}")
        result = await call("query_metrics", metric_args)
        outcome.metrics.append({"metric_key": metric_key, "result": result})

        if _is_error(result):
            _absorb_error(outcome, f"query_metrics[{metric_key}]", result)
            continue
        _absorb_warnings(outcome, f"query_metrics[{metric_key}]", result)

        series = result.get("series") or []
        outcome.add_fact(
            f"mcp_tool:query_metrics[{metric_key}]",
            f"{metric_key} returned {len(series)} series over "
            f"{start_time} .. {end_time}",
            evidence={
                "generated_query": result.get("generated_query"),
                "unit": result.get("unit"),
                "window": result.get("window"),
                "series": [
                    {"labels": s.get("labels"), "points": len(s.get("points") or [])}
                    for s in series if isinstance(s, Mapping)
                ],
            },
        )
        if not series:
            outcome.add_gap(
                f"mcp_tool:query_metrics[{metric_key}]",
                "empty_result",
                f"no {metric_key} data in the selected window; this is an observation "
                "gap and must not be read as the service being healthy",
                severity="missing",
            )
            continue

        _attribute_pods(
            outcome, metric_key, result,
            anomaly_threshold=anomaly_threshold,
            peer_ratio=peer_ratio,
            target_kind="service",
        )

    # ---- 5. search_traces -------------------------------------------------
    trace_args: dict[str, Any] = {"service_name": service_name, "limit": 20}
    if cluster:
        trace_args["cluster"] = cluster
    if namespace:
        trace_args["namespace"] = namespace
    trace_args["start_time"] = start_time
    trace_args["end_time"] = end_time
    if scope.status_code is not None:
        trace_args["tags"] = {"http.status_code": str(scope.status_code)}

    outcome.tool_calls.append("search_traces")
    traces = await call("search_traces", trace_args)
    outcome.traces = traces

    if _is_error(traces):
        _absorb_error(outcome, "search_traces", traces)
    else:
        _absorb_warnings(outcome, "search_traces", traces)
        found_traces = [t for t in (traces.get("traces") or []) if isinstance(t, Mapping)]
        outcome.add_fact(
            "mcp_tool:search_traces",
            f"{len(found_traces)} trace(s) matched service {service_name} "
            f"in {start_time} .. {end_time}",
            evidence={
                "service_tag": traces.get("service_tag"),
                "tags_applied": traces.get("tags_applied"),
                "traces": [
                    {
                        "trace_id": t.get("trace_id"),
                        "duration_ms": t.get("duration_ms"),
                        "status": t.get("status"),
                        "root_span": t.get("root_span"),
                    }
                    for t in found_traces
                ],
            },
        )
        if not found_traces:
            outcome.add_gap(
                "mcp_tool:search_traces",
                "empty_result",
                "no trace matched the selected filters; this does not mean the "
                "service has no fault",
                severity="missing",
            )
        else:
            error_traces = [t for t in found_traces if str(t.get("status")) == "error"]
            if error_traces:
                outcome.add_inference(
                    f"{len(error_traces)} of {len(found_traces)} traces are marked error",
                    basis=["mcp_tool:search_traces"],
                    confidence="high",
                )

    # ---- 6. get_trace (optional, one failing trace) -----------------------
    if fetch_trace_detail and outcome.traces and not _is_error(outcome.traces):
        traced = [t for t in (outcome.traces.get("traces") or []) if isinstance(t, Mapping)]
        target_trace = next(
            (t for t in traced if str(t.get("status")) == "error"), None
        ) or (traced[0] if traced else None)
        trace_id = str((target_trace or {}).get("trace_id") or "")
        if trace_id:
            outcome.tool_calls.append("get_trace")
            detail = await call(
                "get_trace",
                {"trace_id": trace_id, "start_time": start_time, "end_time": end_time},
            )
            outcome.trace_detail = detail
            if _is_error(detail):
                _absorb_error(outcome, "get_trace", detail)
            else:
                _absorb_warnings(outcome, "get_trace", detail)
                outcome.add_fact(
                    "mcp_tool:get_trace",
                    f"trace {trace_id} contains {detail.get('span_count', 0)} span(s), "
                    f"max depth {detail.get('max_depth', UNKNOWN)}",
                    evidence={
                        "spans": [
                            {
                                "service": s.get("service"),
                                "operation": s.get("operation"),
                                "duration_ms": s.get("duration_ms"),
                                "status": s.get("status"),
                                "depth": s.get("depth"),
                            }
                            for s in (detail.get("spans") or []) if isinstance(s, Mapping)
                        ],
                        "critical_path": detail.get("critical_path"),
                        "truncated_reason": detail.get("truncated_reason"),
                    },
                )
                failing = [
                    s for s in (detail.get("spans") or [])
                    if isinstance(s, Mapping) and str(s.get("status")) == "error"
                ]
                if failing:
                    deepest = max(failing, key=lambda s: int(s.get("depth") or 0))
                    outcome.add_inference(
                        f"the deepest failing span is {deepest.get('service')}"
                        f"/{deepest.get('operation')} at depth {deepest.get('depth')}",
                        basis=[f"mcp_tool:get_trace[{trace_id}]"],
                        confidence="medium",
                    )

    # ---- verdict ----------------------------------------------------------
    # ``requires_confirmation`` outcomes already carry their own status.
    if outcome.requires_confirmation:
        return outcome

    missing = [g for g in outcome.data_gaps if g.get("severity") == "missing"]
    if not missing:
        outcome.status = DISCOVERY_OK
        outcome.evidence_status = 'available' if outcome.metrics or (outcome.traces or {}).get('traces') else 'unavailable'
        return outcome

    unavailable = {"timeout", "gateway_unreachable", "tool_not_published", "tool_error"}
    if any(g.get("reason") in unavailable for g in missing) and not outcome.metrics:
        outcome.status = DISCOVERY_ERROR
    else:
        outcome.status = DISCOVERY_PARTIAL
    outcome.evidence_status = 'partial' if outcome.metrics or (outcome.traces or {}).get('traces') else 'unavailable'

    return outcome


def outcome_to_triage_state(outcome: DiscoveryOutcome) -> dict[str, Any]:
    """Shape a discovery outcome into the Triage agent's state keys.

    The four legacy keys are preserved so the existing Triage prompt and the
    unchanged Planner/Worker chain keep working.
    """
    if outcome.status == DISCOVERY_NOT_FOUND:
        triage_status = 'not_found'
    elif outcome.requires_confirmation:
        triage_status = (
            "awaiting_confirmation"
            if outcome.status in (DISCOVERY_AMBIGUOUS, DISCOVERY_PARTIAL)
            else "insufficient"
        )
    elif outcome.status == DISCOVERY_ERROR:
        triage_status = "discovery_failed"
    else:
        triage_status = "discovered"

    return {
        "problematic_pods": {
            'pod_fallback': outcome.coverage,
            "discovery_status": outcome.status,
            "scope": outcome.scope.as_summary(),
            "candidates": outcome.candidates,
            "resolved_service": outcome.service,
            "candidate_pods": (outcome.backends or {}).get("pods"),
            "attributed_pods": outcome.attributed_pods,
            "note": (
                "candidate Pods come from the resource inventory selector; they are "
                "not confirmed Ready backends and must not be treated as the single "
                "fault target"
            ),
        },
        "problematic_metrics": {
            "discovery_status": outcome.status,
            "metrics": outcome.metrics,
            "attributed_pods": outcome.attributed_pods,
            "data_gaps": outcome.data_gaps,
        },
        "slow_traces": {
            "traces": (outcome.traces or {}).get("traces"),
            "trace_detail": outcome.trace_detail,
        },
        "problematic_traces": {
            "traces": (outcome.traces or {}).get("traces"),
            "trace_detail": outcome.trace_detail,
        },
        "triage_status": triage_status,
        'evidence_status': outcome.evidence_status,
        "clarification": outcome.clarification,
        "discovery_record": outcome.as_summary(),
    }


async def collect_discovery_evidence(
    state: Mapping[str, Any],
    *,
    call_tool: Optional[ToolCaller] = None,
    now: Optional[datetime] = None,
    fetch_trace_detail: bool = True,
) -> dict[str, Any]:
    """Resolve ``state['incident_text']`` and return Triage state updates."""
    scope = resolve_incident_scope(state)
    outcome = await discover_service_target(
        scope, call_tool=call_tool, now=now, fetch_trace_detail=fetch_trace_detail
    )
    return outcome_to_triage_state(outcome)


__all__ = [
    "BASE_METRIC_KEYS",
    "LATENCY_METRIC_KEYS",
    "DEFAULT_ANOMALY_THRESHOLD",
    "DEFAULT_PEER_RATIO",
    "DISCOVERY_OK",
    "DISCOVERY_AMBIGUOUS",
    "DISCOVERY_NOT_FOUND",
    "DISCOVERY_PARTIAL",
    "DISCOVERY_INSUFFICIENT",
    "DISCOVERY_ERROR",
    "ToolCaller",
    "DiscoveryOutcome",
    "collect_discovery_evidence",
    "discover_service_target",
    "outcome_to_triage_state",
]
