"""证据分级与结论约束（补充 PRD §4 P0-1）。

这个模块是**唯一权威**：证据分级枚举、禁止断言清单、最终报告审计，都在这里。
Supervisor 闸门与测试共用同一份，避免规则漂移。

设计要点（补充 PRD §4 P0-1）：

* **没数据 ≠ 测得零**。`no_data` 表示"没有样本"，`observed_zero` 才是"确实采到值 0"。
  两者被混同，就会把观测盲区写成"零流量 → 网络中断"。
* **`unknown` ≠ NotReady**。资源清单只能给 `ready_status=unknown`，
  不能据此写"没有 Ready 副本"。
* 闸门是**确定性**的：它不依赖模型是否听话，而是在报告产出后按证据复核，
  把没有证据支撑的肯定结论降级为待验证嫌疑点，并记录被降级的内容。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 1. 证据分级
# ---------------------------------------------------------------------------

#: 观测结果的分类。**Agent 只能消费这些标签，不能自己用自然语言重新解释。**
EVIDENCE_OBSERVED_ZERO = "observed_zero"   # 确实采到数值 0
EVIDENCE_OBSERVED_VALUE = "observed_value"  # 确实采到非零数值
EVIDENCE_NO_DATA = "no_data"               # 没有样本
EVIDENCE_NOT_FOUND = "not_found"           # 目标未找到
EVIDENCE_ERROR = "error"                   # 上游返回错误（含 provider_error）
EVIDENCE_TIMEOUT = "timeout"               # 超时
EVIDENCE_UNKNOWN = "unknown"               # 上游不提供该事实

#: 这些分类**不能**支撑任何肯定结论（PRD §4 P0-1 第 2 条）。
INCONCLUSIVE_KINDS = frozenset({
    EVIDENCE_NO_DATA, EVIDENCE_NOT_FOUND, EVIDENCE_ERROR,
    EVIDENCE_TIMEOUT, EVIDENCE_UNKNOWN,
})


def classify_result(result: Any) -> str:
    """把任意 Gateway Tool 的返回分类成上面的枚举。

    必须能同时认出**多种形态**（修复前 `payment-db-0` 就同时暴露了三种）：
    ``no_data`` 缺口、``provider_error`` 缺口、以及"解析不到任何对象"的空结果。
    """
    if result is None:
        return EVIDENCE_UNKNOWN
    if isinstance(result, str):
        text = result.strip().lower()
        if not text:
            return EVIDENCE_UNKNOWN
        if "timeout" in text or "deadline" in text:
            return EVIDENCE_TIMEOUT
        return EVIDENCE_ERROR

    if not isinstance(result, dict):
        return EVIDENCE_UNKNOWN

    # 协议层错误
    if result.get("is_error") or result.get("isError"):
        return EVIDENCE_ERROR

    sc = result.get("structuredContent")
    if sc is None:
        sc = result.get("structured") if isinstance(result.get("structured"), dict) else result
    if not isinstance(sc, dict):
        return EVIDENCE_UNKNOWN

    # 显式 status（五个新 Tool）
    status = str(sc.get("status") or "").lower()
    if status == "error":
        return EVIDENCE_ERROR
    if status == "not_found":
        return EVIDENCE_NOT_FOUND
    # partial 的语义由缺口决定，继续往下看

    # 收集所有缺口的 reason
    reasons: List[str] = []
    for holder in (sc, sc.get("meta") or {}, (sc.get("result") or {}) if isinstance(sc.get("result"), dict) else {}):
        if not isinstance(holder, dict):
            continue
        for gap in holder.get("data_gaps") or []:
            if isinstance(gap, dict) and gap.get("reason"):
                reasons.append(str(gap["reason"]).lower())

    for r in reasons:
        if "timeout" in r or "deadline" in r:
            return EVIDENCE_TIMEOUT
        if "provider_error" in r or r.endswith("_error") or r == "error":
            return EVIDENCE_ERROR
    for r in reasons:
        if "no_data" in r:
            return EVIDENCE_NO_DATA
    for r in reasons:
        if "not_found" in r or "not_covered" in r:
            return EVIDENCE_NOT_FOUND

    # A resolved inventory record can legitimately carry
    # ``freshness_unavailable``.  That caveat is not a missing Pod.
    if isinstance(sc.get("pods"), list) and not sc["pods"]:
        return EVIDENCE_NOT_FOUND

    # 没有缺口：看是否真的采到了数值
    if status == "not_found":
        return EVIDENCE_NOT_FOUND

    has_value, all_zero = _scan_numeric(sc)
    if has_value:
        return EVIDENCE_OBSERVED_ZERO if all_zero else EVIDENCE_OBSERVED_VALUE
    return EVIDENCE_UNKNOWN


def _scan_numeric(obj: Any, depth: int = 0) -> Tuple[bool, bool]:
    """返回 (是否出现过数值, 是否所有数值都是 0)。只看度量类键，避免把计数当指标。"""
    if depth > 6:
        return (False, True)
    metric_keys = {
        "establish_fail_ratio", "retrans_ratio", "rtt_avg_ms", "rst_count",
        "zero_window_count", "error_ratio", "total_rps", "p95_ms", "p99_ms",
        "duration_ms", "v", "value",
    }
    found = False
    all_zero = True
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in metric_keys and isinstance(v, (int, float)) and not isinstance(v, bool):
                found = True
                if v != 0:
                    all_zero = False
            else:
                f, z = _scan_numeric(v, depth + 1)
                found = found or f
                all_zero = all_zero and z
    elif isinstance(obj, list):
        for v in obj:
            f, z = _scan_numeric(v, depth + 1)
            found = found or f
            all_zero = all_zero and z
    return (found, all_zero)


# ---------------------------------------------------------------------------
# 2. 禁止断言清单（缺数据时不得出现的肯定结论）
# ---------------------------------------------------------------------------

#: (正则, 说明)。**权威清单**：Supervisor 闸门与测试必须共用这一份。
FORBIDDEN_NO_DATA_ASSERTIONS: Tuple[Tuple[str, str], ...] = (
    # no_data 被说成零流量
    (r"zero\s+traffic", "把 no_data 说成零流量"),
    (r"no\s+L4\s+traffic", "把 no_data 说成没有 L4 流量"),
    (r"no\s+network\s+traffic", "把 no_data 说成没有网络流量"),
    (r"completely\s+empty\s+network", "把空结果说成网络完全空白"),
    (r"complete\s+L4\s+network\s+silence", "把空结果说成 L4 完全静默"),
    (r"零流量", "把 no_data 说成零流量"),
    (r"没有任何\s*L4\s*流量", "把 no_data 说成没有 L4 流量"),
    (r"无任何网络流量", "把 no_data 说成没有网络流量"),
    # 由缺数据推出网络故障
    (r"network[- ]level\s+outage", "由缺数据推出网络中断"),
    (r"network\s+outage", "由缺数据推出网络中断"),
    (r"network\s+failure", "由缺数据推出网络故障"),
    (r"network\s+isolation", "由缺数据推出网络隔离"),
    (r"网络中断", "由缺数据推出网络中断"),
    (r"网络层故障", "由缺数据推出网络层故障"),
    (r"网络隔离", "由缺数据推出网络隔离"),
    # unknown 被说成 NotReady
    (r"no\s+ready\s+replicas?", "把 ready_status=unknown 说成没有 Ready 副本"),
    (r"no\s+failover", "把 unknown 说成没有故障转移"),
    (r"\bnot\s+ready\b", "把 ready_status=unknown 说成 NotReady"),
    (r"没有\s*Ready\s*副本", "把 ready_status=unknown 说成没有 Ready 副本"),
    (r"无可用副本", "把 ready_status=unknown 说成无可用副本"),
    # 未被证据支持的处置方向
    (r"\bCNI\b", "给出未被证据支持的 CNI 处置方向"),
    (r"\biptables\b", "给出未被证据支持的 iptables 处置方向"),
    # 缺数据时声称已排除
    (r"ruled\s+out", "缺数据时声称已排除"),
    (r"已排除", "缺数据时声称已排除"),
)

#: 出现这些措辞说明是"待验证嫌疑点"而非"已确认根因"。
HEDGE_MARKERS: Tuple[str, ...] = (
    "suspect", "suspected", "suspicion", "hypothes", "unverified", "unconfirmed",
    "cannot be determined", "cannot confirm", "not enough evidence",
    "insufficient evidence", "needs more evidence", "unproven",
    "嫌疑", "待验证", "未证实", "证据不足", "无法判断",
)


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    for ch in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014"):
        t = t.replace(ch, "-")
    return t


def _flatten_strings(obj: Any, acc: List[Tuple[str, str]], path: str = "") -> None:
    if isinstance(obj, str):
        acc.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_strings(v, acc, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _flatten_strings(v, acc, f"{path}[{i}]")


def find_forbidden_assertions(
    report: Dict[str, Any], network_supported_targets: Iterable[str] = (),
) -> List[Dict[str, str]]:
    """在报告里找出所有禁止断言。返回 [{path, pattern, matched, why, context}]。"""
    strings: List[Tuple[str, str]] = []
    _flatten_strings(report or {}, strings)
    out: List[Dict[str, str]] = []
    supported = {_norm(str(name)).lower() for name in network_supported_targets if name}
    for path, text in strings:
        t = _norm(text)
        for pat, why in FORBIDDEN_NO_DATA_ASSERTIONS:
            # A real, target-specific L4 anomaly is allowed to support a
            # network-fault finding.  It still cannot support "zero traffic",
            # a CNI mechanism, or a Ready claim without their own evidence.
            if why.startswith("由缺数据推出网络") and any(
                name in t.lower() for name in supported
            ):
                continue
            m = re.search(pat, t, re.IGNORECASE)
            if m:
                out.append({
                    "path": path, "pattern": pat, "matched": m.group(0), "why": why,
                    "context": t[max(0, m.start() - 120): m.end() + 120],
                })
    return out


# ---------------------------------------------------------------------------
# 3. 最终报告闸门
# ---------------------------------------------------------------------------

def collect_worker_evidence(analyses: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Extract *ToolMessage* results, never a Worker's prose, as RCA evidence.

    The message history is already returned by each Worker.  Matching
    ``tool_call_id`` to the AI request keeps concurrent tool calls attributable
    to the right Pod; older saved histories fall back to the task target.
    """
    observations: List[Dict[str, Any]] = []
    for analysis in analyses or []:
        if not isinstance(analysis, dict):
            continue
        task = analysis.get("task") or {}
        task_target = str(task.get("target_resource") or "") if isinstance(task, dict) else ""
        requests: Dict[str, Dict[str, Any]] = {}
        for msg in analysis.get("message_history") or []:
            if not isinstance(msg, dict):
                continue
            for call in msg.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    requests[str(call["id"])] = call
            if msg.get("type") != "ToolMessage":
                continue
            request = requests.get(str(msg.get("tool_call_id") or ""), {})
            name = str(msg.get("name") or request.get("name") or "")
            if name == "submit_final_diagnosis":
                continue
            args = request.get("args") or {}
            if not isinstance(args, dict):
                args = {}
            raw = msg.get("content")
            try:
                result = json.loads(raw) if isinstance(raw, str) else raw
            except (ValueError, TypeError):
                result = raw
            if isinstance(result, dict) and isinstance(result.get("content"), str):
                try:
                    result = json.loads(result["content"])
                except ValueError:
                    pass
            target = str(args.get("pod") or task_target or args.get("pod_uid") or "")
            kind = classify_result(result)
            domains: List[str] = []
            metrics: Dict[str, Any] = {}
            if isinstance(result, dict) and kind not in INCONCLUSIVE_KINDS:
                summary = result.get("summary") or {}
                if name == "get_pod_network_summary" and isinstance(summary, dict):
                    for key in ("establish_fail_ratio", "retrans_ratio", "rtt_avg_ms",
                                "rst_count", "zero_window_count"):
                        if isinstance(summary.get(key), (int, float)):
                            metrics[key] = summary[key]
                    if (summary.get("anomalies") or [] or
                        (metrics.get("establish_fail_ratio") or 0) > 0.05 or
                        (metrics.get("retrans_ratio") or 0) > 0.05 or
                        (metrics.get("rst_count") or 0) > 0 or
                        (metrics.get("zero_window_count") or 0) > 0):
                        domains.append("network")
                if name == "get_pod_application_summary" and isinstance(summary, dict):
                    if isinstance(summary.get("error_ratio"), (int, float)):
                        metrics["error_ratio"] = summary["error_ratio"]
                        if summary["error_ratio"] > 0.05:
                            domains.append("application")
                if name == "diagnose_pod":
                    diagnosis = result.get("result") or {}
                    for candidate in diagnosis.get("root_cause_candidates") or []:
                        if not isinstance(candidate, dict):
                            continue
                        category = str(candidate.get("category") or "")
                        if category in ("network", "application") and (
                            candidate.get("evidence_ids") or []
                        ):
                            domains.append(category)
            observations.append({
                "source": f"mcp_tool:{name}", "tool": name, "target": target,
                "evidence_kind": kind, "fault_domains": sorted(set(domains)),
                "metrics": metrics,
            })
    return observations

def _has_positive_evidence(name: str, evidence: Iterable[Dict[str, Any]]) -> bool:
    """A target needs a *fault signal*, not merely a number in an LLM sentence."""
    if not name:
        return False
    target = _norm(str(name))
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("evidence_kind") or item.get("kind") or ""
        if kind not in (EVIDENCE_OBSERVED_VALUE, EVIDENCE_OBSERVED_ZERO):
            continue
        observed_target = _norm(str(item.get("target") or ""))
        if observed_target == target and item.get("fault_domains"):
            return True
        # Compatibility for explicitly typed facts passed by existing callers.
        statement = _norm(str(item.get("statement", "")))
        if target in statement and (
            re.search(r"establish_fail_ratio\s*[=: ]\s*0\.[1-9]", statement) or
            re.search(r"error_ratio\s*[=: ]\s*0\.[1-9]", statement)
        ):
            return True
    return False


def _hedge(text: str) -> str:
    return (
        "[未证实的嫌疑点] 以下内容缺少工具证据支撑，仅作为待验证方向保留："
        + text.strip()
    )


def enforce_evidence_discipline(
    report: Optional[Dict[str, Any]],
    evidence: Optional[Iterable[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """对最终报告做确定性的证据复核（补充 PRD §4 P0-1 第 2、3 条）。

    返回 ``(修订后的报告, 审计记录)``。审计记录会放进报告的
    ``evidence_audit`` 字段，供验收核对；**不删改模型的原始表述**，
    而是把缺证据的结论移入 ``unsupported_claims`` 并降级措辞。

    规则：
      1. 命中 ``FORBIDDEN_NO_DATA_ASSERTIONS`` 的句子 → 从原字段移除，
         记入 ``unsupported_claims``；
      2. ``localization`` 里没有正向证据支撑的目标 → 移除并记入
         ``unverified_localization``；
      3. 若发生了降级，追加一条数据缺口说明，并把 ``detection`` 保持原值
         （detection 表示"是否检测到异常"，与根因是否证实是两件事）。
    """
    report = dict(report or {})
    original_root = str(report.get("root_cause") or "")
    evidence = list(evidence or [])
    audit: Dict[str, Any] = {
        "violations": [],
        "downgraded": False,
        "notes": [],
    }

    supported_network_targets = {
        _norm(str(item.get("target") or "")) for item in evidence
        if isinstance(item, dict) and item.get("evidence_kind") not in INCONCLUSIVE_KINDS
        and "network" in (item.get("fault_domains") or [])
    }
    # --- 规则 1：禁止断言 ---
    violations = find_forbidden_assertions(report, supported_network_targets)
    if violations:
        audit["violations"] = violations
        audit["downgraded"] = True
        unsupported = list(report.get("unsupported_claims") or [])
        for key in ("root_cause", "evidence_summary", "investigation_summary"):
            text = report.get(key)
            if not isinstance(text, str) or not text:
                continue
            cleaned = text
            hit_here = False
            for v in violations:
                if v["path"].split(".")[0] != key:
                    continue
                # 逐句过滤：包含禁止措辞的句子整句移出
                kept = []
                for sentence in re.split(r"(?<=[.!?。！？])\s*", cleaned):
                    if re.search(v["pattern"], _norm(sentence), re.IGNORECASE):
                        unsupported.append({"field": key, "sentence": sentence.strip(),
                                            "why": v["why"], "pattern": v["pattern"]})
                        hit_here = True
                    else:
                        kept.append(sentence)
                cleaned = " ".join(s for s in kept if s.strip())
            if hit_here:
                if cleaned.strip():
                    report[key] = cleaned.strip()
                else:
                    report[key] = _hedge("（原表述因缺少证据已被移除，见 unsupported_claims）")
        if unsupported:
            report["unsupported_claims"] = unsupported

    # --- 规则 2：localization 必须有正向证据 ---
    loc = report.get("localization")
    if isinstance(loc, str):
        loc = [loc]
    if loc:
        kept, dropped = [], []
        for item in loc:
            if _has_positive_evidence(str(item), evidence):
                kept.append(item)
            else:
                dropped.append(item)
        if dropped:
            audit["downgraded"] = True
            report["unverified_localization"] = dropped
            audit["notes"].append(
                "以下目标没有任何带数值的上游事实支撑，已从 localization 移除并记为待验证："
                + ", ".join(str(x) for x in dropped))
        report["localization"] = kept

        # If the named root cause itself lost localization, sentence filtering
        # is not enough: a paraphrased causal claim could still survive.
        if dropped and any(_norm(str(name)) in _norm(original_root) for name in dropped):
            report.setdefault("unsupported_claims", []).append({
                "field": "root_cause", "sentence": original_root,
                "why": "定位目标没有正向故障证据", "pattern": "unverified_localization",
            })
            report["root_cause"] = (
                "已检测到异常，但目前没有足够的工具证据确认根因；"
                "被移除的目标仅是待验证嫌疑点。"
            )

    # --- 规则 3：降级后必须留数据缺口 ---
    if audit["downgraded"]:
        gaps = list(report.get("data_gaps") or [])
        gap = {
            "reason": "insufficient_evidence_for_root_cause",
            "impact": "关键结论缺少工具证据支撑，已按补充 PRD §4 P0-1 降级为待验证嫌疑点；"
                      "需要补充可采样的指标/Trace 或把目标纳入可诊断夹具后才可确认根因",
            "severity": "missing",
        }
        if gap not in gaps:
            gaps.append(gap)
        report["data_gaps"] = gaps
        audit["notes"].append("已追加 insufficient_evidence_for_root_cause 数据缺口")

    if audit["violations"] or audit["downgraded"]:
        report["evidence_audit"] = audit
    return report, audit
