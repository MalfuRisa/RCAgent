"""Deterministic Kylin-compatible MCP fixture for offline RCAgent testing.

This server is test-only. It mirrors the current Gateway tool names and
arguments while returning one fixed, evidence-rich incident. It never queries
Kubernetes, Querier, Server, or external observability storage.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP


CLUSTER = "local"
NAMESPACE = "shop"
POD = "checkout-7d9f6c8d9f-abc12"
POD_UID = "fixture-checkout-pod-uid"
TRACE_ID = "fixture-trace-db-timeout-001"

mcp = FastMCP(
    "RCAgent Local Fixture",
    host="127.0.0.1",
    port=8890,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


def _window(start_time: str = "", end_time: str = "") -> dict[str, str]:
    return {
        "start_time": start_time or "2026-09-29T09:45:00+08:00",
        "end_time": end_time or "2026-09-29T10:00:00+08:00",
    }


@mcp.tool(description="Resolve a local fixture Pod to its stable identity.")
def resolve_pod(
    cluster: str,
    namespace: str,
    pod: str = "",
    pod_ip: str = "",
) -> dict[str, Any]:
    matched = cluster == CLUSTER and namespace == NAMESPACE and (
        pod in ("", POD) or pod_ip == "10.244.1.23"
    )
    if not matched:
        return {
            "status": "not_found",
            "ambiguous": False,
            "candidates": [],
            "data_gaps": ["Only the documented local fixture Pod is available."],
        }
    return {
        "status": "ok",
        "ambiguous": False,
        "pod_uid": POD_UID,
        "pod": POD,
        "pod_ip": "10.244.1.23",
        "node": "fixture-node-a",
        "workload": {"kind": "Deployment", "name": "checkout"},
        "services": ["checkout"],
        "labels": {"app": "checkout", "version": "v1"},
        "evidence": ["fixture://inventory/pods/checkout"],
        "data_gaps": [],
    }


@mcp.tool(description="Return the current limited runtime status for a fixture Pod.")
def get_pod_runtime_status(
    pod_uid: str,
    cluster: str,
    start_time: str = "",
    end_time: str = "",
) -> dict[str, Any]:
    return {
        "status": "partial",
        "pod_uid": pod_uid,
        "cluster": cluster,
        "phase": "Unknown",
        "window": _window(start_time, end_time),
        "evidence": [],
        "data_gaps": [
            "Kubernetes control-plane runtime details are unavailable in the current SDK contract."
        ],
    }


@mcp.tool(description="Return deterministic L4 facts for the fixture incident.")
def get_pod_network_summary(
    pod_uid: str,
    cluster: str,
    direction: str = "both",
    top_n: int = 20,
    start_time: str = "",
    end_time: str = "",
) -> dict[str, Any]:
    return {
        "status": "ok",
        "pod_uid": pod_uid,
        "cluster": cluster,
        "direction": direction,
        "window": _window(start_time, end_time),
        "summary": {
            "tcp_connect_failure_ratio": 0.31,
            "retransmission_ratio": 0.18,
            "rtt_p95_ms": 486,
            "rst_count": 127,
            "zero_window_count": 0,
        },
        "abnormal_peers": [
            {
                "peer": "postgres.shop.svc:5432",
                "direction": "egress",
                "connect_failure_ratio": 0.61,
                "rtt_p95_ms": 812,
                "retransmission_ratio": 0.29,
            }
        ][:top_n],
        "evidence": ["fixture://network/checkout-to-postgres"],
        "data_gaps": [],
    }


@mcp.tool(description="Return deterministic L7 RED facts for the fixture incident.")
def get_pod_application_summary(
    pod_uid: str,
    cluster: str,
    role: str = "server",
    top_n: int = 20,
    start_time: str = "",
    end_time: str = "",
) -> dict[str, Any]:
    return {
        "status": "ok",
        "pod_uid": pod_uid,
        "cluster": cluster,
        "role": role,
        "window": _window(start_time, end_time),
        "red": {"rps": 41.2, "error_ratio": 0.27, "p95_ms": 1420, "p99_ms": 2280},
        "abnormal_endpoints": [
            {
                "endpoint": "POST /checkout",
                "status_code": 500,
                "error_ratio": 0.44,
                "p95_ms": 1910,
            }
        ][:top_n],
        "protocols": {"http": 1.0},
        "evidence": ["fixture://application/checkout-red"],
        "data_gaps": [],
    }


@mcp.tool(description="Return representative failed requests for the fixture incident.")
def get_abnormal_requests(
    pod_uid: str,
    cluster: str,
    abnormal_type: str = "",
    sample_size: int = 10,
    start_time: str = "",
    end_time: str = "",
) -> dict[str, Any]:
    samples = [
        {
            "trace_id": TRACE_ID,
            "endpoint": "POST /checkout",
            "status_code": 500,
            "duration_ms": 2043,
            "client": "frontend",
            "server": "checkout",
            "error": "database connection timeout",
        }
    ]
    return {
        "status": "ok",
        "pod_uid": pod_uid,
        "cluster": cluster,
        "abnormal_type": abnormal_type or "all",
        "window": _window(start_time, end_time),
        "samples": samples[: max(0, min(sample_size, 50))],
        "evidence": ["fixture://requests/checkout-errors"],
        "data_gaps": [],
    }


@mcp.tool(description="Return a deterministic trace critical path for the fixture incident.")
def trace_request(trace_id: str) -> dict[str, Any]:
    if trace_id != TRACE_ID:
        return {"status": "not_found", "trace_id": trace_id, "data_gaps": ["Unknown fixture trace ID."]}
    return {
        "status": "ok",
        "trace_id": trace_id,
        "duration_ms": 2043,
        "critical_path": [
            {"service": "frontend", "operation": "POST /checkout", "duration_ms": 2043},
            {"service": "checkout", "operation": "create_order", "duration_ms": 1968},
            {"service": "postgres", "operation": "connect", "duration_ms": 1802, "error": "timeout"},
        ],
        "bottleneck_span": "postgres/connect",
        "downstream_duration_ratio": 0.88,
        "evidence": ["fixture://traces/fixture-trace-db-timeout-001"],
        "data_gaps": [],
    }


@mcp.tool(description="Compare the fixture Pod with healthy workload peers.")
def compare_pod_with_peers(
    pod_uid: str,
    cluster: str,
    metrics: list[str] | None = None,
    start_time: str = "",
    end_time: str = "",
) -> dict[str, Any]:
    return {
        "status": "ok",
        "pod_uid": pod_uid,
        "cluster": cluster,
        "metrics": metrics or ["rrt", "error_ratio", "retrans", "rtt"],
        "window": _window(start_time, end_time),
        "scope": "single_instance",
        "target": {"error_ratio": 0.27, "retransmission_ratio": 0.18, "rtt_p95_ms": 486},
        "peer_baseline": {"replica_count": 2, "error_ratio": 0.01, "retransmission_ratio": 0.002, "rtt_p95_ms": 18},
        "evidence": ["fixture://peers/checkout"],
        "data_gaps": [],
    }


@mcp.tool(description="Return a deterministic composite diagnosis for the fixture incident.")
def diagnose_pod(
    cluster: str,
    namespace: str,
    pod: str,
    symptom: str,
    scope: list[str] | None = None,
    compare_with_peers: bool = False,
    start_time: str = "",
    end_time: str = "",
) -> dict[str, Any]:
    return {
        "status": "ok",
        "target": {"cluster": cluster, "namespace": namespace, "pod": pod, "pod_uid": POD_UID},
        "symptom": symptom,
        "scope": scope or ["network", "application", "trace"],
        "window": _window(start_time, end_time),
        "root_cause_candidates": [
            {
                "cause": "checkout Pod egress degradation toward postgres.shop.svc:5432",
                "confidence": 0.93,
                "evidence": [
                    "61% TCP connection failures to postgres",
                    "29% retransmission ratio and 812 ms p95 RTT",
                    "Trace bottleneck is postgres/connect with timeout",
                    "Healthy checkout peers do not show the anomaly" if compare_with_peers else "Peer comparison was not requested",
                ],
            }
        ],
        "excluded": ["global checkout workload failure", "HTTP ingress saturation"],
        "recommendations": [
            "Inspect the target Pod/node egress path to postgres.",
            "Compare CNI and node network counters before remediation.",
        ],
        "evidence": [
            "fixture://network/checkout-to-postgres",
            "fixture://traces/fixture-trace-db-timeout-001",
        ],
        "data_gaps": ["Kubernetes control-plane runtime details are unavailable."],
    }


if __name__ == "__main__":
    try:
        mcp.run(transport="streamable-http")
    except KeyboardInterrupt:
        pass
