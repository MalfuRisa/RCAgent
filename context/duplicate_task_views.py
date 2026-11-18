"""Prefer an active exact read over its duplicate task-question projection."""
import copy
import json
from workspace.investigations import same_owner
from workspace.receipts import matches_verified_read


def share_task_questions(b, payload, messages, receipts, snapshot):
    versions = {r['id']: r['revision'] for r in snapshot.get('task') or []}
    copies = {}
    for index, message in enumerate(messages):
        if message.type != 'tool':
            continue
        try:
            body = json.loads(message.content)
        except (TypeError, ValueError):
            continue
        receipt = receipts.get(body.get('read_receipt_id')) if isinstance(body, dict) else None
        if (not receipt or receipt.get('source_kind') != 'task'
                or receipt.get('storage_error')
                or not same_owner(b, {**receipt, 'owner': receipt.get('role')}, b.role)
                or receipt.get('source_revision') != versions.get(receipt.get('source_id'))
                or not matches_verified_read(receipt, body)):
            continue
        task = body.get('task') or {}
        goal = task.get('investigation_goal')
        if isinstance(goal, str):
            copies[(receipt['source_id'], goal)] = {'tool_call_id': message.tool_call_id,
                'receipt_id': receipt['id'], 'source_revision': receipt['source_revision'],
                'field': 'task.investigation_goal', 'retained_message_index': index}
    output, decisions = copy.deepcopy(payload), []
    for index, row in enumerate(output.get('tasks') or []):
        goal = row.get('question')
        if not isinstance(goal, str):
            continue
        proof = copies.get((row['task_id'], goal))
        if proof is None:
            continue
        replacement = {'active_tool_question_ref': proof['tool_call_id']}
        if len(json.dumps(replacement)) >= len(json.dumps(goal)):
            continue
        row['question'] = replacement
        decisions.append({'replaced_path': '/tasks/%d/question' % index, 'retained': proof,
                          'reason': 'exact same task id, revision, owner and complete active read value'})
    if decisions:
        output['active_task_question_note'] = ('active_tool_question_ref resolves to task.investigation_goal in the '
            'complete ToolMessage with that tool_call_id in this request; the read and its source remain active.')
    return output, decisions
