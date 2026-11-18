"""Shared stop conditions; diagnostic topics never select workflows."""

BLOCKING_TRIAGE_STATUSES = frozenset(
    {"awaiting_confirmation", "insufficient", "not_found", "discovery_failed"}
)


def triage_blocked(state):
    return (
        state.get("triage_status") in BLOCKING_TRIAGE_STATUSES
        or state.get("evidence_status") == "unavailable"
        or bool(state.get("execution_error"))
        or state.get("execution_status") == "failed"
    )


def clarification_result(state):
    return {
        "symptoms": [],
        "triage_status": state.get("triage_status") or "insufficient",
        "clarification": state.get("clarification") or {"reason": "verified target required"},
    }
