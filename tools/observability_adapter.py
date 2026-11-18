"""RCAgent-facing adapter for the Kylin MCP Gateway.

This module is the only programmatic data entry used by Triage and Planner.
It deliberately calls the same MCP tools exposed to RCA workers instead of
reaching Kubernetes, Prometheus, Jaeger, Querier, or a fixture directly.

Two tool families are available through the same Gateway:

* the original eight Pod-diagnosis tools (``resolve_pod`` … ``diagnose_pod``),
  used by the preserved structured path; and
* the five target-discovery / observability tools added for natural-language
  incidents (``search_k8s_targets``, ``get_service_backends``,
  ``query_metrics``, ``search_traces``, ``get_trace``).

Every call is bounded by a timeout and every failure is classified, because
"the Gateway is unreachable", "the RPC timed out", "the arguments were
rejected" and "the query returned nothing" are different evidence gaps and
must never be collapsed into "the service is healthy".
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Mapping

#: The five tools added for natural-language target discovery. Kept explicit so
#: Triage and the acceptance runner can report which ones the Gateway publishes.
DISCOVERY_TOOL_NAMES = (
    "search_k8s_targets",
    "get_service_backends",
    "query_metrics",
    "search_traces",
    "get_trace",
)

#: Original eight Pod-diagnosis tools. Unchanged, and still the structured path.
POD_DIAGNOSIS_TOOL_NAMES = (
    "resolve_pod",
    "get_pod_runtime_status",
    "get_pod_network_summary",
    "get_pod_application_summary",
    "get_abnormal_requests",
    "trace_request",
    "compare_pod_with_peers",
    "diagnose_pod",
)

DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0
TOOL_TIMEOUT_ENV = "RCAGENT_TOOL_TIMEOUT_SECONDS"
_GATEWAY_UNREACHABLE_HINTS = (
    "connect",
    "connection",
    "unreachable",
    "refused",
    "clientconnector",
    "network is unreachable",
    "getaddrinfo",
)
_INVALID_ARGUMENT_HINTS = (
    "invalid_argument",
    "invalidargument",
    "invalid argument",
    "validation error",
    "missing required",
)


def tool_timeout_seconds() -> float:
    """Per-call Gateway timeout, configurable through the environment."""
    raw = os.environ.get(TOOL_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TOOL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TOOL_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TOOL_TIMEOUT_SECONDS


_TOOLS_BY_NAME: dict[str, Any] | None = None


def _tools_by_name() -> dict[str, Any]:
    """Resolve Gateway tools lazily.

    ``.mcp_tools`` opens the Gateway connection at import time. Doing that here
    would make this module unusable in unit tests (and would crash any caller
    when the Gateway is down), so the lookup happens on first real call.
    """
    global _TOOLS_BY_NAME
    if _TOOLS_BY_NAME is None:
        from .mcp_tools import TOOLS

        _TOOLS_BY_NAME = {tool.name: tool for tool in TOOLS}
    return _TOOLS_BY_NAME


def available_tool_names() -> list[str]:
    """Names the Gateway currently publishes (empty when it is unreachable)."""
    try:
        return sorted(_tools_by_name())
    except Exception:
        return []


def _decode(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"data": decoded}
    return {"data": value}


def classify_tool_error(name: str, exc: BaseException) -> dict[str, Any]:
    """Turn a raised exception into an explicit, non-collapsible evidence gap."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        error_type, explanation = (
            "timeout",
            f"{name} exceeded the {tool_timeout_seconds()}s Gateway timeout; "
            "the query result is unknown, not empty and not healthy",
        )
    else:
        text = f"{type(exc).__name__}: {exc}".lower()
        if any(hint in text for hint in _GATEWAY_UNREACHABLE_HINTS):
            error_type, explanation = (
                "gateway_unreachable",
                f"{name} could not reach the MCP Gateway; no observation was made",
            )
        elif any(hint in text for hint in _INVALID_ARGUMENT_HINTS):
            error_type, explanation = (
                "invalid_argument",
                f"{name} rejected the arguments; the query was not executed as asked",
            )
        else:
            error_type, explanation = (
                "tool_error",
                f"{name} failed upstream; the requested evidence is unavailable",
            )

    return {
        "error": f"{name} failed: {exc}",
        "status": "error",
        "error_type": error_type,
        "data_source": "mcp_gateway",
        "warnings": [explanation],
        "data_gaps": [
            {
                "source": f"mcp_tool:{name}",
                "reason": error_type,
                "impact": explanation,
            }
        ],
    }


def tool_not_published(name: str) -> dict[str, Any]:
    """The Gateway does not expose this tool (yet)."""
    return {
        "error": f"MCP Gateway did not publish required tool: {name}",
        "status": "error",
        "error_type": "tool_not_published",
        "data_source": "mcp_gateway",
        "warnings": [
            f"{name} is not published by the Gateway; this capability cannot be "
            "used and its evidence is missing"
        ],
        "data_gaps": [
            {
                "source": f"mcp_tool:{name}",
                "reason": "tool_not_published",
                "impact": (
                    f"the Gateway does not expose {name}; results that depend on it "
                    "are unavailable, which is not evidence that the target is healthy"
                ),
            }
        ],
    }


def gateway_unavailable(name: str, exc: BaseException) -> dict[str, Any]:
    """The Gateway tool list could not be loaded at all.

    This is *not* a per-tool upstream failure: no RPC was attempted, so the
    evidence for every tool is missing for the same single reason.
    """
    return {
        "error": f"could not load the MCP Gateway tool list: {exc}",
        "status": "error",
        "error_type": "gateway_unreachable",
        "data_source": "mcp_gateway",
        "warnings": [
            f"the Gateway tool list could not be loaded ({type(exc).__name__}); "
            f"{name} was never called and no observation was made"
        ],
        "data_gaps": [
            {
                "source": "mcp_gateway",
                "reason": "gateway_unreachable",
                "impact": (
                    "no Gateway tool could be called, so every evidence source is "
                    "missing; this is not evidence that the target is healthy"
                ),
            }
        ],
    }


async def call_gateway_tool(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Call one allow-listed Gateway tool and normalize failures for Agent prompts.

    The two-positional-argument signature is part of the local experiment
    harness contract (``local_testing/capture_mcp_mock_pipeline.py`` wraps it),
    so it is preserved exactly. The timeout is applied internally.
    """
    from workspace.runtime import current
    from workspace.queries import execute
    binding = current.get()
    if binding is not None:
        return await execute(binding, name, arguments, lambda effective: _call_gateway_raw(name, effective))
    return await _call_gateway_raw(name, arguments)


async def _call_gateway_raw(name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    global _TOOLS_BY_NAME
    try:
        if _TOOLS_BY_NAME is None:
            from .mcp_tools import _mcp_client, get_mcp_tools
            tools = await asyncio.wait_for(get_mcp_tools(_mcp_client), tool_timeout_seconds())
            _TOOLS_BY_NAME = {t.name: t for t in tools}
        tool = _TOOLS_BY_NAME.get(name)
    except Exception as exc:
        return gateway_unavailable(name, exc)

    if tool is None:
        return tool_not_published(name)

    try:
        result = await asyncio.wait_for(
            tool.ainvoke(dict(arguments)), timeout=tool_timeout_seconds()
        )
        return _decode(result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # Tool/backend errors are evidence gaps, not process crashes.
        return classify_tool_error(name, exc)


def target_from_state(state: Mapping[str, Any]) -> dict[str, str]:
    """Read the focused incident target from graph input with env fallbacks.

    Unchanged semantics: this is the original structured Pod path and it still
    raises when the caller did not provide a concrete Pod target.
    """
    cluster = str(
        state.get("target_cluster")
        or os.environ.get("RCAGENT_TARGET_CLUSTER", "")
    ).strip()
    namespace = str(state.get("target_namespace") or "").strip()
    pod = str(
        state.get("target_resource")
        or state.get("target_pod")
        or state.get("trace_service_starting_point")
        or os.environ.get("RCAGENT_TARGET_POD", "")
    ).strip()
    missing = [
        name
        for name, value in (("target_cluster", cluster), ("target_namespace", namespace), ("target_resource", pod))
        if not value
    ]
    if missing:
        raise ValueError(
            "RCAgent MCP input is missing " + ", ".join(missing)
            + ". Provide a concrete Pod target or an incident_text the Triage agent can resolve."
        )
    return {"cluster": cluster, "namespace": namespace, "pod": pod}


async def resolve_target(state: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
    target = target_from_state(state)
    resolved = await resolve_identity_target(target)
    return target, resolved


async def resolve_identity_target(target):
    from workspace.runtime import current
    from workspace.resources import register, ensure_identity
    from workspace.queries import read_raw
    binding = current.get()
    if binding:
        resource = await register(binding, "pod", target["pod"], target.get("namespace"))
        if resource.get("source_refs"):
            result = await ensure_identity(binding, resource["id"])
            if result["status"] != "resolved":
                return {"status": result["status"], "pods": [], "data_gaps": ["Identity unresolved"]}
            resource = result["resource"]
            raw = await read_raw(binding, resource["source_refs"][-1])
            if "pods" not in raw:
                raw = {"pods": [{"name": resource["name"], "pod_uid": resource["uid"],
                                  "cluster": resource["cluster"], "namespace": resource["namespace"]}],
                       "identity_source_refs": resource["source_refs"], "freshness": resource.get("freshness", "unknown")}
            return raw
    return await call_gateway_tool("resolve_pod", target)


async def collect_triage_evidence(state: Mapping[str, Any]) -> dict[str, Any]:
    """Collect the three P0 summary views through the real MCP Gateway."""
    try:
        target, resolved = await resolve_target(state)
    except ValueError as exc:
        error = {"error": str(exc)}
        return {
            'triage_status': 'insufficient', 'evidence_status': 'unavailable',
            "problematic_pods": error,
            "problematic_metrics": error,
            "slow_traces": {"info": "Summary triage did not request trace samples."},
            "problematic_traces": {"info": "Summary triage did not request trace samples."},
        }

    pods = resolved.get("pods") if isinstance(resolved, dict) else None
    from workspace.resources import incomplete
    if pods and (len(pods) != 1 or incomplete(resolved) or not (pods[0].get('pod_uid') or pods[0].get('uid')) or
                 pods[0].get('name') != target['pod'] or pods[0].get('namespace') != target['namespace'] or
                 pods[0].get('cluster', target['cluster']) != target['cluster']):
        return {'triage_status': 'awaiting_confirmation', 'evidence_status': 'unavailable',
                'stop_reason': 'ambiguous_or_incomplete_identity', 'symptoms': [],
                'discovery_record': {'scope': target, 'resolution': resolved},
                'clarification': {'reason': 'ambiguous_or_incomplete_identity', 'scope': target},
                'problematic_pods': {'error': 'Unique complete identity required'}}
    if not pods:
        error = resolved if "error" in resolved else {"error": "resolve_pod returned no Pod"}
        return {
            'triage_status': 'discovery_failed' if resolved.get('error_type') or resolved.get('error') else 'not_found',
            'evidence_status': 'unavailable', 'stop_reason': 'target_resolution_failed' if resolved.get('error_type') or resolved.get('error') else 'target_not_found',
            'clarification': {'reason': 'target_not_resolved', 'scope': target, 'question': '请核对目标名称与明确命名空间；本次未命中不证明不存在或健康。'},
            'discovery_record': {'scope': target, 'resolution': resolved},
            "problematic_pods": error,
            "problematic_metrics": error,
            "slow_traces": {"info": "Target resolution failed; trace lookup was skipped."},
            "problematic_traces": {"info": "Target resolution failed; trace lookup was skipped."},
        }

    pod = pods[0]
    pod_uid = str(pod.get('pod_uid') or pod.get('uid'))
    common = {"pod_uid": pod_uid, "cluster": target["cluster"]}
    runtime = await call_gateway_tool("get_pod_runtime_status", common)
    network = await call_gateway_tool(
        "get_pod_network_summary", {**common, "direction": "both", "top_n": 20}
    )
    application = await call_gateway_tool(
        "get_pod_application_summary", {**common, "role": "both", "top_n": 20}
    )

    return {
        'triage_status': 'discovered',
        'evidence_status': 'unavailable' if all(v.get('error') or v.get('error_type') or v.get('status') == 'error' for v in (runtime, network, application)) else
                           'partial' if any(v.get('error') or v.get('error_type') or v.get('status') == 'partial' or (v.get('meta') or {}).get('data_gaps') for v in (runtime, network, application)) else 'available',
        "problematic_pods": {
            "target": target,
            "resolved_pod": pod,
            "runtime_status": runtime,
        },
        "problematic_metrics": {
            "target": target,
            "network_summary": network,
            "application_summary": application,
        },
        "slow_traces": {
            "info": "Triage intentionally stops at MCP summary evidence; Worker may request abnormal samples and traces."
        },
        "problematic_traces": {
            "info": "Triage intentionally stops at MCP summary evidence; Worker may request abnormal samples and traces."
        },
    }


async def collect_resource_context(
    symptom: Any, state: Mapping[str, Any]
) -> dict[str, Any]:
    """Give Planner Gateway-derived topology hints without direct K8s/Datagraph access."""
    result: dict[str, Any] = {
        "resource_name": symptom.affected_resource,
        "resource_type": symptom.resource_type,
    }
    if symptom.resource_type != "pod":
        result["data_gaps"] = [
            "The Gateway resolves Pods; use search_k8s_targets and get_service_backends "
            "to turn a Service-level symptom into candidate Pods."
        ]
        return result

    target = {
        "cluster": str(state.get("target_cluster") or os.environ.get("RCAGENT_TARGET_CLUSTER", "")).strip(),
        "namespace": str(state.get("target_namespace") or "").strip(),
        "pod": symptom.affected_resource,
    }
    resolved = await resolve_identity_target(target)
    pods = resolved.get("pods") if isinstance(resolved, dict) else None
    if not pods:
        result["data_gaps"] = [resolved.get("error", "resolve_pod returned no Pod")]
        return result

    pod = pods[0]
    pod_uid = str(pod.get("pod_uid", ""))
    network = await call_gateway_tool(
        "get_pod_network_summary",
        {"pod_uid": pod_uid, "cluster": target["cluster"], "direction": "both", "top_n": 20},
    )
    summary = network.get("summary", {}) if isinstance(network, dict) else {}
    peers = []
    for direction in ("upstreams", "downstreams"):
        for peer in summary.get(direction, []) or []:
            peers.append({"direction": direction, **peer})

    result.update(
        {
            "resolved_pod": pod,
            "data_dependencies": peers,
            "infra_dependencies": {
                "workload": pod.get("workload"),
                "services": pod.get("services", []),
            },
            "network_evidence": summary,
        }
    )
    return result


# ---------------------------------------------------------------------------
# Five new tools. Thin, honest wrappers: they add no interpretation, they only
# give the discovery flow a typed place to call from and a uniform error shape.
# ---------------------------------------------------------------------------


async def search_k8s_targets(
    query: str,
    *,
    kind: str = "auto",
    cluster: str | None = None,
    namespace: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Find candidate Services/Pods for a name the operator mentioned."""
    arguments: dict[str, Any] = {"query": query, "kind": kind, "limit": limit}
    if cluster:
        arguments["cluster"] = cluster
    if namespace:
        arguments["namespace"] = namespace
    return await call_gateway_tool("search_k8s_targets", arguments)


async def get_service_backends(
    *,
    service_uid: str | None = None,
    service_name: str | None = None,
    cluster: str | None = None,
    namespace: str | None = None,
    pod_scan_limit: int = 500,
) -> dict[str, Any]:
    """Resolve the candidate Pods behind one Service via inventory selector."""
    arguments: dict[str, Any] = {"pod_scan_limit": pod_scan_limit}
    if service_uid:
        arguments["service_uid"] = service_uid
    if service_name:
        arguments["service_name"] = service_name
    if cluster:
        arguments["cluster"] = cluster
    if namespace:
        arguments["namespace"] = namespace
    return await call_gateway_tool("get_service_backends", arguments)


async def query_metrics(
    *,
    metric_key: str,
    target_kind: str,
    target_name: str,
    cluster: str | None = None,
    namespace: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    step: str = "60s",
    max_series: int = 10,
    max_points: int = 300,
) -> dict[str, Any]:
    """Query one Gateway-approved metric template. The agent never sends PromQL."""
    arguments: dict[str, Any] = {
        "metric_key": metric_key,
        "target_kind": target_kind,
        "target_name": target_name,
        "step": step,
        "max_series": max_series,
        "max_points": max_points,
    }
    if cluster:
        arguments["cluster"] = cluster
    if namespace:
        arguments["namespace"] = namespace
    if start_time:
        arguments["start_time"] = start_time
    if end_time:
        arguments["end_time"] = end_time
    return await call_gateway_tool("query_metrics", arguments)


async def search_traces(
    *,
    service_name: str,
    cluster: str | None = None,
    namespace: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    min_duration: str | None = None,
    max_duration: str | None = None,
    tags: Mapping[str, str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Find traces for a service inside a time window."""
    arguments: dict[str, Any] = {"service_name": service_name, "limit": limit}
    if cluster:
        arguments["cluster"] = cluster
    if namespace:
        arguments["namespace"] = namespace
    if start_time:
        arguments["start_time"] = start_time
    if end_time:
        arguments["end_time"] = end_time
    if min_duration:
        arguments["min_duration"] = min_duration
    if max_duration:
        arguments["max_duration"] = max_duration
    if tags:
        arguments["tags"] = dict(tags)
    return await call_gateway_tool("search_traces", arguments)


async def get_trace(
    *,
    trace_id: str,
    start_time: str | None = None,
    end_time: str | None = None,
) -> dict[str, Any]:
    """Expand one trace into a span tree."""
    arguments: dict[str, Any] = {"trace_id": trace_id}
    if start_time:
        arguments["start_time"] = start_time
    if end_time:
        arguments["end_time"] = end_time
    return await call_gateway_tool("get_trace", arguments)


__all__ = [
    "DISCOVERY_TOOL_NAMES",
    "POD_DIAGNOSIS_TOOL_NAMES",
    "DEFAULT_TOOL_TIMEOUT_SECONDS",
    "TOOL_TIMEOUT_ENV",
    "available_tool_names",
    "call_gateway_tool",
    "classify_tool_error",
    "collect_resource_context",
    "collect_triage_evidence",
    "gateway_unavailable",
    "get_service_backends",
    "get_trace",
    "query_metrics",
    "resolve_target",
    "search_k8s_targets",
    "search_traces",
    "target_from_state",
    "tool_not_published",
    "tool_timeout_seconds",
]
