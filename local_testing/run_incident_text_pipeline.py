"""Run one complete RCAgent investigation driven by a natural-language incident.

This is the local acceptance entry point for PRD §10.3/§10.4:

    一条自然语言用例 "payment 服务最近 15 分钟报 500" 实际经过
    RCAgent → 真实 Gateway → Mock Server/Mock Querier

It reports, for the run it just performed:

* which Gateway tools were called, with per-step duration;
* the total time-to-answer (TTA);
* which statements are upstream/Mock **facts**, which are **agent inferences**,
  and where **data is missing**.

The Gateway address is configuration, never a business-code constant: it comes
from ``--gateway-url`` or ``MCP_GATEWAY_URL`` and only defaults to the local
development Gateway.

Usage (from the RCAgent repository root, inside WSL)::

    python local_testing/run_incident_text_pipeline.py \
        --incident-text "payment 服务最近 15 分钟一直报 500，帮我看看"

    # discovery only: no model calls, safe to run without credentials
    python local_testing/run_incident_text_pipeline.py --discovery-only \
        --incident-text "payment 服务最近 15 分钟一直报 500，帮我看看"

    # prove that structured input wins over parsed text
    python local_testing/run_incident_text_pipeline.py --incident-text "payment 服务报 500" \
        --cluster test-cluster --namespace b01-http-5xx --pod checkout-0
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Local development Gateway. Used only when neither --gateway-url nor
#: MCP_GATEWAY_URL is provided; it is never written into business code.
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8888/mcp"

DEFAULT_INCIDENT_TEXT = "payment 服务最近 15 分钟一直报 500，帮我看看"


def _configure_gateway(args: argparse.Namespace) -> str:
    """Resolve the Gateway URL *before* anything imports config/settings."""
    gateway_url = (
        args.gateway_url
        or os.environ.get("MCP_GATEWAY_URL", "").strip()
        or DEFAULT_GATEWAY_URL
    )
    # MCP_GATEWAY_URL is the documented knob read by config/settings.py.
    os.environ["MCP_GATEWAY_URL"] = gateway_url
    os.environ.setdefault("RCAGENT_MCP_MODE", "kylin_gateway")
    if args.timeout is not None:
        os.environ["RCAGENT_TOOL_TIMEOUT_SECONDS"] = str(args.timeout)
    return gateway_url


class ToolCallRecorder:
    """Wrap the adapter's single Gateway entry point and time every call."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        from tools import observability_adapter

        started = perf_counter()
        result = await observability_adapter.call_gateway_tool(name, arguments)
        duration_ms = round((perf_counter() - started) * 1000, 2)
        record: dict[str, Any] = {
            "seq": len(self.calls) + 1,
            "tool": name,
            "arguments": dict(arguments),
            "duration_ms": duration_ms,
        }
        if isinstance(result, dict):
            record["status"] = result.get("status", "ok" if "error" not in result else "error")
            record["data_source"] = result.get("data_source")
            if result.get("error_type"):
                record["error_type"] = result["error_type"]
            if result.get("truncated"):
                record["truncated"] = True
        record["ok"] = not (isinstance(result, dict) and (
            result.get("error") or result.get("error_type")
        ))
        self.calls.append(record)
        return result

    def totals(self) -> dict[str, Any]:
        total = round(sum(call["duration_ms"] for call in self.calls), 2)
        per_tool: dict[str, dict[str, Any]] = {}
        for call in self.calls:
            bucket = per_tool.setdefault(
                call["tool"], {"calls": 0, "total_ms": 0.0, "failures": 0}
            )
            bucket["calls"] += 1
            bucket["total_ms"] = round(bucket["total_ms"] + call["duration_ms"], 2)
            if not call["ok"]:
                bucket["failures"] += 1
        return {"tool_call_count": len(self.calls), "tool_time_ms": total, "per_tool": per_tool}


def summarize_graph_trace(path: Path) -> dict[str, Any]:
    """Summarize *all* graph tool callbacks; full redacted I/O stays in JSONL.

    Tool durations are a sum of individual calls, not a wall-clock phase length:
    parallel Workers overlap.  The run's monotonic TTA remains authoritative.
    """
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    parents = {str(row["run_id"]): str(row.get("parent_run_id") or "")
               for row in rows if row.get("run_id")}
    names = {str(row["run_id"]): str(row.get("name") or "")
             for row in rows if row.get("run_id") and row.get("event") == "chain_start"}
    starts = {str(row["run_id"]): row for row in rows if row.get("event") == "tool_start"}

    def stage_of(parent: str) -> str:
        current = parent
        for _ in range(30):
            if not current:
                break
            name = names.get(current, "")
            if name in ("triage_agent", "planner_agent", "supervisor_agent"):
                return name
            if name == "rca_agent":
                return f"worker:{current[-8:]}"
            current = parents.get(current, "")
        return "graph_other"

    calls: list[dict[str, Any]] = []
    per_tool: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("event") not in ("tool_end", "tool_error"):
            continue
        start = starts.get(str(row.get("run_id")), {})
        name = str(row.get("name") or start.get("name") or "tool")
        output = row.get("output")
        if isinstance(output, dict) and isinstance(output.get("content"), str):
            try:
                output = json.loads(output["content"])
            except ValueError:
                pass
        elif isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                pass
        gaps: list[str] = []
        if isinstance(output, dict):
            for holder in (output, output.get("meta") or {}, output.get("result") or {}):
                if isinstance(holder, dict):
                    gaps.extend(str(g.get("reason")) for g in holder.get("data_gaps") or []
                                if isinstance(g, dict) and g.get("reason"))
        failed = row.get("event") == "tool_error" or (
            isinstance(output, dict) and bool(output.get("error") or output.get("error_type"))
        )
        duration = float(row.get("duration_ms") or 0)
        call = {
            "seq": row.get("seq"), "stage": stage_of(str(start.get("parent_run_id") or "")),
            "tool": name, "input": start.get("input"),
            "status": output.get("status") if isinstance(output, dict) else None,
            "data_gaps": sorted(set(gaps)), "failed": failed,
            "duration_ms": duration,
        }
        calls.append(call)
        bucket = per_tool.setdefault(name, {"calls": 0, "total_ms": 0.0, "failures": 0})
        bucket["calls"] += 1
        bucket["total_ms"] = round(bucket["total_ms"] + duration, 2)
        bucket["failures"] += int(failed)
    gateway = [c for c in calls if c["tool"] != "submit_final_diagnosis"]
    return {
        "llm_response_count": sum(row.get("event") == "llm_end" for row in rows),
        "gateway_tool_call_count": len(gateway),
        "local_submission_count": len(calls) - len(gateway),
        "gateway_tool_time_sum_ms": round(sum(c["duration_ms"] for c in gateway), 2),
        "per_tool": per_tool,
        "calls": calls,
        "events_jsonl": str(path),
        "timing_note": "per-tool durations overlap across parallel Workers; do not add them to TTA",
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from tools import observability_adapter
    from tools.incident_text import parse_incident_text, resolve_incident_scope
    from tools.triage_discovery import discover_service_target

    started_all = perf_counter()
    text = args.incident_text
    structured = {
        key: value
        for key, value in (
            ("target_cluster", args.cluster),
            ("target_namespace", args.namespace),
            ("target_resource", args.pod),
        )
        if value
    }
    state: dict[str, Any] = {"incident_text": text, **structured}

    # ---- 0. what the Gateway publishes right now -------------------------
    published = observability_adapter.available_tool_names()
    missing_discovery = [
        name for name in observability_adapter.DISCOVERY_TOOL_NAMES
        if name not in published
    ]

    # ---- 1. natural-language parsing -------------------------------------
    facts = parse_incident_text(text)
    if args.with_llm:
        from tools.incident_text import parse_incident_text_with_llm

        facts = await parse_incident_text_with_llm(text, use_llm=True)
    scope = resolve_incident_scope(state, facts=facts)

    # ---- 2. Triage target discovery through the Gateway ------------------
    recorder = ToolCallRecorder()
    discovery_started = perf_counter()
    outcome = await discover_service_target(
        scope,
        call_tool=recorder,
        fetch_trace_detail=not args.no_trace_detail,
    )
    discovery_ms = round((perf_counter() - discovery_started) * 1000, 2)

    # ---- 3. optional full graph run (Planner -> Worker -> Supervisor) ----
    graph_result: dict[str, Any] | None = None
    graph_ms: float | None = None
    graph_error: str | None = None
    graph_trace: dict[str, Any] | None = None
    events_path: Path | None = None
    graph_started: float | None = None
    if not args.discovery_only:
        try:
            from graph import parent_graph
            from local_testing.capture_mcp_mock_pipeline import Recorder

            events_arg = getattr(args, "events_out", None)
            json_arg = getattr(args, "json_out", None)
            events_path = Path(events_arg) if events_arg else (
                Path(json_arg).with_suffix(".events.jsonl") if json_arg else None
            )
            callbacks = []
            if events_path:
                events_path.parent.mkdir(parents=True, exist_ok=True)
                events_path.touch(exist_ok=False)
                callbacks = [Recorder(events_path)]

            graph_input = {
                "app_name": f"RCAgent incident: {text[:80]}",
                "app_summary": text,
                "incident_text": text,
                "target_cluster": args.cluster or "",
                "target_namespace": args.namespace or "",
                "target_resource": args.pod or "",
                "trace_service_starting_point": (
                    args.pod or (scope.service if scope.service != "unknown" else "")
                ),
                "problematic_pods": {},
                "slow_traces": {},
                "problematic_metrics": {},
                "problematic_traces": {},
                "symptoms": [],
                "rca_tasks": [],
                "rca_analyses_list": [],
                "tasks_to_be_executed": [],
                "final_report": {},
                "prompts_config": {},
                "triage_status": "",
                "clarification": {},
                "discovery_record": {},
            }
            graph_started = perf_counter()
            result = await parent_graph.ainvoke(graph_input, config={"callbacks": callbacks})
            graph_ms = round((perf_counter() - graph_started) * 1000, 2)
            if events_path:
                graph_trace = summarize_graph_trace(events_path)
            graph_result = {
                "triage_status": result.get("triage_status"),
                "clarification": result.get("clarification"),
                "symptoms": [
                    item.model_dump() if hasattr(item, "model_dump") else item
                    for item in result.get("symptoms", [])
                ],
                "rca_tasks": [
                    item.model_dump() if hasattr(item, "model_dump") else item
                    for item in result.get("rca_tasks", [])
                ],
                "final_report": result.get("final_report", {}),
                "worker_analyses": [
                    {key: value for key, value in item.items() if key != "message_history"}
                    for item in result.get("rca_analyses_list", []) if isinstance(item, dict)
                ],
            }
        except Exception as exc:  # a model/credential problem must not hide the evidence
            graph_error = f"{type(exc).__name__}: {exc}"
            if graph_started is not None:
                graph_ms = round((perf_counter() - graph_started) * 1000, 2)
            if events_path and events_path.exists():
                graph_trace = summarize_graph_trace(events_path)

    tta_ms = round((perf_counter() - started_all) * 1000, 2)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "incident_text": text,
        "structured_input": structured,
        "gateway": {
            "url": os.environ.get("MCP_GATEWAY_URL"),
            "tool_timeout_seconds": observability_adapter.tool_timeout_seconds(),
            "published_tool_count": len(published),
            "published_tools": published,
            "missing_discovery_tools": missing_discovery,
            "missing_discovery_note": (
                "these tools are not published by the Gateway yet, so their evidence "
                "is unavailable; that is a data gap, not a healthy-service finding"
                if missing_discovery else ""
            ),
        },
        "parsing": {
            "facts": facts.as_summary(),
            "scope": scope.as_summary(),
            "llm_assisted": bool(args.with_llm),
        },
        "discovery": outcome.as_summary(),
        "timing": {
            "discovery_ms": discovery_ms,
            "graph_ms": graph_ms,
            **recorder.totals(),
            "graph_gateway_tool_call_count": (
                graph_trace or {}).get("gateway_tool_call_count"),
            "graph_gateway_tool_time_sum_ms": (
                graph_trace or {}).get("gateway_tool_time_sum_ms"),
            "all_gateway_tool_call_count": (
                recorder.totals()["tool_call_count"]
                + (graph_trace or {}).get("gateway_tool_call_count", 0)
            ),
            "total_tta_ms": tta_ms,
        },
        "epistemic_breakdown": {
            "upstream_facts": {
                "count": len(outcome.facts),
                "note": (
                    "reported verbatim by the Gateway (locally: the Mock Server/Querier "
                    "chain). These are observations, not conclusions."
                ),
                "items": outcome.facts,
            },
            "agent_inferences": {
                "count": len(outcome.inferences),
                "note": (
                    "derived by RCAgent from the facts above (thresholds, attribution). "
                    "The rule is RCAgent's, not the Gateway's."
                ),
                "items": outcome.inferences,
            },
            "data_gaps": {
                "count": len(outcome.data_gaps),
                "note": (
                    "observations that could not be made. Absence of data is never "
                    "reported as good health."
                ),
                "items": outcome.data_gaps,
            },
        },
        "graph_run": {
            "skipped": bool(args.discovery_only),
            "error": graph_error,
            "result": graph_result,
            "trace": graph_trace,
        },
        "disclaimer": (
            "This run used the locally configured Gateway. Metric names, the Tempo "
            "service tag and the resource inventory are Mock contracts: they are NOT "
            "confirmed against a real K8s/Prometheus/Tempo environment, and this is "
            "not a production acceptance result."
        ),
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--incident-text", default=DEFAULT_INCIDENT_TEXT,
                        help="natural-language incident description")
    parser.add_argument("--gateway-url", default=None,
                        help=f"MCP Gateway URL (default: MCP_GATEWAY_URL or {DEFAULT_GATEWAY_URL})")
    parser.add_argument("--cluster", default=None, help="structured target_cluster (wins over parsed text)")
    parser.add_argument("--namespace", default=None, help="structured target_namespace (wins over parsed text)")
    parser.add_argument("--pod", default=None, help="structured target_resource (wins over parsed text)")
    parser.add_argument("--timeout", type=float, default=None,
                        help="per-tool Gateway timeout in seconds (default 30)")
    parser.add_argument("--discovery-only", action="store_true",
                        help="run Triage discovery only; no model calls, no credentials needed")
    parser.add_argument("--with-llm", action="store_true",
                        help="refine the deterministic parse with grounded LLM proposals")
    parser.add_argument("--no-trace-detail", action="store_true",
                        help="skip the optional get_trace expansion step")
    parser.add_argument("--json-out", default=None, help="write the full report to this path")
    parser.add_argument("--events-out", default=None,
                        help="write redacted graph model/tool events as JSONL; defaults beside --json-out")
    args = parser.parse_args()

    _configure_gateway(args)
    report = asyncio.run(run(args))

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
    if report["graph_run"]["error"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
