"""Run-issued execution capabilities and mechanical native request boundaries."""

from dataclasses import dataclass, field

from models.native_tasks import Dataset, Window, timestamp
from workspace.native_materials import BUSINESS_TOOLS


class LocalRejection(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.error = {
            "code": code,
            "message": message,
            "source": "rcagent",
            "stage": "validation",
            "mcp_call_started": False,
            **details,
        }


@dataclass(eq=False)
class TaskExecution:
    task_id: str
    conversation_id: str
    spec: object
    seed: bool = False
    status: str = "queued"
    role: str = "worker"
    dataset: object = None
    source_ids: list = field(default_factory=list)
    scope_changed: bool = False
    business_started: int = 0
    target_started: int = 0
    submission_index: int = 0
    discovered_tools: object = field(default_factory=dict)
    revoked: bool = False
    window_kind: str | None = None
    handoff_facts: dict | None = None
    handoff_reason: str | None = None

    def __post_init__(self):
        self.dataset = self.spec.dataset


def reject(message, path=None):
    raise LocalRejection("scope_change_required", message, path=path)


def check_scope(context, name, arguments, *, lock=False):
    """Called in wire order before concurrency, and again at actual send."""
    spec = context.spec
    from platforms.business_policy import check_business_scope, BUSINESS_READS
    if name in BUSINESS_READS:
        check_business_scope(context, name, arguments)
        return
    if name not in spec.effective_tools(context.discovered_tools):
        reject("Tool is outside task effective_tools", "/tools")
    business = name in BUSINESS_TOOLS
    if business and name not in spec.business_tools:
        reject("This business tool is outside the assigned family", "/business_tools")
    body = arguments.get("body", {})
    if not isinstance(body, dict):
        body = {}
    dataset = None
    if name in {"insights_query_list", "insights_query_metric"}:
        try:
            dataset = Dataset(database=body.get("database"), table=body.get("table"))
        except ValueError:
            reject("Request requires body.database and body.table", "/body")
        if not spec.window:
            reject(
                "Assigned window is unknown; do not guess a business window",
                "/body/time_start",
            )
        try:
            window = Window(start=body.get("time_start"), end=body.get("time_end"))
            window.seconds()
        except (ValueError, TypeError):
            reject(
                "Explicit Unix seconds time_start/time_end required", "/body/time_start"
            )
        if window.start < spec.window.start or window.end > spec.window.end:
            reject(
                "Request expands assigned window; ask Planner for a comparison task",
                "/body/time_start",
            )
    elif name == "insights_list_cpu_events":
        try:
            if not all(
                isinstance(arguments.get(k), str) for k in ("start_time", "end_time")
            ):
                raise ValueError()
            start, end = (timestamp(arguments[k]) for k in ("start_time", "end_time"))
            if (
                not spec.window
                or start < spec.window.start
                or end > spec.window.end
                or end <= start
            ):
                raise ValueError()
        except (ValueError, TypeError):
            reject(
                "Explicit RFC3339 start_time/end_time inside assigned window required",
                "/start_time",
            )
    elif name in {"insights_get_tags", "insights_describe_query"}:
        if arguments.get("database") or arguments.get("table"):
            dataset = (arguments.get("database"), arguments.get("table"))
    elif name in {"insights_meta_metrics", "insights_meta_tag_values"}:
        dataset = (body.get("db"), body.get("table"))
    if dataset is not None:
        pair = (
            (dataset.database, dataset.table)
            if isinstance(dataset, Dataset)
            else dataset
        )
        if context.dataset and pair != (
            context.dataset.database,
            context.dataset.table,
        ):
            reject(
                "Dataset differs from assigned/locked dataset",
                "/body" if body else "/database",
            )
        if business and context.dataset is None and lock:
            context.dataset = dataset
    if (
        name == "insights_list_tables"
        and context.dataset
        and arguments.get("database") != context.dataset.database
    ):
        reject("list_tables is limited to the assigned database", "/database")


class TaskAuthority:
    def __init__(self):
        self.contexts = {}

    def register(self, context):
        self.contexts[context.task_id] = context

    def verify(self, context, name, arguments, *, lock=False):
        if (
            not isinstance(context, TaskExecution)
            or self.contexts.get(context.task_id) is not context
            or context.role not in {"worker", "planner"}
            or context.status != "running"
            or context.revoked
        ):
            raise LocalRejection(
                "execution_context_required",
                "A current run-issued running Worker or single-use Planner capability is required",
            )
        if context.role == "planner" and name != "insights_query_metrics":
            raise LocalRejection(
                "planner_direct_scope",
                "Planner direct execution is limited to insights_query_metrics",
            )
        check_scope(context, name, arguments, lock=lock)
