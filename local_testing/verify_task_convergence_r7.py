"""PRD27 offline acceptance and durable normal-graph evidence; no network/model."""

import argparse
import asyncio
import json
import logging
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.investigation_session import InvestigationSession
from graph import build_parent_graph
from local_testing.tests.test_task_convergence_r7 import (
    END,
    START,
    Model,
    Native,
    chunk,
    dispatch,
    query,
    spec,
)

MODULES = [
    "local_testing.tests.test_task_convergence_r7",
    "local_testing.tests.test_owned_investigation_r5",
    "local_testing.tests.test_phase5_investigation_r6",
]


async def normal_graph(root):
    planner_round = 0
    instances = []

    def scripted(request):
        nonlocal planner_round
        task = request["task"]
        if task:
            if not request["tools"]:
                from langchain_core.messages import ToolMessage

                sources = [
                    json.loads(m.content).get("source_id")
                    for m in request["messages"]
                    if isinstance(m, ToolMessage)
                ]
                return chunk(
                    "脚本阶段答复："
                    + task["question"]
                    + "；来源 "
                    + ", ".join(s for s in sources if s)
                    + "；仅证明脚本行为。"
                )
            table = (task.get("dataset") or {}).get("table", "network")
            return chunk(calls=[("query", "insights_query_list", query(table))])
        if not request["tools"]:
            return chunk(
                "脚本完整复核稿：已取得各阶段脚本材料，范围卡和来源已检查；不代表真实现场结论，根因未决。"
            )
        planner_round += 1
        if planner_round == 1:
            return dispatch(spec("定位与网络探测"))
        if planner_round == 2:
            feedback = json.loads(request["messages"][-1].content.split("\n", 1)[1])
            sources = feedback[0]["source_ids"]
            return dispatch(
                spec(
                    "网络小问题",
                    dataset={"database": "flow_metrics", "table": "network"},
                    context_source_ids=sources,
                ),
                spec(
                    "应用小问题",
                    dataset={"database": "flow_metrics", "table": "application"},
                    context_source_ids=sources,
                ),
            )
        return chunk(
            "脚本完整Planner草稿：首轮探测后，两个独立任务分别交回。未核验真实现场；证据状态unchecked。"
        )

    model = Model(scripted)

    def factory(out, **kw):
        native = Native(out, cancellation=kw["cancellation"], deadline=kw["deadline"])
        instances.append(native)
        return InvestigationSession(out, model=model, native=native, **kw)

    with patch("config.settings.NATIVE_TRACE_REQUESTS", True):
        result = await build_parent_graph(runtime_factory=factory).ainvoke(
            {
                "incident_text": "脚本检查：观察目标业务值与材料缺口",
                "query_window": {"start_time": START, "end_time": END},
                "output_directory": str(root / "script-normal"),
            }
        )
    events = [
        json.loads(line)
        for line in (root / "script-normal/events.jsonl").read_text().splitlines()
    ]
    proof = {
        "verification_kind": "scripted_product_graph_not_real_model_or_mcp",
        "test_modules": MODULES,
        "review_remote_mcp_calls": sum(
            e["type"] == "mcp_call_started" and e["role"] == "review" for e in events
        ),
        "model_peak": model.peak,
        "native_tool_peak": instances[0].peak,
        "connection_closed_on_owner": instances[0].closed,
        "task_conversations": [
            {
                k: r[k]
                for k in (
                    "task_id",
                    "conversation_id",
                    "status",
                    "end_reason",
                    "source_ids",
                )
            }
            for r in result["final_report"]["task_results"]
        ],
        "actual_request_count": len(model.requests),
        "normal_graph_result": result["execution_status"],
    }
    (root / "behavior-proof.json").write_text(
        json.dumps(proof, ensure_ascii=False, indent=2)
    )
    assert proof["normal_graph_result"] == "completed"
    assert proof["review_remote_mcp_calls"] == 0 and proof["connection_closed_on_owner"]
    assert len({t["conversation_id"] for t in proof["task_conversations"]}) == 3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--tests", nargs="+", default=MODULES)
    parser.add_argument("--skip-graph", action="store_true")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    logging.getLogger("asyncio").setLevel(logging.ERROR)
    with (args.output / "short-checks.log").open("w", encoding="utf-8") as stream:
        suite = unittest.defaultTestLoader.loadTestsFromNames(args.tests)
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    print(
        json.dumps(
            {
                "tests": result.testsRun,
                "failures": len(result.failures),
                "errors": len(result.errors),
            }
        )
    )
    if not result.wasSuccessful():
        raise SystemExit(1)
    if not args.skip_graph:
        asyncio.run(normal_graph(args.output))
        print(
            "script-normal and behavior-proof saved; no paid model or remote MCP called"
        )


if __name__ == "__main__":
    main()
