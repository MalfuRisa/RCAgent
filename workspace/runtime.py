"""Explicit node bindings, propagated only within the node's async context."""

import asyncio
import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from config import settings
from .repository import Repository, WorkspaceError, new_id, utcnow


@dataclass
class Binding:
    repository: Repository
    incident_id: str
    run_id: str
    role: str
    deadline: float
    task_id: str | None = None
    attempt_id: str | None = None
    resource_id: str | None = None
    mcp_calls: int = 0
    reads: int = 0
    model_steps: int = 0
    knowledge_rounds: int = 0
    last_context_id: str | None = None
    focus_resource_ids: list[str] = field(default_factory=list)
    query_window: dict = field(default_factory=dict)
    query_policy: dict = field(default_factory=dict)
    reuse_calls: int = 0
    checkpoint_required: bool = False
    local_model_limit: int | None = None
    context_preparers: dict = field(default_factory=dict)
    lifecycle_mode: str | None = (
        None  # None follows configuration; the unified role scope sets active.
    )
    capability_catalog: object | None = None
    last_response_id: str | None = None
    recovery_used: bool = False
    recovery_view: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def remaining(self):
        seconds = self.deadline - time.time()
        if seconds <= 0:
            raise TimeoutError("investigation deadline exceeded")
        return seconds


current = contextvars.ContextVar("incident_node_binding", default=None)


@contextmanager
def bind(binding):
    token = current.set(binding)
    try:
        yield binding
    finally:
        current.reset(token)


def from_state(state, role):
    result = Binding(
        Repository(state["workspace_root"]),
        state["incident_id"],
        state["run_id"],
        role,
        state["deadline"],
        state.get("task_id"),
        state.get("attempt_id"),
        state.get("resource_id"),
    )
    result.query_window, result.query_policy = (
        state.get("query_window", {}),
        state.get("query_policy", {}),
    )
    return result


async def initialize_incident(state):
    repo = Repository(settings.WORKSPACE_ROOT)
    await repo.initialize()
    from tools.incident_text import resolve_incident_scope

    scope = resolve_incident_scope(state)
    cluster = state.get("target_cluster") or scope.cluster
    if cluster in ("unknown", ""):
        cluster = None
    access = {"cluster": cluster, "namespaces": settings.ALLOWED_NAMESPACES}
    incident = state.get("incident_id") or new_id("incident")
    continuing = bool(state.get("incident_id"))
    from .windows import normalize_window

    start_time, end_time = scope.facts.window()
    window = normalize_window(
        state.get("query_window") or {"start_time": start_time, "end_time": end_time}
    )
    policy = state.get("query_policy", {})
    if (
        not isinstance(policy, dict)
        or set(policy) - {"force_refresh", "allow_partial"}
        or any(type(v) is not bool for v in policy.values())
    ):
        raise WorkspaceError(
            "query_policy accepts only boolean force_refresh and allow_partial"
        )

    def create(db):
        row = db.execute(
            "SELECT scope FROM incidents WHERE id=?", (incident,)
        ).fetchone()
        if continuing:
            import json

            if not row or json.loads(row[0]) != access:
                raise WorkspaceError("continuation scope does not match incident")
        else:
            import json

            db.execute(
                "INSERT INTO incidents VALUES (?,?)", (incident, json.dumps(access))
            )
        run = new_id("run")
        repo.put(
            db,
            incident,
            "run",
            {
                "id": run,
                "started_at": utcnow(),
                "scope": access,
                "query_window": window,
                "query_policy": policy,
                "execution_status": "incomplete",
                "execution_error": None,
                "stop_reason": None,
                "secondary_stop_reasons": [],
                "execution_errors": [],
            },
        )
        return run

    run = await repo.transact(create)
    return {
        "incident_id": incident,
        "run_id": run,
        "workspace_root": str(repo.root),
        "deadline": time.time() + settings.INVESTIGATION_TIMEOUT_SECONDS,
        "supervisor_rounds": 0,
        "no_progress_rounds": 0,
        "progress_signature": "",
        "workflow_stage": "triage",
        "query_window": window,
        "query_policy": policy,
        "execution_status": "incomplete",
        "execution_error": None,
        "stop_reason": None,
        "secondary_stop_reasons": [],
        "execution_errors": [],
    }


async def invoke_model(model, messages, *, phase="request", request=None):
    """Always async; persist the exact visible input before invoking the model."""
    binding = current.get()
    from .model_requests import send

    if binding is None:
        return await send(model, messages)
    from .model_recovery import invoke_with_recovery

    return await invoke_with_recovery(
        binding, model, messages, phase=phase, request=request
    )


async def audited_send(model, prepared, binding, manifest, assembly_ms):
    """One response/error audit boundary for ordinary and reserved repair calls."""
    from .model_requests import send
    from . import model_audit

    try:
        result = await send(model, prepared, binding, manifest, assembly_ms)
    except BaseException as exc:
        from .model_requests import safe_message

        await asyncio.shield(
            model_audit.response(
                binding,
                manifest,
                error=getattr(
                    exc,
                    "detail",
                    {"type": type(exc).__name__, "message": safe_message(exc)},
                ),
            )
        )
        await asyncio.shield(
            model_audit.processed(
                binding,
                "model_request",
                {
                    "status": "failed",
                    "response_available": False,
                    "type": type(exc).__name__,
                },
            )
        )
        raise
    await model_audit.response(binding, manifest, result=result)
    return result


async def invoke_stage_repair(model, pending, request):
    """Only a reserved, owner-verified pending proposal can bypass workbench assembly."""
    binding = current.get()
    if binding is None:
        raise WorkspaceError("stage_repair_conflict: missing role binding")
    from .stage_pending import check_guard

    await binding.repository.transact(lambda db: check_guard(binding, db, pending))
    if not pending.get("repair_used") or pending.get("status") != "reserved":
        raise WorkspaceError("stage_repair_conflict: Repair slot not reserved")
    binding.remaining()
    from .model_recovery import step_available

    try:
        step_available(binding)
    except WorkspaceError as error:
        raise WorkspaceError(
            "stage_repair_budget_exhausted: no role model steps remain"
        ) from error

    def claim(db):
        from .stage_pending import check_guard

        check_guard(binding, db, pending)
        row = binding.repository.get(
            db, binding.incident_id, pending["id"], "stage_pending"
        )
        if row.get("status") != "reserved":
            raise WorkspaceError(
                "stage_repair_budget_exhausted: logical call already claimed"
            )
        row["status"] = "preparing"
        binding.repository.put(
            db, binding.incident_id, "stage_pending", row, row["revision"]
        )

    await binding.repository.transact(claim)
    from context.stage_repair import prepare_repair
    from .model_requests import send

    started = time.monotonic()
    prepared, manifest = await prepare_repair(binding, pending, request)
    from .investigation_phases import reserve

    await reserve(binding)
    binding.model_steps += 1
    reference = new_id("model_call")
    manifest.update(
        model_call_reference=reference,
        model_phase="stage_repair",
        total_role_model_steps=binding.model_steps,
        knowledge_rounds_used=binding.knowledge_rounds,
    )
    await binding.repository.save(binding.incident_id, "context", manifest)
    binding.last_context_id = manifest["id"]
    return await audited_send(
        model, prepared, binding, manifest, (time.monotonic() - started) * 1000
    )


async def record_execution(
    binding, status, error=None, stop_reason=None, *, execution=None
):
    from .execution import normalize_execution

    result = (
        execution
        if execution is not None
        else normalize_execution(status, error, stop_reason)
    )

    def update(db):
        run = binding.repository.get(db, binding.incident_id, binding.run_id, "run")
        run.update(**result, finished_at=utcnow())
        return binding.repository.put(
            db, binding.incident_id, "run", run, run["revision"], "run_finished"
        )

    return await binding.repository.transact(update)
