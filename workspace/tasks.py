"""Task, attempt and judgment transitions share one transaction and event."""

from .repository import WorkspaceError, new_id, utcnow
from .resources import register, scope, allowed


async def task_resource(binding, task, main_target, main_namespace):
    if not task.resource_id:
        return await register(
            binding,
            task.resource_type,
            task.target_resource,
            main_namespace if task.target_resource == main_target else None,
        )
    resource = await binding.repository.read(binding.incident_id, task.resource_id, "resource")
    if resource["kind"] != task.resource_type or resource["name"] != task.target_resource:
        raise WorkspaceError("task resource ID does not match its kind/name")
    if (
        resource.get("identity_status") != "resolved"
        or resource.get("replaced_by")
        or not allowed(await scope(binding), resource.get("cluster"), resource.get("namespace"))
    ):
        raise WorkspaceError("task requires a verified in-scope resource identity")
    return resource


def validate_target_refs(binding, db, refs, targets):
    for ref in refs:
        evidence = binding.repository.get(db, binding.incident_id, ref, "evidence")
        if not set(evidence["resource_ids"]).intersection(targets):
            raise WorkspaceError("evidence belongs to another target")


async def register_tasks(binding, tasks, main_target=None, main_namespace=None):
    assigned = []
    existing_tasks = await binding.repository.all(binding.incident_id, "task")
    next_priority = max((t["priority"] for t in existing_tasks), default=0) + 1
    for priority, task in enumerate(sorted(tasks, key=lambda t: t.priority), next_priority):
        resource = await task_resource(binding, task, main_target, main_namespace)
        assigned.append(
            task.model_copy(
                update={
                    "task_id": new_id("task"),
                    "resource_id": resource["id"],
                    "priority": priority,
                    "status": "pending",
                    "execution_status": "pending",
                    "answer_status": "unanswered",
                }
            )
        )

    def commit(db):
        existing = {t["id"] for t in binding.repository.list(db, binding.incident_id, "task")}
        ids = existing | {t.task_id for t in assigned}
        graph = {
            t["id"]: t.get("depends_on", [])
            for t in binding.repository.list(db, binding.incident_id, "task")
        }
        graph.update({t.task_id: t.depends_on for t in assigned})

        def visit(node, path):
            if node in path:
                raise WorkspaceError("task dependency cycle")
            for parent in graph.get(node, []):
                if parent not in ids:
                    raise WorkspaceError("dependency not registered in incident")
                visit(parent, path | {node})

        for node in graph:
            visit(node, set())
        for original, task in zip(sorted(tasks, key=lambda t: t.priority), assigned):
            for question_id in task.question_ids:
                question = binding.repository.get(db, binding.incident_id, question_id, "question")
                if question.get("run_id") != binding.run_id:
                    raise WorkspaceError("task question belongs to another run")
                question["owner_task_ids"] = list(
                    dict.fromkeys((question.get("owner_task_ids") or []) + [task.task_id])
                )
                binding.repository.put(
                    db,
                    binding.incident_id,
                    "question",
                    question,
                    question["revision"],
                    "question_assigned",
                )
            for kind, refs in (
                ("observation", task.known_observation_ids),
                ("judgment", task.candidate_judgment_ids),
            ):
                for ref in refs:
                    record = binding.repository.get(db, binding.incident_id, ref, kind)
                    questions = record.get("question_ids") or [record.get("question_id")]
                    if record.get("run_id") != binding.run_id or not set(questions).intersection(
                        task.question_ids
                    ):
                        raise WorkspaceError("task material belongs to another run or question")
            body = task.model_dump()
            body.update(
                id=task.task_id,
                question=task.investigation_goal,
                target_resource_ids=[task.resource_id],
                latest_attempt_id=None,
                original_priority=original.priority,
            )
            binding.repository.put(db, binding.incident_id, "task", body, action="task_registered")

    await binding.repository.transact(commit)
    return assigned


async def start(binding, task_id, expected=None):
    def commit(db):
        task = binding.repository.get(db, binding.incident_id, task_id, "task")
        if expected is not None and task["revision"] != expected:
            raise WorkspaceError("task revision conflict")
        if task["execution_status"] == "running":
            raise WorkspaceError("task already running")
        for dep in task.get("depends_on", []):
            parent = binding.repository.get(db, binding.incident_id, dep, "task")
            if parent["execution_status"] != "finished" or parent["answer_status"] != "supported":
                raise WorkspaceError("task prerequisite not met")
        attempt = {
            "id": new_id("attempt"),
            "task_id": task_id,
            "run_id": binding.run_id,
            "owner": "worker",
            "started_at": utcnow(),
            "status": "running",
            "task_revision": task["revision"] + 1,
        }
        binding.repository.put(db, binding.incident_id, "attempt", attempt)
        task.update(
            execution_status="running", status="in_progress", latest_attempt_id=attempt["id"]
        )
        binding.repository.put(
            db, binding.incident_id, "task", task, task["revision"], "task_started"
        )
        return attempt

    return await binding.repository.transact(commit)


async def finish(binding, response, status="finished", stop_reason=None):
    def commit(db):
        task = binding.repository.get(db, binding.incident_id, binding.task_id, "task")
        attempt = binding.repository.get(db, binding.incident_id, binding.attempt_id, "attempt")
        if (
            attempt["task_id"] != binding.task_id
            or attempt["run_id"] != binding.run_id
            or task["latest_attempt_id"] != binding.attempt_id
        ):
            raise WorkspaceError("attempt does not own this task/run")
        if attempt["status"] != "running":
            previous = [
                a
                for a in binding.repository.list(db, binding.incident_id, "assessment")
                if a["attempt_id"] == binding.attempt_id
            ]
            if previous and previous[0]["submission"] == response:
                return previous[0]
            raise WorkspaceError("attempt already finished with a different result")
        if task["execution_status"] != "running" or task["revision"] != attempt["task_revision"]:
            raise WorkspaceError("task state or revision changed during attempt")
        support = response.get("supporting_evidence_ids", [])
        oppose = response.get("opposing_evidence_ids", [])
        validate_target_refs(binding, db, support + oppose, task["target_resource_ids"])
        hypotheses = response.get("hypotheses", [])
        for h in hypotheses:
            if h.get("status", "proposed") not in (
                "proposed",
                "supported",
                "refuted",
                "unresolved",
            ):
                raise WorkspaceError("invalid hypothesis status")
            validate_target_refs(
                binding,
                db,
                h.get("supporting_evidence_ids", []) + h.get("opposing_evidence_ids", []),
                task["target_resource_ids"],
            )
        gaps = response.get("gaps", [])[:]
        answer = response.get("answer_status", "inconclusive")
        if answer not in ("supported", "inconclusive", "not_applicable"):
            raise WorkspaceError("invalid answer status")
        if not support:
            answer = "inconclusive"
            gaps.append("No supporting evidence submitted; legacy submission remains unresolved")
        if answer == "supported":
            usable = [
                binding.repository.get(db, binding.incident_id, ref, "evidence") for ref in support
            ]
            if (
                oppose
                or gaps
                or any(
                    e["transport_status"] != "ok"
                    or e["source_status"] in ("error", "not_found", "no_peer")
                    or e.get("truncated")
                    or e["data_gaps"]
                    for e in usable
                )
            ):
                answer = "inconclusive"
                gaps.append("Support is incomplete or opposed; question remains unresolved")
        if status != "finished":
            answer = "inconclusive"
        assessment = {
            "id": new_id("assessment"),
            "task_id": binding.task_id,
            "attempt_id": binding.attempt_id,
            "producer": "worker",
            "submission": response,
            "answer_status": answer,
            "diagnosis": response.get("diagnosis", "Unresolved"),
            "reasoning": response.get("reasoning", ""),
            "supporting_evidence_ids": support,
            "opposing_evidence_ids": oppose,
            "gaps": gaps,
            "hypotheses": hypotheses,
        }
        binding.repository.put(db, binding.incident_id, "assessment", assessment)
        attempt.update(status=status, finished_at=utcnow(), stop_reason=stop_reason)
        binding.repository.put(
            db, binding.incident_id, "attempt", attempt, attempt["revision"], "attempt_finished"
        )
        task.update(
            execution_status=status,
            answer_status=answer,
            status="completed" if status == "finished" else "pending",
        )
        binding.repository.put(
            db, binding.incident_id, "task", task, task["revision"], "task_finished"
        )
        return assessment

    return await binding.repository.transact(commit)


async def views(binding):
    from models.schemas import RCATask

    return [
        RCATask.model_validate(t) for t in await binding.repository.all(binding.incident_id, "task")
    ]
