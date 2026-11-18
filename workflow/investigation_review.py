"""Save-first synthesis and one no-tool review, never a second investigation."""

import asyncio
import json

from workspace.investigation_projection import decision_materials, project_materials
from workspace.native_checks import check_draft


def review_materials(outputs, checks):
    return {
        "question_and_limits": outputs.instruction,
        "draft_excerpt": outputs.answer("worker")[:10000],
        "draft_truncated": len(outputs.answer("worker")) > 10000,
        "checks": {k: checks[k] for k in ("references", "evidence_status", "limitations")},
        **decision_materials(outputs),
    }


async def review_draft(session, mode="program"):
    outputs = session.outputs
    checks = check_draft(outputs, outputs.answer("worker"))
    checks["execution_receipts"] = outputs.control_events
    outputs.save("checks.json", checks)
    outputs.review_status = "program_checked"
    if mode == "program":
        outputs.event("review_finished", "system", {"mode": mode, "model_calls": 0, "remote_mcp_calls": 0})
        return
    if session.budget and session.budget.remaining("generation") < 2:
        outputs.review_status = "skipped_budget"
        return
    runner = session.runner("review", definitions={})
    runner.guide("finalizing-investigation")
    runner.instruction("只输出简短纠错提示或未核验点，不重写报告，不查工具。有限原文投影、阶段缺口与控制回执：\n"
        + json.dumps(review_materials(outputs, checks), ensure_ascii=False))
    from agents.investigation_transport import content_text
    from workflow.native_tool_calls import calls_from
    try:
        limit = min(20, session.budget.remaining("generation")) if session.budget else 20
        async with asyncio.timeout(limit):
            response = await runner.request(tools=False)
        text = content_text(response.content) if response else ""
        if not response or calls_from(response) or response.response_metadata.get("finish_reason") in {"length", "content_filter"} or not text.strip():
            outputs.review_status = "partial" if text else "unavailable"
        else:
            outputs.review_status = "focused_completed"
            outputs.review_note = text
        if text:
            outputs.save("review-note.md", text)
    except asyncio.CancelledError:
        outputs.review_status = "interrupted"
        raise
    except Exception as exc:  # noqa: BLE001 -- optional review never discards the saved draft
        outputs.review_status = "unavailable"
        runner.outputs.event("review_failed", "review", {"error": str(exc) or type(exc).__name__})
    finally:
        runner.outputs.event("review_finished", "review", {"status": outputs.review_status, "remote_mcp_calls": 0})


def assemble_partials(outputs):
    parts = [
        f"## 阶段 {r['task_id']}（{r['status']} / {r['end_reason']}）\n{r['answer_text']}"
        for r in outputs.task_results
        if r["answer_text"]
    ]
    if parts:
        outputs.partial_body = (
            "以下为程序拼接的阶段文字，尚未完成模型汇总；保留原文和缺口。\n\n"
            + "\n\n".join(parts)
        )
        outputs.save("partial-answer.md", outputs.partial_body)
    if outputs.sources:
        outputs.partial_body += "\n未完成模型汇总，以下为已收到的有限原值投影；不代表原题已完成。\n" + json.dumps(
            project_materials(outputs), ensure_ascii=False)
        outputs.save("partial-answer.md", outputs.partial_body)
