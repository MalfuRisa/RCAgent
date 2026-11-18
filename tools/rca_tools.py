"""RCA-specific tools including submission tool."""
from typing import Annotated
from langgraph.types import Command
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool, InjectedToolCallId
from models.stage_proposals import StageProposal


@tool
def submit_final_diagnosis(
    diagnosis: str,
    reasoning: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    supporting_evidence_ids: list[str] = [],
    opposing_evidence_ids: list[str] = [],
    gaps: list[str] = [],
    stage_proposal: StageProposal | None = None,
    observation_ids: list[str] | None = None,
    checkpoint_id: str | None = None,
    answer_status: str = "inconclusive",
    hypotheses: list[dict] = []
) -> Command:
    """Submit the final diagnosis when investigation is complete.
    
    Args:
        diagnosis: The issue you have identified (without fixing it)
        reasoning: Your reasoning and thought process behind the diagnosis (keep it concise)
        tool_call_id: Injected tool call ID from LangChain
    
    Returns:
        Command to update state and end workflow
    """
    final_response = {
        "diagnosis": diagnosis,
        "reasoning": reasoning,
        "supporting_evidence_ids": supporting_evidence_ids,
        "opposing_evidence_ids": opposing_evidence_ids,
        "gaps": gaps,
        "answer_status": answer_status,
        "hypotheses": hypotheses
    }
    
    return Command(
        update={
            "rca_output": final_response,
            "messages": [
                ToolMessage(
                    content="Final diagnosis submitted successfully. Investigation complete.",
                    tool_call_id=tool_call_id
                )
            ]
        },
        goto="format-output"  # End the loop cycle
    )
