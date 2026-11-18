"""Archive a completed segment and retire only precisely replaced read batches."""
import json

from .artifacts import write_artifact


async def retire_messages(b, checkpoint, messages):
    from context.history import plan_batches
    commit = await b.repository.read(b.incident_id, checkpoint['stage_commit_id'], 'stage_commit')
    covered = [row for row in commit.get('material_dispositions') or []
               if row['disposition'] == 'covered_by']
    artifact = await write_artifact(b, {'kind': 'investigation_segment', 'checkpoint_id': checkpoint['id'],
                                       'messages': [m.model_dump(mode='json') for m in messages]})
    await b.repository.save(b.incident_id, 'artifact', artifact)
    retired, retired_receipts = set(), []
    batches, _ = plan_batches(messages)
    receipts = {r['id']: r for r in await b.repository.all(b.incident_id, 'read_receipt')}
    from .material_ranges import disposition_covers
    from .receipts import matches_read
    from .investigations import same_owner
    replacements, deferred = {}, []
    for batch in batches:
        can_retire, batch_receipts = True, []
        for index in batch.reply_indices:
            message = messages[index]
            if message.name == 'commit_investigation_stage':
                continue
            try:
                body = json.loads(message.content)
            except (TypeError, ValueError):
                can_retire = False
                continue
            receipt = receipts.get(body.get('read_receipt_id')) if isinstance(body, dict) else None
            if (isinstance(body, dict) and body.get('status') == 'rejected'
                    and str(body.get('reason', '')).startswith('checkpoint_required:')):
                # No source was fetched. Archive the rejected request and carry
                # its executable arguments forward, rather than retaining an
                # ever-growing failed batch beside the committed page.
                original = next((c for c in messages[batch.ai_index].tool_calls
                                 if c['id'] == message.tool_call_id), None)
                if original:
                    deferred.append({'name': original['name'], 'args': original['args']})
                    continue
            if (not receipt or not same_owner(b, {**receipt, 'owner': receipt.get('role')}, b.role)
                    or not await matches_read(b, receipt, body)):
                can_retire = False
                continue
            matched = any(disposition_covers(row, receipt) for row in covered)
            if not matched:
                can_retire = False
                continue
            batch_receipts.append(receipt['id'])
            # Keep pairing when another reply in this batch is still active.
            replacements[index] = message.model_copy(update={'content': json.dumps({
                'status': 'covered_by_commit', 'commit_id': commit['id'], 'source_id': receipt['source_id'],
                'field': receipt.get('field'), 'range': receipt.get('range'),
                'range_unit': receipt.get('range_unit'), 'read': 'read_investigation(kind="stage_commit", record_id=<commit_id>)'})})
        retired_receipts.extend(batch_receipts)
        if can_retire:
            retired.update([batch.ai_index] + batch.reply_indices)
    checkpoint['segment_archive_artifact_id'] = artifact['id']
    checkpoint['deferred_tool_calls'] = deferred
    checkpoint['retired_receipt_ids'] = sorted(set(retired_receipts))
    checkpoint['retained_message_count'] = len(messages) - len(retired)
    saved = await b.repository.save(b.incident_id, 'checkpoint', checkpoint, checkpoint['revision'])
    checkpoint.update(saved)
    return [replacements.get(index, message) for index, message in enumerate(messages) if index not in retired]


def retired_receipts(binding, checkpoints):
    from .investigations import same_owner
    return {rid for row in checkpoints if same_owner(binding, row, binding.role)
            for rid in row.get('retired_receipt_ids') or []}
