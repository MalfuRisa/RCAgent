"""Deterministic, field-aware rejection guidance; never repairs model content."""

import json
from pydantic import ValidationError


def details(error):
    if isinstance(error, ValidationError):
        return [
            {
                "path": "/" + "/".join(map(str, row["loc"])),
                "code": row["type"],
                "reason": row["msg"],
            }
            for row in error.errors(include_input=False, include_url=False)
        ][:5]
    text = str(error)
    # Preserve the verifier's structured field/range/source reason when present.
    try:
        body = json.loads(text[text.index("{") :])
        if isinstance(body.get("errors"), list):
            return body["errors"][:5]
        row = body.get("rejected", body)
        if isinstance(row, dict):
            return [
                {
                    "code": row.get("reason", "submission_rejected"),
                    "path": row.get("json_pointer") or row.get("field"),
                    "reason": row.get("message", text[:1200]),
                }
            ]
    except (ValueError, TypeError):
        pass
    return []


def feedback(operation, result, phase, *, fields=()):
    reason = str(result.get("reason", "submission rejected"))
    fields = list(fields) or result.get("field_errors") or result.get("errors") or []
    text = json.dumps(fields, ensure_ascii=False) + reason
    prefix = reason.split(":", 1)[0]
    if prefix in {
        "unknown_ref",
        "not_visible",
        "scope_mismatch",
        "source_unavailable",
        "source_stale",
        "invalid_question",
        "invalid_shape",
        "phase_disallowed",
    }:
        fields = [
            {
                "code": prefix,
                "path": "/ref_ids" if operation == "record_observations" else None,
                "reason": reason,
            }
        ]
    observation_error = (
        operation
        in ("record_observations", "complete_task", "commit_investigation_stage")
        or operation == "submit_report"
        and (
            any(
                str(row.get("path") or row.get("field") or "").startswith(
                    ("/observations", "/candidate_ids")
                )
                for row in fields
            )
            or any(
                word in text
                for word in (
                    "expected_value",
                    "source_hash",
                    "source_document",
                    "json_pointer",
                    "pointer_not_found",
                    "/observations/",
                    "not_shown_to_model",
                )
            )
        )
    )
    final = phase == "finalize_report"
    if not final and not observation_error and operation != "submit_report":
        return {
            "code": fields[0].get("code", "tool_rejected")
            if fields
            else "tool_rejected",
            "field_errors": fields,
            "next_action": "Correct the indicated arguments using the current tool schema and "
            "allowed_tools; preserve the original error and current investigation state.",
        }
    observation_action = (
        "Correct only the indicated object using current visible values; resubmit with "
        + operation
        + "."
        if operation in ("complete_task", "commit_investigation_stage")
        else "Correct only the indicated object using current visible values; register new observations "
        "with record_observations before a separate report."
    )
    return {
        "code": fields[0].get("code", "submission_rejected")
        if fields
        else "submission_rejected",
        "field_errors": fields,
        "next_action": (
            "Use submit_report with formal observation_ids and per-question unresolved_reasons; "
            "new raw proposals are forbidden in finalization."
            if final
            else observation_action
            if observation_error
            else "Use current formal observation IDs and dispose every current question; "
            "submit_report does not accept raw proposals."
        ),
        "skill_hint": {
            "name": "submitting-observations"
            if observation_error and not final
            else "finalizing-investigation",
            "resource": "references/recovery-examples.md",
            "tool": "read_skill",
        },
    }
