"""Local read-only tools: short by default, exactly traceable, never a data dump.

Contracts (PRD §8, tightened by §17.5/§17.6):

* one metering rule for every entry point: the returned JSON, including the
  receipt id, source info, cursor and markers, never exceeds ``max_chars``;
* list pages report the rows they actually returned (``range``/``next``) and a
  char page is chosen from the remaining space, so pages rejoin exactly and a
  bounded page never skips content;
* an empty catalog is a normal result; ``page_limit_too_small`` only appears
  when rows exist but not even one minimal entry fits;
* every successful read stores a ``read_receipt`` (source, revision, paths,
  windows, limits, archived body) and returns its id; a failed read is audited
  as an error and never claims coverage;
* ``field`` on ``view=metadata`` reads the workbench record, ``auto``/``raw``
  fields read the tool's original response, and both names mark their source.
"""

import json
from contextvars import ContextVar

from langchain_core.tools import tool
from config import settings
from .runtime import current
from .repository import WorkspaceError, new_id
from .queries import read_raw
from .artifacts import read_artifact_raw
from .paging import (
    MIN_PAGE_CHARS,
    PAGE_DEFAULT_LIMIT,
    PAGE_MAX_LIMIT,
    PAGE_SERIALIZED_LIMIT,
    fit_list,
    fit_text,
    resolve_max_chars,
    serialized_chars,
)
from .receipts import new_receipt, publish_read, record_failed_read, source_hash

#: Tool-call id of the local read currently being executed (set by the agent loop).
CURRENT_TOOL_CALL = ContextVar("local_read_tool_call_id", default=None)

DETAIL_ENTRIES = {
    "metadata": "read_evidence(view=metadata)",
    "raw_field": "read_evidence(field=<name>)",
    "raw_text": "read_evidence(field=<name>, char_offset=0)",
}


def binding():
    value = current.get()
    if value is None:
        raise WorkspaceError("workspace not bound")
    value.remaining()
    from .investigation_phases import active

    if (
        not active(value)
        and value.role == "supervisor"
        and value.reads >= settings.SUPERVISOR_MAX_READS
    ):
        raise WorkspaceError("local read budget exhausted")
    value.reads += 1
    return value


def limit_setting(requested):
    """Resolve max_chars, reporting illegal values as a workspace error."""
    try:
        return resolve_max_chars(requested)
    except ValueError as exc:
        raise WorkspaceError(str(exc)) from exc


async def failed(b, tool_name, reference, error):
    await record_failed_read(b, tool_name, reference, error)
    return error


def record(value, revision):
    """Identity of one stored record for a receipt."""
    return {"source_revision": revision, "source_hash": source_hash(value)}


def window_args(evidence):
    """Requested/returned windows and the shared window table of one record."""
    from .windows import normalize_window

    requested = (
        evidence.get("time_range")
        if isinstance(evidence.get("time_range"), dict)
        else None
    )
    returned = (
        evidence.get("returned_time_range")
        if isinstance(evidence.get("returned_time_range"), dict)
        else None
    )
    from context.views import window_table

    windows, refs = window_table([evidence])
    return {
        "windows": windows,
        "requested_window": (requested or {}).get("start_time") is not None
        and requested
        or None,
        "returned_window": (returned or {}).get("start_time") is not None
        and returned
        or None,
        "requested_window_ref": refs.get(
            ((requested or {}).get("start_time"), (requested or {}).get("end_time"))
        ),
        "returned_window_ref": refs.get(
            ((returned or {}).get("start_time"), (returned or {}).get("end_time"))
        ),
    }


def task_windows(task):
    """Task records hold no tool window; identity and scope still travel."""
    return {
        "source_scope": task.get("target_resource_ids") or [],
        "requested_window": task.get("query_window")
        if isinstance(task.get("query_window"), dict)
        else None,
        "returned_window": None,
    }


def window_marker(start_time, end_time):
    if bool(start_time) != bool(end_time):
        raise WorkspaceError("start_time and end_time must be supplied together")
    if not start_time:
        return None
    from .windows import normalize_window

    return normalize_window({"start_time": start_time, "end_time": end_time})


def window_overlaps(evidence, window):
    if window is None:
        return True
    actual = evidence.get("returned_time_range") or evidence.get("time_range")
    if (
        not isinstance(actual, dict)
        or not actual.get("start_time")
        or not actual.get("end_time")
    ):
        return False  # no recorded window: not a match, and not silently assumed equal
    return (
        actual["start_time"] < window["end_time"]
        and actual["end_time"] > window["start_time"]
    )


def matches(evidence, filters):
    if filters.get("resource_id") and filters["resource_id"] not in (
        evidence.get("resource_ids") or []
    ):
        return False
    if (
        filters.get("task_id")
        and evidence.get("producer_task_id") != filters["task_id"]
    ):
        return False
    if filters.get("tool_name") and evidence.get("tool_name") != filters["tool_name"]:
        return False
    if (
        filters.get("source_status")
        and evidence.get("source_status") != filters["source_status"]
    ):
        return False
    return window_overlaps(evidence, filters.get("window"))


def encode_cursor(b, filters, last_rowid, snapshot_max):
    """Cursor binds incident, filters and the rowid upper bound of its snapshot."""
    import base64

    payload = {
        "incident": b.incident_id,
        "filters": json.dumps(filters, sort_keys=True, default=str),
        "last_rowid": last_rowid,
        "snapshot_max": snapshot_max,
    }
    return base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")


def decode_cursor(b, filters, cursor):
    import base64

    try:
        payload = json.loads(
            base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        )
    except Exception as exc:
        raise WorkspaceError("invalid cursor: " + str(exc)) from exc
    if payload.get("incident") != b.incident_id:
        raise WorkspaceError("cursor belongs to another incident")
    if payload.get("filters") != json.dumps(filters, sort_keys=True, default=str):
        raise WorkspaceError("cursor does not match the current filters")
    return int(payload.get("last_rowid") or 0), payload.get("snapshot_max")


@tool
async def get_resource(
    resource_id: str,
    field: str = "",
    view: str = "auto",
    offset: int = 0,
    limit: int = 20,
    char_offset: int | None = None,
    max_chars: int = 0,
) -> dict:
    """Read a registered resource identity and its unknown fields."""
    b = binding()
    limit_chars = limit_setting(max_chars)
    if view not in ("auto", "raw", "metadata"):
        raise WorkspaceError("view must be auto, raw or metadata")
    try:
        stored = await b.repository.read(b.incident_id, resource_id, "resource")
    except WorkspaceError:
        raise await failed(
            b,
            "get_resource",
            resource_id,
            WorkspaceError("resource_id is unknown in this incident: %s" % resource_id),
        )
    result = dict(stored)
    result["unknown_fields"] = [
        k for k in ("cluster", "namespace", "uid") if not result.get(k)
    ]
    receipt = new_receipt(
        b,
        tool_name="get_resource",
        source_kind="resource",
        source_id=resource_id,
        view="auto",
        returned_paths=["*"],
        complete_paths=["*"],
        source_scope={"resource_id": resource_id},
        **record(stored, stored.get("revision")),
    )
    result["read_receipt_id"] = receipt["id"]
    if field or char_offset is not None or view == "raw":
        return await content_page(
            b,
            tool_name="get_resource",
            reference=resource_id,
            identity={"resource_id": resource_id},
            raw=stored,
            field=field,
            offset=offset,
            limit=limit,
            char_offset=char_offset,
            limit_chars=limit_chars,
            view="raw",
            source_marker="resource_record",
            receipt=receipt,
        )
    return await publish_read(
        b, receipt, result, kind="local_read_resource", max_chars=limit_chars
    )


def catalog_envelope(
    b,
    items,
    *,
    start,
    end,
    total,
    has_more,
    cursor_value,
    filters,
    revision,
    view,
    receipt_id,
    placeholder_cursor,
):
    return {
        "items": items,
        "total": total,
        "returned": len(items),
        "range": [start, end],
        "has_more": has_more,
        "next": end if has_more else None,
        "next_cursor": (
            cursor_value if cursor_value is not None else placeholder_cursor
        )
        if has_more
        else None,
        "workspace_revision": revision,
        "filters": {k: v for k, v in filters.items() if v},
        "view": view,
        "view_version": _view_version(),
        "read_receipt_id": receipt_id,
    }


def _view_version():
    from context.views import CONTEXT_VIEW_VERSION

    return CONTEXT_VIEW_VERSION


@tool
async def query_evidence(
    resource_id: str = "",
    task_id: str = "",
    tool_name: str = "",
    source_status: str = "",
    start_time: str = "",
    end_time: str = "",
    view: str = "brief",
    offset: int = 0,
    limit: int = PAGE_DEFAULT_LIMIT,
    cursor: str = "",
    max_chars: int = 0,
) -> dict:
    """List evidence briefly with exact filters and a stable cursor (max 100 rows)."""
    b = binding()
    if view not in ("brief", "minimal"):
        raise WorkspaceError("view must be brief or minimal")
    if offset < 0 or not 1 <= limit <= PAGE_MAX_LIMIT:
        raise WorkspaceError("invalid evidence page")
    if cursor and offset:
        raise WorkspaceError("cursor and a non-zero offset cannot be combined")
    limit_chars = limit_setting(max_chars)
    filters = {
        "resource_id": resource_id or None,
        "task_id": task_id or None,
        "tool_name": tool_name or None,
        "source_status": source_status or None,
        "window": window_marker(start_time, end_time),
    }

    def fetch(db):
        rows = []
        for row in db.execute(
            "SELECT rowid, body FROM records WHERE incident=? AND kind='evidence' ORDER BY rowid",
            (b.incident_id,),
        ):
            body = json.loads(row[1])
            body["_rowid"] = row[0]
            rows.append(body)
        revision = db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE incident=?", (b.incident_id,)
        ).fetchone()[0]
        return rows, revision

    rows, revision = await b.repository.transact(fetch)
    matched = [row for row in rows if matches(row, filters)]
    boundary, snapshot_max = (
        decode_cursor(b, filters, cursor) if cursor else (None, None)
    )
    # `total` stays the snapshot's match count on every page; the boundary only
    # decides which rows this page may return.
    scoped = (
        matched
        if snapshot_max is None
        else [row for row in matched if row["_rowid"] <= snapshot_max]
    )
    snapshot_max = (
        snapshot_max
        if snapshot_max is not None
        else (max(row["_rowid"] for row in scoped) if scoped else 0)
    )
    total = len(scoped)
    after = [row for row in scoped if boundary is None or row["_rowid"] > boundary]
    start = offset
    ceiling = min(len(after), start + limit)
    receipt = new_receipt(
        b,
        tool_name="query_evidence",
        source_kind="evidence_catalog",
        source_id="evidence catalog (%s)" % (tool_name or "all tools"),
        view=view,
        page_range=[start, ceiling],
        range_unit="rows",
        returned_paths=["catalog"],
        complete_paths=[],
        source_scope={"filters": {k: v for k, v in filters.items() if v}},
    )
    from context.views import directory_row, evidence_view, window_table

    def clean(rows_slice):
        return [
            {key: value for key, value in row.items() if key != "_rowid"}
            for row in rows_slice
        ]

    def projected(rows_slice, refs):
        return [
            directory_row(row, refs) if view == "minimal" else evidence_view(row, refs)
            for row in rows_slice
        ]

    def envelope(items, end, has_more):
        # The cursor is part of the measurement; a longest-possible placeholder
        # keeps the real page from growing past the limit once it is attached.
        placeholder = encode_cursor(b, filters, 10**14, snapshot_max)
        return catalog_envelope(
            b,
            items,
            start=start,
            end=end,
            total=total,
            has_more=has_more,
            cursor_value=None,
            filters=filters,
            revision=revision,
            view=view,
            receipt_id=receipt["id"],
            placeholder_cursor=placeholder,
        )

    def build(rows_slice):
        windows, refs = window_table(clean(rows_slice))
        return envelope(
            projected(clean(rows_slice), refs),
            start + len(rows_slice),
            start + len(rows_slice) < len(after),
        )

    page_rows, end, too_small = fit_list(after, start, ceiling, limit_chars, build)
    if too_small:
        probe = build(after[start : start + 1])
        result = {
            "items": [],
            "total": total,
            "returned": 0,
            "range": [start, start],
            "has_more": start < len(after),
            "next": None,
            "next_cursor": cursor or None,
            "workspace_revision": revision,
            "filters": {k: v for k, v in filters.items() if v},
            "view": view,
            "view_version": _view_version(),
            "read_receipt_id": receipt["id"],
            "page_limit_too_small": True,
            "minimum_chars": min(PAGE_SERIALIZED_LIMIT, serialized_chars(probe)),
            "retry_hint": "raise max_chars or use view=minimal in one explicit retry",
        }
        receipt.update(
            truncated=True,
            has_more=start < len(after),
            next_position=start,
            returned_paths=[],
            complete_paths=[],
        )
        return await publish_read(
            b, receipt, result, kind="local_read_catalog", max_chars=limit_chars
        )
    has_more = end < len(after)
    windows, refs = window_table(clean(page_rows))
    items = projected(clean(page_rows), refs)
    result = envelope(items, end, has_more)
    if page_rows and has_more:
        result["next_cursor"] = encode_cursor(
            b, filters, page_rows[-1]["_rowid"], snapshot_max
        )
    result["next"] = end if has_more else None
    receipt.update(
        page_range=[start, end],
        truncated=has_more,
        has_more=has_more,
        next_position=end if has_more else None,
        returned_paths=[row.get("id") for row in page_rows] or ["catalog"],
        complete_paths=[],
    )
    return await publish_read(
        b,
        receipt,
        result,
        kind="local_read_catalog",
        max_chars=limit_chars,
        evidence_ids=[row.get("evidence_id") for row in result["items"]],
    )


def select_field(raw, field):
    """Read a dotted field path; a numeric segment indexes into a list."""
    if not field:
        return raw
    node = raw
    for key in field.split("."):
        if isinstance(node, list):
            if not key.lstrip("-").isdigit():
                raise WorkspaceError(
                    "field not found: %s (list segment must be an index)" % field
                )
            index = int(key)
            if not -len(node) <= index < len(node):
                raise WorkspaceError("field not found: %s (index out of range)" % field)
            node = node[index]
            continue
        if not isinstance(node, dict) or key not in node:
            raise WorkspaceError("field not found: %s" % field)
        node = node[key]
    return node


def page_payload(
    *,
    source_marker,
    identity,
    field,
    view,
    text=None,
    items=None,
    page_range=None,
    truncated=None,
    next_position=None,
    next_char_offset=None,
    extra=None,
    receipt_id,
    field_chars=None,
    detail=None,
):
    """One bounded page; only non-empty extras are carried, so the row fits.

    The same envelope is used for measuring and for returning, so the final
    serialization - receipt id included - is what the budget applied to.
    """
    # `view` and the full source metadata live in the receipt; the page itself carries
    # identity, source kind, field, position and its receipt id.
    result = {
        "source": source_marker,
        "field": field or None,
        "read_receipt_id": receipt_id,
    }
    result.update({key: value for key, value in (identity or {}).items() if value})
    if extra:
        result.update(
            {
                key: value
                for key, value in extra.items()
                if value not in (None, {}, [], "")
            }
        )
    if text is not None:
        result["text"] = text
        result["range"] = page_range
        result["truncated"] = bool(truncated)
        result["next_char_offset"] = next_char_offset
    else:
        result["items"] = items if items is not None else []
        result["range"] = page_range
        result["truncated"] = bool(truncated)
        result["next"] = next_position
    if field_chars is not None and truncated:
        result["field_chars"] = field_chars
    if detail:
        result["detail_entries"] = detail
    return result


async def content_page(
    b,
    *,
    tool_name,
    reference,
    identity,
    raw,
    field,
    offset,
    limit,
    char_offset,
    limit_chars,
    view,
    source_marker,
    receipt,
    extra=None,
):  # `view` is recorded on the receipt
    """Bounded list/object page or character page of one field, metered once."""
    if offset < 0 or not 1 <= limit <= PAGE_MAX_LIMIT:
        raise await failed(
            b, tool_name, reference, WorkspaceError("invalid field page")
        )
    try:
        node = select_field(raw, field)
    except WorkspaceError as exc:
        raise await failed(b, tool_name, reference, exc)
    # Resolve negative list segments once, so receipts name actual positions.
    field = canonical_field(raw, field)
    serialized = json.dumps(node, ensure_ascii=False)
    receipt.update(
        total_chars=len(serialized),
        total_items=len(node) if isinstance(node, list) else 1,
    )
    if char_offset is not None:
        if char_offset < 0:
            raise WorkspaceError("invalid character offset")

        def build(chunk):
            end = char_offset + len(chunk)
            return page_payload(
                source_marker=source_marker,
                identity=identity,
                field=field,
                view=view,
                text=chunk,
                page_range=[char_offset, end],
                truncated=end < len(serialized),
                next_char_offset=end if end < len(serialized) else None,
                receipt_id=receipt["id"],
                extra=extra,
            )

        chunk, end, has_more = fit_text(serialized, char_offset, limit_chars, build)
        if chunk is None:
            # No position is claimed here: a repeated call with the same budget
            # cannot make progress, so the caller must raise max_chars.
            result = page_payload(
                source_marker=source_marker,
                identity=identity,
                field=field,
                view=view,
                text="",
                page_range=[char_offset, char_offset],
                truncated=True,
                next_char_offset=None,
                receipt_id=receipt["id"],
                detail={"retry": "raise max_chars for this field"},
            )
            result["page_limit_too_small"] = True
            result["minimum_chars"] = serialized_chars(
                build(serialized[char_offset : char_offset + 1])
            )
            receipt.update(truncated=True, has_more=True, next_position=char_offset)
            return await publish_read(b, receipt, result, max_chars=limit_chars)
        result = build(chunk)
        receipt.update(
            field=field,
            view=view,
            truncated=result["truncated"],
            range=[char_offset, end],
            range_unit="chars",
            has_more=result["truncated"],
            next_position=result["next_char_offset"],
            returned_paths=[field or "<root>"] if chunk else [],
            complete_paths=[field or "<root>"]
            if char_offset == 0 and not result["truncated"]
            else [],
        )
        return await publish_read(b, receipt, result, max_chars=limit_chars)
    rows = node if isinstance(node, list) else [node]
    ceiling = min(len(rows), offset + limit)
    if offset > len(rows):
        raise WorkspaceError("offset is past the end of this field")

    def build(items):
        end = offset + len(items)
        return page_payload(
            source_marker=source_marker,
            identity=identity,
            field=field,
            view=view,
            items=items,
            page_range=[offset, end],
            truncated=end < len(rows),
            next_position=end if end < len(rows) else None,
            receipt_id=receipt["id"],
            extra=extra,
        )

    items, end, too_small = fit_list(rows, offset, ceiling, limit_chars, build)
    if too_small:
        result = build([])
        result["page_limit_too_small"] = True
        result["next"] = None  # no progress without a larger budget or a field read
        result["retry_hint"] = (
            "read this entry by field path or char_offset instead of a list page"
        )
        result["minimum_chars"] = min(
            PAGE_SERIALIZED_LIMIT, serialized_chars(build(rows[offset : offset + 1]))
        )
        receipt.update(truncated=True, has_more=True, next_position=None)
        return await publish_read(b, receipt, result, max_chars=limit_chars)
    result = build(items)
    complete = offset == 0 and not result["truncated"]
    receipt.update(
        field=field,
        view=view,
        truncated=result["truncated"],
        range=[offset, end],
        range_unit="items",
        has_more=result["truncated"],
        next_position=result["next"],
        returned_paths=[field or "<root>"] if items else [],
        complete_paths=[field or "<root>"] if complete else [],
    )
    return await publish_read(b, receipt, result, max_chars=limit_chars)


def canonical_field(raw, field):
    node, segments = raw, []
    for key in field.split(".") if field else []:
        if isinstance(node, list):
            index = int(key)
            key = str(index if index >= 0 else len(node) + index)
            node = node[int(key)]
        else:
            node = node[key]
        segments.append(key)
    return ".".join(segments)


@tool
async def read_evidence(
    evidence_id: str,
    field: str = "",
    view: str = "auto",
    offset: int = 0,
    limit: int = 20,
    char_offset: int | None = None,
    max_chars: int = 0,
) -> dict:
    """Read one evidence: brief by default, bounded metadata, field or raw pages."""
    b = binding()
    if view not in ("auto", "brief", "metadata", "raw"):
        raise WorkspaceError("view must be auto, brief, metadata or raw")
    limit_chars = limit_setting(max_chars)
    try:
        evidence = await b.repository.read(b.incident_id, evidence_id, "evidence")
    except WorkspaceError:
        raise await failed(
            b,
            "read_evidence",
            evidence_id,
            WorkspaceError("evidence_id is unknown in this incident: %s" % evidence_id),
        )
    from context.views import evidence_view, window_table

    windows, refs = window_table([evidence])
    window_info = window_args(evidence)
    limitations = {
        "data_gaps": evidence.get("data_gaps") or [],
        "warnings": evidence.get("warnings") or [],
    }
    limitations = {key: value for key, value in limitations.items() if value}
    if view == "metadata":
        receipt = new_receipt(
            b,
            tool_name="read_evidence",
            source_kind="evidence_metadata",
            source_id=evidence_id,
            view="metadata",
            field=field or None,
            source_scope={"resource_ids": evidence.get("resource_ids") or []},
            requested_window=window_info["requested_window"],
            returned_window=window_info["returned_window"],
            source_limitations=limitations,
            **record(evidence, evidence.get("revision")),
        )
        if field or char_offset is not None:
            raw = bounded_metadata(evidence, limit_chars)
            return await content_page(
                b,
                tool_name="read_evidence",
                reference=evidence_id,
                identity={"evidence_id": evidence_id},
                raw=raw,
                field=field,
                offset=offset,
                limit=limit,
                char_offset=char_offset,
                limit_chars=limit_chars,
                view="metadata",
                source_marker="workbench_metadata",
                receipt=receipt,
            )
        payload = {
            "source": "workbench_metadata",
            "evidence_id": evidence_id,
            "metadata": bounded_metadata(evidence, limit_chars),
            "artifact_id": evidence.get("artifact_id"),
            "read_receipt_id": receipt["id"],
            "read_entries": {
                **DETAIL_ENTRIES,
                "metadata_field": "read_evidence(view=metadata, field=<name>)",
            },
        }
        if limitations:
            payload["limitations"] = limitations
        receipt.update(
            returned_paths=sorted(payload["metadata"]),
            complete_paths=sorted(payload["metadata"]),
        )
        return await publish_read(b, receipt, payload, max_chars=limit_chars)
    if field or char_offset is not None or view == "raw":
        raw = await read_raw(b, evidence_id)
        receipt = new_receipt(
            b,
            tool_name="read_evidence",
            source_kind="evidence",
            source_id=evidence_id,
            view="raw" if view == "raw" else "field",
            field=field or None,
            source_scope={
                "resource_ids": evidence.get("resource_ids") or [],
                "resource_revision_refs": evidence.get("resource_identities") or [],
            },
            requested_window=window_info["requested_window"],
            returned_window=window_info["returned_window"],
            source_limitations=limitations,
            **record(evidence, evidence.get("revision")),
        )
        extra = {
            "windows": windows,
            "requested_window_ref": window_info["requested_window_ref"],
            "returned_window_ref": window_info["returned_window_ref"],
            "resource_revision_refs": evidence.get("resource_identities") or [],
            "limitations": limitations,
        }
        if evidence.get("truncated"):
            extra["source_truncated"] = True
        return await content_page(
            b,
            tool_name="read_evidence",
            reference=evidence_id,
            identity={"evidence_id": evidence_id},
            raw=raw,
            field=field,
            offset=offset,
            limit=limit,
            char_offset=char_offset,
            limit_chars=limit_chars,
            view="raw" if view == "raw" else "field",
            source_marker="tool_raw",
            receipt=receipt,
            extra=extra,
        )
    brief = evidence_view(evidence, refs)
    receipt = new_receipt(
        b,
        tool_name="read_evidence",
        source_kind="evidence",
        source_id=evidence_id,
        view="brief",
        source_scope={"resource_ids": evidence.get("resource_ids") or []},
        requested_window=window_info["requested_window"],
        returned_window=window_info["returned_window"],
        source_limitations=limitations,
        returned_paths=[],
        complete_paths=[],
        **record(evidence, evidence.get("revision")),
    )
    payload = {
        "evidence_id": evidence_id,
        "brief": brief,
        "windows": windows,
        "readable_fields": brief.get("detail_fields", []),
        "read_entries": {
            **DETAIL_ENTRIES,
            "metadata_field": "read_evidence(view=metadata, field=<name>)",
        },
        "read_receipt_id": receipt["id"],
    }
    payload["read_entries"]["raw_text"] = "read_evidence(field=<name>, char_offset=0)"
    payload["read_entries"]["raw_view"] = "read_evidence(view=raw)"
    if limitations:
        payload["limitations"] = limitations
    return await publish_read(b, receipt, payload, max_chars=limit_chars)


def bounded_metadata(evidence, limit_chars):
    """Full archival metadata, observations reduced to a key directory."""
    meta = {
        key: value
        for key, value in evidence.items()
        if key not in ("observations", "incident_id")
    }
    observations = evidence.get("observations")
    if isinstance(observations, dict):
        meta["observations_keys"] = sorted(observations.keys())
    return meta


@tool
async def read_artifact(
    artifact_id: str,
    field: str = "",
    view: str = "auto",
    offset: int = 0,
    limit: int = 20,
    char_offset: int | None = None,
    max_chars: int = 0,
) -> dict:
    """Read an incident artifact by bounded page; takes IDs only, never paths."""
    b = binding()
    if view not in ("auto", "raw", "metadata"):
        raise WorkspaceError("view must be auto, raw or metadata")
    limit_chars = limit_setting(max_chars)
    try:
        stored = await b.repository.read(b.incident_id, artifact_id, "artifact")
        raw = await read_artifact_raw(b, artifact_id)
    except WorkspaceError:
        raise await failed(
            b,
            "read_artifact",
            artifact_id,
            WorkspaceError(
                "artifact is unknown or unreadable in this incident: %s" % artifact_id
            ),
        )
    receipt = new_receipt(
        b,
        tool_name="read_artifact",
        source_kind="artifact",
        source_id=artifact_id,
        view="raw" if view == "raw" else "metadata" if view == "metadata" else "auto",
        field=field or None,
        source_scope={"content_type": stored.get("content_type")},
        source_hash=stored.get("sha256"),
        source_revision=stored.get("revision"),
    )
    extra = {"artifact_id": artifact_id, "content_type": "application/json"}
    if field or char_offset is not None or view == "raw":
        return await content_page(
            b,
            tool_name="read_artifact",
            reference=artifact_id,
            identity={"artifact_id": artifact_id},
            raw=raw,
            field=field,
            offset=offset,
            limit=limit,
            char_offset=char_offset,
            limit_chars=limit_chars,
            view="raw" if view == "raw" else "field",
            source_marker="artifact_raw",
            receipt=receipt,
            extra=extra,
        )
    shape = (
        "object"
        if isinstance(raw, dict)
        else "array"
        if isinstance(raw, list)
        else "scalar"
    )
    keys = sorted(raw.keys()) if isinstance(raw, dict) else None
    payload = {
        "artifact_id": artifact_id,
        "content_type": shape,
        "top_level_fields": keys,
        "serialized_chars": serialized_chars(raw),
        "read_receipt_id": receipt["id"],
        "read_entries": {
            "field": "read_artifact(field=<name>)",
            "raw_page": "read_artifact(view=raw)",
            "chars": "read_artifact(field=<name>, char_offset=0)",
            "metadata": "read_artifact(view=metadata)",
        },
    }
    if view == "metadata":
        payload["metadata"] = {
            key: value for key, value in stored.items() if key != "incident_id"
        }
    receipt.update(returned_paths=keys or ["<root>"], complete_paths=[])
    return await publish_read(
        b, receipt, payload, kind="local_read_artifact", max_chars=limit_chars
    )


@tool
async def read_task(
    task_id: str,
    field: str = "",
    view: str = "auto",
    offset: int = 0,
    limit: int = 20,
    char_offset: int | None = None,
    max_chars: int = 0,
) -> dict:
    """Read one task: brief by default, bounded field/raw pages on request."""
    b = binding()
    if view not in ("auto", "brief", "raw", "metadata"):
        raise WorkspaceError("view must be auto, brief, raw or metadata")
    limit_chars = limit_setting(max_chars)
    try:
        task = await b.repository.read(b.incident_id, task_id, "task")
    except WorkspaceError:
        raise await failed(
            b,
            "read_task",
            task_id,
            WorkspaceError("task_id is unknown in this incident: %s" % task_id),
        )
    from context.views import task_view

    receipt = new_receipt(
        b,
        tool_name="read_task",
        source_kind="task",
        source_id=task_id,
        view="raw" if field or char_offset is not None or view == "raw" else "brief",
        field=field or None,
        source_scope={"target_resource_ids": task.get("target_resource_ids") or []},
        requested_window=task.get("query_window")
        if isinstance(task.get("query_window"), dict)
        else None,
        **record(task, task.get("revision")),
    )
    if field or char_offset is not None or view == "raw":
        return await content_page(
            b,
            tool_name="read_task",
            reference=task_id,
            identity={"task_id": task_id},
            raw=task,
            field=field,
            offset=offset,
            limit=limit,
            char_offset=char_offset,
            limit_chars=limit_chars,
            view="raw" if view == "raw" else "field",
            source_marker="task_record",
            receipt=receipt,
        )
    payload = {
        "task_id": task_id,
        "task": task_view(task, role="worker", main_task_id=task_id),
        "read_receipt_id": receipt["id"],
        "read_entries": {
            "raw_field": "read_task(field=<name>)",
            "raw_text": "read_task(field=<name>, char_offset=0)",
            "raw_view": "read_task(view=raw)",
        },
    }
    if view == "metadata":
        payload["metadata"] = {
            key: value for key, value in task.items() if key != "incident_id"
        }
    receipt.update(returned_paths=["brief"], complete_paths=[])
    return await publish_read(
        b, receipt, payload, kind="local_read_task", max_chars=limit_chars
    )


@tool
async def read_assessment(
    assessment_id: str,
    field: str = "",
    view: str = "auto",
    offset: int = 0,
    limit: int = 20,
    char_offset: int | None = None,
    max_chars: int = 0,
) -> dict:
    """Read one submitted judgment in full (diagnosis, support/opposition, gaps)."""
    b = binding()
    if view not in ("auto", "brief", "raw", "metadata"):
        raise WorkspaceError("view must be auto, brief, raw or metadata")
    limit_chars = limit_setting(max_chars)
    try:
        assessment = await b.repository.read(b.incident_id, assessment_id, "assessment")
    except WorkspaceError:
        raise await failed(
            b,
            "read_assessment",
            assessment_id,
            WorkspaceError(
                "assessment_id is unknown in this incident: %s" % assessment_id
            ),
        )
    from context.views import assessment_view

    receipt = new_receipt(
        b,
        tool_name="read_assessment",
        source_kind="assessment",
        source_id=assessment_id,
        view="raw" if field or char_offset is not None or view == "raw" else "brief",
        field=field or None,
        source_scope={
            "task_id": assessment.get("task_id"),
            "attempt_id": assessment.get("attempt_id"),
        },
        **record(assessment, assessment.get("revision")),
    )
    if field or char_offset is not None or view == "raw":
        return await content_page(
            b,
            tool_name="read_assessment",
            reference=assessment_id,
            identity={"assessment_id": assessment_id},
            raw=assessment,
            field=field,
            offset=offset,
            limit=limit,
            char_offset=char_offset,
            limit_chars=limit_chars,
            view="raw" if view == "raw" else "field",
            source_marker="assessment_record",
            receipt=receipt,
        )
    payload = {
        "assessment_id": assessment_id,
        "assessment": assessment_view(assessment, full_diagnosis=True),
        "reasoning": assessment.get("reasoning", ""),
        "submission": assessment.get("submission"),
        "read_receipt_id": receipt["id"],
        "read_entries": {
            "raw_field": "read_assessment(field=<name>)",
            "raw_text": "read_assessment(field=<name>, char_offset=0)",
            "raw_view": "read_assessment(view=raw)",
        },
    }
    if view == "metadata":
        payload["metadata"] = {
            key: value for key, value in assessment.items() if key != "incident_id"
        }
    receipt.update(returned_paths=["brief"], complete_paths=[])
    return await publish_read(
        b, receipt, payload, kind="local_read_assessment", max_chars=limit_chars
    )


READ_TOOLS = [
    get_resource,
    query_evidence,
    read_evidence,
    read_artifact,
    read_task,
    read_assessment,
]

INVESTIGATION_KINDS = (
    "question",
    "observation",
    "judgment",
    "stage_commit",
    "checkpoint",
    "material_usage",
    "report_packet",
)
INVESTIGATION_DEFAULT_LIMIT = 20
INVESTIGATION_MAX_LIMIT = 100


@tool
async def read_investigation(
    kind: str = "",
    record_id: str = "",
    question_id: str = "",
    owner: str = "",
    limit: int = INVESTIGATION_DEFAULT_LIMIT,
    cursor: str = "",
    max_chars: int = 0,
    field: str = "",
    offset: int = 0,
    char_offset: int | None = None,
) -> dict:
    """Read questions, observations, judgments, stage commits, checkpoints, material usage or report packets.

    Use `record_id` for one official id, or `question_id` to list what that
    question is linked to; `kind="checkpoint", owner=<role>` lists an owner's
    checkpoint chain with the active head marked. Bounded like every other read.
    """
    from .investigation_reads import read

    return await read(
        binding(),
        kind=kind,
        record_id=record_id,
        question_id=question_id,
        owner=owner,
        limit=limit,
        cursor=cursor,
        max_chars=max_chars,
        field=field,
        offset=offset,
        char_offset=char_offset,
    )


READ_TOOLS = READ_TOOLS + [read_investigation]
