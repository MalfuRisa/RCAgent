"""Read window metric rows and coverage from their saved business MCP receipts."""

from dataclasses import dataclass
from math import isfinite

from .history_evidence import endpoint_filters


@dataclass
class MetricResult:
    source_id: str
    index: int
    rows: list
    metrics: dict
    coverage: dict

    def values(self, metric, statistic):
        unit = self.metrics.get(metric)
        if not unit:
            return
        for row_index, row in enumerate(self.rows):
            label = row.get("label")
            stats = (row.get("values") or {}).get(metric) or {}
            value = stats.get(statistic)
            if label and type(value) in (int, float) and isfinite(value):
                pointer = f"/structuredContent/data/results/{self.index}/rows/{row_index}/values/{metric}"
                yield {
                    "label": label, "unit": unit, "value": value, "stats": stats,
                    "source_id": self.source_id, "pointer": pointer,
                }


def metric_results(outputs, client, window):
    expected_filters = {"auto_service_0": client}
    for source_id, source in outputs.sources.items():
        if source.get("tool_name") != "insights_query_metrics" or source.get("status") != "ok":
            continue
        args = source.get("arguments") or {}
        if args.get("time") != window:
            continue
        raw = outputs.materials.raw.get(source_id) or {}
        data = (raw.get("structuredContent") or {}).get("data") or {}
        for index, block in enumerate(data.get("results") or []):
            meta = block.get("queryMeta") or {}
            request = meta.get("requested") or {}
            if (
                block.get("datasetId") != "app-path"
                or block.get("window") != window
                or request.get("time") != window
                or endpoint_filters(request.get("filters")) != expected_filters
            ):
                continue
            constructed = (meta.get("constructed") or {}).get("appliedFilters")
            if constructed is not None and endpoint_filters(constructed) != expected_filters:
                continue
            units = {item.get("key"): item.get("unit") for item in block.get("metrics") or []}
            yield MetricResult(source_id, index, block.get("rows") or [], units, meta.get("coverage") or {})


def metric_values(results, metric, statistic):
    return [value for result in results for value in result.values(metric, statistic)]
