"""Apply existing history, lossless sharing and protected-page rules to one provider."""

import copy
import json
from langchain_core.messages import SystemMessage
from config import settings
from workspace.receipts import matches_verified_read
from . import history, parts, budget_policy
from .exact_representation import serialize
from .investigations import select_investigation, coverage_three_ranges
from .publication import base_manifest, workbench_message, publish, refuse_context
from .read_details import verify_receipts, transferred_pages, retention_audit
from .request_budget import measure
from .selection import message_chars
from .snapshot import read_snapshot


def reduced_view(payload, level, manifest):
    """Only uncommitted evidence previews/candidate catalogs become read entries."""
    view = copy.deepcopy(payload)
    if not level:
        return view
    protected = {
        r["source_ref"].get("evidence_id")
        for r in view.get("investigation", {}).get("observations", [])
        if r.get("source_ref")
    }
    kept, directory = [], list(view.get("evidence_directory", []))
    for card in view.get("evidence", []):
        if card.get("evidence_id") in protected:
            kept.append(card)
            continue
        eid = card.get("evidence_id")
        # Every limitation, warning, identity and window stays on the entry.
        directory.append(
            {k: v for k, v in card.items() if k not in ("observations", "source")}
        )
        directory[-1]["read"] = f'read_evidence(evidence_id="{eid}", field=<field>)'
        manifest["omitted_references_and_reasons"].append(
            {
                "id": eid,
                "reason": "optional evidence preview externalized; raw source, scope and limitations retained",
            }
        )
    view["evidence"], view["evidence_directory"] = kept, directory
    candidates = view.get("current_candidates", [])
    limit = 16 if level == 1 else 0
    view["current_candidates"] = candidates[:limit]
    for row in candidates[limit:]:
        manifest["omitted_references_and_reasons"].append(
            {
                "id": row["candidate_id"],
                "reason": "optional current candidate catalog; exact source remains readable",
            }
        )
    view["candidate_directory"] = {
        "total": len(candidates),
        "listed": min(limit, len(candidates)),
        "read": "read_evidence(evidence_id=<id>, field=<field>)",
    }
    return view


def details_not_in_messages(details, messages, receipts):
    exact = set()
    for message in messages:
        rid = history.message_receipt_id(message)
        if rid not in receipts:
            continue
        try:
            if matches_verified_read(receipts[rid], json.loads(message.content)):
                exact.add(rid)
        except (ValueError, TypeError):
            continue
    return [r for r in details if r["receipt_id"] not in exact]


async def prepare(b, messages, phase, request):
    from .investigation import investigation_payload
    from .lifecycle import effective_mode

    snapshot = await read_snapshot(b)
    payload, evidence, candidates = await investigation_payload(b, snapshot, phase)
    manifest = base_manifest(b, snapshot, phase)
    manifest.update(
        context_policy="unified_sre",
        lifecycle_mode=effective_mode(b),
        view_version=payload["context_view_version"],
        budget_enabled=True,
        nominal_limit=settings.CONTEXT_MAX_CHARS,
        request_basis="v2",
        recovery_view=bool(getattr(b, "recovery_view", False)),
    )
    from workspace.skill_knowledge import deduplicate_replies

    original = deduplicate_replies(messages)
    from workspace.evidence_references import externalize_delivered

    original, acknowledged = await externalize_delivered(
        b, original, snapshot["read_receipt"]
    )
    receipts = {r["id"]: r for r in snapshot["read_receipt"]}
    bodies = await verify_receipts(b, original, receipts, phase)
    batches, _ = history.plan_batches(original)
    recent = history.history_settings()
    transformed, decisions = await history.externalize_earlier(
        b, original, batches, recent, history.evidence_index(snapshot), receipts
    )
    manifest["history_replacements"] = decisions + acknowledged
    positions = (
        {p for batch in batches[-recent:] for p in batch.reply_indices}
        if recent
        else set()
    )
    kinds = parts.classify(transformed, phase=phase, recent_positions=positions)
    details = payload.get("verified_read_details", [])
    details = details_not_in_messages(details, transformed, receipts)
    details.extend(
        transferred_pages(b, original, transformed, receipts, bodies, details)
    )
    if phase != "packet_submission":
        payload["verified_read_details"] = details
    levels = (1, 2) if getattr(b, "recovery_view", False) else (0, 1, 2)
    steps = []
    # One finite ladder at the soft goal; necessary protected content may then
    # expand up to the separate engineering ceiling. No recursive preparation.
    limits = [settings.CONTEXT_MAX_CHARS]
    if budget_policy.can_expand():
        limits.append(budget_policy.hard_limit())
    reserve = 0
    names = [
        (r.get("function") or {}).get("name") for r in (request or {}).get("tools", [])
    ]
    if (
        b.role == "worker"
        and phase != "checkpoint_control"
        and "commit_investigation_stage" in names
    ):
        from .stage_budget import reserve as stage_reserve

        fixed = stage_reserve()
        reserve = sum(fixed.values()) - fixed["commit_schema"]
    for ceiling in limits:
        for level in levels:
            view = reduced_view(payload, level, manifest)
            text, _ = serialize(b, view, phase)
            visible_messages, _ = await parts.shrink_optional(
                b, transformed, kinds, manifest, text, ceiling - reserve, True
            )
            if phase != "packet_submission":
                existing = details_not_in_messages(details, visible_messages, receipts)
                existing.extend(
                    transferred_pages(
                        b, transformed, visible_messages, receipts, bodies, existing
                    )
                )
                view["verified_read_details"] = existing
            view = history.deduplicate(
                view,
                history.visible_coverage(b, visible_messages, receipts),
                {r["id"]: r["revision"] for r in evidence},
            )
            text, sharing = serialize(b, view, phase)
            visible = [
                SystemMessage(content=workbench_message(text))
            ] + visible_messages
            size, components = measure(visible, request)
            steps.append(
                {"level": level, "request_chars_v2": size, "target": ceiling - reserve}
            )
            if size <= ceiling - reserve and size <= budget_policy.hard_limit():
                return await accepted(
                    b,
                    snapshot,
                    manifest,
                    original,
                    visible_messages,
                    view,
                    visible,
                    size,
                    components,
                    steps,
                    sharing,
                    names,
                )
    # Reserve exhaustion first rebinds Worker controls. If the smaller control
    # request cannot fit either, archive the exact rejected candidate and stop.
    if reserve and size <= budget_policy.hard_limit():
        b.checkpoint_required = True
        reason = "expansion_reserve_exhausted: rebind checkpoint controls"
    else:
        reason = "protected_parts_exceed_budget: protected unified input exceeds engineering ceiling"
    return await refuse_context(
        b,
        manifest,
        view,
        visible_messages,
        phase,
        reason=reason,
        visible=visible,
        protected=parts.protected_parts(visible_messages, kinds),
        extra={
            "budget_steps": steps,
            "request_chars_v2": size,
            "component_chars": components,
            "read_retention_checks": retention_audit(original, visible_messages, view),
        },
    )


async def accepted(
    b,
    snapshot,
    manifest,
    original,
    messages,
    view,
    visible,
    size,
    components,
    steps,
    sharing,
    names,
):
    retention = retention_audit(original, messages, view)
    if any(not row["required_values_retained"] for row in retention):
        return await refuse_context(
            b,
            manifest,
            view,
            messages,
            manifest["phase"],
            reason="read_retention_failed: uncovered exact pages must remain visible",
            visible=visible,
            extra={
                "request_chars_v2": size,
                "read_retention_checks": retention,
                "component_chars": components,
            },
        )
    manifest.update(
        input_chars=message_chars(visible),
        injected_skill_resources=[
            {
                k: r[k]
                for k in (
                    "name",
                    "resource",
                    "content_hash",
                    "read_id",
                    "activation_source",
                )
                if k in r
            }
            for r in view.get("skill_knowledge", {}).get("loaded", [])
        ],
        request_chars_v2=size,
        component_chars=components,
        budget_steps=steps,
        exact_representation=sharing,
        soft_target_expanded=size > settings.CONTEXT_MAX_CHARS,
        bound_tool_names=names,
        injected_evidence_ids=[r["evidence_id"] for r in view.get("evidence", [])],
        candidate_ids=[r["candidate_id"] for r in view.get("current_candidates", [])],
        read_retention_checks=retention,
        **select_investigation(b, snapshot, manifest["phase"])["manifest"],
    )
    manifest["coverage_three_ranges"] = coverage_three_ranges(
        b, snapshot, snapshot["read_receipt"], snapshot["stage_commit"], view, messages
    )
    return await publish(b, manifest, visible)
