"""Conservative claim rules and consistent report reconstruction."""

import json
from datetime import datetime
from .repository import WorkspaceError, new_id
from .queries import read_raw
from pydantic import ValidationError
from models.schemas import EvidenceClaim
from .claim_conflicts import unresolved_conflicts

# Initial direct mechanism rule: a scoped runtime result's explicit OOMKilled
# termination. Inventory, network symptoms, no_peer and trace emptiness are not
# mechanism evidence. Extend here only with verified Gateway field contracts.
DIRECT_MECHANISMS = {"oom_termination": ("get_pod_runtime_status", "OOMKilled")}


def termination_reasons(value):
    if isinstance(value, dict):
        result = [value["reason"]] if value.get("reason") == "OOMKilled" else []
        for key, child in value.items():
            if key in (
                "terminated",
                "last_state",
                "lastState",
                "container_statuses",
                "containerStatuses",
                "runtime_status",
                "status",
                "containers",
            ):
                result.extend(termination_reasons(child))
        return result
    if isinstance(value, list):
        return [r for child in value for r in termination_reasons(child)]
    return []


def termination_times(value):
    if isinstance(value, dict):
        result = (
            [value.get("finished_at") or value.get("finishedAt")]
            if value.get("reason") == "OOMKilled"
            else []
        )
        for key, child in value.items():
            if key in (
                "terminated",
                "last_state",
                "lastState",
                "state",
                "container_statuses",
                "containerStatuses",
                "runtime_status",
                "status",
                "containers",
            ):
                result.extend(termination_times(child))
        return result
    if isinstance(value, list):
        return [stamp for child in value for stamp in termination_times(child)]
    return []


def termination_in_window(raw, time_range):
    try:
        start = datetime.fromisoformat(time_range["start_time"])
        end = datetime.fromisoformat(time_range["end_time"])
        if start.tzinfo is None or end.tzinfo is None or start > end:
            return False
        for stamp in termination_times(raw):
            if stamp:
                at = datetime.fromisoformat(stamp)
                if at.tzinfo and start <= at <= end:
                    return True
    except (KeyError, ValueError, TypeError):
        pass
    return False


async def validate_claim(binding, claim):
    try:
        claim = EvidenceClaim.model_validate(claim).model_dump()
    except (ValidationError, TypeError) as exc:
        return False, "malformed claim: " + str(exc)
    try:
        resource = await binding.repository.read(
            binding.incident_id, claim.get("resource_id"), "resource"
        )
        refs = claim.get("supporting_evidence_ids", [])
        evidence = [
            await binding.repository.read(binding.incident_id, ref, "evidence")
            for ref in refs
        ]
        for ref in claim.get("opposing_evidence_ids", []):
            await binding.repository.read(binding.incident_id, ref, "evidence")
        if resource["identity_status"] != "resolved" or not evidence:
            return False, "unverified identity or missing evidence"
        if any(resource["id"] not in e["resource_ids"] for e in evidence):
            return False, "evidence target mismatch"
        if claim["type"] != "causal":
            return False, "observation/hypothesis does not establish a cause"
        rule = DIRECT_MECHANISMS.get(claim.get("mechanism"))
        if (
            not rule
            or not claim.get("limitations")
            or claim.get("opposing_evidence_ids")
        ):
            return False, "mechanism unsupported or limitations/opposition unresolved"
        conflict = await unresolved_conflicts(binding, claim)
        if conflict:
            return False, "authoritative workbench conflict unresolved: " + conflict
        for e in evidence:
            raw = await read_raw(binding, e["id"])
            if (
                e["tool_name"] == rule[0]
                and e["transport_status"] == "ok"
                and e["source_status"]
                not in ("error", "partial", "not_found", "no_peer")
                and not e["data_gaps"]
                and not e.get("truncated")
                and e["arguments"].get("pod_uid") == resource["uid"]
                and rule[1] in termination_reasons(raw)
                and termination_in_window(raw, e.get("time_range") or {})
            ):
                return (
                    True,
                    f"{resource['cluster']}/{resource['namespace']}/{resource['name']}: observed OOMKilled termination; limits: {claim['limitations']}",
                )
        return False, "no direct scoped termination evidence"
    except (WorkspaceError, OSError) as exc:
        return False, str(exc)


async def conservative_report(binding, reason, symptoms=()):
    evidence = await binding.repository.all(binding.incident_id, "evidence")
    resources = await binding.repository.all(binding.incident_id, "resource")
    assessments = await binding.repository.all(binding.incident_id, "assessment")
    observations = [
        {
            "evidence_id": e["id"],
            "tool": e["tool_name"],
            "resource_ids": e["resource_ids"],
            "observations": e["observations"],
        }
        for e in evidence
        if e["transport_status"] == "ok"
    ]
    gaps = [
        {
            "evidence_id": e["id"],
            "status": e["source_status"],
            "gaps": e["data_gaps"],
            "warnings": e["warnings"],
            "truncated": e.get("truncated"),
            "freshness": e.get("freshness"),
        }
        for e in evidence
        if e["data_gaps"]
        or e["warnings"]
        or e.get("source_status") in ("not_found", "partial", "error", "no_peer")
        or e.get("truncated")
    ]
    gaps.extend(
        {"resource_id": r["id"], "reason": "identity_" + r["identity_status"]}
        for r in resources
        if r["identity_status"] != "resolved"
    )
    from .recovery import issues

    gaps.extend(
        {
            "source": "workflow_recovery",
            "role": row["role"],
            "reason": row["code"],
            "action": row["action"],
            "detail": row.get("detail"),
        }
        for row in await issues(binding)
    )
    hypotheses = [
        {
            "assessment_id": a["id"],
            "producer": a["producer"],
            "answer_status": a["answer_status"],
            "candidate_judgment": a["diagnosis"],
            "hypotheses": a["hypotheses"],
            "support": a["supporting_evidence_ids"],
            "opposition": a["opposing_evidence_ids"],
            "gaps": a["gaps"],
        }
        for a in assessments
    ]
    # No legal model proposal exists: this report is the program fallback (PRD §19.10).
    return {
        "fallback": True,
        "root_cause": "根因未决：尚无足够的直接机制证据。",
        "affected_resources": list(dict.fromkeys(r["name"] for r in resources)),
        "evidence_summary": json.dumps(
            {"observations": observations, "limitations": gaps}, ensure_ascii=False
        ),
        "investigation_summary": json.dumps(
            {
                "status": "inconclusive",
                "stop_reason": reason,
                "judgments_are_hypotheses": hypotheses,
            },
            ensure_ascii=False,
        ),
        "detection": bool(symptoms),
        "localization": None,
        "answer_status": "inconclusive",
        "observations": observations,
        "data_gaps": gaps,
        "hypotheses": hypotheses,
        "stop_reason": reason,
    }


async def adjudicate(
    binding,
    proposed,
    claims,
    symptoms=(),
    reason="unsupported_causal_claim",
    report_items=None,
    execution=None,
    verified_model_packet=False,
):
    claims = (
        [c.model_dump() if isinstance(c, EvidenceClaim) else c for c in claims]
        if isinstance(claims, (list, tuple))
        else [claims]
    )
    if report_items is not None:
        from .report_checks import inspect_proposal
        from .report_render import render_checked

        typed_checks, accepted, errors = await inspect_proposal(
            binding, claims, report_items
        )
        base = await conservative_report(binding, reason, symptoms)
        final = render_checked(base, typed_checks, accepted, errors)
        if verified_model_packet and not claims and accepted and not errors:
            final.update(
                fallback=False, report_origin="checked_model_unresolved_packet"
            )
        supported = [c for c in typed_checks if c["supports_cause"]]
        if supported:
            final["localization"] = [
                (
                    await binding.repository.read(
                        binding.incident_id, c["statement"]["resource_id"], "resource"
                    )
                )["name"]
                for c in supported
            ]
        if execution:
            final.update(execution)
            if execution.get("execution_error"):
                final["investigation_summary"] += "\n运行未完成：" + str(
                    execution["execution_error"]
                )
        audit = {
            "id": new_id("report"),
            "run_id": binding.run_id,
            "proposed": proposed,
            "final": final,
            "claims": claims,
            "report_items": report_items,
            "checks": typed_checks,
            "errors": errors,
            "workspace_revision": await binding.repository.transact(
                lambda db: db.execute(
                    "SELECT COALESCE(MAX(seq),0) FROM events WHERE incident=?",
                    (binding.incident_id,),
                ).fetchone()[0]
            ),
        }
        await binding.repository.save(binding.incident_id, "report", audit)
        final["audit_id"] = audit["id"]
        return final
    checks = [await validate_claim(binding, c) for c in claims]
    supported = [description for ok, description in checks if ok]
    final = await conservative_report(binding, reason, symptoms)
    if supported:
        # Program-controlled statements do not reuse unverified proposed prose.
        final.update(
            root_cause="; ".join(supported),
            answer_status="supported",
            detection=True,
            investigation_summary=json.dumps(
                {
                    "status": "supported",
                    "direct_observed_mechanisms": supported,
                    "hypotheses": final["hypotheses"],
                    "limitations": final["data_gaps"],
                },
                ensure_ascii=False,
            ),
            localization=[
                (
                    await binding.repository.read(
                        binding.incident_id, c["resource_id"], "resource"
                    )
                )["name"]
                for c, (ok, _) in zip(claims, checks)
                if ok
            ],
        )
    audit = {
        "id": new_id("report"),
        "run_id": binding.run_id,
        "proposed": proposed,
        "final": final,
        "claims": claims,
        "checks": [{"accepted": ok, "reason": why} for ok, why in checks],
        "modification": "deterministic reconstruction from workbench observations and labeled judgments",
    }
    if execution:
        final.update(execution)
        if execution.get("execution_error"):
            final["investigation_summary"] += "\n运行未完成：" + str(
                execution["execution_error"]
            )
    await binding.repository.save(binding.incident_id, "report", audit)
    final["audit_id"] = audit["id"]
    return final
