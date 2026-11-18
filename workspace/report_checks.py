"""Statement retention is separate from permission to confirm a mechanism."""
from models.schemas import EvidenceClaim, ReportItem
from pydantic import ValidationError
from .repository import WorkspaceError, new_id
from collections import Counter


def assertive_candidate(text):
    # Auxiliary wording guard, never the causal decision rule. Natural-language
    # equivalence cannot be completely verified; typed evidence checks govern.
    words = ('definitely', 'root cause is confirmed', 'completely healthy', '已确认', '根因就是', '确定是', '完全健康', '彻底排除')
    return any(word in text.lower() for word in words)


async def check_statement(binding, raw):
    result = {'claim_id': raw.get('claim_id') if isinstance(raw, dict) else None, 'retained': False, 'supports_cause': False,
              'errors': [], 'statement': raw}
    def reject(field, reason):
        result['errors'].append({'claim_id': result['claim_id'], 'field': field, 'type': 'invalid_statement', 'reason': reason})
        return result
    try:
        statement = EvidenceClaim.model_validate(raw).model_dump()
    except (ValidationError, TypeError) as exc:
        return reject('claim', str(exc))
    result['statement'] = statement
    try:
        refs = statement['supporting_evidence_ids'] + statement['opposing_evidence_ids']
        evidence = [await binding.repository.read(binding.incident_id, ref, 'evidence') for ref in refs]
        if statement['resource_id']:
            try:
                resource = await binding.repository.read(binding.incident_id, statement['resource_id'], 'resource')
            except WorkspaceError as exc:
                return reject('resource_id', str(exc) + '; expected a resource ID, not an evidence ID')
            if any(resource['id'] not in e['resource_ids'] for e in evidence):
                return reject('supporting_evidence_ids', 'evidence belongs to another target')
            for e in evidence:
                snapshots = [r for r in e.get('resource_identities', []) if r['id'] == resource['id']]
                if snapshots and snapshots[0].get('uid') and snapshots[0]['uid'] != resource.get('uid'):
                    return reject('resource_id', 'evidence describes a different resource UID revision')
        else:
            lookup = statement['lookup_target']
            if statement['type'] not in ('observation', 'hypothesis'):
                return reject('resource_id', 'unverified lookup cannot establish exclusion or cause')
            for e in evidence:
                args = e['arguments']
                if (e['tool_name'] not in ('resolve_pod', 'search_k8s_targets') or
                    (args.get('query') or args.get('pod')) != lookup['name'] or args.get('cluster') != lookup['cluster'] or
                    args.get('namespace') != lookup['namespace'] or args.get('kind', 'pod') not in (lookup['kind'], 'auto')):
                    return reject('lookup_target', 'lookup does not match actual query scope and target')
        if statement['task_id']:
            task = await binding.repository.read(binding.incident_id, statement['task_id'], 'task')
            if statement['resource_id'] and statement['resource_id'] not in task['target_resource_ids']:
                return reject('task_id', 'task belongs to a different target')
        if any(text.strip().lower() in ('无', 'none', 'no limits', '没有限制') for text in statement['limitations']):
            return reject('limitations', 'real scope/coverage/unknowns must be stated')
        if statement['type'] in ('hypothesis', 'exclusion') and assertive_candidate(statement['content']):
            return reject('content', 'candidate or scoped exclusion still contains categorical unsupported wording')
        if statement['type'] == 'exclusion' and any(e['transport_status'] != 'ok' or e['source_status'] in ('error', 'not_found', 'no_peer') for e in evidence):
            return reject('supporting_evidence_ids', 'failed/empty lookup cannot exclude a fault')
        if statement['type'] == 'causal':
            from .reports import validate_claim
            accepted, reason = await validate_claim(binding, statement)
            if not accepted:
                return reject('mechanism', reason + '; withdraw definite wording or submit a supported hypothesis')
            result['supports_cause'], result['mechanism_statement'] = True, reason
            statement['content'] = reason
        result['retained'] = True
        return result
    except (WorkspaceError, OSError) as exc:
        return reject('resource_id' if 'resource' in str(exc) else 'references', str(exc))


async def normalize_proposal(binding, claims, items, mapping):
    seen, normalized, errors = set(), [], []
    identifiers = [c.claim_id if isinstance(c, EvidenceClaim) else c.get('claim_id') for c in claims if isinstance(c, (dict, EvidenceClaim))]
    counts = Counter(identifier for identifier in identifiers if isinstance(identifier, str))
    for index, claim in enumerate(claims):
        if not isinstance(claim, (dict, EvidenceClaim)):
            errors.append({'field': f'claims[{index}]', 'type': 'invalid_claim', 'reason': 'expected a statement object'})
            continue
        claim = claim.model_dump() if isinstance(claim, EvidenceClaim) else dict(claim)
        temporary = claim.get('claim_id')
        if not isinstance(temporary, str) or not temporary.strip() or temporary in seen or counts[temporary] > 1:
            errors.append({'claim_id': temporary, 'field': f'claims[{index}].claim_id', 'type': 'ambiguous_claim_id', 'reason': 'nonempty unique proposal ID required'})
            continue
        seen.add(temporary)
        if temporary not in mapping:
            official = new_id('claim')
            mapping[temporary], mapping[official] = official, official
        claim['claim_id'] = mapping[temporary]
        await binding.repository.save(binding.incident_id, 'claim', dict(claim, id=claim['claim_id'], producer='supervisor', verified=False))
        normalized.append(claim)
    normalized_items = []
    for raw in items:
        if not isinstance(raw, (dict, ReportItem)):
            errors.append({'field': 'report_items', 'type': 'invalid_item', 'reason': 'expected a report item object'})
            continue
        item = raw.model_dump() if isinstance(raw, ReportItem) else dict(raw)
        item['claim_ids'] = [mapping.get(ref, ref) if isinstance(ref, str) else ref for ref in item.get('claim_ids', [])] if isinstance(item.get('claim_ids', []), list) else item.get('claim_ids')
        normalized_items.append(item)
    return normalized, normalized_items, errors


async def check_items(binding, items, checks):
    accepted, errors = [], []
    statements = {c['claim_id']: c for c in checks}
    section_types = {'observations': {'observation', 'causal'}, 'hypotheses': {'hypothesis'}, 'exclusions': {'exclusion'}}
    for index, raw in enumerate(items):
        try:
            item = ReportItem.model_validate(raw).model_dump()
            if not any(item[key] for key in ('claim_ids', 'evidence_ids', 'task_ids')):
                raise WorkspaceError('report item requires a registered claim, evidence or task reference')
            if item['section'] in section_types and not item['claim_ids']:
                raise WorkspaceError('fact/hypothesis/exclusion requires its corresponding statement ID')
            for reference in item['claim_ids']:
                claim = statements.get(reference)
                if claim is None or not claim['retained']:
                    raise WorkspaceError('unknown or rejected statement: ' + reference)
                if item['section'] in section_types and claim['statement']['type'] not in section_types[item['section']]:
                    raise WorkspaceError('report section does not match statement type')
            for kind, field in (('evidence', 'evidence_ids'), ('task', 'task_ids')):
                for ref in item[field]:
                    await binding.repository.read(binding.incident_id, ref, kind)
            if item['section'] in section_types:
                canonical = '\n'.join(statements[ref]['statement']['content'] for ref in item['claim_ids'])
                if item['content'] != canonical:
                    raise WorkspaceError('report text must equal its accepted statements; independent paraphrases can add unsupported conclusions')
                item['content'] = canonical
            elif assertive_candidate(item['content']):
                raise WorkspaceError('candidate/limitation wording still asserts an unverified cause or blanket exclusion')
            accepted.append(item)
        except (WorkspaceError, ValidationError, TypeError) as exc:
            errors.append({'field': f'report_items[{index}]', 'type': 'invalid_report_item', 'reason': str(exc)})
    return accepted, errors


async def inspect_proposal(binding, claims, items):
    checks = [await check_statement(binding, claim) for claim in claims]
    accepted, errors = await check_items(binding, items, checks)
    errors.extend(error for check in checks for error in check['errors'])
    if not items:
        errors.append({'field': 'report_items', 'type': 'missing_structured_report', 'reason': 'old free report requires structured statement references'})
    return checks, accepted, errors
