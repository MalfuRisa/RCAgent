"""Shared visible-content budget for Planner read_material calls in one response."""

import json


def _dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


class ReadBudget:
    """Counts the serialized Planner-visible receipts, never the archived originals."""

    def __init__(self, limit):
        self.limit = max(1, int(limit))
        self.used = 0
        self.calls = 0
        self.truncated = 0

    def remaining(self):
        return max(0, self.limit - self.used)

    def card(self):
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining(),
            "calls": self.calls,
            "truncated": self.truncated,
        }

    def _next_entry(self, args, result):
        pointer = args.get("pointer", "")
        end = None
        value_range = result.get("range")
        if isinstance(value_range, list) and len(value_range) == 2:
            end = value_range[1]
        offset = result.get("next_offset")
        if offset is None and end is not None and result.get("range_unit") != "scalar":
            offset = end
        return {
            "source_id": args.get("source_id"),
            "pointer": pointer,
            "offset": offset,
            "max_chars": max(256, self.remaining()),
        }

    def apply(self, call_id, name, args, result):
        if name != "read_material":
            return result
        self.calls += 1
        visible = dict(result)
        visible["visible_budget"] = self.card()
        text = _dump(visible)
        size = len(text)
        remaining = self.remaining()
        if size <= remaining:
            self.used += size
            return visible
        self.truncated += 1
        next_entry = self._next_entry(args, result)
        compact = {
            "source_id": args.get("source_id"),
            "pointer": args.get("pointer", ""),
            "range": result.get("range"),
            "range_unit": result.get("range_unit"),
            "returned_count": result.get("returned_count"),
            "truncated": True,
            "next_offset": next_entry.get("offset"),
            "read_entry": {
                "source_id": args.get("source_id"),
                "pointer": args.get("pointer", ""),
            },
            "next": next_entry,
            "visible_budget": self.card(),
            "notice": (
                "This response's visible read budget is exhausted; content omitted. "
                "Continue with next.offset or narrow the JSON Pointer in a later response."
            ),
        }
        compact_text = _dump(compact)
        if len(compact_text) > remaining:
            compact = {
                "source_id": args.get("source_id"),
                "pointer": args.get("pointer", ""),
                "truncated": True,
                "next": next_entry,
                "visible_budget": self.card(),
                "notice": "Visible read budget exhausted.",
            }
            compact_text = _dump(compact)
        self.used += len(compact_text)
        return compact