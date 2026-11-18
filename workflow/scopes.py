"""Role binding lifecycle and the existing graph-run error boundary."""

import asyncio
from contextlib import contextmanager
from workspace.runtime import from_state, bind, record_execution
from workspace.repository import WorkspaceError
from workspace.model_requests import ModelRequestError


@contextmanager
def investigation_binding(binding, catalog):
    """Apply and restore the authoritative policy for an already-bound compatibility caller."""
    from context.investigation import common_preparers

    previous = (
        binding.context_preparers,
        binding.lifecycle_mode,
        binding.capability_catalog,
    )
    binding.context_preparers = common_preparers()
    binding.lifecycle_mode = "active"
    binding.capability_catalog = catalog
    try:
        yield binding
    finally:
        (
            binding.context_preparers,
            binding.lifecycle_mode,
            binding.capability_catalog,
        ) = previous


def investigation_node(role, action, catalog_for, model_for):
    """Bind one role and apply the common deadline, cancellation and error policy."""

    async def node(state):
        from context.investigation import common_preparers
        from .errors import execution_error

        with role_scope(state, role, context_preparers=common_preparers()) as binding:
            try:
                from config import settings

                binding.local_model_limit = {
                    "triage": 3,
                    "planner": 5,
                    "supervisor": None,
                }.get(role)
                binding.capability_catalog = await catalog_for(state)
                return await asyncio.wait_for(
                    action(binding, model_for(), state, binding.capability_catalog),
                    binding.remaining(),
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    record_execution(
                        binding,
                        "cancelled",
                        {
                            "code": "execution_cancelled",
                            "message": "workflow cancelled",
                        },
                    )
                )
                raise
            except (WorkspaceError, ModelRequestError, TimeoutError) as error:
                detail = execution_error(error, role)
                await record_execution(binding, "failed", detail)
                return {
                    "execution_status": "failed",
                    "execution_error": detail,
                    "stop_reason": detail["code"],
                    "tasks_to_execute_ids": [],
                }
            except Exception as error:
                from workspace.model_requests import safe_message

                await record_execution(
                    binding,
                    "failed",
                    {
                        "code": role + "_unexpected_failure",
                        "message": safe_message(error),
                    },
                )
                raise

    return node


@contextmanager
def role_scope(state, role, *, context_preparers=None):
    binding = from_state(state, role)
    if context_preparers is not None:
        binding.context_preparers = context_preparers
        from context.investigation import prepare_investigation

        if prepare_investigation in context_preparers.values():
            binding.lifecycle_mode = "active"
    with bind(binding):
        yield binding


async def scoped_graph(graph, state, role, config):
    with role_scope(state, role) as binding:
        try:
            return await asyncio.wait_for(
                graph.ainvoke(state, config), binding.remaining()
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                record_execution(
                    binding,
                    "cancelled",
                    {"code": "execution_cancelled", "message": "workflow cancelled"},
                )
            )
            await asyncio.shield(
                binding.repository.transact(
                    lambda db: binding.repository.event(
                        db,
                        binding.incident_id,
                        binding.task_id,
                        "workflow_cancelled",
                        {"role": role, "code": "execution_cancelled"},
                    )
                )
            )
            raise
        except ModelRequestError as exc:
            return {
                "execution_status": "failed",
                "execution_error": exc.detail,
                "stop_reason": exc.detail["code"],
                "triage_status": "discovered",
                "evidence_status": "partial",
                "rca_tasks": [],
                "symptoms": state.get("symptoms", []),
            }
        except (TimeoutError, WorkspaceError) as exc:
            if role == "triage":
                return {
                    "triage_status": "discovery_failed",
                    "symptoms": [],
                    "execution_status": "failed",
                    "execution_error": {
                        "code": "investigation_deadline"
                        if isinstance(exc, TimeoutError)
                        else "workspace_error",
                        "message": str(exc) or "investigation deadline",
                    },
                    "stop_reason": str(exc) or "investigation_deadline",
                    "clarification": {"reason": str(exc) or "investigation_deadline"},
                }
            if role == "planner":
                return {
                    "rca_tasks": [],
                    "execution_status": "failed",
                    "execution_error": {
                        "code": "planner_incomplete",
                        "message": str(exc) or "investigation_deadline",
                    },
                    "stop_reason": str(exc) or "investigation_deadline",
                }
            raise
