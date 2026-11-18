"""Run RCAgent end to end through Gateway -> mock Querier providers."""

from __future__ import annotations

import asyncio
import json

from graph import parent_graph


INPUT = {
    "app_name": "RCAgent local checkout scenario",
    "app_summary": "checkout-0 is returning HTTP 5xx responses; investigate the evidence and identify the likely failure domain.",
    "target_cluster": "test-cluster",
    "target_namespace": "b01-http-5xx",
    "target_resource": "checkout-0",
    "trace_service_starting_point": "checkout-0",
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
}


async def main() -> None:
    result = await parent_graph.ainvoke(INPUT)
    output = {
        "status": "passed" if result.get("final_report") else "inconclusive",
        "target": {
            "cluster": INPUT["target_cluster"],
            "namespace": INPUT["target_namespace"],
            "pod": INPUT["target_resource"],
        },
        "symptoms": [item.model_dump() for item in result.get("symptoms", [])],
        "tasks": [item.model_dump() for item in result.get("rca_tasks", [])],
        "final_report": result.get("final_report", {}),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
