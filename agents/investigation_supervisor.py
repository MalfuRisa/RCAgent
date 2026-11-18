"""Common evidence review, observation submission and packet selection."""

import json
from config import settings
from langchain_core.messages import HumanMessage
from models.investigation import ReportFinalization
from workspace.repository import WorkspaceError
from workspace.investigations import active_checkpoint
from workspace.report_finalization import finalize
from .investigation_rules import RULES
from .packet_submission import submit_packet
from workflow.role_tools import take_step
from workflow.tool_execution import contract_tool
from workspace.reference_registration import (
    record_observations,
    read_evidence_reference,
    find_evidence_references,
)
from workspace import investigation_phases as stages


async def review(b, model, state, catalog):
    from config import settings
    from langchain_core.tools import tool
    from workspace.read_tools import read_evidence, read_investigation, read_artifact
    from models.investigation import InvestigationPlan
    from agents.investigation_planning import create_tasks
    from workspace.investigation_materials import (
        search_investigation_materials,
        read_investigation_material,
    )

    @contract_tool(args_schema=ReportFinalization)
    async def submit_report(
        unresolved_reasons: dict[str, str],
        limitations: list[str],
        observation_ids: list[str] = (),
        next_steps: list[str] = (),
        claims: list = (),
    ) -> dict:
        """Finalize using formal observation IDs and per-question dispositions. No new raw observations; an unresolved report may have no observations or claims."""
        return await finalize(
            b,
            ReportFinalization(
                observation_ids=observation_ids,
                unresolved_reasons=unresolved_reasons,
                limitations=limitations,
                next_steps=next_steps,
                claims=claims,
            ),
        )

    @tool
    async def begin_consolidation() -> dict:
        """Stop exploration; retain local evidence review, registration and reporting."""
        return await stages.consolidate(b)

    @tool(args_schema=InvestigationPlan)
    async def request_followup(tasks: list) -> dict:
        """Request bounded additional investigation tasks using the same Worker and capability directory."""
        plan = InvestigationPlan(tasks=tasks)
        registered = await create_tasks(b, plan.tasks, catalog)
        return {"status": "accepted", "tasks": registered}

    messages = [
        RULES,
        HumanMessage(
            content=json.dumps(
                {
                    "role": "Supervisor",
                    "capabilities": catalog.directory(),
                    "packet_recheck_error": getattr(b, "packet_recheck_error", None),
                },
                ensure_ascii=False,
            )
        ),
    ]
    from workspace.skill_knowledge import business_steps, hydrate

    await hydrate(b)
    while (await stages.refresh(b)) not in ("finished", "stopped"):
        stages.can_send(b)
        tools = [
            submit_report,
            read_evidence,
            read_investigation,
            read_artifact,
            record_observations,
            read_evidence_reference,
            find_evidence_references,
            begin_consolidation,
            search_investigation_materials,
            read_investigation_material,
        ]
        if state.get("supervisor_rounds", 0) + 1 < settings.SUPERVISOR_MAX_ROUNDS:
            tools.append(request_followup)
        opening = await active_checkpoint(b, b.role)
        outcomes = await take_step(
            b,
            model,
            tools,
            messages,
            phase="local_read",
            empty_instruction="请使用当前工具审阅并提交。",
            denied_reason="Supervisor tool policy denied",
        )
        for name, result in outcomes:
            if name == "submit_report" and result.get("status") == "rejected":
                messages = [
                    RULES,
                    HumanMessage(
                        content=json.dumps(
                            {
                                "role": "Supervisor",
                                "last_rejection": result,
                                "instruction": "定点修正报告：只引用正式观测，逐问题处置；不要把拒绝稿当作事实。",
                            },
                            ensure_ascii=False,
                        )
                    ),
                ]
            if result.get("status") == "accepted" and name in (
                "submit_report",
                "request_followup",
            ):
                return result
    raise WorkspaceError("Supervisor review model step budget exhausted")


async def supervisor(b, model, state, catalog):
    await stages.initialize(b, review_round=state.get("supervisor_rounds", 0))
    selection = await review(b, model, state, catalog)
    if "tasks" in selection:
        b.phase_budget["phase"] = "finished"
        await stages.save(b)
        return {
            "rca_tasks": selection["tasks"],
            "tasks_to_execute_ids": [t.task_id for t in selection["tasks"]],
            "supervisor_rounds": state.get("supervisor_rounds", 0) + 1,
        }
    await stages.consolidate(b)
    try:
        result = await submit_packet(b, model, RULES, selection["packet_id"])
    except WorkspaceError as error:
        if "packet_stale" not in str(error):
            raise
        # One re-review shares this Binding, deadlines, steps and corrections.
        b.packet_recheck_error = str(error)
        selection = await review(
            b,
            model,
            {**state, "supervisor_rounds": settings.SUPERVISOR_MAX_ROUNDS},
            catalog,
        )
        result = await submit_packet(b, model, RULES, selection["packet_id"])
    b.phase_budget["phase"] = "finished"
    await stages.save(b)
    return {
        **result,
        "supervisor_rounds": state.get("supervisor_rounds", 0) + 1,
        "tasks_to_execute_ids": [],
        "execution_status": "completed",
        "execution_error": None,
    }
