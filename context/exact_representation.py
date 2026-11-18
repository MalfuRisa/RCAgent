"""Lossless, same-request sharing of repeated source/identity audit objects.

This is representation only: it neither covers material nor retires receipts.
Every reference resolves to a complete object in the very same visible payload.
"""

import copy
import json
from collections import Counter
from config import settings
from workspace.repository import WorkspaceError

REFERENCE = "shared_audit_object_ref"
TABLE = "shared_audit_objects"
FIELDS = {
    "scope",
    "resource_revision_refs",
    "effective_filters",
    "source_ref",
    "requested_window",
    "returned_window",
    "resource_refs",
}


def enabled(b, phase):
    from .lifecycle import effective_mode

    return (
        effective_mode(b) == "active"
        and b.role in ("worker", "supervisor")
        and phase not in ("packet_submission", "stage_repair")
    )


def expand(payload):
    """Resolve only complete named references; broken references never prove visibility."""
    if payload.get("shared_audit_format") != "same-request-v1":
        return copy.deepcopy(payload)
    objects = payload.get(TABLE) or {}

    def restore(value):
        if isinstance(value, dict):
            if "shared_source_identity_ref" in value:
                key = value["shared_source_identity_ref"]
                if key not in objects:
                    raise WorkspaceError("shared_representation_invalid: missing source identity")
                return {
                    **copy.deepcopy(objects[key]),
                    **{
                        k: restore(v) for k, v in value.items() if k != "shared_source_identity_ref"
                    },
                }
            if set(value) == {REFERENCE}:
                key = value[REFERENCE]
                if key not in objects:
                    raise WorkspaceError("shared_representation_invalid: missing audit object")
                return copy.deepcopy(objects[key])
            return {k: restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [restore(v) for v in value]
        return value

    return {
        k: restore(v)
        for k, v in payload.items()
        if k not in (TABLE, "shared_audit_note", "shared_audit_format")
    }


def represent(payload):
    def collision(value):
        if isinstance(value, dict):
            return any(
                k in value
                for k in (REFERENCE, "shared_source_identity_ref", TABLE, "shared_audit_format")
            ) or any(collision(v) for v in value.values())
        return isinstance(value, list) and any(collision(v) for v in value)

    if collision(payload):
        return copy.deepcopy(
            payload
        ), []  # original JSON value with reserved spelling stays literal

    def signature(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    counts, source_counts = Counter(), Counter()
    identity_keys = ("evidence_id", "artifact_id", "source_revision", "source_hash")

    def source_identity(value):
        return {k: value[k] for k in identity_keys if k in value}

    def collect(value, field=""):
        if isinstance(value, (dict, list)):
            if field == "source_ref" and isinstance(value, dict):
                identity = source_identity(value)
                if identity.get("source_revision") and (
                    identity.get("evidence_id") or identity.get("artifact_id")
                ):
                    source_counts[signature(identity)] += 1
            if field in FIELDS and len(signature(value)) > 100:
                counts[signature(value)] += 1
            if isinstance(value, dict):
                for key, row in value.items():
                    collect(row, key)
            else:
                for row in value:
                    collect(row, field)

    collect(payload)
    table, names, decisions = {}, {}, []

    def share(value, path="", field=""):
        if isinstance(value, (dict, list)):
            if field == "source_ref" and isinstance(value, dict):
                identity = source_identity(value)
                sig = signature(identity)
                if source_counts[sig] > 1:
                    key = ("source_identity", sig)
                    if key not in names:
                        names[key] = "source_identity_" + str(len(names) + 1)
                        table[names[key]] = copy.deepcopy(identity)
                    name = names[key]
                    decisions.append(
                        {
                            "replaced_path": path,
                            "retained_path": "/" + TABLE + "/" + name,
                            "reason": "same exact source identity/revision/hash; locator/range remain per fact",
                            "source_ref": copy.deepcopy(value),
                        }
                    )
                    return {
                        "shared_source_identity_ref": name,
                        **{k: copy.deepcopy(v) for k, v in value.items() if k not in identity_keys},
                    }
            key = signature(value)
            if field in FIELDS and counts[key] > 1 and len(key) > 100:
                if key not in names:
                    name = field + "_" + str(len(names) + 1)
                    names[key] = name
                    table[name] = copy.deepcopy(value)
                name = names[key]
                decisions.append(
                    {
                        "replaced_path": path,
                        "retained_path": "/" + TABLE + "/" + name,
                        "reason": "identical complete audit object in this same request",
                        "complete_object": copy.deepcopy(value),
                    }
                )
                return {REFERENCE: name}
            if isinstance(value, dict):
                return {k: share(v, path + "/" + k, k) for k, v in value.items()}
            return [share(v, path + "/" + str(i), field) for i, v in enumerate(value)]
        return value

    output = share(payload)
    if table:
        output[TABLE] = table
        output["shared_audit_format"] = "same-request-v1"
        output["shared_audit_note"] = (
            "Replace each shared_audit_object_ref with the complete named object in "
            "shared_audit_objects in this message. This changes representation only, not source/coverage/review state."
        )
    if expand(output) != payload:
        raise WorkspaceError("shared_representation_invalid: lossless restoration failed")
    return output, decisions


def serialize(b, payload, phase):
    if not enabled(b, phase):
        return json.dumps(payload, ensure_ascii=False), []
    view, decisions = represent(payload)
    text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    # Validate AFTER actual serialization. A deleted retained table cancels the
    # decision by failing restoration, rather than silently losing unique facts.
    if expand(json.loads(text)) != payload:
        raise WorkspaceError("shared_representation_invalid: serialized proof failed")
    return text, decisions
