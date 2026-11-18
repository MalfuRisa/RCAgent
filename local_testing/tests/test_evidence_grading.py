"""证据分级与结论闸门的反例测试（补充 PRD §4 P0-1）。

三层断言（PRD 明确要求不能只测字符串辅助函数）：
  1. 分类层：``classify_result`` 必须认出修复前 `payment-db-0` 暴露的**三种形态**；
  2. 报告层：``enforce_evidence_discipline`` 必须挡掉上一轮那条误判结论；
  3. 图接线层：``evidence_gate`` 必须真的接在 supervisor 之后，
     且不改动 `tasks_to_be_executed`（否则路由行为会变）。
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

# graph 的导入经 agents/__init__ 惰性构造 LLM 配置；在**任何** graph 导入之前
# 放占位凭证，保证离线可跑、且不需要真实密钥（不读、不打印任何真实 secret）。
os.environ.setdefault("RCAGENT_LLM_API_KEY", "test-placeholder-not-a-real-key")
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")

from tools.evidence_grading import (
    EVIDENCE_ERROR,
    EVIDENCE_NO_DATA,
    EVIDENCE_NOT_FOUND,
    EVIDENCE_OBSERVED_VALUE,
    EVIDENCE_OBSERVED_ZERO,
    EVIDENCE_TIMEOUT,
    EVIDENCE_UNKNOWN,
    classify_result,
    collect_worker_evidence,
    enforce_evidence_discipline,
    find_forbidden_assertions,
)

try:  # 图接线测试用；导入失败时该组测试跳过，而不是让整个套件挂掉
    from graph import evidence_gate as _graph_evidence_gate
except Exception as _exc:  # pragma: no cover
    _graph_evidence_gate = None
    _GRAPH_IMPORT_ERROR = _exc
else:
    _GRAPH_IMPORT_ERROR = None

# 上一轮的误判结论（逐字来自 test-results/.../nl-full-llm.json）
MISJUDGED_REPORT = {
    "root_cause": (
        "The database pod `payment-db-0` is experiencing a network-level outage: it has not "
        "processed any L4 network traffic (TCP connections) for the past 15 minutes, as shown "
        "by a completely empty network summary. This isolates the database from the payment "
        "service, causing all queries from `payment-7d9f-abcde` to fail. Since `payment-db-0` "
        "is the sole backend for the `payment-db` Service, there is no failover mechanism. "
        "The root cause lies in the database pod's networking layer - possibly a CNI "
        "disruption, iptables misconfiguration, or the application inside the pod has stopped "
        "listening - preventing any connectivity even though the pod status appears as Running "
        "in the inventory."
    ),
    "evidence_summary": (
        "the payment-db service has only one backend (payment-db-0) with no ready replicas."
    ),
    "investigation_summary": (
        "Priority #1 ruled out network issues on the payment pod. Priority #2 discovered the "
        "database pod's complete L4 network silence. This aligns with the root cause being the "
        "database pod's network isolation."
    ),
    "localization": ["payment-db-0"],
    "detection": True,
}


class TestClassification(unittest.TestCase):
    """1) 分类层：必须认出多种形态，不能只处理 no_data 一个字符串。"""

    def test_observed_zero_is_not_missing_data(self):
        """真 0 必须被认成 observed_zero，而不是缺数据。"""
        kind = classify_result({"structuredContent": {
            "status": "ok",
            "summary": {"establish_fail_ratio": 0, "retrans_ratio": 0, "rtt_avg_ms": 0,
                        "rst_count": 0, "zero_window_count": 0},
        }})
        self.assertEqual(kind, EVIDENCE_OBSERVED_ZERO,
                         "采到数值 0 必须与「没有样本」区分开")

    def test_observed_value(self):
        kind = classify_result({"structuredContent": {
            "status": "ok", "summary": {"establish_fail_ratio": 0.4, "rtt_avg_ms": 15},
        }})
        self.assertEqual(kind, EVIDENCE_OBSERVED_VALUE)

    def test_no_data_gap(self):
        kind = classify_result({"structuredContent": {
            "meta": {"data_gaps": [{"reason": "no_data", "impact": "x"}]},
        }})
        self.assertEqual(kind, EVIDENCE_NO_DATA)

    def test_provider_error_gap(self):
        """修复前 diagnose_pod(payment-db-0) 返回的正是 provider_error。"""
        kind = classify_result({"structuredContent": {
            "status": "unknown",
            "result": {"data_gaps": [{"reason": "provider_error", "impact": "x"}]},
        }})
        self.assertEqual(kind, EVIDENCE_ERROR)

    def test_empty_resolve_is_not_found(self):
        """修复前 resolve_pod(payment-db-0) 返回 0 pods —— 空解析不是「零流量」。"""
        kind = classify_result({"structuredContent": {"status": "not_found", "pods": []}})
        self.assertEqual(kind, EVIDENCE_NOT_FOUND)

    def test_inventory_freshness_gap_does_not_erase_a_resolved_pod(self):
        kind = classify_result({"pods": [{"name": "payment-0"}], "meta": {
            "data_gaps": [{"reason": "freshness_unavailable"}],
        }})
        self.assertEqual(kind, EVIDENCE_UNKNOWN)

    def test_timeout(self):
        # kitex 的 DeadlineExceeded 文本没有空格，必须也能认出
        self.assertEqual(classify_result("... DeadlineExceeded ..."), EVIDENCE_TIMEOUT)
        self.assertEqual(classify_result("rpc timeout"), EVIDENCE_TIMEOUT)
        self.assertEqual(
            classify_result({"structuredContent": {
                "meta": {"data_gaps": [{"reason": "timeout"}]}}}),
            EVIDENCE_TIMEOUT)

    def test_rpc_error(self):
        self.assertEqual(classify_result({"is_error": True, "structured": {}}), EVIDENCE_ERROR)


class TestForbiddenAssertions(unittest.TestCase):
    """上一轮那条报告必须被完整命中。"""

    def test_misjudged_report_hits_all_expected_patterns(self):
        hits = find_forbidden_assertions(MISJUDGED_REPORT)
        matched = {h["matched"].lower() for h in hits}
        for want in ("completely empty network", "network-level outage", "no failover",
                     "cni", "iptables", "no ready replicas", "network isolation", "ruled out"):
            self.assertTrue(any(want in m for m in matched),
                            f"应命中 {want!r}，实际命中: {sorted(matched)}")
        self.assertGreaterEqual(len(hits), 8, "该误判报告至少应命中 8 条")

    def test_hedged_wording_is_allowed(self):
        """待验证嫌疑点不能也被当成违规（否则矫枉过正）。"""
        report = {
            "root_cause": (
                "The payment service returns HTTP 500 (observed). The database query span "
                "fails (observed). `payment-db-0` is a **suspected** fault domain, but the "
                "network layer cannot be determined from the available evidence."
            ),
            "localization": [],
        }
        self.assertEqual(find_forbidden_assertions(report), [])


class TestEvidenceGate(unittest.TestCase):
    """2) 报告层：闸门必须挡掉误判结论并留下审计。"""

    def setUp(self):
        # 有正向证据：payment 入口错误率 42%（带数值）
        self.evidence = [{
            "source": "mcp_tool:diagnose_pod",
            "statement": "Pod payment-7d9f-abcde 入口错误率 0.42",
            "evidence_kind": EVIDENCE_OBSERVED_VALUE,
        }]

    def test_gate_removes_forbidden_sentences(self):
        revised, audit = enforce_evidence_discipline(MISJUDGED_REPORT, self.evidence)
        # 只看**对外叙述字段**：审计留档（unsupported_claims / evidence_audit）
        # 按设计保留原文以便追溯，那不是"报告在断言"。
        narrative = " ".join(
            str(revised.get(k) or "") for k in
            ("root_cause", "evidence_summary", "investigation_summary")
        ).lower()
        for bad in ("network-level outage", "completely empty network", "no failover",
                    "iptables", "no ready replicas", "network isolation"):
            self.assertNotIn(bad, narrative, f"降级后叙述里仍残留 {bad!r}")
        self.assertTrue(audit["downgraded"])
        self.assertTrue(revised.get("unsupported_claims"),
                        "被移除的表述必须留档，不能悄悄删")
        # 留档必须能指回原句，便于人工复核
        for item in revised["unsupported_claims"]:
            self.assertTrue(item.get("sentence"))
            self.assertTrue(item.get("why"))

    def test_gate_drops_localization_without_positive_evidence(self):
        revised, _ = enforce_evidence_discipline(MISJUDGED_REPORT, self.evidence)
        self.assertEqual(revised.get("localization"), [],
                         "payment-db-0 没有正向证据，必须从 localization 移除")
        self.assertEqual(revised.get("unverified_localization"), ["payment-db-0"])

    def test_gate_keeps_localization_with_positive_evidence(self):
        evidence = [{
            "source": "mcp_tool:get_pod_network_summary",
            "statement": "Pod payment-db-0 establish_fail_ratio 0.55, rst_count 42",
            "evidence_kind": EVIDENCE_OBSERVED_VALUE,
        }]
        revised, _ = enforce_evidence_discipline(MISJUDGED_REPORT, evidence)
        self.assertEqual(revised.get("localization"), ["payment-db-0"],
                         "有正向数值证据时不得误删 localization")

    def test_gate_adds_data_gap(self):
        revised, _ = enforce_evidence_discipline(MISJUDGED_REPORT, self.evidence)
        reasons = {g.get("reason") for g in revised.get("data_gaps") or []}
        self.assertIn("insufficient_evidence_for_root_cause", reasons)

    def test_clean_report_is_untouched(self):
        clean = {
            "root_cause": "payment 入口 5xx（observed），数据库查询 Span 报错（observed）；"
                          "网络层无法判断，payment-db-0 为待验证嫌疑点。",
            "localization": [],
            "detection": True,
        }
        revised, audit = enforce_evidence_discipline(clean, self.evidence)
        self.assertFalse(audit["downgraded"])
        self.assertEqual(revised.get("root_cause"), clean["root_cause"])
        self.assertNotIn("evidence_audit", revised)

    def test_worker_no_data_is_not_network_outage(self):
        analyses = [{
            "task": {"target_resource": "payment-db-0"},
            "message_history": [
                {"type": "AIMessage", "tool_calls": [{
                    "id": "call-1", "name": "get_pod_network_summary",
                    "args": {"pod_uid": "uid-db"},
                }]},
                {"type": "ToolMessage", "tool_call_id": "call-1",
                 "content": json.dumps({"meta": {"data_gaps": [{"reason": "no_data"}]},
                                        "summary": {}})},
            ],
        }]
        observations = collect_worker_evidence(analyses)
        self.assertEqual(observations[0]["evidence_kind"], EVIDENCE_NO_DATA)
        self.assertEqual(observations[0]["fault_domains"], [])
        revised, _ = enforce_evidence_discipline(MISJUDGED_REPORT, observations)
        self.assertEqual(revised["localization"], [])
        self.assertIn("没有足够的工具证据", revised["root_cause"])

    def test_normal_numeric_network_data_is_not_fault_evidence(self):
        analyses = [{
            "task": {"target_resource": "payment-db-0"},
            "message_history": [{"type": "ToolMessage", "name": "get_pod_network_summary",
                                 "content": json.dumps({"summary": {
                                     "establish_fail_ratio": 0, "retrans_ratio": 0.005,
                                     "rtt_avg_ms": 12, "rst_count": 0,
                                 }})}],
        }]
        observations = collect_worker_evidence(analyses)
        self.assertEqual(observations[0]["evidence_kind"], EVIDENCE_OBSERVED_VALUE)
        self.assertEqual(observations[0]["fault_domains"], [])
        revised, _ = enforce_evidence_discipline(MISJUDGED_REPORT, observations)
        self.assertEqual(revised["localization"], [])

    def test_real_network_anomaly_can_support_network_localization(self):
        analyses = [{
            "task": {"target_resource": "payment-db-0"},
            "message_history": [{"type": "ToolMessage", "name": "get_pod_network_summary",
                                 "content": json.dumps({"summary": {
                                     "establish_fail_ratio": 0.62, "retrans_ratio": 0.35,
                                     "rtt_avg_ms": 480, "rst_count": 48,
                                 }})}],
        }]
        observations = collect_worker_evidence(analyses)
        self.assertIn("network", observations[0]["fault_domains"])
        supported = {
            "root_cause": "payment-db-0 has a network-level outage: TCP establish failure is 62%.",
            "localization": ["payment-db-0"], "detection": True,
        }
        revised, audit = enforce_evidence_discipline(supported, observations)
        self.assertEqual(revised["localization"], ["payment-db-0"])
        self.assertIn("network-level outage", revised["root_cause"])
        self.assertFalse(audit["downgraded"])


class TestGraphWiring(unittest.TestCase):
    """3) 图接线层：闸门必须真的在 supervisor 之后，且不破坏路由。"""

    def test_gate_is_wired_after_supervisor(self):
        from graph import build_parent_graph
        edges = {(e.source, e.target) for e in build_parent_graph().get_graph().edges}
        self.assertIn(("supervisor_agent", "evidence_gate"), edges)
        self.assertIn(("evidence_gate", "__end__"), edges)
        self.assertIn(("supervisor_agent", "rca_agent"), edges)
        self.assertNotIn(("supervisor_agent", "schedule_rca_tasks"), edges)

    def test_gate_passes_routing_state_through(self):
        if _graph_evidence_gate is None:  # pragma: no cover
            self.skipTest(f"graph 不可导入（离线/缺依赖）: {_GRAPH_IMPORT_ERROR}")
        state = {
            "final_report": dict(MISJUDGED_REPORT),
            "tasks_to_be_executed": [1, 2],
            "rca_analyses_list": [],
        }
        import asyncio
        import tempfile
        from unittest.mock import patch
        from config import settings
        from workspace.runtime import initialize_incident
        async def run():
            with tempfile.TemporaryDirectory() as directory, patch.object(settings, "WORKSPACE_ROOT", Path(directory)):
                workspace = await initialize_incident({"target_cluster": "c"})
                return await _graph_evidence_gate({**workspace, **state})
        out = asyncio.run(run())
        self.assertEqual(state["tasks_to_be_executed"], [1, 2],
                         "闸门不得改动路由输入")
        self.assertNotIn("tasks_to_be_executed", out,
                         "闸门只应返回 final_report，避免覆盖路由状态")
        self.assertIn("final_report", out)


if __name__ == "__main__":
    unittest.main()
