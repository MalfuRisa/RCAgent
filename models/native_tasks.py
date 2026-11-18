"""Small scheduling contracts; these are not evidence or report schemas."""

import json
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from platforms.insights_v5.diagnostic_policy import NATIVE_READS
from platforms.business_policy import BUSINESS_READS, DATA_READS, is_business
from workspace.native_materials import BUSINESS_TOOLS

from .native_capabilities import RESOURCE_TOOLS, effective_tools, normalize_proposal, task_source


def timestamp(value):
    if isinstance(value, bool):
        raise TypeError("time must be Unix integer seconds or timezone-aware ISO8601")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            return int(parsed.timestamp())
    raise ValueError("time must be Unix integer seconds or timezone-aware ISO8601")


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    database: str = Field(min_length=1)
    table: str = Field(min_length=1)


class Window(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: int | str
    end: int | str

    @field_validator("start", "end", mode="before")
    @classmethod
    def parse_time(cls, value):
        return timestamp(value)

    def seconds(self):
        if self.end <= self.start:
            raise ValueError("window.end must be after window.start")
        return self.end - self.start

    def card(self):
        seconds = self.seconds()
        return {
            "start": self.start,
            "end": self.end,
            "duration_seconds": seconds,
            "duration": f"{seconds // 3600}小时{seconds % 3600 // 60}分{seconds % 60}秒",
            "utc_start": datetime.fromtimestamp(self.start, UTC).isoformat(),
            "utc_end": datetime.fromtimestamp(self.end, UTC).isoformat(),
        }


def original_window(value):
    if not isinstance(value, dict):
        return None
    try:
        window = Window(
            start=value.get("start", value.get("start_time")),
            end=value.get("end", value.get("end_time")),
        )
        window.seconds()
        return window
    except (ValueError, TypeError):
        return None


_BEIJING_INTERVAL = re.compile(
    r'北京时间\s*(\d{4})年(\d{1,2})月(\d{1,2})日\s*'
    r'(\d{1,2}:\d{2}(?::\d{2})?)\s*[–—~～至-]\s*(\d{1,2}:\d{2}(?::\d{2})?)'
)


def explicit_question_window(question):
    """Resolve a single dated Beijing same-day interval already in the user's question."""
    intervals = _BEIJING_INTERVAL.findall(str(question))
    if len(intervals) != 1:
        return None
    year, month, day, start, end = intervals[0]
    zone = timezone(timedelta(hours=8))

    def instant(clock):
        parts = [int(part) for part in clock.split(':')]
        return int(datetime(int(year), int(month), int(day), *parts, tzinfo=zone).timestamp())

    window = Window(start=instant(start), end=instant(end))
    window.seconds()
    return window


def family(name):
    return "query" if name in {"insights_query_list", "insights_query_metric"} else name


class CatalogTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    dataset_id: str = Field(min_length=1, max_length=60)
    metric_keys: list[str] = Field(default_factory=list, max_length=4)


class TaskSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1)
    deliverable: str = Field(min_length=1)
    completion_criteria: str = Field(min_length=1)
    why_needed: str = Field(min_length=1)
    decision_impact: str = Field(min_length=1)
    dataset: Dataset | None = None
    tools: list[str] = Field(default=[], description="Explicit discovered readonly capabilities for this profile and task; no implicit business queries")
    legacy_default: list[str] = Field(default=[], exclude=True)
    contract_version: str = "native_tasks_v2"
    routing_family: str | None = Field(default=None, exclude=True)

    @classmethod
    def model_validate(cls, obj, **kwargs):
        return super().model_validate(normalize_proposal(obj), **kwargs)

    @property
    def business_tools(self):
        return sorted(set(self.tools) & (BUSINESS_TOOLS | DATA_READS))

    def effective_tools(self, discovered):
        return effective_tools(self.tools + self.legacy_default, discovered)

    @property
    def target_tools(self):
        return (set(self.tools) | set(self.legacy_default)) & (BUSINESS_TOOLS | RESOURCE_TOOLS | DATA_READS)

    window: Window | None = None
    window_reason: str = ""
    context_source_ids: list[str] = Field(default=[])
    continue_task_id: str | None = None
    return_mode: Literal['first_result', 'answer'] | None = Field(
        default=None,
        description="first_result: one batch of direct evidence, including several independent queries. answer: dependent steps or interpretation. Omitted: first_result for target tools, answer otherwise.",
    )
    multi_step_reason: str = Field(default="", description="Optional compatibility field. Use why_needed, decision_impact and completion_criteria to explain the task.")
    handoff_mode: Literal['facts', 'worker_note'] | None = Field(
        default=None, description="first_result defaults to facts for recognized structures; worker_note requests prose. answer uses Worker explanation.")
    catalog_targets: list[CatalogTarget] = Field(default_factory=list, max_length=3,
        description="Optional dataset IDs and metric keys needed by this task; reuse already archived matching catalog fields")

    def normalize(self, *, seed, window, tools, source_ids, source=None):
        self.tools = sorted(set(self.tools))
        self.routing_family = source or task_source(self.tools + self.legacy_default, tools)
        business = self.routing_family == 'business'
        allowed = BUSINESS_READS if business else NATIVE_READS
        self.context_source_ids = sorted(set(self.context_source_ids))
        for name in self.tools:
            if name not in allowed:
                raise ValueError(f"/tools: {name} outside RCA readonly profile; allowed={sorted(set(tools) & allowed)}")
            if name not in tools:
                raise ValueError(f"/tools: {name} not discovered; allowed={sorted(tools)}")
        if not business and len({family(t) for t in self.business_tools}) > 1:
            raise ValueError("one business query family per task")
        if (
            not seed
            and any(family(t) == "query" for t in self.business_tools)
            and self.dataset is None
        ):
            raise ValueError("/dataset required for a normal list/metric task")
        if not set(self.context_source_ids) <= set(source_ids):
            raise ValueError("/context_source_ids must exist in this run")
        self.window = self.window or window
        if self.window:
            self.window.seconds()
        if window and self.window != window and not self.window_reason:
            raise ValueError("/window_reason required for a comparison window")
        self.return_mode = self.return_mode or ("first_result" if self.target_tools else "answer")
        return self

    def signature(self):
        return json.dumps({**self.model_dump(), "legacy_default": self.legacy_default}, sort_keys=True, ensure_ascii=False)
