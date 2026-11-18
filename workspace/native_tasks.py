"""Explicit event ownership and durable, non-overwritten task stage results."""


class OutputContext:
    def __init__(self, outputs, **ids):
        self.base, self.ids = outputs, ids

    def __getattr__(self, name):
        return getattr(self.base, name)

    def event(self, kind, role, payload=None, **ids):
        return self.base.event(kind, role, payload, **{**self.ids, **ids})

    def save(self, name, value):
        # Partial streams belong to a conversation/task, never another Worker.
        if name.endswith("-partial.md"):
            owner = self.ids.get("task_id") or self.ids.get("conversation_id")
            name = f"partials/{owner}/{name}"
        return self.base.save(name, value)


def save_task(
    outputs, context, text, *, status, reason, errors=(), planner_decision_id=None
):
    if outputs.sealed or getattr(context, "revoked", False):
        return None
    result = {
        "task_id": context.task_id,
        "conversation_id": context.conversation_id,
        "submission_index": context.submission_index,
        "status": status,
        "end_reason": reason,
        "answer_text": text,
        "answer_path": outputs.save(f"tasks/{context.task_id}/answer.md", text),
        "source_ids": list(context.source_ids),
        "context_source_ids": context.spec.context_source_ids,
        "continues_task_id": context.spec.continue_task_id,
        "dispatch_entry": getattr(context, "dispatch_entry", None),
        "dispatch_call_id": getattr(context, "dispatch_call_id", None),
        "errors": list(errors),
        "spec": context.spec.model_dump(),
        "locked_dataset": context.dataset.model_dump() if context.dataset else None,
        "window_kind": context.window_kind,
        "handoff_mode": 'facts' if context.handoff_facts else 'worker_note',
        "handoff_reason": context.handoff_reason or 'worker_note_or_unsettled',
        "facts": context.handoff_facts,
    }
    outputs.task_results.append(result)
    outputs.task_results.sort(key=lambda item: item["submission_index"])
    outputs.save(f"tasks/{context.task_id}/result.json", result)
    outputs.event(
        "task_finished",
        "worker",
        {
            "status": status,
            "end_reason": reason,
            "answer_path": result["answer_path"],
            "source_ids": context.source_ids,
            "dispatch_entry": result["dispatch_entry"],
            "dispatch_call_id": result["dispatch_call_id"],
        },
        task_id=context.task_id,
        conversation_id=context.conversation_id,
        planner_decision_id=planner_decision_id,
    )
    outputs.checkpoint()
    return result
