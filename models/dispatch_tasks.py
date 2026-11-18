"""Short Planner dispatch inputs; execution state stays program-owned.

Both dispatch entries share this one field definition.  The model-visible
schema and the program-side validation are generated from the same model so a
tool that the entry advertises is also a tool that entry accepts.
"""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .native_capabilities import (
    ENTRY_SOURCES,
    advertised_tools,
    entry_name,
    tool_source,
)
from .native_tasks import CatalogTarget, Dataset, TaskSpec, Window

INTERNAL_FIELDS = {
    "deliverable",
    "completion_criteria",
    "multi_step_reason",
    "business_tools",
}
EXAMPLE_QUESTION = "取该客户端在原题时间窗内的延迟和流量"


class DispatchRejection(ValueError):
    """One actionable rejection; every field is safe to return to the Planner."""

    def __init__(
        self,
        code,
        message,
        *,
        path=None,
        allowed=None,
        allowed_entry=None,
        fix=None,
        example=None,
        snippet=None,
        parsed=None,
    ):
        super().__init__(message)
        self.code, self.message = code, message
        self.path, self.allowed, self.allowed_entry = path, allowed, allowed_entry
        self.fix, self.example, self.snippet, self.parsed = (
            fix,
            example,
            snippet,
            parsed,
        )

    def receipt(self):
        payload = {
            "code": self.code,
            "message": self.message,
            "mcp_call_started": False,
        }
        for key, value in (
            ("path", self.path),
            ("allowed", self.allowed),
            ("allowed_entry", self.allowed_entry),
            ("fix", self.fix),
            ("example", self.example),
            ("argument_snippet", self.snippet),
            ("arguments_parsed", self.parsed),
        ):
            if value is not None:
                payload[key] = value
        return payload


class DispatchTaskInput(BaseModel):
    """Only the question and the tools are required; everything else is program-filled."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(
        min_length=1,
        description="Natural-language sub-question for one Worker; state what to look up and when to return.",
    )
    tools: list[str] = Field(
        description="One or more discovered readonly tools of this entry's source; no wildcards.",
    )
    dataset: Dataset | None = Field(
        default=None,
        description="Optional native database/table when the run has no confirmed dataset; reuse an observed dataset.",
    )
    catalog_targets: list[CatalogTarget] = Field(
        default_factory=list,
        max_length=3,
        description="Optional business dataset IDs and metric keys already observed in this run.",
    )
    context_source_ids: list[str] = Field(
        default_factory=list,
        description="Optional existing source_ids this task's question depends on.",
    )
    continue_task_id: str | None = Field(
        default=None,
        description="Optional latest returned task id in the same conversation to continue.",
    )
    window: Window | None = Field(
        default=None,
        description="Optional absolute window; omit to inherit the confirmed main window.",
    )
    window_reason: str = Field(
        default="",
        description="Required only when asking for a comparison window outside the main window.",
    )
    return_mode: Literal["first_result", "answer"] | None = Field(
        default=None,
        description="first_result returns one settled target batch; answer is for dependent steps or interpretation.",
    )
    handoff_mode: Literal["facts", "worker_note"] | None = Field(
        default=None,
        description="Known structures default to program facts; worker_note asks for prose.",
    )
    why_needed: str = Field(
        default="",
        description="Optional for follow-up tasks: which returned gap this new evidence changes.",
    )
    decision_impact: str = Field(
        default="",
        description="Optional: which conclusion or follow-up decision this task's result can change.",
    )
    deliverable: str = Field(default="", exclude=True)
    completion_criteria: str = Field(default="", exclude=True)
    multi_step_reason: str = Field(default="", exclude=True)

    # Excluded compatibility field kept so archived proposals still parse.
    business_tools: list[str] | None = Field(default=None, exclude=True)


def _path(loc):
    rendered = ""
    for part in loc:
        rendered += f"[{part}]" if isinstance(part, int) else (
            "." + str(part) if rendered else str(part)
        )
    return rendered or None


def _message(error):
    return str(error.get("msg", "invalid value"))


def _snippet(raw, limit=400):
    try:
        return json.dumps(raw, ensure_ascii=False, default=str)[:limit]
    except (TypeError, ValueError):
        return str(raw)[:limit]


def _example(source, discovered):
    tools = advertised_tools(source, discovered)
    return {
        "question": EXAMPLE_QUESTION,
        "tools": tools[:1],
        "note": "Only question and tools are required; the program inherits the confirmed window and source.",
    }


def _input_error(exc, raw, source, discovered):
    first = exc.errors()[0] if exc.errors() else {}
    path = _path(first.get("loc", ()))
    if first.get("type") == "extra_forbidden":
        return DispatchRejection(
            "unknown_field",
            f"{path} is not a dispatch field; use only the declared keys",
            path=path,
            fix="Remove the undeclared key; optional fields are listed in the schema.",
            snippet=_snippet(raw),
        )
    return DispatchRejection(
        "schema_error",
        f"{path or 'arguments'}: {_message(first)}",
        path=path,
        fix="Correct the value at this path and keep the other fields.",
        example=_example(source, discovered),
        snippet=_snippet(raw),
    )


def _check_tools(names, source, discovered):
    allowed = advertised_tools(source, discovered)
    for index, name in enumerate(names):
        owner = tool_source(name)
        path = f"tools[{index}]"
        if owner != source:
            other = entry_name(owner) if owner else None
            detail = (
                f"{name} belongs to {other}; this call is {entry_name(source)}"
                if other
                else f"{name} was not discovered as a readonly tool in this run"
            )
            raise DispatchRejection(
                "source_mismatch" if other else "tool_not_discovered",
                detail,
                path=path,
                allowed=allowed,
                allowed_entry=other,
                fix=(
                    f"Submit one task per source: use {other} for {name}, and a separate "
                    f"{entry_name(source)} task for this source's tools."
                    if other
                    else "Use only tools listed for this entry in the current run."
                ),
                snippet=name,
            )


def normalize_input(raw, *, entry, discovered):
    """Validate one dispatch call's arguments and return the short input."""
    source = ENTRY_SOURCES.get(entry)
    if source is None:
        raise DispatchRejection(
            "unknown_entry",
            f"{entry} is not a dispatch entry",
            allowed_entry=sorted(ENTRY_SOURCES),
        )
    if not isinstance(raw, dict):
        raise DispatchRejection(
            "arguments_unparsed",
            "Dispatch arguments could not be parsed as a JSON object; nothing was dispatched",
            parsed=False,
            fix='Send one object such as {"question": "...", "tools": ["..."]}.',
            example=_example(source, discovered),
            snippet=_snippet(raw),
        )
    if "business_tools" in raw:
        raw = {k: v for k, v in raw.items() if k != "business_tools"} | {
            "tools": raw.get("business_tools")
        }
    try:
        parsed = DispatchTaskInput.model_validate(raw)
    except ValidationError as exc:
        raise _input_error(exc, raw, source, discovered) from exc
    tools = list(dict.fromkeys(parsed.tools))
    _check_tools(tools, source, discovered)
    if parsed.dataset is not None and source == "business":
        raise DispatchRejection(
            "dataset_not_applicable",
            "dataset is not used by business tools; select catalog_targets instead",
            path="dataset",
            fix="Remove dataset; business metric keys go through catalog_targets.",
        )
    return parsed.model_copy(update={"tools": tools})


def legacy_spec(raw, *, index, discovered):
    """Archived full-form batch item; failures still point at the exact field."""
    prefix = f"tasks[{index}]"
    if not isinstance(raw, dict):
        raise DispatchRejection(
            "schema_error",
            "Each archived task must be a JSON object",
            path=prefix,
            fix="Keep the archived task object shape or resubmit one short dispatch call.",
            snippet=_snippet(raw),
        )
    names = raw.get("tools") or raw.get("business_tools") or []
    owner = None
    if isinstance(names, list):
        for position, name in enumerate(names):
            source = tool_source(name) if isinstance(name, str) else None
            if source is None:
                continue
            if owner and source != owner:
                entry = entry_name(source)
                raise DispatchRejection(
                    "source_mismatch",
                    f"{name} belongs to {entry}; this task already uses {entry_name(owner)}",
                    path=f"{prefix}.tools[{position}]",
                    allowed=advertised_tools(owner, discovered),
                    allowed_entry=entry,
                    fix=(
                        f"Split into one {entry_name(owner)} task and one {entry} task; "
                        "each task keeps exactly one source."
                    ),
                    snippet=name,
                )
            owner = owner or source
    try:
        return TaskSpec.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = _path(first.get("loc", ()))
        raise DispatchRejection(
            "schema_error",
            f"{field or 'task'}: {_message(first)}",
            path=f"{prefix}.{field}" if field else prefix,
            fix="Correct this field; independent valid tasks in the same call still run.",
            example=_example(owner, discovered) if owner else None,
            snippet=_snippet(raw),
        ) from exc


def _mechanical(seed, source, tools, return_mode):
    target = set(tools) & target_tools()
    mode = return_mode or ("first_result" if target else "answer")
    return {
        "deliverable": "交回本任务的实际结果、对象与绝对窗口、来源引用和缺口",
        "completion_criteria": (
            "首批目标调用结算后交回；失败也交回具体错误"
            if mode == "first_result"
            else "完成本问题的判断或说明无法完成的具体缺口"
        ),
        "why_needed": "原题首个最小取证任务" if seed else "承接已返回阶段的关键缺口",
        "decision_impact": "影响原题结论、补查方向或可交付范围",
    }


def build_spec(raw, *, entry, seed, discovered):
    """Turn one short call into the internal, still un-normalized task contract."""
    item = normalize_input(raw, entry=entry, discovered=discovered)
    defaults = _mechanical(seed, ENTRY_SOURCES[entry], item.tools, item.return_mode)
    values = {
        "question": item.question,
        "deliverable": item.deliverable or defaults["deliverable"],
        "completion_criteria": item.completion_criteria or defaults["completion_criteria"],
        "why_needed": item.why_needed or defaults["why_needed"],
        "decision_impact": item.decision_impact or defaults["decision_impact"],
        "tools": item.tools,
    }
    for name in (
        "dataset",
        "catalog_targets",
        "context_source_ids",
        "continue_task_id",
        "window",
        "window_reason",
        "return_mode",
        "handoff_mode",
        "multi_step_reason",
    ):
        if name in item.model_fields_set:
            values[name] = getattr(item, name)
    return TaskSpec(**values)


def normalize_spec(spec, *, seed, main_window, discovered, source_ids, entry):
    """Normalize one task against the run, reporting the failing field."""
    try:
        return spec.normalize(
            seed=seed,
            window=main_window,
            tools=discovered,
            source_ids=source_ids,
            source=ENTRY_SOURCES.get(entry),
        )
    except (ValueError, TypeError) as exc:
        raise DispatchRejection(
            "field_error",
            str(exc),
            path=_field_path(str(exc)),
            fix="Correct the named field; the confirmed window, source and ids stay unchanged.",
            example=_example(ENTRY_SOURCES[entry], discovered),
        ) from exc


def unparsed_call_rejection(raw, *, entry, discovered):
    """The SDK could not turn this call's arguments into a JSON object."""
    source = ENTRY_SOURCES.get(entry) or "native"
    return DispatchRejection(
        "arguments_unparsed",
        "Dispatch arguments could not be parsed as a JSON object; nothing was dispatched",
        parsed=False,
        fix='Send one object such as {"question": "...", "tools": ["..."]}.',
        example=_example(source, discovered),
        snippet=_snippet(raw),
    )


def classify_rejection(exc, *, discovered, entry):
    """Map one lifecycle/normalization failure to a stable code and field path."""
    message = str(exc)
    code = "field_error"
    if message.startswith("First dispatch"):
        code = "seed_limit"
    elif message.startswith("Continue requires"):
        code = "continue_invalid"
    elif message.startswith("Exact repeated"):
        code = "duplicate_task"
    elif message.startswith("First task must use confirmed main window"):
        code = "window_conflict"
    elif message.startswith("Changed dataset/window/family"):
        code = "continuation_scope"
    elif message.startswith("Comparison window must be separate"):
        code = "window_conflict"
    source = ENTRY_SOURCES.get(entry)
    return DispatchRejection(
        code,
        message,
        path=_field_path(message),
        fix="Correct this item; independent valid items in the same response still run.",
        example=_example(source, discovered) if source else None,
    )


def _field_path(message):
    text = str(message)
    for prefix in ("/tools:", "/dataset", "/window_reason", "/context_source_ids"):
        if prefix == "/tools:" and text.startswith("/tools:"):
            return "tools"
        if prefix != "/tools:" and text.startswith(prefix):
            return prefix.lstrip("/")
    return None


def planner_schema(source, discovered):
    """One model-visible object per entry, sharing this module's field definitions."""
    schema = DispatchTaskInput.model_json_schema()
    properties = schema.get("properties", {})
    for name in INTERNAL_FIELDS:
        properties.pop(name, None)
    properties["tools"] = {
        "type": "array",
        "items": {"type": "string", "enum": advertised_tools(source, discovered)},
        "description": "Discovered readonly tools of this entry's source only.",
    }
    schema["required"] = ["question", "tools"]
    schema["additionalProperties"] = False
    return _inline(schema, schema)


def _inline(root, value):
    """Inline local $refs; the model endpoint need not resolve JSON Schema $defs."""
    if isinstance(value, list):
        return [_inline(root, item) for item in value]
    if isinstance(value, dict):
        if "$ref" in value:
            name = value["$ref"].split("/")[-1]
            return _inline(root, root.get("$defs", {}).get(name, {}))
        return {
            key: _inline(root, item)
            for key, item in value.items()
            if key != "$defs"
        }
    return value


def entry_description(source, entry):
    other = sorted(set(ENTRY_SOURCES.values()) - {entry})
    peer = f" Independent tasks of the other source need {entry_name(other[0])}." if other else ""
    return (
        f"Assign one small {source} question per call. Required: question and tools. "
        f"The program inherits the confirmed target scope, absolute window and source, fills the "
        f"mechanical deliverable and stop condition, and returns results after the first settled batch. "
        f"Omit window unless you need a comparison/subwindow; explain window_reason then. "
        f"Use return_mode=answer only for dependent steps or interpretation. "
        f"List only tools of this source; a wrong-source tool gets its own entry name in the error.{peer}"
    )


_TARGET_TOOLS = None


def target_tools():
    """Target tools are read-only capabilities, distinct from metadata helpers."""
    global _TARGET_TOOLS
    if _TARGET_TOOLS is None:
        from platforms.business_policy import DATA_READS
        from workspace.native_materials import BUSINESS_TOOLS

        from .native_capabilities import RESOURCE_TOOLS

        _TARGET_TOOLS = BUSINESS_TOOLS | RESOURCE_TOOLS | DATA_READS
    return _TARGET_TOOLS


def rejection_item(index, exc, *, tool=None):
    item = {"index": index, "status": "rejected"}
    if tool:
        item["tool"] = tool
    item["error"] = exc.receipt()
    return item


def rejection_signature(items):
    """Same call semantics and field path means the Planner learned nothing new."""
    return sorted(
        (
            item.get("error", {}).get("code"),
            item.get("error", {}).get("path"),
            item.get("tool"),
            item.get("error", {}).get("allowed_entry"),
        )
        for item in items
    )


def truncation_item(call, index):
    """Only a real finish_reason justifies telling the Planner output was truncated."""
    return {
        "index": index,
        "status": "rejected",
        "tool": call.get("name"),
        "error": {
            "code": "output_truncated",
            "message": "Model output ended with finish_reason=length; the dispatch arguments may be incomplete and nothing was dispatched",
            "arguments_parsed": not call.get("invalid"),
            "mcp_call_started": False,
            "fix": "Resubmit one smaller dispatch call: question and tools only.",
        },
    }


def correction_prompt(items, remaining):
    lines = []
    for item in items[:4]:
        error = item.get("error", {})
        location = error.get("path") or error.get("argument_snippet") or "arguments"
        lines.append(f"- [{location}] {error.get('message')}")
    lead = (
        "派工未执行。先只纠正原问题一次；若仍被拒，再按同一错误定位做第二次修正（本次剩余纠正次数："
        + str(remaining)
        + "）：\n"
    )
    return (
        lead
        + "\n".join(lines)
        + "\n选错来源时改用错误中给出的允许入口；字段错误只改该路径；不要重复相同调用。"
        "保持原目标、禁查项、窗口和交付物，不换题。若无法修正，直接说明阻断。"
    )