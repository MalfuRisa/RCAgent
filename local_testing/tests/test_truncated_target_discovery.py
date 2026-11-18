"""P0-3 的三组测试：完整唯一 / 完整多候选 / **截断但仅一候选**。

补充 PRD §4 P0-3 第 3 条明确要求第三组必须**走 RCAgent Triage/图路由**，
不能只测 Gateway 返回的 JSON —— 所以这里驱动真正的
``discover_service_target``，并断言它在截断时**没有**调用任何下游 Tool。

复用 `test_triage_discovery` 里既有的 FakeGateway / fixture 助手，避免两处漂移。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_triage_discovery import (  # noqa: E402
    FakeGateway,
    happy_gateway,
    run_discovery,
    search_response,
    service_candidate,
)

from tools.triage_discovery import (  # noqa: E402
    DISCOVERY_AMBIGUOUS,
    DISCOVERY_PARTIAL,
)


class TruncatedSingleCandidateTest(unittest.TestCase):
    """第三组：截断 + 仅 1 个候选 → 同样要求确认。"""

    def setUp(self) -> None:
        self.only = service_candidate()
        self.gateway = FakeGateway({
            "search_k8s_targets": search_response(
                [self.only], status=DISCOVERY_PARTIAL, truncated=True,
                warnings=["扫描或返回条数已达上限，候选列表可能不完整（truncated=true）"],
            ),
        })
        self.outcome = run_discovery(self.gateway)

    def test_requires_confirmation_even_with_one_candidate(self) -> None:
        self.assertTrue(self.outcome.requires_confirmation,
                        "截断时即使只有一个候选也必须要求确认")
        self.assertEqual(self.outcome.status, DISCOVERY_PARTIAL)

    def test_stops_before_downstream_tools(self) -> None:
        """必须**没有**进入 get_service_backends / 指标 / Trace。"""
        self.assertEqual(self.gateway.called_tools, ["search_k8s_targets"],
                         f"截断后不得继续调用下游 Tool，实际: {self.gateway.called_tools}")

    def test_no_service_pod_or_metric_was_investigated(self) -> None:
        self.assertIsNone(self.outcome.service)
        self.assertIsNone(self.outcome.backends)
        self.assertEqual(self.outcome.metrics, [])
        self.assertEqual(self.outcome.attributed_pods, [])

    def test_clarification_explains_why_one_candidate_is_not_unique(self) -> None:
        clar = self.outcome.clarification
        self.assertEqual(clar.get("reason"), "incomplete_target_scan")
        self.assertTrue(clar.get("truncated"))
        question = clar.get("question", "")
        self.assertIn("不能", question)
        self.assertIn("唯一", question)
        self.assertEqual(len(clar.get("candidates") or []), 1,
                         "仍要把看到的候选如实返回，只是不能当作唯一")

    def test_records_a_data_gap_never_a_health_statement(self) -> None:
        reasons = {g.get("reason") for g in self.outcome.data_gaps}
        self.assertIn("incomplete_target_scan", reasons,
                      "必须记为数据缺口，而不是对服务健康的判断")
        for gap in self.outcome.data_gaps:
            impact = str(gap.get("impact", "")).lower()
            for bad in ("zero traffic", "network outage", "healthy",
                        "零流量", "健康", "正常"):
                self.assertNotIn(bad, impact)


class CompleteSingleCandidateTest(unittest.TestCase):
    """第一组：完整 + 唯一 → 正常继续（别把正常路径也堵了）。"""

    def test_proceeds_when_list_is_complete(self) -> None:
        gateway = happy_gateway()
        outcome = run_discovery(gateway, structured={
            "target_cluster": "prod-a", "target_namespace": "payments"})
        self.assertFalse(outcome.requires_confirmation,
                         "完整清单 + 唯一候选不应要求确认")
        self.assertIn("get_service_backends", gateway.called_tools,
                      "完整唯一时应继续查后端 Pod")


class CompleteMultipleCandidatesTest(unittest.TestCase):
    """第二组：完整 + 多候选 → 要求确认。"""

    def setUp(self) -> None:
        self.gateway = FakeGateway({
            "search_k8s_targets": search_response(
                [service_candidate(cluster="prod-a"),
                 service_candidate(cluster="staging")],
                status=DISCOVERY_AMBIGUOUS),
        })
        self.outcome = run_discovery(self.gateway)

    def test_multiple_candidates_require_confirmation(self) -> None:
        self.assertTrue(self.outcome.requires_confirmation)
        self.assertEqual(self.outcome.status, DISCOVERY_AMBIGUOUS)

    def test_does_not_continue_and_does_not_pick_the_first(self) -> None:
        self.assertEqual(self.gateway.called_tools, ["search_k8s_targets"],
                         "歧义时不得继续调用下游 Tool")
        self.assertEqual(len(self.outcome.candidates), 2,
                         "必须返回两个完整候选，不能悄悄选第一个")
        self.assertIsNone(self.outcome.service)


if __name__ == "__main__":
    unittest.main()