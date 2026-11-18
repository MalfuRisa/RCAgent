"""Run-local immutable originals and bounded, advancing material views."""
import json


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def render_tool_result(raw):
    structured = raw.get("structuredContent")
    parts = [dumps(structured)] if structured is not None else []
    for block in raw.get("content", []):
        text = block.get("text")
        if text is not None:
            try:
                duplicate = structured is not None and json.loads(text) == structured
            except (ValueError, TypeError):
                duplicate = False
            if not duplicate:
                parts.append(text)
        else:
            parts.append(dumps({"non_text_block": block.get("type"), "original": "read_material"}))
    return "\n".join(parts) or dumps(raw)


BUSINESS_TOOLS = frozenset({"insights_query_list", "insights_query_metric",
    "insights_list_cpu_events", "insights_flow_log_detail", "insights_app_log_detail"})


def page_counts(raw, tool_name=None):
    candidates = [raw.get("structuredContent")]
    for block in raw.get("content", []):
        try:
            candidates.append(json.loads(block.get("text", "")))
        except (ValueError, TypeError):
            pass
    for value in candidates:
        if not isinstance(value, dict):
            continue
        meta = value.get("meta") or {}
        pagination = meta.get("pagination") if isinstance(meta, dict) else None
        confirmed = isinstance(pagination, dict) and pagination.get("count_kind") == "business_rows"
        if tool_name not in BUSINESS_TOOLS and not confirmed:
            continue
        data = value.get("data", value)
        if isinstance(data, dict) and isinstance(data.get("list"), list):
            rows = data["list"]
            result = {"returned_rows": len(rows), "backend_total": data.get("total"),
                "returned_fields": list(rows[0]) if rows and isinstance(rows[0], dict) else []}
            if isinstance(pagination, dict):
                result["pagination"] = pagination
            return result
    return {}


def pointer_value(raw, pointer):
    if not pointer:
        return raw
    if not pointer.startswith("/"):
        raise ValueError("JSON Pointer must start with /")
    value = raw
    for segment in pointer[1:].split("/"):
        key = segment.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def structure_index(value, pointer="", budget=10000):
    """Preview nested field maps, including names omitted by first-row previews."""
    entries, used, stopped = [], 0, False
    def visit(item, path):
        nonlocal used, stopped
        if stopped:
            return
        entry = None
        if isinstance(item, dict):
            names = {k: item[k] for k in ("name", "tag_name", "client_name", "server_name", "type")
                     if k in item and isinstance(item[k], (str, int))}
            if "name" in names or "tag_name" in names:
                entry = {"pointer": path, **names}
            elif any(isinstance(v, dict) for v in item.values()):
                entry = {"pointer": path, "keys": list(item)}
        if entry:
            size = len(dumps(entry).encode("utf-8"))
            if used + size > budget:
                stopped = True
                return
            entries.append(entry)
            used += size
        if isinstance(item, dict):
            for key, child in item.items():
                if isinstance(child, (dict, list)):
                    visit(child, path + "/" + str(key).replace("~", "~0").replace("/", "~1"))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, path + "/" + str(index))
    visit(value, pointer)
    return {"entries": entries, "truncated": stopped, "read_pointer": pointer}


class SourceMaterials:
    def __init__(self, outputs):
        self.outputs = outputs
        self.raw = {}
        self.tool_names = {}

    def store(self, source_id, raw, tool_name=None):
        self.raw[source_id] = raw
        self.tool_names[source_id] = tool_name
        return self.outputs.save("sources/" + source_id + ".json", raw)

    def read(self, source_id, pointer="", offset=0, limit=100, max_chars=16000):
        if source_id not in self.raw:
            raise ValueError("Unknown source_id in this run")
        if offset < 0 or limit <= 0 or max_chars <= 0:
            raise ValueError("offset >= 0, limit and max_chars > 0 required")
        raw = self.raw[source_id]
        value = pointer_value(raw, pointer)
        result = {"source_id": source_id, "pointer": pointer, **page_counts(raw, self.tool_names.get(source_id))}
        if isinstance(value, str):
            if offset > len(value):
                raise ValueError("offset out of range")
            selected = value[offset:offset + min(limit, max_chars)]
            end, size, unit = offset + len(selected), len(value), "unicode_characters"
        elif isinstance(value, (list, dict)):
            items = value if isinstance(value, list) else list(value.items())
            if offset > len(items):
                raise ValueError("offset out of range")
            selected = []
            for item in items[offset:offset + limit]:
                if len(dumps(selected + [item])) > max_chars:
                    break
                selected.append(item)
            if not selected and offset < len(items):
                # Advance past an oversized item with an explicit nested read entry.
                key = offset if isinstance(value, list) else items[offset][0]
                child = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
                selected = [{"oversized_item": True, "read_entry": {
                    "source_id": source_id, "pointer": child, "offset": 0,
                    "limit": max_chars, "max_chars": max_chars},
                    "preview": dumps(items[offset])[:max_chars], "preview_is_fragment": True}]
            end, size, unit = offset + len(selected), len(items), "items"
            if isinstance(value, dict) and (not selected or isinstance(selected[0], tuple)):
                selected = dict(selected)
        else:
            selected, end, size, unit = value, 1, 1, "scalar"
            if offset not in (0, 1):
                raise ValueError("offset out of range")
            if offset == 1:
                selected = None
        result.update(range=[offset, end], range_unit=unit, items_or_text=selected,
            returned_count=end-offset, truncated=end < size, next_offset=end if end < size else None,
            read_entry={"source_id": source_id, "pointer": pointer})
        return result

    def model_view(self, source_id, raw):
        from .tool_projection import compact_view
        return compact_view(source_id, raw)

    def search(self, query, scope="current"):
        if scope not in ("current", "history"):
            raise ValueError("scope must be current or history")
        # No implicit external/history mount. Never scan arbitrary paths.
        if scope == "history":
            return {"scope": "history", "historical": True, "results": [], "notice": "No history mounted in this run"}
        return {"scope": scope, "results": [{"source_id": key,
            "read_entry": {"source_id": key}, "historical": False}
            for key, value in self.raw.items() if query.lower() in dumps(value).lower()]}
