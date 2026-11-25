"""Check requested evidence obligations before switching the Planner to generation."""

from dataclasses import dataclass, field

from .evidence_questions import MinuteComparison, WindowMetrics, classify, question_client, requested_metrics
from .history_evidence import adjacent_points, path_of, point_value, result_series, scope_of, window_peak
from .metric_evidence import metric_results, metric_values
from .published_evidence import checked_history, checked_series, path_text, verify_change


HISTORY_TOOL = "insights_metric_history"
METRICS_TOOL = "insights_query_metrics"


@dataclass
class Readiness:
    kind: str = "unhandled"
    covered: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    gap_sources: dict = field(default_factory=dict)
    source_ids: list = field(default_factory=list)
    peaks: list = field(default_factory=list)
    comparison: dict | None = None
    metrics: dict = field(default_factory=dict)
    results: list = field(default_factory=list)
    ranking: bool = False

    @property
    def ready(self):
        return self.kind != "unhandled" and not self.gaps

    @property
    def tool(self):
        return {"temporal_history": HISTORY_TOOL, "direct_metrics": METRICS_TOOL}.get(self.kind)

    @property
    def reason(self):
        if self.ready:
            return "原题的可核对问项均已取得原值"
        if self.gaps:
            return "仍缺原题所需证据"
        return "由 Planner 根据已结算回执判断是否续查"

    def missing(self, gap, *, queryable=True, source_id=None):
        self.gaps.append(gap)
        if queryable:
            self.pending.append(gap)
        if source_id:
            self.gap_sources[gap] = source_id

    def to_dict(self):
        return {
            "ready": self.ready, "kind": self.kind, "reason": self.reason,
            "covered": self.covered, "gaps": self.gaps, "pending": self.pending,
            "source_ids": self.source_ids, "gap_sources": self.gap_sources, "requested_metrics": list(self.metrics),
        }


def evaluate(outputs, question, main_window):
    obligation = classify(question, main_window)
    if obligation is None:
        return Readiness()
    window = {"start": main_window.start, "end": main_window.end}
    if isinstance(obligation, MinuteComparison):
        return temporal_readiness(outputs, obligation, window)
    if isinstance(obligation, WindowMetrics):
        return direct_readiness(outputs, obligation, window)
    return Readiness()


def direct_readiness(outputs, obligation, window):
    results = list(metric_results(outputs, obligation.client, window))
    readiness = Readiness(
        "direct_metrics", results=results, ranking=obligation.ranking,
        source_ids=list(dict.fromkeys(block.source_id for block in results)),
    )
    statistic = "max" if obligation.ranking else "last"
    for metric in obligation.metrics:
        rows = metric_values(results, metric, statistic)
        units = {row["unit"] for row in rows}
        readiness.metrics[metric] = rows if len(units) == 1 else []
        if len(units) == 1:
            readiness.covered.append(metric)
        elif units:
            readiness.missing(f"{obligation.client} 的 {metric} 返回单位不一致")
        else:
            received = any(metric in block.metrics for block in results)
            state = "回执中无可用原值" if received else "尚无对应取数回执"
            readiness.missing(f"{obligation.client} 的 {metric} 缺少 {statistic}（{state}）", queryable=not received)
    return readiness


def verified_window_findings(outputs, question, window):
    client = question_client(question)
    metrics = requested_metrics(question)
    if not client or not metrics or window is None:
        return None
    return direct_readiness(outputs, WindowMetrics(client, tuple(metrics), False), window)


def _matches_history_group(group, window, scope, metric):
    return (
        group["window"] == window and group.get("scope") == scope
        and group["dataset"] == "app-path" and group["sampling_seconds"] == 60
        and (metric is None or group["metric"] == metric)
    )


def _verified_peaks(outputs, group, paths=None):
    found = {}
    for summary in group["paths"]:
        path = tuple(summary["path"])
        if paths is not None and path not in paths:
            continue
        series = checked_series(outputs, group, summary)
        peak = window_peak(series, 60_000) if series else None
        if peak is None or not verify_change(outputs, peak, peak["timestamp_ms"], 60_000, group):
            continue
        found[path] = {
            "path": path, "summary": summary, "series": series, "peak": peak,
            "unit": group["unit"], "metric": group["metric"], "group": group,
        }
    return found


def verified_minute_findings(outputs, question, main_window):
    if main_window is None:
        return []
    window = {"start": main_window.start, "end": main_window.end}
    history = checked_history(outputs) or {}
    client = question_client(question)
    found = []
    for group in history.get("groups", ()):
        if _matches_history_group(group, window, "path", "server_response_time"):
            found.extend(record for path, record in _verified_peaks(outputs, group).items() if client is None or path[0] == client)
    for record in found:
        record["window_points"], record["expected_points"] = _window_coverage(record, window)
    return found[:6]


def _history_attempt(outputs, path, metric, window):
    for source_id, source in reversed(outputs.sources.items()):
        if source.get("tool_name") != HISTORY_TOOL:
            continue
        args = source.get("arguments") or {}
        if (
            args.get("datasetId") != "app-path" or args.get("time") != window
            or (args.get("client") or None, args.get("server")) != path
            or metric not in (args.get("metrics") or ())
        ):
            continue
        if source.get("status") != "ok":
            error = source.get("error") or {}
            if source.get("mcp_call_started") and not (isinstance(error, dict) and error.get("retryable")):
                return source_id, "对应查询失败"
            continue
        raw = outputs.materials.raw.get(source_id) or {}
        data = (raw.get("structuredContent") or {}).get("data") or {}
        scope = scope_of(source, data)
        if scope and scope[1] == path:
            charts = data.get("charts") or ()
            if not any(chart.get("metricKey") == metric for chart in charts):
                return source_id, "对应分钟查询没有返回该指标"
            series = [series for key, series in result_series(source, data) if key[-3] == metric]
            if series and not any(point_value(p) is not None for item in series for p in item.points.values()):
                return source_id, "对应分钟查询无有效样本"
        meta = data.get("queryMeta") or {}
        if path_of(meta) == path and data.get("time") == window:
            return source_id, "已有回执，尚无可核对的分钟原值"
    return None, "尚无匹配口径的分钟回执"


def _window_coverage(record, window):
    first = (window["start"] + 59) // 60 * 60
    expected = range(first, window["end"] + 1, 60)
    points = record["series"].points
    finite = sum(point_value(points.get(minute * 1000)) is not None for minute in expected)
    return finite, len(expected)


def _peak_coverage(readiness, record, obligation, window):
    peak = record["peak"]
    name = path_text(record["path"])
    finite, expected = _window_coverage(record, window)
    record["window_points"] = finite
    record["expected_points"] = expected
    readiness.covered.append(f"{name} 的已返回分钟最高值")
    for label, field in (("峰前一分钟", "before"), ("峰后一分钟", "after")):
        if point_value(peak[field]) is None:
            readiness.missing(f"{name} 的 {label} 缺少原值（分钟回执缺点）", queryable=False, source_id=peak["source_id"])
        else:
            readiness.covered.append(f"{name} 的 {label}")
    if obligation.shape:
        if expected >= 3 and finite == expected:
            readiness.covered.append(f"{name} 的窗口形态")
        else:
            readiness.missing(f"{name} 的形态依据不足，窗口有值分钟 {finite}/{expected}", queryable=False, source_id=peak["source_id"])


def _comparison(readiness, outputs, paired, anchor, other):
    if anchor not in paired or other not in paired:
        readiness.missing(f"{path_text(other)} 在 {path_text(anchor)} 达峰时的升降缺少同口径两路回执",
                          queryable=not readiness.gaps)
        return
    at = paired[anchor]["peak"]["timestamp_ms"]
    record = paired[other]
    sample = adjacent_points(record["series"], at, 60_000)
    before, current = point_value(sample["before"]), point_value(sample["current"])
    if before is None or current is None or not verify_change(outputs, sample, at, 60_000, record["group"]):
        readiness.missing(f"{path_text(other)} 在 {path_text(anchor)} 达峰分钟或前一分钟缺少可比原值",
                          queryable=False)
        return
    readiness.comparison = {
        "anchor": anchor, "anchor_source": paired[anchor]["peak"]["source_id"],
        "path": other, "sample": sample, "unit": record["unit"],
    }
    readiness.covered.append("指定分钟相对上一分钟的升降")


def temporal_readiness(outputs, obligation, window):
    readiness = Readiness("temporal_history")
    history = checked_history(outputs) or {}
    groups = [
        _verified_peaks(outputs, group, obligation.paths) for group in history.get("groups", ())
        if _matches_history_group(group, window, obligation.scope, obligation.metric)
    ]
    # Choose one common scope for comparisons. A separate scope can still support an individual peak.
    groups.sort(key=lambda group: (
        len(group), sum(record["summary"]["finite_points"] for record in group.values()),
    ), reverse=True)
    paired = groups[0] if groups else {}
    for path in obligation.paths:
        record = next((group[path] for group in groups if path in group), None)
        if record is not None:
            readiness.peaks.append(record)
            readiness.source_ids.append(record["peak"]["source_id"])
            _peak_coverage(readiness, record, obligation, window)
            continue
        source_id, reason = _history_attempt(outputs, path, obligation.metric, window)
        if source_id:
            readiness.source_ids.append(source_id)
        queryable = reason not in {"对应查询失败", "对应分钟查询没有返回该指标", "对应分钟查询无有效样本"}
        readiness.missing(f"{path_text(path)} 缺少 {obligation.metric} 分钟曲线（{reason}）",
                          queryable=queryable, source_id=source_id)
    if obligation.anchor is not None:
        other = next(path for path in obligation.paths if path != obligation.anchor)
        _comparison(readiness, outputs, paired, obligation.anchor, other)
    return readiness
