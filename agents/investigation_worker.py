"""The single bounded Worker for all diagnostic capabilities."""

import json
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from config import settings
from models.investigation import TaskSubmission
from workspace.repository import WorkspaceError
from workspace.query_execution import execute_query
from workspace.observation_selection import selected_sources
from workspace.reference_registration import (
    record_observations,
    read_evidence_reference,
    find_evidence_references,
)
from workspace import investigation_phases as stages
from workflow.tool_execution import contract_tool
from workspace.investigations import active_checkpoint, save_judgment
from workspace.tasks import finish
from workflow.role_tools import take_step
from .investigation_rules import RULES


async def worker(b, model, state, catalog):
    task = await b.repository.read(b.incident_id, b.task_id, "task")
    opening = None
    await stages.initialize(b)

    @tool
    async def begin_consolidation() -> dict:
        """Stop remote exploration and retain local reading, registration and completion."""
        return await stages.consolidate(b)

    @tool
    async def inspect_capability(name: str) -> dict:
        """Read the authorized argument schema before querying a capability."""
        return catalog.inspect(name)

    @tool
    async def query_diagnostics(
        name: str,
        arguments: dict,
        force_refresh: bool = False,
        allow_partial: bool = False,
    ) -> dict:
        """Execute an authorized read-only diagnostic query or reuse an exact compatible result."""
        from workspace.read_tools import CURRENT_TOOL_CALL

        result = await execute_query(
            b,
            catalog,
            name,
            arguments,
            CURRENT_TOOL_CALL.get(),
            force_refresh=force_refresh,
            allow_partial=allow_partial,
        )
        evidence = await b.repository.read(
            b.incident_id, result["_rcagent_query"]["evidence_id"], "evidence"
        )
        from context.views import evidence_view, window_table
        from workspace.visible_candidates import candidate_snapshot

        _, refs = window_table([evidence])
        card = evidence_view(evidence, refs)
        candidates = await candidate_snapshot(b, [evidence], [card], [])
        if not candidates:
            from context.json_projection import project_json
            from workspace.queries import read_raw

            card.update(project_json(await read_raw(b, evidence["id"])))
            candidates = await candidate_snapshot(b, [evidence], [card], [])
        return {
            **card,
            "reference_candidates": [
                {
                    "ref_id": r["candidate_id"],
                    "json_pointer": r["source_ref"]["json_pointer"],
                    "value": r["expected_value"],
                }
                for r in candidates[:8]
            ],
            "query_reuse": result["_rcagent_query"],
        }

    @contract_tool(args_schema=TaskSubmission)
    async def complete_task(
        interpretation: str,
        limitations: list[str],
        supporting_observation_ids: list[str] = (),
        opposing_observation_ids: list[str] = (),
        unresolved_reasons: dict[str, str] | None = None,
    ) -> dict:
        """Finish from previously registered supporting/opposing observation IDs and explicit interpretation/gaps."""
        unresolved_reasons = unresolved_reasons or {}
        if not set(unresolved_reasons) <= set(task["question_ids"]):
            raise WorkspaceError(
                "invalid_question: task disposition names another question"
            )
        support = list(dict.fromkeys(supporting_observation_ids))
        sources = await selected_sources(b, support, task["question_ids"])
        opposing = await selected_sources(
            b, opposing_observation_ids, task["question_ids"]
        )
        from workspace.observation_selection import selected_observations

        support_rows = await selected_observations(b, support, task["question_ids"])
        opposing_rows = await selected_observations(
            b, opposing_observation_ids, task["question_ids"]
        )
        for qid in task["question_ids"]:
            await save_judgment(
                b,
                question_id=qid,
                type="interpretation",
                content=interpretation,
                supporting_observation_ids=[
                    o["id"] for o in support_rows if qid in o.get("question_ids", [])
                ],
                opposing_observation_ids=[
                    o["id"] for o in opposing_rows if qid in o.get("question_ids", [])
                ],
                limitations=limitations
                + ([unresolved_reasons[qid]] if unresolved_reasons.get(qid) else []),
                status="unresolved",
            )
        assessment = await finish(
            b,
            {
                "answer_status": "inconclusive",
                "diagnosis": interpretation,
                "reasoning": "Model interpretation; observations and unresolved coverage remain separate",
                "supporting_evidence_ids": sources,
                "opposing_evidence_ids": opposing,
                "gaps": limitations,
                "hypotheses": [],
            },
        )
        return {
            "status": "accepted",
            "assessment_id": assessment["id"],
            "observation_ids": support,
        }

    from workspace.read_tools import read_evidence, read_investigation, read_artifact
    from workspace.stage_tools import new_segment_after_commit
    from workspace.stage_progress import commit_investigation_stage
    from workspace.investigation_materials import (
        search_investigation_materials,
        read_investigation_material,
    )
    from workspace.skill_knowledge import business_steps, hydrate

    all_tools = [
        inspect_capability,
        query_diagnostics,
        read_evidence,
        read_investigation,
        read_artifact,
        commit_investigation_stage,
        complete_task,
        record_observations,
        read_evidence_reference,
        find_evidence_references,
        begin_consolidation,
        search_investigation_materials,
        read_investigation_material,
    ]
    messages = [
        RULES,
        HumanMessage(
            content=json.dumps(
                {
                    "role": "Worker",
                    "task": task,
                    "capabilities": catalog.directory(),
                },
                ensure_ascii=False,
            )
        ),
    ]
    b.active_role_model = model
    await hydrate(b)
    while (await stages.refresh(b)) not in ("finished", "stopped"):
        stages.can_send(b)
        opening = await active_checkpoint(b, b.role)
        tools = all_tools
        outcomes = await take_step(
            b,
            model,
            tools,
            messages,
            phase="worker_step",
            empty_instruction="使用当前可用工具提交；保留未知和缺口。",
            denied_reason="role tool policy denied",
        )
        for name, result in outcomes:
            if (
                name == "commit_investigation_stage"
                and result.get("status") == "accepted"
            ):
                messages = await new_segment_after_commit(
                    b,
                    result["checkpoint_id"],
                    RULES,
                    task_id=b.task_id,
                    extra={"capabilities": catalog.directory()},
                    messages=messages,
                )
                b.checkpoint_required = False
            if name == "complete_task" and result.get("status") == "accepted":
                return result
    raise WorkspaceError("worker model step budget exhausted")
