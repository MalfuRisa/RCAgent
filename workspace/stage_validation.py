"""Reference and material-scope checks for a stage transaction."""
from .repository import WorkspaceError


def owner_scope(binding, owner):
    return '|'.join(str(part or '') for part in (
        binding.incident_id, binding.run_id, owner, binding.task_id, binding.attempt_id))


def validate_local_ids(proposal):
    seen, errors = set(), []
    for group in ('question_proposals', 'observation_proposals', 'judgments', 'material_dispositions'):
        for index, item in enumerate(proposal.get(group) or []):
            if not isinstance(item, dict):
                errors.append({'field': '%s[%d]' % (group, index), 'reason': 'must be an object'})
                continue
            local = item.get('local_id')
            if local and (not isinstance(local, str) or local in seen):
                errors.append({'field': '%s[%d].local_id' % (group, index), 'reason': 'must be unique'})
            if isinstance(local, str):
                seen.add(local)
            for field in ('question_ids', 'resource_ids', 'origin_refs', 'owner_task_ids', 'covered_by',
                          'supporting_observation_ids', 'opposing_observation_ids', 'evidence_refs'):
                refs = item.get(field, [])
                if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
                    errors.append({'field': '%s[%d].%s' % (group, index, field), 'reason': 'must be a list of nonempty IDs'})
    return errors


async def validate_judgments(binding, rows, new_observations):
    known = {row['id']: row for row in await binding.repository.all(binding.incident_id, 'observation')
             if row.get('run_id') == binding.run_id}
    known.update({row['id']: row for row in new_observations})
    errors = []
    for index, row in enumerate(rows):
        for field in ('supporting_observation_ids', 'opposing_observation_ids'):
            for ref in row.get(field) or []:
                observation = known.get(ref)
                if observation is None:
                    errors.append({'field': 'judgments[%d].%s' % (index, field),
                                   'reason': 'observation is unknown', 'reference': ref})
                elif row['question_id'] not in observation.get('question_ids', []):
                    errors.append({'field': 'judgments[%d].question_id' % index,
                                   'reason': 'observation belongs to another question', 'reference': ref})
    return errors


async def validate_dispositions(binding, rows, observations, question_ids):
    """A covered field must have a matching verified replacement; no whole-source inference."""
    known = {row['id']: row for row in await binding.repository.all(binding.incident_id, 'observation')
             if row.get('run_id') == binding.run_id}
    known.update({row['id']: row for row in observations})
    errors = []
    for index, row in enumerate(rows):
        field = 'material_dispositions[%d]' % index
        source_id = row.get('source_id')
        try:
            try:
                source = await binding.repository.read(binding.incident_id, source_id, 'evidence')
            except WorkspaceError:
                source = await binding.repository.read(binding.incident_id, source_id, 'artifact')
            if source.get('run_id') != binding.run_id:
                raise WorkspaceError('source belongs to another run')
            version = source['revision']
            if row.get('source_version') not in (None, version):
                raise WorkspaceError('source version changed')
            row['source_version'] = version
        except WorkspaceError as exc:
            errors.append({'field': field, 'reason': str(exc)})
            continue
        if row['disposition'] == 'out_of_scope' and not row.get('reason'):
            errors.append({'field': field, 'reason': 'out_of_scope requires a reason'})
        if row['disposition'] != 'covered_by':
            continue
        replacements = [known.get(ref) for ref in row.get('covered_by') or []]
        from .material_ranges import replacement_covers, checked_span
        if row.get('range') is not None:
            try:
                checked_span(row['range'])
            except WorkspaceError as exc:
                errors.append({'field': field, 'reason': str(exc)})
                continue
        valid = [o for o in replacements if o and set(question_ids).intersection(o.get('question_ids') or [])]
        if (not replacements or len(valid) != len(replacements) or not replacement_covers(
                valid, source_id, version, row.get('field'), row.get('range'), row.get('range_unit'))):
            errors.append({'field': field, 'reason': 'covered_by requires verified observations for this exact field and question'})
    return errors
