"""Isolated archived material for skill tests/evaluation; no remote transport."""

import json
from pathlib import Path
from config import settings
from models.investigation import InvestigationTask
from agents.investigation_planning import create_tasks
from tools.diagnostic_catalog import CapabilityCatalog, Capability, QueryResult
from workspace.runtime import initialize_incident, from_state
from workspace.investigations import save_question
from workspace.query_execution import execute_query
from workspace.tasks import start
from context.investigation import common_preparers

FIXTURE = Path(__file__).parent / "fixtures" / "skill_cases.json"


async def seed(root, case="E1", *, start_attempt=True):
    from unittest.mock import patch

    data = json.loads(FIXTURE.read_text())
    meta = data["raw"]["meta"]
    with patch.object(settings, "WORKSPACE_ROOT", Path(root)):
        state = await initialize_incident(
            {
                "target_cluster": "rd2",
                "target_resource": "k8s-10-44-55-63",
                "incident_text": "仅核验归档材料，不做远端查询",
                "query_window": {
                    "start_time": meta["effective_start_time"],
                    "end_time": meta["effective_end_time"],
                },
            }
        )
    b = from_state(state, "worker")
    b.context_preparers = common_preparers()
    b.lifecycle_mode = "active"
    resource = await b.repository.save(
        b.incident_id,
        "resource",
        {
            "id": "resource_archive",
            "kind": "node",
            "name": "k8s-10-44-55-63",
            "cluster": "rd2",
            "namespace": None,
            "uid": None,
            "identity_status": "resolved",
            "source_refs": [],
            "candidate_resource_ids": [],
            "lookup_scope": {},
            "verified_at": "archived fixture",
        },
    )
    q = await save_question(
        b,
        text="归档调度事件能证明什么，分位数与业务影响是否仍未知？",
        resource_ids=[resource["id"]],
    )
    catalog = CapabilityCatalog([], service_identity={"offline": True})
    task = (
        await create_tasks(
            b,
            [
                InvestigationTask(
                    investigation_goal="审核归档原值并保留未知",
                    question_ids=[q["id"]],
                    resource_id=resource["id"],
                )
            ],
            catalog,
        )
    )[0]
    attempt = await start(b, task.task_id) if start_attempt else None
    b.task_id, b.attempt_id, b.resource_id = (
        task.task_id,
        attempt["id"] if attempt else None,
        resource["id"],
    )
    raw = data["raw"]
    raw["explicit_null"] = (
        None  # Synthetic null/missing boundary, marked in provenance.
    )

    async def normalize(binding, arguments):
        return arguments

    async def archived(binding, arguments):
        from workspace.windows import normalize_window

        actual = normalize_window(
            {
                "start_time": raw["meta"]["effective_start_time"],
                "end_time": raw["meta"]["effective_end_time"],
            }
        )
        if actual != normalize_window(binding.query_window):
            raise ValueError("archived actual window differs from requested window")
        return QueryResult(raw, [resource["id"]], actual, freshness="archived_fixture")

    preload = CapabilityCatalog(
        [
            Capability(
                "archived_cpu_events",
                "Read isolated R3 raw",
                {"type": "object"},
                normalize,
                archived,
                "archive-v1",
                True,
            )
        ],
        service_identity={"offline_archive": True},
    )
    saved = await execute_query(b, preload, "archived_cpu_events", {})
    data["evidence_id"] = saved["_rcagent_query"]["evidence_id"]
    if case == "E4":
        from workspace.tasks import finish

        await finish(
            b,
            {
                "diagnosis": "Archived worker submission failed",
                "gaps": ["No accepted observations"],
            },
            status="failed",
            stop_reason="archived_submission_failure",
        )
        b.role, b.task_id, b.attempt_id = "supervisor", None, None
    return state, b, catalog, data
