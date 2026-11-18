"""One scope review and Planner for all SRE problem statements."""

import json
from langchain_core.messages import HumanMessage
from models.investigation import ScopeReview, InvestigationPlan
from models.schemas import RCATask
from workflow.structured import ask_structured
from workflow.routing import triage_blocked, clarification_result
from workspace.investigations import save_question
from workspace.tasks import register_tasks
from workspace.repository import WorkspaceError
from .investigation_rules import RULES


async def triage(b, model, state, catalog):
    if triage_blocked(state):
        return clarification_result(state)
    resources = await b.repository.all(b.incident_id, "resource")
    resources = [
        r
        for r in resources
        if r.get("identity_status") == "resolved" and not r.get("replaced_by")
    ]
    if not resources:
        return clarification_result(
            {
                "triage_status": "awaiting_confirmation",
                "clarification": {
                    "reason": "No unique verified resource identity is available"
                },
            }
        )
    review = await ask_structured(
        model,
        ScopeReview,
        [
            RULES,
            HumanMessage(
                content=json.dumps(
                    {
                        "role": "Triage",
                        "problem": state.get("incident_text")
                        or state.get("app_summary"),
                        "verified_resources": resources,
                        "window": state["query_window"],
                        "capabilities": catalog.directory(),
                        "instruction": "只提出待验证问题/缺口；仅当目标身份/授权范围或问题语义确实无法确定时clarification。"
                        "尚无正式证据、表/指标未发现或业务影响未知是后续调查事项，不单独构成停止理由。"
                        "不选择领域工作流、不预写查询顺序或结论。",
                    },
                    ensure_ascii=False,
                )
            ),
        ],
        phase="triage",
    )
    questions = [
        await save_question(
            b,
            text=q,
            resource_ids=[r["id"] for r in resources],
            scope=state["query_window"],
        )
        for q in review.questions
    ]
    return {
        "question_ids": [q["id"] for q in questions],
        "triage_status": "awaiting_confirmation"
        if review.clarification
        else "discovered",
        "clarification": {"reason": review.clarification}
        if review.clarification
        else {},
        "symptoms": [],
    }


async def create_tasks(b, items, catalog):
    resources = {r["id"]: r for r in await b.repository.all(b.incident_id, "resource")}
    questions = {
        q["id"]
        for q in await b.repository.all(b.incident_id, "question")
        if q.get("run_id") == b.run_id
    }
    tasks = []
    for index, item in enumerate(items):
        if not set(item.question_ids) <= questions:
            raise WorkspaceError("unknown task question reference")
        if any(c not in catalog.capabilities for c in item.suggested_capabilities):
            raise WorkspaceError(
                "Planner requested unavailable capability; revise task or record a gap"
            )
        resource = resources.get(item.resource_id)
        if resource is None:
            raise WorkspaceError(
                "Planner must name a registered resource ID; clarify target rather than inventing identity"
            )
        tasks.append(
            RCATask(
                priority=index + 1,
                resource_type=resource["kind"],
                resource_id=resource["id"],
                target_resource=resource["name"],
                investigation_goal=item.investigation_goal,
                question_ids=item.question_ids,
                suggested_tools=item.suggested_capabilities,
                acceptance_checks=item.evidence_needs,
            )
        )
    return await register_tasks(b, tasks)


async def planner(b, model, state, catalog):
    payload = {
        "role": "Planner",
        "problem": state.get("incident_text"),
        "questions": await b.repository.all(b.incident_id, "question"),
        "resources": await b.repository.all(b.incident_id, "resource"),
        "capabilities": catalog.directory(),
    }
    messages = [RULES, HumanMessage(content=json.dumps(payload, ensure_ascii=False))]
    for attempt in range(2):
        plan = await ask_structured(model, InvestigationPlan, messages, phase="plan")
        try:
            return {"rca_tasks": await create_tasks(b, plan.tasks, catalog)}
        except WorkspaceError as exc:
            if attempt:
                raise
            messages.append(
                HumanMessage(content="修正本次规划引用/能力错误：" + str(exc))
            )
