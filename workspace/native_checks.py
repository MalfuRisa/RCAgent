"""Mechanical source cards. Existence/counts never certify a causal claim."""

import json
import re
from pathlib import Path

from models.native_tasks import Window, timestamp
from .query_context import model_query_context, model_source_context
from .published_evidence import checked_history


def source_card(source, raw):
    model_context = {
        'mcp_source': source.get('mcp_source'),
        'source_context': model_source_context(source.get('source_context')),
        'query_context': model_query_context(source.get('query_context')),
    }
    if source.get('material_kind'):
        return {k: source.get(k) for k in ('source_id', 'task_id', 'tool_name', 'material_kind', 'status',
            'arguments', 'resolved_time', 'scope_status', 'business_value_count', 'original_values_preview', 'limitations')} | model_context
    args = source.get("arguments")
    args = args if isinstance(args, dict) else {}
    body = args.get("body")
    body = body if isinstance(body, dict) else {}
    business = source.get("tool_name")
    request = {
        "database": body.get("database", args.get("database", body.get("db"))),
        "table": body.get("table", args.get("table")),
        "data_source": body.get("data_source"),
        "time_start": body.get("time_start", args.get("start_time")),
        "time_end": body.get("time_end", args.get("end_time")),
    }
    time_card = {"status": "unknown"}
    try:
        window = Window(
            start=timestamp(request["time_start"]), end=timestamp(request["time_end"])
        )
        time_card = {
            "status": "request_only",
            **window.card(),
            "timezone": "UTC (Unix seconds)",
        }
    except (ValueError, TypeError):
        pass
    structured = raw.get("structuredContent") if isinstance(raw, dict) else None
    meta = structured.get("meta", {}) if isinstance(structured, dict) else {}
    if len(json.dumps(meta, ensure_ascii=False)) > 3000:
        meta = {
            "truncated": True,
            "read_entry": {
                "source_id": source["source_id"],
                "pointer": "/structuredContent/meta",
            },
        }
    pagination = source.get("pagination") or {}
    return {
        **model_context,
        "source_id": source["source_id"],
        "task_id": source.get("task_id"),
        "tool": business,
        "status": source["status"],
        "original_saved": bool(
            source.get("raw_path") and Path(source["raw_path"]).is_file()
        ),
        "request": request,
        "time": time_card,
        "returned_rows": source.get("returned_rows"),
        "backend_total": source.get("backend_total"),
        "total_status": pagination.get("total_status", "unknown"),
        "backend_and_contract": meta,
        "unit": "unknown unless explicitly defined in this source",
        "limitations": [
            "Requested scope is not proof of backend applied scope",
            "where/object attribution not mechanically proven",
            "Multiple queries may not all execute; row counts are not unique business request counts",
        ],
        "query_count": len(body.get("queries", []))
        if isinstance(body.get("queries"), list)
        else None,
    }


def cards(outputs, source_ids):
    return [
        source_card(outputs.sources[s], outputs.materials.raw.get(s))
        for s in source_ids
        if s in outputs.sources
    ]


def references(text):
    return list(dict.fromkeys(re.findall(r"\bsource_[0-9a-fA-F]{32}\b", text)))


def check_references(outputs, text):
    return [
        {
            "source_id": s,
            "exists_in_run": s in outputs.sources,
            "original_saved": bool(
                outputs.sources.get(s, {}).get("raw_path")
                and Path(outputs.sources[s]["raw_path"]).is_file()
            ),
        }
        for s in references(text)
    ]


NUMBER = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?(?![\w])")


def numeric_values(value):
    if type(value) in (int, float):
        yield float(value)
    elif isinstance(value, str) and len(value) <= 40:
        try:
            yield float(value.replace(",", ""))
        except ValueError:
            return
    elif isinstance(value, dict):
        for item in value.values():
            yield from numeric_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from numeric_values(item)


def number_checks(outputs, text):
    """Mechanical presence check; unit conversion and derived prose can stay unmatched."""
    body_numbers = NUMBER.findall(text)
    available = set()
    for source in outputs.sources.values():
        for key in ("returned_rows", "business_value_count", "backend_total"):
            available |= set(numeric_values(source.get(key)))
        for item in source.get("original_values_preview") or []:
            available |= set(numeric_values(item.get("value")))
            available |= set(numeric_values(item.get("stats")))
        available |= set(numeric_values(source.get("resolved_time")))
    matched, unmatched = [], []
    for token in body_numbers:
        try:
            number = float(token.replace(",", ""))
        except ValueError:
            continue
        if any(abs(number - value) <= max(1e-6, abs(number) * 1e-9) for value in available):
            matched.append(token)
        else:
            unmatched.append(token)
    return {
        "body_numbers": body_numbers[:40],
        "matched_in_sources": matched[:40],
        "unmatched": unmatched[:40],
        "note": "Presence check only; unit conversions, percentages and derived values may be unmatched.",
    }


def check_draft(outputs, text):
    checks = {
        "references": check_references(outputs, text),
        "history_evidence": checked_history(outputs),
        "source_cards": cards(outputs, outputs.sources),
        "number_checks": number_checks(outputs, text),
        "evidence_status": "unchecked",
        "limitations": [
            "Checks do not validate arbitrary prose, causal inference, units or object attribution"
        ],
    }
    outputs.checked_evidence = checks
    outputs.save("checks.json", checks)
    outputs.event("program_checks", "system", checks)
    return checks
