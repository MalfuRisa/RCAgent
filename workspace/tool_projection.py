"""Deterministic model view; immutable received originals remain separately readable."""
import json


def project_tree(value, pointer='', omissions=None):
    omissions = omissions if omissions is not None else []
    if isinstance(value, dict):
        result = {}
        items=list(value.items())
        if len(items)>24:
            omissions.append({'pointer':pointer,'shown':24,'total_received':len(items),'reason':'mapping view sample; direct keys remain readable'})
        for key, item in items[:24]:
            child = pointer + '/' + str(key).replace('~','~0').replace('/','~1')
            if key in {'charts', 'display', 'history'}:
                omissions.append({'pointer':child,'reason':'optional rendered/series detail; original retained'})
                continue
            result[key] = project_tree(item, child, omissions)
        return result
    if isinstance(value, list):
        limit = 12 if pointer.endswith(('/metrics','/metricSemantics','/flags')) else 8 if pointer.endswith('/results') else 64 if pointer.endswith('/checked') else 10 if pointer.endswith(('/paths','/anomalies','/resourceRefs')) else 3
        if len(value)>limit:
            omissions.append({'pointer':pointer,'shown':limit,'total_received':len(value),'reason':'view sample, not full result'})
        return [project_tree(item,pointer+'/'+str(i),omissions) for i,item in enumerate(value[:limit])]
    if isinstance(value,str) and len(value)>1000:
        omissions.append({'pointer':pointer,'reason':'text fragment','full_characters':len(value)})
        return {'fragment':value[:1000],'fragment_only':True}
    return value


def content_view(raw, structured, omissions):
    """Only drop text whose parsed value equals the whole structured payload."""
    blocks = []
    for index, block in enumerate(raw.get('content') or []):
        pointer = '/content/' + str(index)
        text = block.get('text') if isinstance(block, dict) else None
        try:
            duplicate = structured is not None and isinstance(text, str) and json.loads(text) == structured
        except (ValueError, TypeError):
            duplicate = False
        if duplicate:
            omissions.append({'pointer':pointer,'reason':'exact JSON duplicate of structuredContent; original retained'})
            continue
        if len(blocks) < 3:
            blocks.append({'pointer':pointer,'value':project_tree(block,pointer,omissions)})
        else:
            omissions.append({'pointer':pointer,'reason':'additional independent content block; read original'})
    return blocks


def compact_view(source_id, raw):
    omissions = []
    structured = raw.get('structuredContent')
    view = {'source_id':source_id,'projection':project_tree(structured,'/structuredContent',omissions)} if structured is not None else {
        'source_id':source_id,'projection':project_tree(raw,'',omissions)}
    if structured is not None:
        view['independent_content'] = content_view(raw, structured, omissions)
        if 'isError' in raw:
            view['isError'] = raw['isError']
    return json.dumps({**view,
        'omissions':omissions,'read_entry':{'tool':'read_material','source_id':source_id,'pointer':''},
        'notice':'Only this received API response is archived; projection is not a full population. Omitted content is available by exact JSON Pointer.'},ensure_ascii=False,default=str)
