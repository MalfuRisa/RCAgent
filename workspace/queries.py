"""Exactly one durable record per actual operation, independent of callbacks."""

import asyncio
import time
import inspect
from config import settings
from .artifacts import write_artifact, read_artifact_raw
from .repository import WorkspaceError, new_id, utcnow
from .resources import (
    check_query,
    register,
    accept_inventory,
    query_kind,
    candidates,
    scope,
    allowed,
)
from .evidence_targets import evidence_targets, query_matches
from workspace import evidence_records


def metadata(raw):
    meta = raw.get("meta") or {} if isinstance(raw, dict) else {}
    result = {}
    for key in ("warnings", "data_gaps"):
        result[key] = (raw.get(key) or []) + (meta.get(key) or [])
    for key in ("status", "truncated", "freshness", "data_source"):
        result[key] = raw.get(key, meta.get(key))
    if not isinstance(result["status"], str):
        result["status"] = meta.get("status") if isinstance(meta.get("status"), str) else None
    result["freshness"] = raw.get("freshness", meta.get("data_freshness", result["freshness"]))
    return result


async def save_query(
    binding,
    operation_id,
    name,
    args,
    raw,
    requested,
    duration,
    transport="ok",
    tool_call_id=None,
    reuse_key=None,
):
    # File first, DB references second; an IO failure cannot issue an evidence ID.
    artifact = await write_artifact(binding, raw)
    meta = metadata(raw)
    all_resources = await binding.repository.all(binding.incident_id, "resource")
    expected_kind = query_kind(name, args)
    matching = evidence_targets(
        all_resources, name, args, expected_kind, candidates(raw), await scope(binding)
    )
    evidence = {
        **evidence_records.query_record(
            binding, operation_id, name, args, artifact, requested, duration, tool_call_id
        ),
        "operation_id": operation_id,
        "resource_ids": matching,
        "resource_revisions": {
            r["id"]: r["revision"] for r in all_resources if r["id"] in matching
        },
        "resource_identities": [
            {k: r.get(k) for k in ("id", "revision", "kind", "name", "cluster", "namespace", "uid")}
            for r in all_resources
            if r["id"] in matching
        ],
        "transport_status": transport,
        "source_status": meta.pop("status"),
        "observations": raw if transport == "ok" else {},
        "time_range": raw.get("time_range", (raw.get("meta") or {}).get("time_range"))
        or {k: args[k] for k in ("start_time", "end_time") if k in args},
        "origin_evidence_ids": [],
        **meta,
    }
    from tools.contracts import VERSION, SCHEMA_FINGERPRINTS
    from .windows import returned_window

    evidence.update(
        contract_version=VERSION,
        schema_fingerprint=SCHEMA_FINGERPRINTS.get(name),
        reuse_key=reuse_key,
        returned_time_range=returned_window(raw),
    )

    def commit(db):
        existing = db.execute(
            "SELECT id FROM records WHERE id=? AND incident=?", (operation_id, binding.incident_id)
        ).fetchone()
        if existing:
            return binding.repository.get(db, binding.incident_id, operation_id, "evidence")
        binding.repository.put(db, binding.incident_id, "artifact", artifact)
        return binding.repository.put(
            db, binding.incident_id, "evidence", evidence, action="query_completed"
        )

    evidence = await binding.repository.transact(commit)
    if name in ("resolve_pod", "search_k8s_targets") and transport == "ok":
        await accept_inventory(binding, raw, args, evidence["id"])
    elif name == "get_service_backends" and transport == "ok":
        await accept_inventory(
            binding, raw, {**args, "verified_backend_inventory": True}, evidence["id"]
        )
    return evidence


async def execute(
    binding, name, args, call, tool_call_id=None, *, force_refresh=None, allow_partial=None
):
    from .windows import request_window
    from .resources import refresh_for_query
    from tools.contracts import unsupported_scope
    from .query_reuse import reuse_key, find, shared, annotate

    try:
        args = await request_window(binding, name, args)
        access = await scope(binding)
        unsupported = unsupported_scope(name, args, access)
        if unsupported:
            raise WorkspaceError(
                "unsupported_scope: "
                + ",".join(unsupported)
                + "; use verified backend Pod summaries; do not remove incident scope"
            )
        await refresh_for_query(binding, name, args)
        await check_query(binding, name, args)
    except WorkspaceError as exc:
        reason = str(exc)
        await binding.repository.transact(
            lambda db: binding.repository.event(
                db,
                binding.incident_id,
                binding.task_id,
                "query_rejected",
                {"tool": name, "arguments": args, "reason": reason, "tool_call_id": tool_call_id},
            )
        )
        return {
            "status": "error",
            "error_type": "unsupported_scope"
            if reason.startswith("unsupported_scope")
            else "identity_or_scope_rejected",
            "reason": reason,
            "rejected_fields": list(unsupported) if "unsupported" in locals() else [],
            "allowed_scope": await scope(binding),
            "next_step": "Use current registered identity, explicit namespace/name or UID; refresh expired identity; register separate tasks for other targets",
            "data_gaps": [
                {
                    "source": "workspace_identity",
                    "reason": "identity_or_scope_rejected",
                    "impact": reason,
                }
            ],
        }
    resources = await binding.repository.all(binding.incident_id, "resource")
    resource = next(
        (
            r
            for r in resources
            if (not binding.resource_id or r["id"] == binding.resource_id)
            and query_matches(r, name, args, query_kind(name, args))
        ),
        None,
    )
    key = reuse_key(binding, name, args, resource)
    refresh = (
        binding.query_policy.get("force_refresh", False) if force_refresh is None else force_refresh
    )
    partial = (
        binding.query_policy.get("allow_partial", False) if allow_partial is None else allow_partial
    )
    if type(refresh) is not bool or type(partial) is not bool:
        raise WorkspaceError("execution query options must be boolean")
    if not refresh:
        existing = await find(binding, key, args, partial)
        if existing:
            return await annotate(binding, *existing, "reused", tool_call_id)

    async def operation():
        # Identity may have expired while this operation waited to begin.
        await refresh_for_query(binding, name, args)
        await check_query(binding, name, args)
        # Another independent request may have committed after our first lookup
        # and before singleflight ownership. Recheck before reserving an RPC.
        cached = await find(binding, key, args, partial) if not refresh else None
        if cached:
            return *cached, True
        raw, evidence = await remote_operation(binding, name, args, call, tool_call_id, key)
        return raw, evidence, False

    if key is not None and not refresh:
        (raw, evidence, cached), joined = await shared(binding, key, operation)
        await refresh_for_query(binding, name, args)
        await check_query(binding, name, args)
        return (
            await annotate(
                binding,
                raw,
                evidence,
                "joined" if joined else "reused" if cached else "new",
                tool_call_id,
            )
            if evidence
            else raw
        )
    raw, evidence, _ = await operation()
    return await annotate(binding, raw, evidence, "new", tool_call_id) if evidence else raw


async def remote_operation(binding, name, args, call, tool_call_id, reuse_key):
    async with binding.lock:
        binding.remaining()
        if binding.role == "worker" and binding.mcp_calls >= settings.MAX_TOOL_CALLS:
            await binding.repository.transact(
                lambda db: binding.repository.event(
                    db,
                    binding.incident_id,
                    binding.task_id,
                    "budget_rejected",
                    {"tool": name, "tool_call_id": tool_call_id},
                )
            )
            return {
                "status": "error",
                "error_type": "mcp_budget_exhausted",
                "data_gaps": [
                    {
                        "source": "workspace_budget",
                        "reason": "mcp_budget_exhausted",
                        "impact": "MCP budget exhausted",
                    }
                ],
            }, None
        binding.mcp_calls += 1
    if name == "resolve_pod":
        await register(binding, "pod", args.get("pod"), args.get("namespace"))
    elif name == "search_k8s_targets" and args.get("kind") in ("pod", "service"):
        await register(binding, args["kind"], args.get("query"), args.get("namespace"))
    operation = new_id("evidence")
    requested, started = utcnow(), time.monotonic()
    try:
        from tools.observability_adapter import tool_timeout_seconds

        raw = await asyncio.wait_for(
            call(args) if inspect.signature(call).parameters else call(),
            min(binding.remaining(), tool_timeout_seconds()),
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            save_query(
                binding,
                operation,
                name,
                args,
                {"error_type": "cancelled", "data_gaps": ["query cancelled; result unknown"]},
                requested,
                (time.monotonic() - started) * 1000,
                "cancelled",
                tool_call_id,
                reuse_key,
            )
        )
        raise
    except Exception as exc:
        from tools.observability_adapter import classify_tool_error

        raw = classify_tool_error(name, exc)
    transport = (
        "error"
        if raw.get("error") or raw.get("error_type") or raw.get("isError") or raw.get("is_error")
        else "ok"
    )
    if (
        name == "search_k8s_targets"
        and args.get("kind") == "auto"
        or name == "get_service_backends"
    ) and transport == "ok":
        access = await scope(binding)
        for candidate in candidates(raw):
            kind = str(
                candidate.get("kind", "pod" if name == "get_service_backends" else "")
            ).lower()
            if (
                kind in ("pod", "service")
                and candidate.get("name")
                and candidate.get("namespace")
                and allowed(
                    access, candidate.get("cluster") or args.get("cluster"), candidate["namespace"]
                )
            ):
                # Kind/name come from inventory; unknown input kind stays unknown
                # until this response arrives. Truncation/ambiguity still prevent
                # accept_inventory from marking a unique identity resolved.
                await register(binding, kind, candidate["name"], candidate["namespace"])
    evidence = await save_query(
        binding,
        operation,
        name,
        args,
        raw,
        requested,
        (time.monotonic() - started) * 1000,
        transport,
        tool_call_id,
        reuse_key,
    )
    return raw, evidence


async def read_raw(binding, evidence_id):
    evidence = await binding.repository.read(binding.incident_id, evidence_id, "evidence")
    return await read_artifact_raw(binding, evidence["artifact_id"])
