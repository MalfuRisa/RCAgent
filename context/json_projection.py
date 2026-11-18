"""Loss-labelled scalar previews for otherwise unknown read-only tool responses."""

import json


def scalar_fields(node, pointer="", *, max_fields=60, max_list=3):
    fields = []
    omitted = 0

    def visit(value, path, depth):
        nonlocal omitted
        if len(fields) >= max_fields or depth > 8:
            omitted += 1
            return
        if isinstance(value, dict):
            if not value:
                fields.append((path, value))
            for k, v in value.items():
                visit(v, path + "/" + str(k).replace("~", "~0").replace("/", "~1"), depth + 1)
        elif isinstance(value, list):
            if not value:
                fields.append((path, value))
            for i, v in enumerate(value[:max_list]):
                visit(v, path + "/" + str(i), depth + 1)
            omitted += max(0, len(value) - max_list)
        elif len(json.dumps(value, ensure_ascii=False)) <= 400:
            fields.append((path, value))
        else:
            omitted += 1

    visit(node, pointer, 0)
    return fields, omitted


def project_json(raw):
    fields, omitted = scalar_fields(raw)
    # Dotted origins retain existing visibility semantics; exact JSON pointers
    # are also carried for names which contain dots or slashes.
    values = {f"field_{i}": v for i, (p, v) in enumerate(fields)}
    origins = {f"field_{i}": p.lstrip("/").replace("/", ".") for i, (p, v) in enumerate(fields)}
    return {
        "observations": values,
        "source": origins,
        "exact_fields": [{"json_pointer": p, "value": v} for p, v in fields],
        "omitted_entries": omitted,
        "detail_fields": list(raw) if isinstance(raw, dict) else ["<root>"],
        "detail_required": bool(omitted),
        "projection_note": "Bounded field preview; omitted entries are archived, not read.",
    }
