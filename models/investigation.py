"""One role contract for all SRE questions; telemetry belongs to tool arguments."""

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from models.stage_proposals import ObservationProposal
from models.schemas import EvidenceClaim


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeReview(Contract):
    questions: list[str] = Field(min_length=1, max_length=4)
    clarification: str | None = None
    limitations: list[str] = Field(default_factory=list)


class InvestigationTask(Contract):
    investigation_goal: str = Field(min_length=1)
    question_ids: list[str] = Field(min_length=1)
    resource_id: str | None = None
    evidence_needs: list[str] = Field(default_factory=list)
    suggested_capabilities: list[str] = Field(default_factory=list)


class InvestigationPlan(Contract):
    tasks: list[InvestigationTask] = Field(min_length=1, max_length=3)


class LegacyTaskSubmission(Contract):
    candidate_ids: list[str] = Field(default_factory=list)
    observations: list[ObservationProposal] = Field(default_factory=list)
    supporting_observation_ids: list[str] = Field(default_factory=list)
    opposing_observation_ids: list[str] = Field(default_factory=list)
    interpretation: str = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)


class TaskSubmission(Contract):
    supporting_observation_ids: list[str] = Field(default_factory=list)
    opposing_observation_ids: list[str] = Field(default_factory=list)
    interpretation: str = Field(min_length=1)
    limitations: list[str] = Field(min_length=1)
    unresolved_reasons: dict[str, str] = Field(default_factory=dict)


class ReportSubmission(Contract):
    observation_ids: list[str] = Field(default_factory=list)
    candidate_ids: list[str] = Field(default_factory=list)
    observations: list[ObservationProposal] = Field(default_factory=list)
    unresolved_reasons: dict[str, str]
    limitations: list[str] = Field(min_length=1)
    next_steps: list[str] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    claim_observation_refs: dict[str, list[str]] = Field(default_factory=dict)


class ObservationRegistration(Contract):
    candidate_ids: list[str] = Field(default_factory=list)
    observations: list[ObservationProposal] = Field(default_factory=list)
    limitations: list[str] = Field(min_length=1)


class ReportClaimDraft(Contract):
    type: Literal["hypothesis", "exclusion", "causal"]
    content: str = Field(min_length=1)
    resource_id: str = Field(min_length=1)
    observation_ids: list[str] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list)
    mechanism: Literal["oom_termination"] | None = None


class ReportFinalization(Contract):
    """Select registered work; never submit raw evidence proposals at report time."""

    observation_ids: list[str] = Field(default_factory=list)
    unresolved_reasons: dict[str, str]
    limitations: list[str] = Field(min_length=1)
    next_steps: list[str] = Field(default_factory=list)
    claims: list[ReportClaimDraft] = Field(default_factory=list)
