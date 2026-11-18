"""Compatibility node names and pure historical report helpers."""

import hashlib
import json
from workspace.artifacts import write_artifact


def answer_tool_list(messages):
    """Tool calls of the pending answer, used only for the mixed-batch pre-check."""
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None)
        if calls:
            return calls
    return []


async def save_draft(binding, text):
    """Archive the reading-phase free text as a draft artifact; empty stays None."""
    if not text or not text.strip():
        return None
    artifact = await write_artifact(
        binding,
        {
            "kind": "model_draft",
            "note": "draft from the reading phase, not tool facts",
            "content": text,
        },
    )
    await binding.repository.save(binding.incident_id, "artifact", artifact)
    return artifact["id"]


def progress(snapshot):
    # Deduplicated actual observations and answers, never summaries or timestamps.
    identities = {
        (r.get("kind"), r.get("cluster"), r.get("namespace"), r.get("uid"))
        for r in snapshot["resource"]
        if r["identity_status"] == "resolved"
    }
    facts = {
        json.dumps(
            {
                k: v
                for k, v in e["observations"].items()
                if k not in ("meta", "query_time", "warnings", "data_gaps", "status")
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        for e in snapshot["evidence"]
        if e["transport_status"] == "ok"
        and e["tool_name"] not in ("resolve_pod", "search_k8s_targets")
        and any(
            e["observations"].get(k)
            for k in ("summary", "samples", "series", "traces", "spans", "containers", "events")
        )
        and e["source_status"] not in ("error", "not_found", "no_peer")
    }
    answers = {
        (t["id"], t["answer_status"])
        for t in snapshot["task"]
        if t["answer_status"] != "unanswered"
    }
    return hashlib.sha256(
        repr((sorted(identities, key=str), sorted(facts), sorted(answers))).encode()
    ).hexdigest()


def repair_template(checks, final_report):
    """Offer a concrete correction using only already checked statements."""
    claims = [check["statement"] for check in checks if check["retained"]]
    sections = {
        "observation": "observations",
        "hypothesis": "hypotheses",
        "exclusion": "exclusions",
        "causal": "observations",
    }
    items = [
        {
            "section": sections[claim["type"]],
            "content": claim["content"],
            "claim_ids": [claim["claim_id"]],
        }
        for claim in claims
    ]
    items.extend(
        {
            "section": "limitations",
            "content": "\n".join(claim["limitations"]),
            "claim_ids": [claim["claim_id"]],
        }
        for claim in claims
    )
    return {
        "final_report": final_report,
        "claims": claims,
        "report_items": items,
        "tasks_to_execute_ids": [],
        "tasks_to_be_executed": [],
        "followup_tasks": [],
    }


async def supervisor_agent(state):
    """Deprecated node name; delegates to the common SRE role."""
    from workflow.role_compat import invoke_role

    return await invoke_role(state, "supervisor")


def build_supervisor_graph():
    """Deprecated public graph name; delegates to the one SRE graph."""
    from workflow.investigation import build_investigation_graph

    return build_investigation_graph()


supervisor_agent_graph = build_supervisor_graph()
