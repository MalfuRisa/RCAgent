"""One role/run context policy for every diagnostic capability."""

from config import settings
from .views import evidence_view, window_table
from .json_projection import project_json
from .read_details import read_detail
from workspace.visible_candidates import candidate_snapshot
from workspace.artifacts import read_artifact_raw


async def investigation_payload(b, snapshot, phase="request"):
    from workspace.skill_knowledge import view as knowledge_view, business_steps

    from workspace.skill_knowledge import activate_events

    if phase == "packet_submission":
        await activate_events(b, phase=phase)
        knowledge = await knowledge_view(b)
        from .report_packets import packet_view

        payload = packet_view(b, snapshot)
        packet = payload["report_packet"]
        payload["report_packets"] = [{"id": packet["packet_id"], **packet}]
        payload["current_candidates"] = []
        payload["context_view_version"] = "sre-investigation-v1"
        if knowledge:
            knowledge["directory"] = []
            knowledge["reader_available"] = False
            payload["skill_knowledge"] = knowledge
        return payload, [], []
    evidence = [e for e in snapshot["evidence"] if e.get("run_id") == b.run_id]
    windows, refs = window_table(evidence)
    cards = []
    for e in evidence:
        card = evidence_view(e, refs)
        if not card.get("observations"):
            raw = await read_artifact_raw(b, e["artifact_id"])
            projection = project_json(raw)
            card.update(projection)
        cards.append(card)
    from workspace.stage_segments import retired_receipts

    retired = retired_receipts(b, snapshot["checkpoint"])
    from workspace.investigation_phases import active as phase_active

    receipts = [
        r
        for r in snapshot["read_receipt"]
        if r["id"] not in retired
        if not (phase_active(b) and r.get("delivered_context_id"))
        if (r.get("run_id"), r.get("role"), r.get("task_id"), r.get("attempt_id"))
        == (b.run_id, b.role, b.task_id, b.attempt_id)
    ]
    details = [
        read_detail(r, await read_artifact_raw(b, r["result_artifact_id"]))
        for r in receipts
    ]
    candidates = await candidate_snapshot(b, evidence, cards, receipts)
    await activate_events(b, has_references=bool(candidates), phase=phase)
    knowledge = await knowledge_view(b)
    current_questions = [q for q in snapshot["question"] if q.get("run_id") == b.run_id]
    qids = {q["id"] for q in current_questions}
    tasks = [t for t in snapshot["task"] if set(t.get("question_ids", [])) & qids]
    task_ids = {t["id"] for t in tasks}
    from .investigations import select_investigation

    selection = select_investigation(b, snapshot, phase)
    section = selection["payload"]
    for plural, key in [
        ("questions", "question_id"),
        ("observations", "observation_id"),
        ("judgments", "judgment_id"),
    ]:
        for row in section.get(plural, []):
            row["id"] = row[key]
    # The Planner must see every registered question it can reference. Workers
    # and reviewers use the bounded ownership/required-opposition view above.
    if b.role in ("triage", "planner"):
        section["questions"] = current_questions
    payload = {
        "context_view_version": "sre-investigation-v1",
        "scope": snapshot["scope"],
        "query_window": b.query_window,
        "resources": snapshot["resource"],
        "windows": windows,
        "evidence": cards,
        "tasks": tasks,
        "assessments": [
            a for a in snapshot["assessment"] if a.get("task_id") in task_ids
        ],
        "investigation": section,
        "verified_read_details": details,
        "current_candidates": candidates,
        "recovery_issues": [
            r for r in snapshot["recovery_issue"] if r.get("run_id") == b.run_id
        ],
        "report_packets": [
            {
                k: p.get(k)
                for k in (
                    "id",
                    "statements",
                    "unresolved_question_ids",
                    "question_dispositions",
                )
            }
            for p in snapshot["report_packet"]
            if p.get("run_id") == b.run_id
        ],
        "budget": {
            "model_step_limit": settings.WORKER_MAX_MODEL_STEPS
            if b.role == "worker"
            else b.local_model_limit,
            "model_steps_used": b.model_steps,
            "business_steps_used": business_steps(b),
            "knowledge_rounds_used": b.knowledge_rounds,
            "explicit_total_request_limit": b.local_model_limit,
            "worker_business_step_limit": settings.WORKER_MAX_MODEL_STEPS
            if b.role == "worker"
            else None,
            "knowledge_extra_limit": settings.SKILL_READ_EXTRA_ROUNDS
            if knowledge
            else 0,
            "remaining_seconds": round(b.remaining(), 1),
        },
        "note": "Current snapshot; all submissions remain subject to program verification.",
    }
    if knowledge:
        payload["skill_knowledge"] = knowledge
    from workspace.investigation_phases import view as phase_view

    if phase_view(b):
        payload["execution_phase"] = phase_view(b)
        from workspace.evidence_references import coverage

        payload["reference_coverage"] = await coverage(b, snapshot)
    from workspace.investigations import same_owner

    head = next(
        (
            c
            for c in snapshot["checkpoint"]
            if same_owner(b, c, b.role) and c.get("is_head")
        ),
        {},
    )
    last_commit = next(
        (c for c in snapshot["stage_commit"] if c["id"] == head.get("stage_commit_id")),
        {},
    )
    payload["stage_control"] = {
        "tool": "commit_investigation_stage",
        "expected_checkpoint_id": head.get("id"),
        "expected_revision": head.get("revision"),
        "question_ids": [q["id"] for q in section["questions"]],
        "stage_commit_id": head.get("stage_commit_id"),
        "registered_observation_ids": last_commit.get("observation_ids", []),
        "completion": "partial_stage_is_not_task_completion",
    }
    if b.role == "supervisor":
        # Reviewers receive formal facts/opposition and a source directory, not
        # another copy of Worker reading history. Candidate values remain explicit.
        payload["evidence_directory"] = [
            {
                **{
                    k: v for k, v in card.items() if k not in ("observations", "source")
                },
                "read": 'read_evidence(evidence_id="%s", field=<field>)'
                % card["evidence_id"],
            }
            for card in cards
        ]
        payload["evidence"] = []
        if phase == "finalize_report":
            payload["current_candidates"] = []
            payload["phase"] = "finalize_report"
            payload["finalization"] = {
                "question_ids": [q["id"] for q in current_questions],
                "formal_observation_ids": [
                    r["id"] for r in section.get("observations", [])
                ],
                "instruction": "Only submit_report and available read_skill. No new observation proposals or business reads.",
            }
    return payload, evidence, candidates


async def prepare_investigation(b, messages, phase, request):
    from .investigation_preparation import prepare

    return await prepare(b, messages, phase, request)


def common_preparers():
    return {
        p: prepare_investigation
        for p in (
            "triage",
            "plan",
            "worker_step",
            "local_read",
            "packet_submission",
            "checkpoint_control",
            "finalize_report",
            "investigating",
            "consolidating",
            "repairing",
        )
    }
