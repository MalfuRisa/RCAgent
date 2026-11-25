"""Recognize question shapes whose evidence requirements can be checked mechanically."""

import re
from dataclasses import dataclass


NAME = r"[A-Za-z0-9_][A-Za-z0-9_.:@/-]*"
PAIR = rf"(?P<first>{NAME})\s*(?:和|与|及|、)\s*(?P<second>{NAME})"
CLIENT = re.compile(rf"(?P<client>{NAME})(?:\s+在[^。！？\n]+?)?\s*作为客户端\s*[,，]?\s*访问")
PATHS = re.compile(rf"{CLIENT.pattern}\s*{PAIR}\s*的?\s*(?:这)?两条路径")
SERVICES = re.compile(rf"{PAIR}\s*两个服务\s*汇总(?:所有|全部)客户端入站请求")
OPEN_GOAL = re.compile(r"异常|根因|原因|故障|为什么|为何|是否正常|影响面|值得关注")
METRIC_TERMS = {
    "server_response_time": ("响应时延", "响应延迟", "server_response_time", "时延", "延迟"),
    "request_rate": ("请求率", "qps", "request_rate", "每秒请求"),
    "percentile95": ("p95", "95分位", "percentile95"),
    "percentile99": ("p99", "99分位", "percentile99"),
    "flow_rate": ("流量", "flow_rate"),
}


@dataclass(frozen=True)
class MinuteComparison:
    paths: tuple
    metric: str
    anchor: tuple | None
    shape: bool

    @property
    def scope(self):
        return "service" if self.paths[0][0] is None else "path"


@dataclass(frozen=True)
class WindowMetrics:
    client: str
    metrics: tuple
    ranking: bool


def requested_metrics(question):
    question = str(question or "").lower()
    return [metric for metric, terms in METRIC_TERMS.items() if any(term in question for term in terms)]


def question_client(question):
    match = CLIENT.search(str(question or ""))
    return match.group("client") if match else None


def minute_comparison(text, metrics):
    matched = PATHS.search(text) or SERVICES.search(text)
    if not matched or len(metrics) != 1:
        return None
    if not (re.search(r"最高|峰值", text) and "分钟" in text and "峰前" in text and "峰后" in text):
        return None
    client = matched.groupdict().get("client")
    first, second = matched.group("first", "second")
    if first == second:
        return None
    paths = ((client, first), (client, second))
    anchor = None
    if re.search(r"升|降|变化|涨|跌|同刻|同一[时分]", text[matched.end():]):
        for names, path in (([first, "第一条"], paths[0]), ([second, "第二条"], paths[1])):
            if path[1] == "frontend":
                names.append("前端")
            pattern = "|".join(map(re.escape, names))
            if re.search(rf"(?:{pattern})(?:路径|服务)?\s*(?:达到最高值|达峰|到峰|达到峰值)时", text):
                anchor = path
                break
        if anchor is None:
            return None
    return MinuteComparison(paths, metrics[0], anchor, bool(re.search(r"形态|尖峰|持续", text)))


def classify(question, main_window):
    text = str(question or "")
    if main_window is None or OPEN_GOAL.search(text):
        return None
    metrics = requested_metrics(text)
    minute = minute_comparison(text, metrics)
    if minute is not None:
        return minute
    match = CLIENT.search(text)
    if not match or not metrics or not re.match(r"(?:各|所有)?服务的", text[match.end():].strip()):
        return None
    if re.search(r"分钟|峰前|峰后|尖峰|持续|趋势|形态|同刻|比较", text[match.end():]):
        return None
    ranking = bool(re.search(r"最高|最大|排名", text))
    if ranking and not ("返回范围" in text and re.search(r"哪条路径|排名", text)):
        return None
    return WindowMetrics(match.group("client"), tuple(metrics), ranking)
