"""Deterministic scenarios for migrating historical role-contract regressions.

Only model responses and remote result data are substitutes. The product graph,
repository, query executor, context, observations, recovery and gate remain real.
"""

import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from config import settings
from graph import build_parent_graph
from tools.diagnostic_catalog import Capability, CapabilityCatalog, QueryResult
from workspace.artifacts import read_artifact_raw
from workspace.repository import Repository, WorkspaceError
from workspace.runtime import current, from_state
from test_unified_investigation import InvestigationModel, RESOURCE, WINDOW, payload


class RateLimit(Exception):
    status_code = 429
    response = type("Response", (), {"headers": {}})()


class ScenarioModel(InvestigationModel):
    def __init__(
        self,
        actions,
        *,
        fault=None,
        two_tasks=False,
        followup=False,
        invalid_report=False,
        supervisor_read=False,
        worker_read=False,
    ):
        super().__init__(actions, followup=followup, invalid_report=invalid_report)
        self.fault, self.two_tasks = fault, two_tasks
        self.calls, self.first_attempt = [], None
        self.supervisor_read = supervisor_read
        self.worker_read = worker_read
        self.read_attempts = set()

    def with_structured_output(self, schema, **kwargs):
        ordinary = super().with_structured_output(schema, **kwargs)

        async def respond(messages):
            binding = current.get()
            self.calls.append((binding.role, schema.__name__, messages))
            if binding.role == "planner" and self.fault == "planner_429":
                raise RateLimit("fixture account request limit")
            if binding.role == "planner" and self.fault == "planner_timeout":
                raise TimeoutError("fixture planner request timeout")
            result = await ordinary.ainvoke(messages)
            if schema.__name__ == "InvestigationPlan" and self.two_tasks:
                first = result["tasks"][0]
                result["tasks"] = [
                    dict(first, investigation_goal=goal)
                    for goal in ("Check direct evidence", "Check counterevidence")
                ]
            return result

        return RunnableLambda(respond)

    def bind_tools(self, tools, **kwargs):
        ordinary = super().bind_tools(tools, **kwargs)

        async def respond(messages):
            binding = current.get()
            self.calls.append((binding.role, "tools", messages))
            if binding.role == "worker":
                self.first_attempt = self.first_attempt or binding.attempt_id
                if (
                    self.fault == "one_worker_timeout"
                    and binding.attempt_id == self.first_attempt
                ):
                    raise TimeoutError("fixture one worker timeout")
                if self.fault == "worker_timeout_after_query" and self.steps.get(
                    binding.attempt_id, 0
                ):
                    raise TimeoutError("fixture timeout after query")
                if (
                    self.worker_read
                    and self.steps.get(binding.attempt_id, 0)
                    and binding.attempt_id not in self.read_attempts
                ):
                    self.read_attempts.add(binding.attempt_id)
                    view = payload(messages)
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_evidence",
                                "id": "worker-read",
                                "args": {
                                    "evidence_id": (
                                        view["evidence"]
                                        + view.get("evidence_directory", [])
                                    )[0]["evidence_id"],
                                    "view": "raw",
                                },
                            }
                        ],
                    )
            if binding.role == "supervisor" and self.fault == "supervisor_timeout":
                raise TimeoutError("fixture supervisor request timeout")
            if (
                binding.role == "supervisor"
                and self.fault == "supervisor_correction_timeout"
            ):
                prior = sum(
                    role == "supervisor" and phase == "tools"
                    for role, phase, _ in self.calls
                )
                if prior > 1:
                    raise TimeoutError("fixture correction request timeout")
                self.invalid_report = True
            if binding.role == "supervisor" and self.fault == "invalid_forever":
                self.invalid_report = True
            if binding.role == "supervisor" and self.supervisor_read:
                self.supervisor_read = False
                view = payload(messages)
                return AIMessage(
                    content="reading draft; this is not a fact",
                    tool_calls=[
                        {
                            "name": "read_evidence",
                            "id": "review-read",
                            "args": {
                                "evidence_id": (
                                    view["evidence"]
                                    + view.get("evidence_directory", [])
                                )[0]["evidence_id"],
                                "view": "raw",
                            },
                        }
                    ],
                )
            return await ordinary.ainvoke(messages)

        return RunnableLambda(respond)


async def run_scenario(
    *,
    fault=None,
    two_tasks=False,
    followup=False,
    invalid_report=False,
    raw=None,
    resource=None,
    window=None,
    actions=None,
    initial_evidence=False,
    input_state=None,
    max_steps=16,
    supervisor_read=False,
    worker_read=False,
    model=None,
):
    resource = copy.deepcopy(resource or RESOURCE)
    window = copy.deepcopy(window or WINDOW)
    raw = copy.deepcopy(
        raw if raw is not None else {"summary": {"tcp_errors": 7}, "missing": None}
    )
    actions = actions or [{"name": "network_summary", "arguments": {}}]
    sent = []

    async def normalize(binding, arguments):
        if arguments.get("resource_id", resource["id"]) != resource["id"]:
            raise WorkspaceError("query does not match verified resource identity")
        return arguments

    async def invoke(binding, arguments):
        sent.append(dict(arguments))
        return QueryResult(copy.deepcopy(raw), [resource["id"]], binding.query_window)

    async def bootstrap(binding, state):
        from workspace.resources import scope, allowed

        if not allowed(
            await scope(binding), resource.get("cluster"), resource.get("namespace")
        ):
            raise WorkspaceError(
                "fixture identity is outside incident authorization scope"
            )
        await binding.repository.save(binding.incident_id, "resource", resource)
        if initial_evidence:
            from workspace.query_execution import execute_query

            await execute_query(binding, catalog, "network_summary", {})

    catalog = CapabilityCatalog(
        [
            Capability(
                name,
                "Fixture readonly capability",
                {"type": "object"},
                normalize,
                invoke,
                "scenario-v1",
                True,
            )
            for name in (
                "network_summary",
                "cpu_events",
                "metric_series",
                "get_pod_network_summary",
                "insights_list_cpu_events",
            )
        ],
        service_identity={"fixture": "one-platform"},
        bootstrap=bootstrap,
    )
    model = model or ScenarioModel(
        actions,
        fault=fault,
        two_tasks=two_tasks,
        followup=followup,
        invalid_report=invalid_report,
        supervisor_read=supervisor_read,
        worker_read=worker_read,
    )
    with (
        tempfile.TemporaryDirectory() as directory,
        patch.object(settings, "WORKSPACE_ROOT", Path(directory)),
        patch.object(settings, "MODEL_MIN_INTERVAL_SECONDS", 0),
        patch.object(settings, "WORKER_MAX_MODEL_STEPS", max_steps),
        # Normal parent scenarios use the production defaults. Historical test
        # base classes also exercise strict 24k limits in dedicated budget tests.
        patch.object(settings, "CONTEXT_BUDGET_POLICY", "soft"),
        patch.object(settings, "CONTEXT_MAX_CHARS", 96000),
        patch.object(settings, "CONTEXT_HARD_MAX_CHARS", 512000),
    ):
        state = await build_parent_graph(catalog=catalog, model=model).ainvoke(
            {
                "incident_text": "Investigate available scoped system evidence",
                "target_cluster": resource["cluster"],
                "target_resource": resource["name"],
                "query_window": window,
                **(input_state or {}),
            },
            config={"recursion_limit": 40},
        )
        repository = Repository(directory)
        kinds = (
            "run",
            "resource",
            "task",
            "attempt",
            "assessment",
            "evidence",
            "artifact",
            "context",
            "question",
            "observation",
            "judgment",
            "stage_commit",
            "checkpoint",
            "read_receipt",
            "model_call",
            "model_attempt",
            "model_response",
            "model_action",
            "recovery_issue",
            "report_packet",
            "report_proposal",
            "report",
        )
        records = {
            kind: await repository.all(state["incident_id"], kind) for kind in kinds
        }
        binding = from_state(state, "test")
        records["visible_messages"] = {
            c["id"]: await read_artifact_raw(binding, c["visible_messages_artifact_id"])
            for c in records["context"]
            if c.get("visible_messages_artifact_id")
        }
        events = await repository.transact(
            lambda db: [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM events WHERE incident=?", (state["incident_id"],)
                )
            ]
        )
        return state, records, events, sent, model
