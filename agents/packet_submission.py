"""Short final selection and a shared, bounded correction budget."""

import json
from langchain_core.messages import HumanMessage
from pydantic import ValidationError
from config import settings
from context.request_budget import structured_request
from models.schemas import PacketSelection
from workspace.report_packets import current_packet, expand_packet
from workspace.repository import WorkspaceError, new_id
from workspace.runtime import invoke_model


async def submit_packet(b, model, system_message, packet_id):
    packet = await current_packet(b, packet_id)
    b.active_packet_id = packet_id
    messages = [
        system_message,
        HumanMessage(
            content=json.dumps(
                {
                    "packet_id": packet_id,
                    "statement_ids": [r["statement_id"] for r in packet["statements"]],
                    "unresolved_question_ids": packet["unresolved_question_ids"],
                    "instruction": "Choose registered statements and preserve unresolved directions. No free report text.",
                }
            )
        ),
    ]
    while True:
        proposal = None
        try:
            from workflow.structured import ask_structured

            proposal = await ask_structured(
                model, PacketSelection, messages, phase="packet_submission"
            )
            choice = PacketSelection.model_validate(proposal)
            if choice.packet_id != packet_id:
                raise WorkspaceError("packet_invalid: selection names another packet")
            expanded = await expand_packet(b, **choice.model_dump())
            await b.repository.save(
                b.incident_id,
                "report_proposal",
                {
                    "id": new_id("proposal"),
                    "packet_id": packet_id,
                    "selection": choice.model_dump(),
                    "normalized": expanded,
                    "errors": [],
                    "response_id": getattr(b, "last_response_id", None),
                    "context_id": b.last_context_id,
                },
            )
            from workspace import model_audit

            await model_audit.processed(
                b,
                "expand_report_selection",
                {"status": "accepted", "packet_id": packet_id},
            )
            return {**expanded, "report_packet_id": packet_id}
        except (WorkspaceError, ValidationError) as exc:
            from workspace import investigation_phases as stages

            if stages.active(b):
                await stages.settle(
                    b,
                    [],
                    [
                        (
                            "PacketSelection",
                            {"status": "rejected", "reason": str(exc)[:1000]},
                        )
                    ],
                )
            if "packet_stale" in str(exc):
                raise  # resume the caller's bounded local review loop
            await b.repository.save(
                b.incident_id,
                "report_proposal",
                {
                    "id": new_id("proposal"),
                    "packet_id": packet_id,
                    "selection": proposal.model_dump()
                    if hasattr(proposal, "model_dump")
                    else proposal,
                    "errors": [str(exc)[:1000]],
                    "response_id": getattr(b, "last_response_id", None),
                    "context_id": b.last_context_id,
                },
            )
            from workspace import model_audit

            await model_audit.processed(
                b,
                "expand_report_selection",
                {"status": "rejected", "reason": str(exc)[:1000]},
            )
            used = getattr(b, "packet_corrections", 0)
            if (
                not stages.active(b) and used >= settings.SUPERVISOR_MAX_CORRECTIONS
            ) or (stages.active(b) and not stages.remaining(b, "repairing")):
                raise
            b.packet_corrections = used + 1
            messages.append(
                HumanMessage(
                    content="Correct only this selection error: "
                    + str(exc)[:1000]
                    + "\nCurrent valid selection: "
                    + json.dumps(
                        {
                            "packet_id": packet_id,
                            "statement_ids": [
                                r["statement_id"] for r in packet["statements"]
                            ],
                            "unresolved_question_ids": packet[
                                "unresolved_question_ids"
                            ],
                        }
                    )
                )
            )
