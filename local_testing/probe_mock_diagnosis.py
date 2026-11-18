"""Probe normal/positive Mock diagnosis through the *real* MCP Gateway.

The output is intentionally a small, machine-readable evidence summary; it
never prints local credentials or full environment variables.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway-url", required=True)
    args = parser.parse_args()
    os.environ["MCP_GATEWAY_URL"] = args.gateway_url
    os.environ["RCAGENT_MCP_MODE"] = "kylin_gateway"
    from tools.observability_adapter import call_gateway_tool
    from tools.evidence_grading import classify_result

    probes = [
        ("normal_payment_network", "get_pod_network_summary", {
            "cluster": "prod-a", "pod_uid": "uid-rcagent-payment-5xx-payment-7d9f-abcde",
        }),
        ("normal_db_not_covered", "diagnose_pod", {
            "cluster": "prod-a", "namespace": "payments", "pod": "payment-db-0",
        }),
        ("fault_db_resolve", "resolve_pod", {
            "cluster": "prod-a", "namespace": "payments-db", "pod": "payment-db-0",
        }),
        ("fault_db_network", "get_pod_network_summary", {
            "cluster": "prod-a", "pod_uid": "uid-rcagent-payment-db-network-fault-payment-db-0",
        }),
        ("fault_db_diagnosis", "diagnose_pod", {
            "cluster": "prod-a", "namespace": "payments-db", "pod": "payment-db-0",
        }),
    ]
    results = []
    for label, tool, arguments in probes:
        response = await call_gateway_tool(tool, arguments)
        result = response.get("result") if isinstance(response, dict) else None
        summary = response.get("summary") if isinstance(response, dict) else None
        if not isinstance(summary, dict):
            summary = {}
        if not isinstance(result, dict):
            result = {}
        results.append({
            "label": label,
            "tool": tool,
            "arguments": arguments,
            "status": response.get("status") if isinstance(response, dict) else None,
            "evidence_kind": classify_result(response),
            "data_gaps": response.get("data_gaps") if isinstance(response, dict) else None,
            "meta": response.get("meta") if isinstance(response, dict) else None,
            "candidates": [
                {"category": c.get("category"), "evidence_ids": c.get("evidence_ids")}
                for c in result.get("root_cause_candidates") or [] if isinstance(c, dict)
            ],
            "summary": {k: summary.get(k) for k in (
                "establish_fail_ratio", "retrans_ratio", "rtt_avg_ms",
                "rst_count", "zero_window_count", "anomalies",
            ) if k in summary},
            "candidate_count": len(result.get("root_cause_candidates") or []),
            "evidence": [
                {"id": e.get("id"), "source": e.get("source"), "summary": e.get("summary")}
                for e in result.get("evidence") or [] if isinstance(e, dict)
            ] if tool == "diagnose_pod" else None,
            "pods": response.get("pods") if label == "fault_db_resolve" else None,
        })
    print(json.dumps({"gateway": args.gateway_url, "probes": results}, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
