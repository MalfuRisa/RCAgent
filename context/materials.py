"""Role/target selection and loss-aware observation summaries."""
import json
import re

SAMPLE_LISTS = {'samples', 'series', 'traces', 'spans', 'events', 'pods', 'targets', 'candidates'}


async def focus_resources(binding, names, main_name=None, main_namespace=None):
    if binding is None:
        return
    resources = await binding.repository.all(binding.incident_id, 'resource')
    binding.focus_resource_ids = [r['id'] for r in resources if r['name'] in names and
        (r['name'] != main_name or not main_namespace or r.get('namespace') == main_namespace)]


def key_observations(value):
    """Retain summaries and limitations verbatim; bulky samples remain in artifacts.

    No clipping of required scalar values: a too-large summary fails the context
    budget explicitly. Small sample heads are optional and labeled incomplete.
    """
    if isinstance(value, list):
        return [key_observations(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in SAMPLE_LISTS and isinstance(item, list):
            small = [key_observations(v) for v in item[:3] if len(json.dumps(v, ensure_ascii=False)) <= 1000]
            result[key] = small
            if len(small) < len(item):
                result[key + '_omitted_count'] = len(item) - len(small)
        else:
            result[key] = key_observations(item)
    return result


def summarized_message(content):
    if not isinstance(content, str):
        return json.dumps(key_observations(content), ensure_ascii=False)
    try:
        return json.dumps(key_observations(json.loads(content)), ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    replaced = False
    def replace(match):
        nonlocal replaced
        try:
            summary = key_observations(json.loads(match.group(1)))
        except ValueError:
            return match.group(0)
        replaced = True
        return '```json\n' + json.dumps(summary, ensure_ascii=False) + '\n```'
    result = re.sub(r'```json\s*([\s\S]*?)```', replace, content)
    return result if replaced else None


def select_materials(snapshot, binding):
    resources, tasks, evidence = snapshot['resource'], snapshot['task'], snapshot['evidence']
    focus = set(binding.focus_resource_ids or ([binding.resource_id] if binding.resource_id else []))
    if not focus and binding.role in ('triage', 'planner'):
        focus = {rid for e in evidence if e.get('run_id') == binding.run_id and not e.get('producer_task_id') for rid in e['resource_ids']}
    if not focus:
        return snapshot, []
    # Inventory/backends may explicitly connect a queried Service to its Pods.
    related = [e for e in evidence if focus.intersection(e['resource_ids'])]
    focus.update(rid for e in related if e['tool_name'] == 'get_service_backends' for rid in e['resource_ids'])
    selected_tasks = [t for t in tasks if focus.intersection(t['target_resource_ids']) or t['id'] == binding.task_id]
    task_ids = {t['id'] for t in selected_tasks}
    assessments = [a for a in snapshot['assessment'] if a['task_id'] in task_ids]
    referenced = {ref for a in assessments for ref in a['supporting_evidence_ids'] + a['opposing_evidence_ids']}
    selected_evidence = [e for e in evidence if focus.intersection(e['resource_ids']) or e['id'] in referenced or
                         e.get('producer_task_id') == binding.task_id and binding.task_id]
    focus.update(rid for e in selected_evidence for rid in e['resource_ids'])
    selected = dict(snapshot, resource=[r for r in resources if r['id'] in focus], task=selected_tasks,
                    assessment=assessments, evidence=selected_evidence)
    kept = {r['id'] for key in ('resource', 'task', 'assessment', 'evidence') for r in selected[key]}
    omitted = [{'id': r['id'], 'reason': 'outside current role/target context; available via local workbench reads'}
               for key in ('resource', 'task', 'assessment', 'evidence') for r in snapshot[key] if r['id'] not in kept]
    return selected, omitted

async def evidence_digest(binding, tool_names, *, run_scoped=True, limit=8):
    """Reference-style digest of already-archived evidence for prompt text.

    Used by Triage/Planner to point at the same facts the workbench will show,
    instead of pasting a second full copy of the gateway response (PRD §7.4).
    Returns ``(lines, covered_tools)``; ``lines`` is empty when nothing applies.
    """
    if binding is None:
        return [], set()
    from .views import evidence_view, window_table
    evidence = [e for e in await binding.repository.all(binding.incident_id, 'evidence')
                if e.get('tool_name') in tool_names and (not run_scoped or e.get('run_id') == binding.run_id)]
    if not evidence:
        return [], set()
    windows, refs = window_table(evidence)
    lines, covered = [], set()
    for row in evidence[:limit]:
        view = evidence_view(row, refs)
        line = {'evidence_id': view['evidence_id'], 'tool_name': view['tool_name'],
                'transport_status': view.get('result_transport_status'),
                'source_status': view.get('result_source_status'),
                'observations': view.get('observations', {}),
                'limitations': view.get('limitations', {}),
                'detail': view['detail']}
        lines.append(line)
        covered.add(row['tool_name'])
    return lines, covered
