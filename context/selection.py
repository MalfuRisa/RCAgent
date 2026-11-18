"""Material sizing and deterministic evidence selection (character budget)."""
import json


def message_chars(messages):
    return sum(len(json.dumps({"content": m.content, "tool_calls": getattr(m, "tool_calls", [])}, ensure_ascii=False)) for m in messages)


def judgment_view(assessment):
    return {k: assessment.get(k) for k in ("id", "task_id", "attempt_id", "producer", "answer_status",
            "diagnosis", "supporting_evidence_ids", "opposing_evidence_ids", "gaps", "hypotheses")}


def group_identical_evidence(evidence):
    """Compress exact duplicate visible facts, preserving every actual query ID.

    This changes presentation only: dynamic queries are still executed and
    persisted separately. Different windows, results, provenance or limitations
    cannot collapse into one record or an extra independent confirmation.
    """
    keys = ("resource_ids", "resource_revisions", "resource_identities", "tool_name", "arguments", "time_range",
            "producer_task_id", "attempt_id", "transport_status", "source_status", "warnings", "data_gaps",
            "truncated", "freshness", "observations")
    groups, indexes = [], {}
    for e in evidence:
        signature = json.dumps({k: e.get(k) for k in keys}, ensure_ascii=False, sort_keys=True)
        if signature in indexes:
            groups[indexes[signature]]["equivalent_evidence_ids"].append(e["id"])
        else:
            indexes[signature] = len(groups)
            groups.append(dict(e, equivalent_evidence_ids=[e["id"]]))
    return groups
