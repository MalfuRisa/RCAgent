"""Typed input parts: what a call may shorten, and what is protected (PRD §17.8).

Every message carries a program-assigned kind. Protected kinds are never
replaced by a generic sentence: if they do not fit, the call is refused with the
specific ``protected_parts_exceed_budget`` reason so the caller can see which
protected material was too large. Only optional kinds (earlier tool results,
catalogs, samples) are externalized, and only with a valid way back to the
original.
"""
import json
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

SYSTEM_RULES = 'system_rules'
USER_REQUIREMENT = 'user_requirement'
CURRENT_TASK = 'current_task'
VERIFIED_READ_DETAIL = 'verified_read_detail'
CORRECTION = 'correction'
RECENT_TOOL_RESULT = 'recent_tool_result'
OLD_TOOL_RESULT = 'old_tool_result'
OPTIONAL_CATALOG = 'optional_catalog'
SAMPLE_PREVIEW = 'sample_preview'
MODEL_DRAFT = 'model_draft'

#: Kinds the budget may shorten. Everything else is protected by default.
OPTIONAL_KINDS = frozenset({OLD_TOOL_RESULT, OPTIONAL_CATALOG, SAMPLE_PREVIEW})
PROTECTED_KINDS = frozenset({SYSTEM_RULES, USER_REQUIREMENT, CURRENT_TASK, VERIFIED_READ_DETAIL,
                             CORRECTION, RECENT_TOOL_RESULT, MODEL_DRAFT})


@dataclass
class ContextPart:
    """One classified piece of the input; ``kind`` is assigned by the program."""
    kind: str
    content: object
    source_refs: list = field(default_factory=list)
    protected: bool = None

    def __post_init__(self):
        if self.protected is None:
            self.protected = self.kind not in OPTIONAL_KINDS


def classify(messages, *, phase='request', recent_positions=()):
    """Assign a kind to every message; unclassified content is protected.

    ``recent_positions`` are the message positions of the most recent complete
    call batches, which keep their results (only the coverage matters there).
    """
    kinds = []
    for position, message in enumerate(messages):
        kinds.append(classify_message(message, phase=phase, recent=position in (recent_positions or ())))
    return kinds


def classify_message(message, *, phase='request', recent=False):
    if isinstance(message, SystemMessage):
        return SYSTEM_RULES
    if isinstance(message, HumanMessage):
        if phase == 'correction':
            return CORRECTION
        if phase in ('triage', 'plan'):
            return USER_REQUIREMENT
        return CURRENT_TASK
    if isinstance(message, AIMessage):
        return MODEL_DRAFT
    if isinstance(message, ToolMessage):
        return RECENT_TOOL_RESULT if recent else OLD_TOOL_RESULT
    return SYSTEM_RULES


def message_size(message):
    from .request_budget import canonical_request, serialized_chars
    return serialized_chars(canonical_request([message])['messages'][0])


def protected_parts(messages, kinds):
    """Explicit report of the protected parts, for refusal and audit."""
    report = []
    for position, message in enumerate(messages):
        kind = kinds[position] if position < len(kinds) else SYSTEM_RULES
        if kind in OPTIONAL_KINDS:
            continue
        report.append({'kind': kind, 'position': position, 'chars': message_size(message),
                       'tool_call_id': getattr(message, 'tool_call_id', None)})
    return report


async def shrink_optional(binding, messages, kinds, manifest, payload_text, limit, enabled):
    """Externalize oversized optional parts; protected parts are left untouched.

    A recent tool result whose full source is archived and referable (a read
    receipt id or an evidence/artifact id) may also be reduced to a preview that
    keeps that reference: the workbench already shows its coverage and the page
    stays readable, so this is a sample of a stored original, not a lost fact
    (PRD §17.5.1, §17.6.3). Everything else stays as it is.

    Returns ``(messages, overflowed)`` where ``overflowed`` is True when the
    visible input still exceeds ``limit`` after every allowed reduction - the
    caller then refuses with the protected-parts reason instead of deleting
    protected content.
    """
    from .builder import visible_chars
    from .history import archived_replacement, bounded_summary
    from workspace.artifacts import write_artifact
    visible = list(messages)
    decisions = []
    order = sorted(range(len(visible)), key=lambda index: message_size(visible[index]), reverse=True)
    for index in order:
        if not enabled or visible_chars(payload_text, visible) <= limit:
            break
        message = visible[index]
        kind = kinds[index] if index < len(kinds) else SYSTEM_RULES
        if message.type in ('system', 'ai', 'human'):
            continue  # only tool results are ever reduced; requirements stay intact
        pointer = source_references(message)
        size = message_size(message)
        if size < 1000 and not (pointer and size >= 400):
            continue  # a result that names its archived source can be a pointer much sooner
        if kind not in OPTIONAL_KINDS and not fully_represented(message, payload_text):
            continue
        original = str(message.content)
        decision = {'kind': kind, 'position': index, 'tool_call_id': getattr(message, 'tool_call_id', None),
                    'before_chars': message_size(message)}
        summary = bounded_summary(original)
        summary_text = json.dumps(summary, ensure_ascii=False) if summary is not None else None
        if summary_text is not None and len(summary_text) < len(original) and kind in OPTIONAL_KINDS:
            replacement = summary_text
            decision['strategy'] = 'bounded_summary'
        else:
            artifact = await write_artifact(binding, {'content': original, 'kind': kind,
                                                      'tool_call_id': getattr(message, 'tool_call_id', None)})
            await binding.repository.save(binding.incident_id, 'artifact', artifact)
            replacement = archived_replacement(artifact['id'], original, source_references(message))
            decision['strategy'] = 'archived_preview'
            decision['artifact_id'] = artifact['id']
            if kind not in OPTIONAL_KINDS:
                decision['reason'] = ('oversized recent tool result reduced to a preview; the full text is '
                                      'archived and its source ids stay readable')
        if len(replacement) >= len(original):
            decision['strategy'] = 'no_reduction'
            decision['reason'] = 'replacement would not be smaller; the original visible content is kept'
            decision['after_chars'] = decision['before_chars']
        else:
            visible[index] = message.model_copy(update={"content": replacement})
            decision['after_chars'] = message_size(visible[index])
        decisions.append(decision)
    if decisions:
        manifest.setdefault('history_replacements', []).extend(decisions)
    overflowed = enabled and visible_chars(payload_text, visible) > limit
    return visible, overflowed


def fully_represented(message, payload_text):
    """A recent result is optional only with values proven in this candidate.

    Local read pages require every returned leaf. For a remote network result,
    its already-projected summary may replace unread optional sample rows; the
    sample directory remains discoverable. Unknown source shapes stay intact.
    """
    from .source_paths import source_values
    try:
        body, payload = json.loads(message.content), json.loads(payload_text)
    except (TypeError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    eid = source_references(message).get('evidence_id')
    row = next((r for r in payload.get('evidence', []) if r.get('evidence_id') == eid), None)
    if not row:
        return False
    shown = {}
    for key, value in (row.get('observations') or {}).items():
        leaves, proven = source_values((row.get('source') or {}).get(key), value)
        if proven:
            shown.update(leaves)
    if body.get('read_receipt_id'):
        if 'text' in body:
            # A serialized character page may be partial; never infer its body.
            return False
        items = body.get('items')
        if not isinstance(items, list) or len(items) != 1 or body.get('truncated'):
            return False
        value, prefix = items[0], body.get('field') or ''
    else:
        if message.name != 'get_pod_network_summary' or not isinstance(body.get('summary'), dict):
            return False
        if body.get('status') in ('error', 'rejected', 'failed'):
            return False
        if set(body) - {'status', 'summary', 'samples', 'meta', '_rcagent_query', 'data_gaps', 'warnings',
                        'time_range', 'truncated', 'freshness', 'data_source'}:
            return False
        limits = row.get('limitations') or {}
        meta = body.get('meta') if isinstance(body.get('meta'), dict) else {}
        for key in ('data_gaps', 'warnings'):
            actual = (body.get(key) or []) + (meta.get(key) or [])
            if any(item not in (limits.get(key) or []) for item in actual):
                return False
        value, prefix = body['summary'], 'summary'
    def check(node, path):
        if isinstance(node, dict):
            return bool(node) and all(check(v, path + '.' + k if path else k) for k, v in node.items())
        if isinstance(node, list):
            return False  # representative lists do not prove the whole result
        return path in shown and shown[path] == node
    return check(value, prefix)


def source_references(message):
    """Ids of the stored source a tool result came from, when it names any.

    MCP results carry the evidence id of their stored operation on
    ``_rcagent_query``; local reads carry a receipt id or the source id. A free
    text result has none, and its preview then relies on the archived artifact
    id alone (PRD §17.6.3).
    """
    try:
        payload = json.loads(message.content)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    references = {}
    for key in ('evidence_id', 'read_receipt_id'):
        if payload.get(key):
            references[key] = payload[key]
    annotation = payload.get('_rcagent_query')
    if isinstance(annotation, dict) and annotation.get('evidence_id'):
        references.setdefault('evidence_id', annotation['evidence_id'])
    for value in payload.values():
        if not isinstance(value, dict):
            continue
        for key in ('evidence_id', 'read_receipt_id'):
            if value.get(key):
                references.setdefault(key, value[key])
    return references
