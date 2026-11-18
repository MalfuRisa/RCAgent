"""TypedDict state definitions for LangGraph agents."""
from typing import TypedDict, List, Annotated, Dict
from langgraph.graph.message import add_messages, AnyMessage
from .schemas import Symptom, RCATask
from .reducers import merge_rca_analyses


class WorkspaceState(TypedDict):
    # Owned investigation run fields; old fields below remain for saved result compatibility.
    native_budget: object
    review_mode: str
    runtime: str
    runtime_version: str
    output_directory: str
    session_id: str
    conversation_id: str
    answer_status: str
    task: str
    question_ids: List[str]
    planning_source: str
    query_window: dict
    query_policy: dict
    execution_status: str
    execution_error: dict
    execution_errors: List[dict]
    secondary_stop_reasons: List[str]
    evidence_status: str
    report_items: List[dict]
    report_packet_id: str
    incident_id: str
    run_id: str
    workspace_root: str
    deadline: float
    task_id: str
    attempt_id: str
    resource_id: str
    tasks_to_execute_ids: List[str]
    supervisor_rounds: int
    no_progress_rounds: int
    progress_signature: str
    report_claims: List[dict]
    stop_reason: str
    scheduled_attempts: Dict[str, str]
    workflow_stage: str


class TriageAgentState(WorkspaceState):
    """State for the Triage Agent"""
    app_name: str
    app_summary: str
    target_namespace: str
    target_cluster: str
    target_resource: str
    trace_service_starting_point: str
    #: Optional natural-language incident description used for target discovery.
    incident_text: str
    problematic_pods: dict
    slow_traces: dict
    problematic_metrics: dict
    problematic_traces: dict
    symptoms: List[Symptom]
    prompts_config: Dict[str, str]
    #: ``discovered`` | ``awaiting_confirmation`` | ``insufficient`` | ``discovery_failed``
    triage_status: str
    #: What Triage needs from the operator when it cannot proceed on its own.
    clarification: dict
    #: Auditable record: Gateway facts, agent inferences and data gaps.
    discovery_record: dict


class PlannerAgentState(WorkspaceState):
    """State for the Planner Agent"""
    app_name: str
    app_summary: str
    target_namespace: str
    target_cluster: str
    target_resource: str
    incident_text: str
    symptoms: List[Symptom]
    rca_tasks: List[RCATask]
    prompts_config: Dict[str, str]
    discovery_record: dict


class RcaAgentState(WorkspaceState):
    """State for the RCA Worker Agent"""
    messages: Annotated[list[AnyMessage], add_messages]
    rca_app_summary: str
    rca_target_namespace: str
    rca_target_cluster: str
    rca_task: RCATask
    insights: list[str]
    prev_steps: list[str]
    rca_output: dict
    rca_analyses_list: list[dict]
    rca_prompts_config: Dict[str, str]


class SupervisorAgentState(WorkspaceState):
    """State for the Supervisor Agent"""
    app_name: str
    app_summary: str
    symptoms: List[Symptom]
    rca_analyses_list: List[dict]
    final_report: dict
    rca_tasks: List[RCATask]
    tasks_to_be_executed: List[int]
    prompts_config: Dict[str, str]


class SreParentState(WorkspaceState):
    """Parent state for the complete SRE workflow"""
    app_name: str
    app_summary: str
    target_namespace: str
    target_cluster: str
    target_resource: str
    trace_service_starting_point: str
    #: Optional natural-language incident description (e.g. "payment 服务最近 15 分钟报 500").
    #: Structured target_* fields always take precedence over anything parsed here.
    incident_text: str

    # Triage Agent
    problematic_pods: dict
    slow_traces: dict
    problematic_metrics: dict
    problematic_traces: dict
    symptoms: List[Symptom]
    #: Set by Triage: ``discovered`` | ``awaiting_confirmation`` | ``insufficient``
    #: | ``discovery_failed``. The workflow stops early when the target is not unique.
    triage_status: str
    clarification: dict
    discovery_record: dict

    # Planner agent
    rca_tasks: List[RCATask]

    # RCA Worker agent
    rca_analyses_list: Annotated[list[dict], merge_rca_analyses]

    # Tasks to be executed by the RCA agent
    tasks_to_be_executed: List[int]

    # Supervisor agent
    final_report: dict

    # Prompt configuration
    prompts_config: Dict[str, str]
