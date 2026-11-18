"""Pydantic model schemas for SRE Agent."""
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator
from typing import List, Literal, Optional
from .stage_proposals import StageProposal


class Symptom(BaseModel):
    """A symptom observed in the Kubernetes cluster"""
    potential_symptom: str = Field(..., description="Type of symptom observed")
    resource_type: Literal["pod", "service", "node"] = Field(..., description="Type of resource experiencing the issue")
    affected_resource: str = Field(..., description="Exact name of the resource experiencing the issue (no namespace or decorators)")
    evidence: str = Field(..., description="Evidence supporting this symptom identification")


class SymptomList(BaseModel):
    """A list of symptoms observed in the Kubernetes cluster"""
    symptoms: List[Symptom] = Field(default_factory=list, description="List of symptoms observed in the cluster")
    stage_proposal: Optional[StageProposal] = Field(default=None, description="Optional stage handoff proposal")


class RCATask(BaseModel):
    """A RCA task to be performed by the RCA agent"""
    task_id: Optional[str] = None
    resource_id: Optional[str] = None
    execution_status: Literal["pending", "running", "finished", "failed", "cancelled"] = "pending"
    answer_status: Literal["unanswered", "supported", "inconclusive", "not_applicable"] = "unanswered"
    depends_on: List[str] = Field(default_factory=list)
    acceptance_checks: List[str] = Field(default_factory=list)
    question_ids: List[str] = Field(default_factory=list)
    known_observation_ids: List[str] = Field(default_factory=list)
    candidate_judgment_ids: List[str] = Field(default_factory=list)
    priority: int = Field(..., description="Order of execution for this RCA task")
    status: Literal["pending", "in_progress", "completed"] = Field(default="pending", description="Status of the RCA task")
    investigation_goal: str = Field(..., description="Goal of the investigation")
    target_resource: str = Field(..., description="Name of the resource to investigate")
    resource_type: Literal["pod", "service", "node"] = Field(..., description="Type of resource being investigated")
    suggested_tools: List[str] = Field(default_factory=list, description="List of tools suggested for the investigation")


class RCATaskList(BaseModel):
    """A list of RCA tasks to be performed by the RCA agent in parallel"""
    rca_tasks: List[RCATask] = Field(default_factory=list, description="List of RCA tasks to be performed")
    stage_proposal: Optional[StageProposal] = Field(default=None, description="Optional stage handoff proposal")


class RCAAgentExplaination(BaseModel):
    """Aggregates all reasoning steps and insights extracted by the RCA agent at the end of the investigation."""
    steps: List[str] = Field(..., description="Chronological list of all actions or analyses performed by the agent during the investigation")
    insights: List[str] = Field(..., description="Comprehensive list of key findings or insights discovered throughout the investigation")


class FinalReport(BaseModel):
    """The Final report created by the supervisor agent"""
    root_cause: str = Field(..., description="The identified root cause of the incident")
    affected_resources: List[str] = Field(..., description="List of all resources affected by the incident")
    evidence_summary: str = Field(..., description="Summary of evidence from all RCA workers")
    investigation_summary: str = Field(..., description="Overview of the investigation process and findings")
    detection: bool = Field(..., description="Whether a problem was detected in the cluster")
    localization: Optional[List[str]] = Field(
        None,
        description="List of faulty components (i.e., service names) identified as the root cause (if applicable)"
    )

class LookupTarget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['pod', 'service']
    name: str = Field(min_length=1)
    cluster: str = Field(min_length=1)
    namespace: Optional[str] = None


class ReportItem(BaseModel):
    section: Literal['observations', 'hypotheses', 'exclusions', 'limitations', 'next_steps']
    content: str = Field(min_length=1, description='For observations/hypotheses/exclusions, exact copy of referenced same-type claim content, joined with newline; no independent rewrite')
    claim_ids: List[str] = Field(default_factory=list)
    evidence_ids: List[str] = Field(default_factory=list)
    task_ids: List[str] = Field(default_factory=list)


class EvidenceClaim(BaseModel):
    """A scoped, auditable statement; causal support still requires the program gate."""
    model_config = ConfigDict(extra='forbid', revalidate_instances='always')
    type: Literal['observation', 'hypothesis', 'exclusion', 'causal']
    claim_id: Optional[str] = Field(default=None, description='Unique proposal-local ID initially; use program-assigned ID in corrections')
    resource_id: Optional[str] = Field(default=None, min_length=1, description='Verified workbench resource ID; null only for a scoped lookup observation')
    lookup_target: Optional[LookupTarget] = None
    content: str = Field(min_length=1, description='Required nonempty statement text describing the observation, tentative hypothesis or scoped exclusion; not the investigation question or proof of causality')
    task_id: Optional[str] = Field(default=None, description='Related registered investigation task ID, when known')
    question: Optional[str] = Field(default=None, description='Exact related investigation question, when known')
    supporting_evidence_ids: List[str] = Field(min_length=1, description='Nonempty workbench evidence IDs supporting this statement')
    opposing_evidence_ids: List[str] = Field(default_factory=list, description='Known counter evidence IDs; the gate also checks workbench judgments')
    mechanism: Optional[Literal['oom_termination']] = Field(default=None, description='Currently only scoped OOMKilled termination has a direct causal rule')
    limitations: List[str] = Field(min_length=1, description='Explicit limits on scope, time, coverage and unverified triggers')

    @model_validator(mode='after')
    def validate_contract(self):
        if not self.content.strip():
            raise ValueError('Statement content must contain non-whitespace text')
        if self.resource_id is None and self.lookup_target is None:
            raise ValueError('A resource_id or explicit lookup_target is required')
        if any(not text.strip() for text in self.supporting_evidence_ids + self.opposing_evidence_ids + self.limitations):
            raise ValueError('Evidence IDs and limitations must be nonempty strings')
        if self.type == 'causal' and self.mechanism is None:
            raise ValueError('Causal claims require a supported mechanism identifier')
        return self


class PacketSelection(BaseModel):
    """The final model chooses registered statements; the program expands their text."""
    packet_id: str
    selected_statement_ids: List[str]
    conclusion_status: Literal['supported', 'inconclusive']
    unresolved_question_ids: List[str]


class SupervisorDecision(BaseModel):
    """The supervisor's decision to either conclude the investigation or request more data."""
    model_config = ConfigDict(revalidate_instances='always')
    tasks_to_execute_ids: List[str] = Field(default_factory=list)
    stage_proposal: Optional[StageProposal] = Field(default=None, description="Optional stage handoff proposal")
    claims: List[EvidenceClaim] = Field(default_factory=list, description='Typed evidence statements; the workbench gate independently validates identity, mechanism and conflicts')
    report_items: List[ReportItem] = Field(default_factory=list, description='Readable statements with claim/evidence/task references; required for final reports')
    followup_tasks: List[RCATask] = Field(default_factory=list)
    tasks_to_be_executed: List[int] = Field(
        default_factory=list,
        description="A list of task priorities to execute next. Provide this ONLY if the investigation is INCOMPLETE and more data is strictly necessary."
    )
    final_report: Optional[FinalReport] = Field(
        default=None,
        description="Final execution report, including an inconclusive result when proof is unavailable. Requires claims and report_items; never combine with tasks to execute."
    )

class EvaluationResult(BaseModel):
    """Evaluation result for an RCA diagnosis."""
    score: int = Field(..., ge=1, le=5, description="Numeric evaluation score (1-5)")
    reasoning: str = Field(..., description="Very short reasoning for the score (1-2 sentences max)")
