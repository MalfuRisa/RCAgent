"""One query/reuse executor across protocol adapters and telemetry types."""

import asyncio
import hashlib
import json
import time
import jsonschema
import httpx
from mcp.shared.exceptions import McpError
from config import settings
from .repository import WorkspaceError, new_id, utcnow
from .artifacts import write_artifact
from .evidence_records import query_record
from .query_reuse import shared, annotate
from .windows import normalize_window
from . import run_stops


async def query_key(b, catalog, capability, args):
    resources = await b.repository.all(b.incident_id, "resource")
    identities = [
        {
            k: r.get(k)
            for k in (
                "kind",
                "cluster",
                "namespace",
                "uid",
                "platform_id",
                "platform_lcuuid",
            )
        }
        for r in resources
        if r.get("identity_status") == "resolved" and not r.get("replaced_by")
    ]
    access = await b.repository.transact(
        lambda db: json.loads(
            db.execute(
                "SELECT scope FROM incidents WHERE id=?", (b.incident_id,)
            ).fetchone()[0]
        )
    )
    return hashlib.sha256(
        json.dumps(
            {
                "service": catalog.service_identity,
                "scope": access,
                "identities": sorted(
                    identities, key=lambda r: json.dumps(r, sort_keys=True)
                ),
                "tool": capability.name,
                "contract": capability.contract_version,
                "schema": capability.fingerprint,
                "parameters": args,
                "window": b.query_window,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


async def find_reusable(b, key, capability, *, force_refresh, allow_partial):
    if force_refresh:
        return None
    from .queries import read_raw

    for e in reversed(await b.repository.all(b.incident_id, "evidence")):
        if e.get("run_id") != b.run_id:
            continue
        if (
            e.get("reuse_key") != key
            or e.get("transport_status") != "ok"
            or e.get("source_status") not in ("ok", "partial")
        ):
            continue
        if capability.windowed and e.get("returned_time_range") != normalize_window(
            b.query_window
        ):
            continue
        if (
            e.get("truncated")
            or e.get("source_status") == "partial"
            or e.get("data_gaps")
        ) and not allow_partial:
            continue
        return await read_raw(b, e["id"]), e
    return None


async def run_operation(
    b, capability, name, args, key, call_id, *, force_refresh, allow_partial
):
    found = await find_reusable(
        b, key, capability, force_refresh=force_refresh, allow_partial=allow_partial
    )
    if found:
        return *found, True
    if await run_stops.persisted_reason(b):
        raise WorkspaceError("run stopped before query send")
    async with b.lock:
        b.remaining()
        if b.mcp_calls >= settings.MAX_TOOL_CALLS:
            raise WorkspaceError("MCP query budget exhausted")
        b.mcp_calls += 1
    started, requested = time.monotonic(), utcnow()
    cancelled = False
    try:
        result = await asyncio.wait_for(
            capability.invoke(b, args), min(b.remaining(), 40)
        )
        raw = result.raw
        transport = (
            "ok" if result.source_status not in ("error", "cancelled") else "error"
        )
    except asyncio.CancelledError:
        from tools.diagnostic_catalog import QueryResult

        cancelled = True
        raw = {
            "status": "cancelled",
            "error": {
                "type": "CancelledError",
                "message": "Query cancelled; outcome unknown",
            },
        }
        result = QueryResult(
            raw,
            source_status="cancelled",
            data_gaps=["Cancellation leaves the remote result unknown"],
        )
        transport = "cancelled"
    except (
        ValueError,
        WorkspaceError,
        TimeoutError,
        ConnectionError,
        httpx.HTTPError,
        McpError,
    ) as exc:
        from tools.diagnostic_catalog import QueryResult
        from .model_requests import safe_message

        raw = getattr(
            exc,
            "raw_result",
            {
                "error": {"type": type(exc).__name__, "message": safe_message(exc)},
                "status": "error",
            },
        )
        result = QueryResult(
            raw,
            source_status="error",
            data_gaps=[
                "Query failed or quarantined; not zero/healthy: " + safe_message(exc)
            ],
        )
        transport = "error"
        if any(
            word in str(exc).lower()
            for word in ("unauthenticated", "forbidden", "401", "403")
        ):
            await run_stops.stop_run(b, "tool_authentication_failed", {"tool": name})
    artifact = await write_artifact(b, raw)
    resources = {r["id"]: r for r in await b.repository.all(b.incident_id, "resource")}
    refs = [resources[r] for r in result.resource_ids if r in resources]
    e = {
        **query_record(
            b,
            new_id("evidence"),
            name,
            args,
            artifact,
            requested,
            (time.monotonic() - started) * 1000,
            call_id,
        ),
        "resource_ids": result.resource_ids,
        "resource_revisions": {r["id"]: r["revision"] for r in refs},
        "resource_identities": [
            {
                k: r.get(k)
                for k in (
                    "id",
                    "revision",
                    "kind",
                    "name",
                    "cluster",
                    "namespace",
                    "uid",
                    "platform_id",
                )
            }
            for r in refs
        ],
        "transport_status": transport,
        "source_status": result.source_status,
        "observations": raw,
        "time_range": dict(b.query_window) if capability.windowed else {},
        "returned_time_range": normalize_window(result.returned_window)
        if result.returned_window
        else None,
        "origin_evidence_ids": [],
        "warnings": result.warnings,
        "data_gaps": result.data_gaps,
        "truncated": result.truncated,
        "freshness": result.freshness,
        "contract_version": capability.contract_version,
        "schema_fingerprint": capability.fingerprint,
        "reuse_key": key,
    }

    def persist(db):
        b.repository.put(db, b.incident_id, "artifact", artifact)
        return b.repository.put(
            db, b.incident_id, "evidence", e, action="query_completed"
        )

    e = (
        await asyncio.shield(b.repository.transact(persist))
        if cancelled
        else await b.repository.transact(persist)
    )
    if cancelled:
        raise asyncio.CancelledError()
    return raw, e, False


async def execute_query(
    b,
    catalog,
    name,
    arguments,
    call_id=None,
    *,
    force_refresh=False,
    allow_partial=False,
):
    from .investigation_phases import guard

    await guard(b, "query_diagnostics")
    capability = catalog.get(
        name
    )  # Discovery and execution use the same authorized directory.
    if type(force_refresh) is not bool or type(allow_partial) is not bool:
        raise WorkspaceError("query controls must be boolean")
    stopped = await run_stops.persisted_reason(b)
    if stopped:
        raise WorkspaceError("run stopped: " + stopped)
    b.remaining()
    jsonschema.validate(arguments, capability.schema)
    args = await capability.normalize(b, dict(arguments))
    key = await query_key(b, catalog, capability, args)

    existing = await find_reusable(
        b, key, capability, force_refresh=force_refresh, allow_partial=allow_partial
    )
    if existing:
        return await annotate(b, *existing, "reused", call_id)

    from functools import partial

    operation = partial(
        run_operation,
        b,
        capability,
        name,
        args,
        key,
        call_id,
        force_refresh=force_refresh,
        allow_partial=allow_partial,
    )
    if force_refresh:
        raw, e, cached = await operation()
        joined = False
    else:
        (raw, e, cached), joined = await shared(b, key, operation)
    return await annotate(
        b, raw, e, "joined" if joined else "reused" if cached else "new", call_id
    )
