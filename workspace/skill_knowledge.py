"""Project guidance, kept separate from evidence and read coverage."""

import hashlib
import re
from pathlib import Path
from langchain_core.tools import tool
from config import settings
from .repository import new_id

ROOT = Path(__file__).resolve().parents[1] / "skills"
NAMES = (
    "kylin-insights-observability",
    "planning-investigation",
    "submitting-observations",
    "continuing-investigation",
    "finalizing-investigation",
)


def enabled(b):
    return settings.INVESTIGATION_SKILLS_ENABLED and b.role in (
        "planner",
        "worker",
        "supervisor",
    )


def owner(b, row):
    return (row.get("run_id"), row.get("role"), row.get("task_id")) == (
        b.run_id,
        b.role,
        b.task_id,
    )


async def records(b):
    return [
        r for r in await b.repository.all(b.incident_id, "skill_read") if owner(b, r)
    ]


async def hydrate(b):
    if not enabled(b):
        return
    rounds = [
        r for r in await b.repository.all(b.incident_id, "skill_round") if owner(b, r)
    ]
    b.knowledge_rounds = min(len(rounds), settings.SKILL_READ_EXTRA_ROUNDS)
    contexts = [
        r for r in await b.repository.all(b.incident_id, "context") if owner(b, r)
    ]
    b.model_steps = max(
        [b.model_steps] + [r.get("total_role_model_steps", 0) for r in contexts]
    )


def remaining(b):
    from .investigation_phases import active, remaining as phase_remaining

    if active(b):
        return phase_remaining(b, "skill") if enabled(b) else 0
    return (
        max(0, settings.SKILL_READ_EXTRA_ROUNDS - b.knowledge_rounds)
        if enabled(b)
        else 0
    )


def business_steps(b):
    return b.model_steps - b.knowledge_rounds


def resources(name):
    """Only the entry and its direct Markdown links are readable."""
    if name not in NAMES:
        raise ValueError("unknown skill")
    base = (ROOT / name).resolve()
    if not base.is_relative_to(ROOT.resolve()):
        raise ValueError("skill path outside registry")
    entry = base / "SKILL.md"
    if not entry.resolve().is_relative_to(base):
        raise ValueError("entry outside skill directory")
    text = entry.read_text(encoding="utf-8")
    header = re.match(r"\A---\n(.*?)\n---(?:\n|$)", text, re.S)
    if (
        not header
        or not re.search(rf"^name: {re.escape(name)}$", header[1], re.M)
        or not re.search(r"^description: .+$", header[1], re.M)
    ):
        raise ValueError("invalid skill metadata")
    allowed = {"SKILL.md"}
    allowed.update(re.findall(r"\]\((references/[a-z0-9-]+\.md)\)", text))
    return base, text, allowed


def load(name, resource):
    base, entry, allowed = resources(name)
    if resource not in allowed or "\\" in resource or Path(resource).is_absolute():
        raise ValueError("resource not in direct skill allowlist")
    target = (base / resource).resolve()
    if not target.is_relative_to(base) or not target.is_relative_to(ROOT.resolve()):
        raise ValueError("resource outside skill directory")
    content = entry if resource == "SKILL.md" else target.read_text(encoding="utf-8")
    return content, sorted(allowed)


def directory():
    result = []
    for name in NAMES:
        try:
            _, text, _ = resources(name)
            description = re.search(r"^description: (.+)$", text, re.M)
            if not description:
                raise ValueError("missing skill description")
            result.append(
                {"name": name, "description": description[1], "read": "read_skill"}
            )
        except (OSError, ValueError):
            result.append(
                {
                    "name": name,
                    "status": "unavailable",
                    "reason": "skill metadata unavailable",
                }
            )
    return result


@tool
async def read_skill(name: str, resource: str = "SKILL.md") -> dict:
    """Read project operation guidance, not incident evidence. Only registered direct resources are allowed."""
    from .runtime import current

    b = current.get()
    allowed = []
    try:
        if not enabled(b) or (
            not remaining(b) and not getattr(b, "phase_skill_execution", False)
        ):
            raise ValueError(
                "knowledge reading allowance exhausted or disabled; use remaining submission tools"
            )
        _, _, direct = resources(name)
        allowed = sorted(direct)
        content, allowed = load(name, resource)
        result = {
            "status": "accepted",
            "name": name,
            "resource": resource,
            "content": content,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        }
    except (OSError, ValueError):
        result = {
            "status": "rejected",
            "code": "skill_unavailable",
            "reason": "Guidance unavailable or path not allowed; continue using current contracts and tools",
            "name": name,
            "resource": resource,
            "available_resources": allowed,
        }
    await b.repository.save(
        b.incident_id,
        "skill_read",
        {
            "id": new_id("skill_read"),
            "run_id": b.run_id,
            "role": b.role,
            "task_id": b.task_id,
            "attempt_id": b.attempt_id,
            "context_id": b.last_context_id,
            "response_id": b.last_response_id,
            **result,
            "activation_source": "model_read"
            if resource == "SKILL.md"
            else "reference_read",
        },
    )
    return result


async def credit_round(b, calls):
    from .investigation_phases import active

    phase_credit = active(b) and (
        getattr(b, "phase_step_category", None) == "skill" or remaining(b)
    )
    if (
        enabled(b)
        and (remaining(b) or phase_credit)
        and len(calls) == 1
        and calls[0]["name"] == "read_skill"
    ):
        await b.repository.save(
            b.incident_id,
            "skill_round",
            {
                "id": new_id("skill_round"),
                "run_id": b.run_id,
                "role": b.role,
                "task_id": b.task_id,
                "response_id": b.last_response_id,
                "model_steps": b.model_steps,
                "tool_call_id": calls[0]["id"],
            },
        )
        b.knowledge_rounds += 1


async def view(b):
    if not enabled(b):
        return {}
    await hydrate(b)
    loaded = {}
    failures = []
    for row in await records(b):
        if row["status"] == "accepted":
            loaded[(row["name"], row["resource"])] = {
                **{k: row[k] for k in ("name", "resource", "content_hash", "content")},
                "read_id": row["id"],
                "activation_source": row.get("activation_source", "model_read"),
            }
        else:
            failures.append({k: row[k] for k in ("name", "resource", "reason")})
    return {
        "directory": directory(),
        "loaded": list(loaded.values()),
        "unavailable": failures,
        "remaining_read_rounds": remaining(b),
        "note": "Operation guidance only; entries are event activated, references are optional reads; never evidence authority.",
    }


async def activate(b, name, event, *, resource="SKILL.md"):
    """Prepare guidance through the same allowlisted reader, with no model/tool call."""
    if not enabled(b):
        return
    try:
        content, _ = load(name, resource)
        version = hashlib.sha256(content.encode()).hexdigest()
        segment = getattr(b, "skill_segment", "initial")
        if any(
            r.get("activation_source") == "runtime"
            and r.get("name") == name
            and r.get("resource") == resource
            and r.get("content_hash") == version
            and r.get("segment") == segment
            for r in await records(b)
        ):
            return
        result = {
            "status": "accepted",
            "name": name,
            "resource": resource,
            "content": content,
            "content_hash": version,
            "segment": segment,
        }
    except (OSError, ValueError) as error:
        result = {
            "status": "rejected",
            "name": name,
            "resource": resource,
            "reason": str(error)[:400],
            "code": "skill_unavailable",
        }
    await b.repository.save(
        b.incident_id,
        "skill_read",
        {
            "id": new_id("skill_read"),
            "run_id": b.run_id,
            "role": b.role,
            "task_id": b.task_id,
            "attempt_id": b.attempt_id,
            "context_id": None,
            "response_id": None,
            "activation_source": "runtime",
            "trigger": event,
            **result,
        },
    )


async def activate_events(b, *, has_references=False, phase="request"):
    if b.role == "planner":
        await activate(b, "planning-investigation", "before_planning")
    if b.role == "supervisor" and phase != "packet_submission":
        await activate(
            b, "planning-investigation", "review_tasks_and_possible_replanning"
        )
    if has_references and b.role in ("worker", "supervisor"):
        await activate(b, "submitting-observations", "exact_material_available")
    from .investigation_phases import active

    if (
        active(b) and b.phase_budget["phase"] in ("consolidating", "repairing")
    ) or phase == "packet_submission":
        await activate(
            b, "finalizing-investigation", "local_consolidation_or_packet_selection"
        )
    if (
        getattr(b, "recovery_view", False)
        or getattr(b, "skill_segment", "initial") != "initial"
    ):
        await activate(b, "continuing-investigation", "recovery_or_new_segment")


async def record_delivery(b, manifest):
    """A delivered-entry audit is distinct from preparing it or a model reading it."""
    if not enabled(b):
        return
    await b.repository.save(
        b.incident_id,
        "skill_delivery",
        {
            "id": new_id("skill_delivery"),
            "run_id": b.run_id,
            "role": b.role,
            "task_id": b.task_id,
            "context_id": manifest["id"],
            "event": "catalog_exposed",
            "catalog": [r["name"] for r in directory()],
        },
    )
    for entry in manifest.get("injected_skill_resources", []):
        await b.repository.save(
            b.incident_id,
            "skill_delivery",
            {
                "id": new_id("skill_delivery"),
                "run_id": b.run_id,
                "role": b.role,
                "task_id": b.task_id,
                "context_id": manifest["id"],
                "event": "runtime_activated"
                if entry.get("activation_source") == "runtime"
                else entry.get("activation_source", "model_read"),
                **entry,
            },
        )


def deduplicate_replies(messages):
    """The current workbench owns the single body; keep every paired receipt."""
    import json

    result = []
    for message in messages:
        if message.type == "tool" and getattr(message, "name", None) == "read_skill":
            try:
                body = json.loads(message.content)
                if "content" in body:
                    body.pop("content")
                    body["content_location"] = (
                        "workbench.skill_knowledge.loaded (current version); original in skill_read audit"
                    )
                    message = message.model_copy(
                        update={"content": json.dumps(body, ensure_ascii=False)}
                    )
            except (ValueError, TypeError):
                pass
        result.append(message)
    return result


async def unavailable_guidance(b):
    """Execution metadata, never a claim or an evidence reference."""
    if not settings.INVESTIGATION_SKILLS_ENABLED:
        return []
    errors = [
        f"{r['name']}: {r['reason']}"
        for r in directory()
        if r.get("status") == "unavailable"
    ]
    errors.extend(
        f"{r['name']}/{r['resource']}: {r['reason']}"
        for r in await b.repository.all(b.incident_id, "skill_read")
        if r.get("run_id") == b.run_id and r.get("status") == "rejected"
    )
    return list(dict.fromkeys(errors))
