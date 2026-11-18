"""Run the real RCA Worker against the deterministic local MCP fixture."""

import asyncio
import json

from agents.rca_agent import rca_agent_graph
from models import RCATask
from workspace.runtime import initialize_incident, from_state, bind
from workspace.tasks import register_tasks, start


def build_fixture_task() -> RCATask:
    """Return the fixed investigation used by the offline baseline."""
    return RCATask(
        priority=1,
        investigation_goal=(
            "Identify why POST /checkout has high latency and HTTP 5xx responses "
            "for the target Pod in cluster local."
        ),
        target_resource="checkout-7d9f6c8d9f-abc12",
        resource_type="pod",
        suggested_tools=[
            "resolve_pod",
            "get_pod_network_summary",
            "get_pod_application_summary",
            "get_abnormal_requests",
            "trace_request",
            "compare_pod_with_peers",
            "diagnose_pod",
        ],
    )


async def run_worker(task: RCATask | None = None) -> dict:
    """Execute a scoped Worker graph and return its first analysis."""
    selected_task = task or build_fixture_task()
    state = {
        "messages": [],
        "rca_app_summary": (
            "Local fixture checkout service in cluster local. The incident window "
            "shows elevated latency and 5xx responses on POST /checkout."
        ),
        "rca_target_namespace": "shop",
        "rca_task": selected_task,
        "insights": [],
        "prev_steps": [],
        "rca_output": {},
        "rca_analyses_list": [],
        "rca_prompts_config": {},
    }

    workspace = await initialize_incident({'target_cluster': 'local', 'target_namespace': 'shop', 'target_resource': selected_task.target_resource})
    binding = from_state(workspace, 'worker')
    selected_task = (await register_tasks(binding, [selected_task], selected_task.target_resource, 'shop'))[0]
    attempt = await start(binding, selected_task.task_id)
    binding.task_id, binding.attempt_id, binding.resource_id = selected_task.task_id, attempt['id'], selected_task.resource_id
    state.update(workspace, rca_task=selected_task, task_id=binding.task_id, attempt_id=binding.attempt_id, resource_id=binding.resource_id)
    with bind(binding):
        result = await rca_agent_graph.ainvoke(state, {"recursion_limit": 30})
    return result["rca_analyses_list"][0]


async def main() -> None:
    analysis = await run_worker()
    summary = {
        "root_cause": analysis.get("root_cause"),
        "confidence": analysis.get("confidence"),
        "tools_stats": analysis.get("tools_stats", {}),
        "steps": len(analysis.get("steps_performed", [])),
        "insights": len(analysis.get("insights", [])),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
