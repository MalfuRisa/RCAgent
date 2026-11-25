"""Turn verified metric evidence into short writing material and a partial answer."""

import re
from dataclasses import dataclass, field

from .evidence_readiness import verified_minute_findings, verified_window_findings
from .history_evidence import point_value
from .published_evidence import clock, path_text


METRIC_NAMES = {
    "server_response_time": "普通响应时延",
    "request_rate": "请求率",
}
MINUTE_GOAL = re.compile(r"分钟|尖峰|持续|同刻|短时")


def number_text(value):
    if value is None:
        return "缺值"
    return format(value, ",")


def anchors(*values):
    return [number_text(value) if type(value) in (float, int) else str(value) for value in values if value is not None]


def compact_text(value):
    return "".join(str(value).split()).replace(",", "").replace("，", "")


@dataclass
class Fact:
    text: str
    anchors: list = field(default_factory=list)
    source_ids: list = field(default_factory=list)
    pointer: str | None = None


@dataclass
class CloseoutMaterial:
    question: str
    window: dict | None
    kind: str
    facts: list = field(default_factory=list)
    coverage: list = field(default_factory=list)
    gaps: list = field(default_factory=list)

    @property
    def source_ids(self):
        return list(dict.fromkeys(sid for item in self.facts + self.coverage + self.gaps for sid in item.source_ids))

    @property
    def numeric_source_ids(self):
        return {sid for fact in self.facts if fact.pointer for sid in fact.source_ids}

    def render(self):
        lines = ["原题：" + self.question]
        if self.window:
            start, end = self.window["start"], self.window["end"]
            lines.append(f"北京时间 {clock(start, '%Y-%m-%d %H:%M')}–{clock(end, '%H:%M')}（{start}–{end}）")
        for title, items in (("已核实事实", self.facts), ("返回范围", self.coverage), ("仍缺的证据", self.gaps)):
            if items:
                lines.append(title + "：")
                lines.extend(f"- {item.text}" + (f" 原文 {item.pointer}" if item.pointer else "") for item in items)
        lines.append("用一段话回答原题，保持对象、统计量和单位对应。关键数字引用来源，无法判断的问项说明缺项。")
        return "\n".join(lines)

    def compose(self, model_text):
        text = " ".join(str(model_text or "").split())
        parts = [text] if text else []
        original = compact_text(text)
        for fact in self.facts + self.coverage + self.gaps:
            if fact.anchors and all(compact_text(anchor) in original for anchor in fact.anchors):
                continue
            parts.append(fact.text)
        return " ".join(parts)

    def partial(self, reason):
        text = self.compose("")
        return f"{text} {reason}".strip() if text else ""

    def to_dict(self):
        return {
            "question": self.question, "window": self.window, "kind": self.kind,
            "facts": [fact.__dict__ for fact in self.facts],
            "coverage": [fact.__dict__ for fact in self.coverage],
            "gaps": [fact.__dict__ for fact in self.gaps],
            "source_ids": self.source_ids, "chars": len(self.render()),
        }


def build(outputs, question, main_window, readiness):
    window = {"start": main_window.start, "end": main_window.end} if main_window else None
    material = CloseoutMaterial(question, window, readiness.kind)
    for gap in readiness.gaps:
        source_id = readiness.gap_sources.get(gap)
        material.gaps.append(Fact(f"{gap}（{source_id}）。", source_ids=[source_id]) if source_id else Fact(gap + "。"))
    if readiness.kind == "direct_metrics":
        material.facts = metric_facts(readiness)
        material.coverage = metric_coverage(readiness)
    elif readiness.kind == "temporal_history":
        material.facts = history_facts(readiness.peaks, readiness.comparison)
    else:
        records = verified_minute_findings(outputs, question, main_window)
        if records:
            material.facts = history_facts(records)
        else:
            metrics = verified_window_findings(outputs, question, window)
            if metrics is not None:
                material.facts = metric_facts(metrics)
                material.coverage = metric_coverage(metrics)
            if MINUTE_GOAL.search(question) and not any(
                source.get("tool_name") == "insights_metric_history" and source.get("status") == "ok"
                for source in outputs.sources.values()
            ):
                window_receipts = [sid for sid, source in outputs.sources.items()
                                   if source.get("tool_name") == "insights_query_metrics" and source.get("status") == "ok"]
                if window_receipts:
                    if not material.facts:
                        sid = window_receipts[0]
                        material.facts.append(Fact(f"已有窗口汇总回执（{sid}），原值和适用范围见回执。", source_ids=[sid]))
                    material.gaps.append(Fact("分钟时序未取得，窗口汇总无法判断尖峰、持续形态和同刻变化。"))
        if not material.facts:
            return None
        material.gaps.append(Fact("以上限已查路径和窗口。其他受影响路径、候选原因和反证仍待查。"))
    return material


def _metric_name(metric):
    label = METRIC_NAMES.get(metric)
    return f"{label}（{metric}）" if label else metric


def metric_facts(readiness):
    facts = []
    for metric, rows in readiness.metrics.items():
        if not rows:
            continue
        top = max(rows, key=lambda row: row["value"])
        name, unit, sid = _metric_name(metric), top["unit"], top["source_id"]
        if readiness.ranking:
            text = f"返回范围内，{name}的窗口最大值最高为 {top['label']} 的 {number_text(top['value'])} {unit}"
            required = anchors(top["label"], "max", top["value"], unit, sid)
            text += f"（max，{sid}）。"
        else:
            values = [top["stats"].get(key) for key in ("last", "avg", "max")]
            rendered = "/".join(number_text(value) for value in values)
            text = f"{name}已返回路径中，最近有效值最高的是 {top['label']}，last/avg/max 为 {rendered} {unit}（{sid}）。"
            required = anchors(top["label"], *values, unit, sid)
        facts.append(Fact(text, required, [sid], top["pointer"]))
    return facts


def metric_coverage(readiness):
    coverage = []
    for block in readiness.results:
        info = block.coverage
        total, limit = info.get("backendTotal"), info.get("requestedLimit")
        scope = f"这一批返回 {len(block.rows)} 行"
        if type(limit) is int:
            scope += f"（请求上限 {limit}）"
        scope += f"，后端总数 {total}" if type(total) is int else "，后端总数未知"
        if info.get("truncated") is True or info.get("hasMore") is True:
            scope += "，回执显示还有未返回行"
        elif info.get("complete") is True:
            scope += "，回执标为覆盖完整"
        scope += f"（{block.source_id}）。"
        coverage.append(Fact(scope, source_ids=[block.source_id]))
    if len(readiness.results) > 1:
        labels = {row["label"] for block in readiness.results for row in block.rows if row.get("label")}
        coverage.append(Fact(f"多批合计 {len(labels)} 个不同的返回路径标签，保留原有观测点标记。"))
    return coverage


def _shape(record, other_max):
    stats = record["summary"]
    count, low, high = stats["finite_points"], stats["min"], stats["max"]
    before, after = (point_value(record["peak"][key]) for key in ("before", "after"))
    span = f"{count} 个有效分钟在 {number_text(low)}–{number_text(high)} {record['unit']}"
    if high > 0 and before is not None and after is not None and max(before, after) < high / 2:
        return "峰前后均明显低于最高值，呈短时尖峰，" + span
    complete = record.get("window_points") == record.get("expected_points") if "window_points" in record else count >= 3
    if complete and other_max is not None and low > other_max:
        return "窗内相对另一对象持续高位，" + span
    if complete and high > 0 and low >= high * 0.75:
        return "窗内维持在相近量级，" + span
    return span


def _peak_fact(record, other_max):
    peak, unit, path = record["peak"], record["unit"], path_text(record["path"])
    time = clock(peak["timestamp_ms"] / 1000, "%H:%M")
    points = [point_value(peak[key]) for key in ("before", "current", "after")]
    triplet = "、".join(number_text(value) for value in points)
    shape = _shape(record, other_max)
    source = peak["source_id"]
    return Fact(
        f"{path} 的{_metric_name(record['metric'])}在 {time} 达到已返回分钟的最高值。峰前一分钟、峰值、峰后一分钟分别为 {triplet} {unit}。{shape}（{source}）。",
        anchors(path, time, *points, unit, shape, source), [source], peak["current"]["pointer"],
    )


def _comparison_fact(comparison):
    sample, unit = comparison["sample"], comparison["unit"]
    time = clock(sample["timestamp_ms"] / 1000, "%H:%M")
    before, current = (point_value(sample[key]) for key in ("before", "current"))
    direction = "下降" if current < before else "上升" if current > before else "持平"
    ids = [comparison["anchor_source"], sample["source_id"]]
    path, anchor = path_text(comparison["path"]), path_text(comparison["anchor"])
    return Fact(
        f"{anchor} 在 {time} 达峰时，{path} 从前一分钟的 {number_text(before)} {unit} 变为 {number_text(current)} {unit}，{direction}（{'、'.join(ids)}）。",
        anchors(time, path, before, current, unit, direction, *ids), ids, sample["current"]["pointer"],
    )


def history_facts(records, comparison=None):
    facts = []
    for record in records:
        peers = [other["summary"]["max"] for other in records if other["path"] != record["path"] and other["group"] == record["group"]]
        facts.append(_peak_fact(record, max(peers) if peers else None))
    if comparison:
        facts.append(_comparison_fact(comparison))
    return facts
