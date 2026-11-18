"""Shared final evidence gate; independent of domain graph assembly."""

from workspace.runtime import from_state, record_execution
from workspace.tasks import views
from workspace.reports import adjudicate, conservative_report
from workspace.execution import normalize_execution


async def evidence_gate(state):
    if (
        state.get("tasks_to_execute_ids")
        and not state.get("final_report")
        and state.get("execution_error") is None
    ):
        return {}
    b = from_state(state, "evidence_gate")
    verified_model_packet = False
    if state.get("report_packet_id"):
        from workspace.report_packets import current_packet, model_selected_packet
        from workspace.repository import WorkspaceError

        try:
            await current_packet(b, state["report_packet_id"])
            verified_model_packet = await model_selected_packet(
                b, state["report_packet_id"]
            )
        except WorkspaceError as exc:
            report = await conservative_report(
                b, "packet_stale", state.get("symptoms", [])
            )
            execution = normalize_execution("incomplete", None, "packet_stale")
            await record_execution(
                b,
                execution["execution_status"],
                stop_reason="packet_stale",
                execution=execution,
            )
            return {"final_report": report, "workflow_stage": "finished", **execution}
    proposed = state.get("final_report") or await conservative_report(
        b,
        state.get("stop_reason") or state.get("triage_status") or "no_symptoms",
        state.get("symptoms", []),
    )
    attempts = {
        a["id"]: a["run_id"] for a in await b.repository.all(b.incident_id, "attempt")
    }
    analyses = [
        a
        for a in state.get("rca_analyses_list", [])
        if a.get("run_id", attempts.get(a.get("attempt_id"), b.run_id)) == b.run_id
    ]
    status = (
        "incomplete"
        if (
            state.get("execution_status") == "incomplete"
            and state.get("supervisor_rounds", 0)
        )
        or state.get("evidence_status") == "unavailable"
        or state.get("triage_status")
        in ("not_found", "discovery_failed", "insufficient", "awaiting_confirmation")
        else "completed"
    )
    execution = normalize_execution(
        status,
        state.get("execution_error"),
        state.get("stop_reason"),
        analyses,
        state.get("secondary_stop_reasons", []),
    )
    from workspace.recovery import issues

    recovered = await issues(b)
    if recovered and execution["execution_status"] == "completed":
        execution.update(
            execution_status="incomplete", stop_reason="completed_with_recovery_gaps"
        )
    report = await adjudicate(
        b,
        proposed,
        state.get("report_claims", []),
        state.get("symptoms", []),
        state.get("stop_reason") or "unsupported_causal_claim",
        report_items=state.get("report_items", [])
        if state.get("supervisor_rounds", 0)
        else None,
        execution=execution,
        verified_model_packet=verified_model_packet,
    )
    await record_execution(
        b,
        execution["execution_status"],
        execution["execution_error"],
        execution["stop_reason"],
        execution=execution,
    )
    from workspace.skill_knowledge import unavailable_guidance

    guidance = await unavailable_guidance(b)
    if guidance:
        report["guidance_limitations"] = guidance
    return {
        "final_report": report,
        "rca_tasks": await views(b),
        "workflow_stage": "finished",
        **execution,
    }
