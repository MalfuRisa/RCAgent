"""Archive standalone failed stage call pairs with a durable, bounded result."""
import json
from langchain_core.messages import HumanMessage
from .artifacts import write_artifact


async def archive_stage_pairs(b, messages):
    from context.history import plan_batches
    batches, _ = plan_batches(messages)
    removed, notices = set(), []
    for batch in batches:
        ai = messages[batch.ai_index]
        if len(ai.tool_calls) != 1 or ai.tool_calls[0]['name'] != 'commit_investigation_stage':
            continue  # mixed requests keep their unchanged rejection semantics
        if len(batch.reply_indices) != 1:
            continue
        reply = messages[batch.reply_indices[0]]
        try:
            result = json.loads(reply.content)
        except (ValueError, TypeError):
            continue
        if result.get('status') != 'rejected':
            continue  # successful pairs retire via the accepted checkpoint
        artifact = await write_artifact(b, {'kind': 'failed_stage_call_pair',
            'messages': [ai.model_dump(mode='json'), reply.model_dump(mode='json')]})
        await b.repository.save(b.incident_id, 'artifact', artifact)
        removed.update([batch.ai_index, *batch.reply_indices])
        notices.append(HumanMessage(content=json.dumps({'stage_submission': 'rejected',
            'reason': result.get('reason'), 'proposal_artifact_id': result.get('proposal_artifact_id'),
            'pending_id': result.get('repair_pending_id'), 'pair_artifact_id': artifact['id'],
            'note': 'No stage switched. Uncovered material remains active. This stage cannot receive another Repair.'})))
    return [m for i, m in enumerate(messages) if i not in removed] + notices
