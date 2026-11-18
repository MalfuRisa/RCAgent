"""Prove a proposed field value was in this owner's current visible input."""

import json
from .artifacts import read_artifact_raw
from .repository import WorkspaceError


def pointer_path(pointer):
    from .material_ranges import pointer_field

    return pointer_field(pointer)


def value_at(node, segments):
    for key in segments:
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return None, False
    return node, True


def page_value(body, path):
    prefix = body.get("field") or ""
    if prefix and path != prefix and not path.startswith(prefix + "."):
        return None, False
    suffix = path[len(prefix) :].lstrip(".").split(".") if path != prefix else []
    if not path and not prefix:
        suffix = []
    if "text" in body:
        span = body.get("range") or []
        if len(span) != 2 or span[0] != 0 or body.get("truncated"):
            return None, False
        try:
            return value_at(json.loads(body["text"]), suffix)
        except ValueError:
            return None, False
    items, span = body.get("items"), body.get("range") or []
    if not isinstance(items, list) or len(span) != 2:
        return None, False
    # Whole object/scalar field pages wrap their single item; array pages need
    # an actual source index. Never treat a tail as an entire array.
    if suffix and suffix[0].isdigit():
        index = int(suffix[0]) - span[0]
        if 0 <= index < len(items):
            return value_at(items[index], suffix[1:])
    if span == [0, 1] and len(items) == 1 and not body.get("truncated"):
        return value_at(items[0], suffix)
    return None, False


def proposed_page_value(body, receipt, source_ref):
    path = pointer_path(source_ref.get("json_pointer", ""))
    if path is None:
        return None, False
    if source_ref.get("range") is None:
        return page_value(body, path)
    span, shown = source_ref["range"], body.get("range") or []
    unit = source_ref.get("range_unit")
    if (
        (body.get("field") or "") != path
        or len(shown) != 2
        or unit != receipt.get("range_unit")
        or not shown[0] <= span[0] <= span[1] <= shown[1]
    ):
        return None, False
    data = body.get("text") if unit == "chars" else body.get("items")
    if not isinstance(data, (str, list)):
        return None, False
    return data[span[0] - shown[0] : span[1] - shown[0]], True


async def field_shown(binding, source_ref, actual, *, source_context_id=None):
    from .evidence_references import proves

    if await proves(binding, source_ref, actual):
        return True
    from .observations import values_match
    from context.source_paths import source_values

    path = pointer_path(source_ref.get("json_pointer", ""))
    source_id = source_ref.get("evidence_id") or source_ref.get("artifact_id")
    context_id = source_context_id or binding.last_context_id
    if not context_id:
        return False
    manifest = await binding.repository.read(binding.incident_id, context_id, "context")
    if (
        manifest.get("run_id"),
        manifest.get("role"),
        manifest.get("task_id"),
        manifest.get("attempt_id"),
    ) != (binding.run_id, binding.role, binding.task_id, binding.attempt_id):
        return False
    try:
        messages = await read_artifact_raw(
            binding, manifest["visible_messages_artifact_id"]
        )
    except (WorkspaceError, KeyError, OSError, ValueError):
        return False
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str):
            continue
        if content.startswith("Incident workbench"):
            payload = json.loads(content.split("\n", 1)[1])
            from context.exact_representation import expand

            payload = expand(payload)
            # Current candidates display exact source-bound values even when a
            # reviewer only receives a source directory. Raw-value/version/scope
            # validation still occurs in verify_observation before this proof.
            for candidate in payload.get("current_candidates") or []:
                ref = candidate.get("source_ref") or {}
                if (
                    source_id in (ref.get("evidence_id"), ref.get("artifact_id"))
                    and ref.get("source_revision") == source_ref.get("source_revision")
                    and ref.get("json_pointer") == source_ref.get("json_pointer")
                    and ref.get("range") == source_ref.get("range")
                    and ref.get("range_unit") == source_ref.get("range_unit")
                    and "expected_value" in candidate
                    and values_match(candidate["expected_value"], actual)
                ):
                    return True
            from context.read_details import read_detail

            for detail in payload.get("verified_read_details") or []:
                receipt = await binding.repository.read(
                    binding.incident_id, detail["receipt_id"], "read_receipt"
                )
                if (
                    receipt.get("source_id") != source_id
                    or receipt.get("source_kind") not in ("evidence", "artifact")
                    or (
                        receipt.get("run_id"),
                        receipt.get("role"),
                        receipt.get("task_id"),
                        receipt.get("attempt_id"),
                    )
                    != (
                        binding.run_id,
                        binding.role,
                        binding.task_id,
                        binding.attempt_id,
                    )
                ):
                    continue
                stored = await read_artifact_raw(binding, receipt["result_artifact_id"])
                if read_detail(receipt, stored)["content"] != detail.get("content"):
                    continue
                value, found = proposed_page_value(stored, receipt, source_ref)
                if found and values_match(value, actual):
                    return True
            for row in (payload.get("investigation") or {}).get("observations") or []:
                ref = row.get("source_ref") or {}
                if (
                    source_id in (ref.get("evidence_id"), ref.get("artifact_id"))
                    and ref.get("json_pointer") == source_ref.get("json_pointer")
                    and ref.get("range") == source_ref.get("range")
                    and ref.get("range_unit") == source_ref.get("range_unit")
                    and "field_value" in row
                    and values_match(row["field_value"], actual)
                ):
                    return True
            for row in (
                payload.get("evidence", []) if source_ref.get("range") is None else []
            ):
                if row.get("evidence_id") != source_id:
                    continue
                if any(
                    item.get("json_pointer") == source_ref.get("json_pointer")
                    and values_match(item.get("value"), actual)
                    for item in row.get("exact_fields", [])
                ):
                    return True
                from context.projection_paths import card_paths

                if any(
                    original == source_ref.get("json_pointer")
                    and values_match(value, actual)
                    for _aliases, original, value in card_paths(row)
                ):
                    return True
                for key, value in (row.get("observations") or {}).items():
                    leaves, _ = source_values((row.get("source") or {}).get(key), value)
                    if any(p == path and values_match(v, actual) for p, v in leaves):
                        return True
            continue
        if message.get("type") != "tool":
            continue
        try:
            body = json.loads(content)
        except ValueError:
            continue
        if not isinstance(body, dict):
            continue
        if (
            body.get("evidence_id") != source_id
            and body.get("artifact_id") != source_id
        ):
            continue
        receipt_id = body.get("read_receipt_id")
        if not receipt_id:
            continue
        receipt = await binding.repository.read(
            binding.incident_id, receipt_id, "read_receipt"
        )
        if receipt.get("source_kind") not in ("evidence", "artifact"):
            continue
        stored = await read_artifact_raw(binding, receipt["result_artifact_id"])
        if stored != body or (
            receipt.get("run_id"),
            receipt.get("role"),
            receipt.get("task_id"),
            receipt.get("attempt_id"),
        ) != (binding.run_id, binding.role, binding.task_id, binding.attempt_id):
            continue
        value, found = proposed_page_value(body, receipt, source_ref)
        if found and values_match(value, actual):
            return True
    return False


async def visible_projection_cards(binding, *, source_context_id=None):
    """Read cards only from this owner's latest persisted model input."""
    context_id = source_context_id or binding.last_context_id
    if not context_id:
        return []
    manifest = await binding.repository.read(binding.incident_id, context_id, "context")
    if (
        manifest.get("run_id"),
        manifest.get("role"),
        manifest.get("task_id"),
        manifest.get("attempt_id"),
    ) != (binding.run_id, binding.role, binding.task_id, binding.attempt_id):
        return []
    messages = await read_artifact_raw(
        binding, manifest["visible_messages_artifact_id"]
    )
    cards = []
    for message in messages:
        text = message.get("content")
        if not isinstance(text, str) or not text.startswith("Incident workbench"):
            continue
        payload = json.loads(text.split("\n", 1)[1])
        from context.exact_representation import expand

        payload = expand(payload)
        cards.extend(payload.get("evidence") or [])
    return cards


async def projection_pointer(
    binding, evidence_id, submitted, *, source_context_id=None
):
    """Normalize only exact aliases in this owner's latest persisted input."""
    from context.projection_paths import resolve_card_pointer

    candidates = set()
    for card in await visible_projection_cards(
        binding, source_context_id=source_context_id
    ):
        if card.get("evidence_id") == evidence_id:
            canonical = resolve_card_pointer(card, submitted)
            if canonical:
                candidates.add(canonical)
    return next(iter(candidates)) if len(candidates) == 1 else None


async def correction_examples(binding, errors):
    """Offer a few copyable visible fields, never suggest a hidden raw value."""
    from context.projection_paths import card_paths

    bad_refs = [error.get("source_ref") or {} for error in errors]
    examples, seen, chars = [], set(), 0
    for ref in bad_refs:
        for card in await visible_projection_cards(binding):
            if card.get("evidence_id") != ref.get("evidence_id"):
                continue
            parent = (ref.get("json_pointer") or "").rsplit("/", 1)[0]
            nodes = sorted(
                card_paths(card), key=lambda node: not node[1].startswith(parent)
            )
            for _aliases, canonical, value in nodes:
                if (
                    isinstance(value, dict)
                    or (isinstance(value, list) and value)
                    or len(json.dumps(value)) > 100
                ):
                    continue
                key = (card["evidence_id"], canonical)
                if key in seen:
                    continue
                item = {
                    "source_ref": {"evidence_id": key[0], "json_pointer": canonical},
                    "expected_value": value,
                }
                size = len(json.dumps(item, ensure_ascii=False))
                if chars + size > 1000 or len(examples) >= 5:
                    return examples
                examples.append(item)
                seen.add(key)
                chars += size
    return examples


async def fragment_shown(binding, source_ref, fragment, *, source_context_id=None):
    """Require the exact source text, rather than an unrelated mention of its ID."""
    context_id = source_context_id or binding.last_context_id
    if not context_id:
        return False
    manifest = await binding.repository.read(binding.incident_id, context_id, "context")
    if (
        manifest.get("run_id"),
        manifest.get("role"),
        manifest.get("task_id"),
        manifest.get("attempt_id"),
    ) != (binding.run_id, binding.role, binding.task_id, binding.attempt_id):
        return False
    messages = await read_artifact_raw(
        binding, manifest["visible_messages_artifact_id"]
    )
    source_id = source_ref.get("evidence_id") or source_ref.get("artifact_id")
    for message in messages:
        if message.get("type") != "tool":
            continue
        try:
            body = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict) or source_id not in (
            body.get("evidence_id"),
            body.get("artifact_id"),
        ):
            continue
        if not body.get("read_receipt_id"):
            continue
        receipt = await binding.repository.read(
            binding.incident_id, body["read_receipt_id"], "read_receipt"
        )
        if (
            receipt.get("run_id"),
            receipt.get("role"),
            receipt.get("task_id"),
            receipt.get("attempt_id"),
        ) != (
            binding.run_id,
            binding.role,
            binding.task_id,
            binding.attempt_id,
        ) or receipt.get("source_kind") not in ("evidence", "artifact"):
            continue
        if await read_artifact_raw(binding, receipt["result_artifact_id"]) != body:
            continue
        if fragment in (body.get("text") or ""):
            return True
        if any(
            fragment
            in (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
            for item in body.get("items") or []
        ):
            return True
    return False
