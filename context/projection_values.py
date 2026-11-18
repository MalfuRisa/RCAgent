"""Fixed field projections for the thirteen Gateway tools.

Storage keeps the full response; this module selects the small, model-facing
brief for one evidence record. Every projected value names its ``source`` path
so a claim can be traced back to the archived response (PRD §6.4/§6.5).

Rules encoded here:

* values are copied unchanged: ``0`` is not ``None``, ``Running`` is not
  "all healthy", ``not_found`` is not "does not exist";
* expected-but-absent fields are reported in ``unknown_fields`` instead of
  being invented as ``ok``;
* bulky lists are capped with an explicit ``shown``/``total`` marker and a
  bounded detail entry, never silently clipped;
* long free text becomes a clearly marked preview plus ``detail_required``;
* a return shape that is not recognised yields a top-level key directory and
  ``detail_required`` instead of the whole object.
"""

import json

MAX_LIST_ITEMS = 5
MAX_SAMPLE_ITEMS = 4
GROUP_SUMMARY_LIMIT = 12
TEXT_LIMIT = 400
PREVIEW_CHARS = 200


def _text(value, limit=TEXT_LIMIT):
    """Copy text unchanged, or keep a marked preview for very long values."""
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return {"preview": value[:PREVIEW_CHARS], "chars": len(value),
            "preview_incomplete": True, "detail_required": True}


def _dig(source, path):
    """Read a dotted path; any missing/non-dict step yields ``(None, False)``."""
    node = source
    for key in path.split('.'):
        if not isinstance(node, dict) or key not in node:
            return None, False
        node = node[key]
    return node, True


def _pick(source, paths):
    """Select dotted paths into ``observations``/``source``/``unknown_fields``."""
    observations, origin, missing = {}, {}, []
    for key, path in paths:
        value, found = _dig(source, path)
        if not found:
            missing.append(path)
            continue
        observations[key] = _text(value) if isinstance(value, str) else value
        origin[key] = path
    return observations, origin, missing


def _items(rows, fields, limit=MAX_LIST_ITEMS):
    """Project list items by field name and record how many were shown."""
    if not isinstance(rows, list):
        return None, 0
    shown = []
    for row in rows[:limit]:
        if fields is None or not isinstance(row, dict):
            shown.append(row)
            continue
        shown.append({name: row.get(name) for name in fields})
    return shown, len(rows)


def _listed(rows, fields, limit=MAX_LIST_ITEMS, key='items'):
    """Return a capped list block with shown/total markers."""
    shown, total = _items(rows, fields, limit)
    if shown is None:
        return None
    block = {key: shown, 'shown': len(shown), 'total': total}
    if len(shown) < total:
        block['detail_required'] = True
    return block


def _representative(rows, group_fields, fields, limit=MAX_SAMPLE_ITEMS):
    """Group by protocol/status-code style fields, then take originals in order.

    Selections are tracked by the original list index; grouping keys are never
    mixed with object identity, so one row cannot be shown twice. A grouping
    field absent from a row is not the same as a null value, and the categories
    with their counts stay visible so the shown rows are never presented as all
    error types (PRD §17.4).
    """
    if not isinstance(rows, list):
        return None
    groups, order = {}, []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        key = tuple(row.get(name) if name in row else '<absent>' for name in group_fields)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(index)
    selected = []
    for key in order:  # one representative per category, in first-seen order
        if len(selected) >= limit:
            break
        selected.append(groups[key][0])
    taken = set(selected)
    for index in range(len(rows)):  # fill up in original order after one per group
        if len(selected) >= limit:
            break
        if index in taken:
            continue
        selected.append(index)
        taken.add(index)
    shown, present = [], set()
    for index in selected:
        row = rows[index]
        if isinstance(row, dict):
            entry = {name: (_text(row[name]) if isinstance(row[name], str) else row[name])
                     for name in fields if name in row}
            present.update(name for name in fields if name in row)
        else:
            entry = {'value': row}
        entry['source_index'] = index
        shown.append(entry)
    shown_per_group = {key: len([index for index in selected if index in indices])
                       for key, indices in groups.items()}
    summary = []
    for key in order:
        entry = {name: (None if value == '<absent>' else value) for name, value in zip(group_fields, key)}
        entry['count'] = len(groups[key])
        entry['shown'] = shown_per_group.get(key, 0)
        summary.append(entry)
    block = {'samples': shown, 'shown': len(shown), 'total': len(rows),
             'shown_indexes': selected, 'group_fields': list(group_fields),
             'groups': summary[:GROUP_SUMMARY_LIMIT]}
    if len(summary) > GROUP_SUMMARY_LIMIT:
        block['groups_omitted'] = len(summary) - GROUP_SUMMARY_LIMIT
    without_sample = [entry for entry in summary if not entry.get('shown')]
    if without_sample:
        block['categories_without_shown_sample'] = without_sample[:GROUP_SUMMARY_LIMIT]
    if not any(isinstance(row, dict) and any(name in row for name in group_fields) for row in rows):
        block['ordering'] = 'original_order_sample_only'
    else:
        block['ordering'] = 'one_representative_per_category_then_original_order'
    if len(shown) < len(rows):
        block['detail_required'] = True
        block['detail'] = 'read_evidence(field=samples, offset=0) lists every stored row'
    unknown = ['samples[].%s' % name for name in fields if name not in present]
    if unknown:
        block['unknown_fields'] = unknown
    return block


def _gap_entries(rows):
    """Declared warnings/gaps stay visible and structured: source/reason kept,
    identical entries deduplicated, only an overlong field becomes a marked
    preview with a detail entry (PRD §6.4.2/§6.4.3)."""
    seen, entries = set(), []
    for item in rows:
        signature = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if signature in seen:
            continue
        seen.add(signature)
        if isinstance(item, str):
            entries.append(_text(item))
        elif isinstance(item, dict):
            entry = dict(item)
            for key in ('impact', 'detail', 'note'):
                if isinstance(entry.get(key), str):
                    entry[key] = _text(entry[key])
            entry.setdefault('detail_preserved', True)
            entries.append(entry)
        else:
            entries.append(item)
    return entries


EXTRA_SCALAR_LIMIT = 8


def _extra_scalars(node, known, prefix):
    """Bounded safety net for scalar fields the fixed map did not name.

    Real responses may carry extra scalars (fixture variance or schema drift).
    They stay visible with a source path so a key observation is never lost to
    the projection; nested objects and long lists are still left to detail.
    """
    if not isinstance(node, dict):
        return {}, {}
    extra, origin = {}, {}
    for key in sorted(node):
        if key in known:
            continue
        value = node[key]
        if not (value is None or isinstance(value, (bool, int, float, str))):
            continue
        extra[key] = _text(value)
        origin[key] = prefix + '.' + key
        if len(extra) >= EXTRA_SCALAR_LIMIT:
            break
    return extra, origin


def _gaps(raw):
    """Warnings and declared data gaps stay visible; they are never dropped."""
    meta = raw.get('meta') if isinstance(raw.get('meta'), dict) else {}
    warnings = (raw.get('warnings') or []) + (meta.get('warnings') or [])
    gaps = (raw.get('data_gaps') or []) + (meta.get('data_gaps') or [])
    return _gap_entries(warnings), _gap_entries(gaps)


def _result(raw):
    meta = raw.get('meta') if isinstance(raw.get('meta'), dict) else {}
    return {'status': raw.get('status', meta.get('status')), 'truncated': raw.get('truncated', meta.get('truncated')),
            'freshness': raw.get('freshness', meta.get('data_freshness'))}


