"""Small model handoff. The archived stage and MCP envelope keep the full details."""

from .stage_facts import fact_source
from .query_context import model_source_context


def model_source(outputs, sid):
    source = outputs.sources[sid]
    raw = outputs.materials.raw.get(sid) or {}
    fact = fact_source(source, raw)
    result = {k: source.get(k) for k in ('source_id', 'tool_name', 'mcp_source', 'status', 'error',
             'mcp_call_started', 'resolved_time', 'scope_status', 'returned_rows', 'backend_total',
             'business_value_count')}
    result['read_entry'] = {'source_id': sid, 'pointer': ''}
    result['source_context'] = model_source_context(source.get('source_context'))
    if fact is None:
        structured = raw.get('structuredContent')
        data = structured.get('data') if isinstance(structured, dict) else None
        if isinstance(data, dict):
            result['data_pointer'] = '/structuredContent/data'
            if isinstance(data.get('list'), list):
                rows = data['list']
                result['list_preview'] = rows[:1]
                result['list_received'] = len(rows)
        return result
    for key in ('subject', 'scope', 'upstream_gap', 'gaps', 'result_warnings', 'comparedStat', 'coverage',
                'received_items', 'collection_pointer', 'points_pointer', 'observation_point'):
        if fact.get(key) is not None:
            result[key] = fact[key]
    for key, limit in (('paths', 5), ('anomalies', 4), ('checked', 12), ('metrics', 4)):
        if key in fact:
            result[key] = fact[key][:limit]
            result[key + '_shown'] = len(result[key])
    if 'panels' in fact:
        result['panels'] = [panel_view(panel) for panel in fact['panels']]
    notices = []
    for block in fact.get('independent_content', []):
        value = block.get('value')
        text = value.get('text') if isinstance(value, dict) else None
        short = text.get('fragment') if isinstance(text, dict) else text
        notices.append({'pointer': block['pointer'], 'text_excerpt': short[:220] if isinstance(short, str) else None})
    result['independent_notices'] = notices
    result['content_omissions'] = [o for o in fact.get('content_omissions', [])
                                  if o['reason']!='exact JSON duplicate of structuredContent; original retained']
    return result


def panel_view(panel):
    keys = ('dataset_id', 'metrics', 'rows_received', 'pointer', 'coverage', 'comparedStat', 'chart_entries')
    return {k: panel[k] for k in keys} | {'rows': panel['rows'][:6], 'rows_shown': min(6,len(panel['rows']))}


def source_cards(outputs, ids):
    return [model_source(outputs, sid) for sid in dict.fromkeys(ids) if sid in outputs.sources]


def stage_handoff(outputs, result):
    if result is None:
        return {'status':'unconfirmed','notice':'Cancelled worker did not settle'}
    spec = result.get('spec') or {}
    card = {k: result.get(k) for k in ('task_id', 'status', 'end_reason', 'source_ids', 'errors',
            'handoff_mode', 'handoff_reason', 'window_kind')}
    card.update(goal=spec.get('question'), expected=spec.get('completion_criteria'),
                decision_impact=spec.get('decision_impact'), window=spec.get('window'))
    if not result.get('facts'):
        note = result.get('answer_text') or ''
        card.update(worker_excerpt=note[:680], worker_excerpt_truncated=len(note)>680,
                    worker_note_archive=result.get('answer_path'))
    return card
