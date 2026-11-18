"""Local current/history material catalog over existing records and explicit archives."""

import base64
import hashlib
import json
from pathlib import Path
from langchain_core.tools import tool
from config import settings
from .runtime import current
from .repository import WorkspaceError
from .investigations import digest
from .receipts import new_receipt, publish_read
from .paging import resolve_max_chars, fit_text, fit_list

CURRENT_KINDS = (
    "evidence",
    "question",
    "observation",
    "judgment",
    "stage_commit",
    "checkpoint",
    "assessment",
    "task",
)
HISTORY_FILES = {"report.md", "run-analysis.json", "outcome.json"}


def encode_cursor(signature, offset):
    return base64.urlsafe_b64encode(json.dumps([signature, offset]).encode()).decode()


def position(value, signature):
    if not value:
        return 0
    try:
        actual, offset = json.loads(base64.urlsafe_b64decode(value))
        if actual == signature and type(offset) is int and offset >= 0:
            return offset
    except (ValueError, TypeError):
        pass
    raise WorkspaceError("invalid_cursor: scope/query/filter/source snapshot mismatch")


def history_catalog():
    base = Path(settings.INVESTIGATION_HISTORY_ROOT).resolve()
    try:
        manifest = json.loads(
            Path(settings.INVESTIGATION_HISTORY_MANIFEST).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        return [], [{"status": "unavailable", "reason": type(error).__name__}]
    rows, failures = [], []
    for case in manifest.get("cases", []):
        try:
            root = (base / case["root"]).resolve()
            if not root.is_relative_to(base):
                raise ValueError("history root outside configured material root")
            for filename in case.get("files", []):
                path = (root / filename).resolve()
                if filename not in HISTORY_FILES or not path.is_relative_to(root):
                    raise ValueError("history file outside explicit allowlist")
                text = path.read_text(encoding="utf-8")
                sha = hashlib.sha256(text.encode()).hexdigest()
                rows.append(
                    {
                        "material_id": "history_"
                        + digest([case["case_id"], filename, sha])[:24],
                        "origin": "history",
                        "run_id": case.get("run_id"),
                        "case_id": case["case_id"],
                        "title": case["case_id"] + "/" + filename,
                        "source_kind": "historical_investigation",
                        "time": case.get("time"),
                        "verification_type": case.get("verification_type", "unknown"),
                        "conclusion_status": case.get("conclusion_status", "unknown"),
                        "environment": case.get("environment"),
                        "target": case.get("target"),
                        "query_window": case.get("query_window"),
                        "known_gaps": case.get("known_gaps", []),
                        "source_hash": sha,
                        "_text": text,
                        "_path": path,
                    }
                )
        except (OSError, ValueError, KeyError) as error:
            failures.append(
                {
                    "case_id": case.get("case_id"),
                    "status": "unavailable",
                    "reason": str(error)[:200],
                }
            )
    return rows, failures


async def current_catalog(b):
    rows = []
    for kind in CURRENT_KINDS:
        for value in await b.repository.all(b.incident_id, kind):
            if value.get("run_id") != b.run_id:
                continue
            rows.append(
                {
                    "material_id": "current_"
                    + digest([b.run_id, kind, value["id"], value["revision"]])[:24],
                    "origin": "current",
                    "run_id": b.run_id,
                    "source_kind": kind,
                    "source_id": value["id"],
                    "source_revision": value["revision"],
                    "title": value.get("tool_name")
                    or value.get("text")
                    or value.get("field")
                    or value["id"],
                    "time": value.get("requested_at") or value.get("created_at"),
                    "question_ids": value.get("question_ids")
                    or ([value["question_id"]] if value.get("question_id") else []),
                    "resource_ids": value.get("resource_ids")
                    or value.get("target_resource_ids")
                    or [],
                    "status": value.get("source_status") or value.get("status"),
                    "verification_type": "current_run_record",
                    "conclusion_status": value.get("answer_status", "unknown"),
                    "_text": json.dumps(value, ensure_ascii=False),
                }
            )
    return rows, []


async def catalog(b, scope):
    if scope == "current":
        return await current_catalog(b)
    if scope == "history":
        return history_catalog()
    raise WorkspaceError("invalid_shape: scope must be current or history")


def public(row):
    return {k: v for k, v in row.items() if not k.startswith("_")}


@tool
async def search_investigation_materials(
    query: str = "",
    scope: str = "current",
    filters: dict | None = None,
    cursor: str = "",
    limit: int = 10,
) -> dict:
    """Search bounded current records or explicitly mounted historical investigations; snippets are not read proofs."""
    b = current.get()
    b.remaining()
    from .investigation_phases import guard

    await guard(b, "search_investigation_materials")
    from .investigation_phases import active

    if scope == "history" and active(b) and b.phase_budget["phase"] != "investigating":
        raise WorkspaceError(
            "phase_disallowed: broad history search stopped; read a previously selected historical material"
        )
    if not 1 <= limit <= 50:
        raise WorkspaceError("invalid_shape: limit must be 1..50")
    filters = filters or {}
    if set(filters) - {
        "question_id",
        "resource_id",
        "source_kind",
        "case_id",
        "status",
    }:
        raise WorkspaceError("invalid_shape: unknown material filter")
    rows, unavailable = await catalog(b, scope)
    signature = digest(
        [
            b.incident_id,
            b.run_id,
            b.role,
            b.task_id,
            scope,
            query,
            filters,
            [(r["material_id"], r.get("source_revision")) for r in rows],
        ]
    )
    offset = position(cursor, signature)

    def matches(r):
        return all(
            str(term).casefold() in r["_text"].casefold() for term in query.split()
        ) and all(
            v in r.get(k + "s", [])
            if k in ("question_id", "resource_id")
            else r.get(k) == v
            for k, v in filters.items()
        )

    rows = [r for r in rows if matches(r)]
    results = [
        {
            **public(r),
            "excerpt": r["_text"][:180],
            "readable_entry": "read_investigation_material",
            "snippet_is_not_read_coverage": True,
        }
        for r in rows
    ]

    def build(items):
        next_offset = offset + len(items)
        return {
            "items": items,
            "scope": scope,
            "truncated": next_offset < len(results),
            "next_cursor": encode_cursor(signature, next_offset)
            if next_offset < len(results)
            else None,
            "unavailable": unavailable[:3],
        }

    items, _, too_small = fit_list(
        results, offset, min(len(results), offset + limit), resolve_max_chars(0), build
    )
    if too_small:
        raise WorkspaceError(
            "invalid_shape: material metadata exceeds page budget; narrow filters"
        )
    await b.repository.transact(
        lambda db: b.repository.event(
            db,
            b.incident_id,
            b.task_id or b.incident_id,
            "material_search",
            {"scope": scope, "query": query, "count": len(items)},
        )
    )
    return build(items)


@tool
async def read_investigation_material(
    material_id: str, selector: str = "", cursor: str = "", max_chars: int = 0
) -> dict:
    """Precisely read an indexed material; no arbitrary filesystem paths or remote fetches."""
    b = current.get()
    b.remaining()
    from .investigation_phases import guard

    await guard(b, "read_investigation_material")
    scope = "history" if material_id.startswith("history_") else "current"
    rows, _ = await catalog(b, scope)
    row = next((r for r in rows if r["material_id"] == material_id), None)
    if row is None:
        raise WorkspaceError(
            "source_unavailable_or_stale: unknown indexed material; search current snapshot"
        )
    if scope == "current":
        cap = resolve_max_chars(max_chars)
        if cap < 1024:
            raise WorkspaceError(
                "invalid_shape: material wrapper needs max_chars >= 1024; use direct field reader for smaller pages"
            )
        signature = digest(
            [b.incident_id, b.run_id, b.role, b.task_id, material_id, selector]
        )
        offset = position(cursor, signature)
        from .read_tools import (
            read_evidence,
            read_investigation,
            read_task,
            read_assessment,
        )

        kind = row["source_kind"]
        if kind == "evidence":
            content = await read_evidence.ainvoke(
                {
                    "evidence_id": row["source_id"],
                    "field": selector,
                    "char_offset": offset,
                    "max_chars": cap - 512,
                }
            )
        elif kind in ("task", "assessment"):
            reader = read_task if kind == "task" else read_assessment
            content = await reader.ainvoke(
                {
                    kind + "_id": row["source_id"],
                    "field": selector,
                    "char_offset": offset,
                    "max_chars": cap - 512,
                }
            )
        else:
            content = await read_investigation.ainvoke(
                {
                    "kind": kind,
                    "record_id": row["source_id"],
                    "field": selector,
                    "char_offset": offset,
                    "max_chars": cap - 512,
                }
            )
        nxt = content.get("next_char_offset")
        return {
            "origin": "current",
            "material_id": material_id,
            "content": content,
            "next_cursor": encode_cursor(signature, nxt) if nxt is not None else None,
        }
    from .investigation_phases import active

    if active(b) and b.phase_budget["phase"] != "investigating":
        receipts = await b.repository.all(b.incident_id, "read_receipt")
        if not any(
            r.get("source_kind") == "history"
            and r.get("source_id") == material_id
            and r.get("run_id") == b.run_id
            and r.get("role") == b.role
            and r.get("task_id") == b.task_id
            for r in receipts
        ):
            raise WorkspaceError(
                "phase_disallowed: historical material not previously selected"
            )
    text = row["_text"]
    if selector:
        if row["_path"].suffix != ".json":
            raise WorkspaceError(
                "invalid_shape: selectors apply only to registered JSON documents"
            )
        from .read_tools import select_field

        text = json.dumps(select_field(json.loads(text), selector), ensure_ascii=False)
    signature = digest(
        [
            b.incident_id,
            b.run_id,
            b.role,
            b.task_id,
            material_id,
            selector,
            row["source_hash"],
        ]
    )
    offset = position(cursor, signature)
    cap = resolve_max_chars(max_chars)
    receipt = new_receipt(
        b,
        tool_name="read_investigation_material",
        source_kind="history",
        source_id=material_id,
        source_hash=row["source_hash"],
        field=selector or None,
        source_scope={"origin": "history", "case_id": row["case_id"]},
    )

    def build(chunk):
        end = offset + len(chunk)
        return {
            "origin": "history",
            "historic": True,
            "material_id": material_id,
            "read_receipt_id": receipt["id"],
            "verification_type": row["verification_type"],
            "conclusion_status": row["conclusion_status"],
            "case_id": row["case_id"],
            "text": chunk,
            "range": [offset, end],
            "range_unit": "chars",
            "truncated": end < len(text),
            "next_cursor": encode_cursor(signature, end) if end < len(text) else None,
            "note": "Historical experience/interpretation, not current-run field evidence",
        }

    chunk, end, more = fit_text(text, offset, cap, build)
    if chunk is None:
        raise WorkspaceError(
            "invalid_shape: max_chars too small for historical metadata"
        )
    payload = build(chunk)
    receipt.update(
        range=[offset, end],
        range_unit="chars",
        truncated=more,
        has_more=more,
        next=end if more else None,
    )
    return await publish_read(b, receipt, payload, max_chars=cap)
