"""Unit tests for natural-language incident parsing and scope resolution.

Run from the RCAgent repository root::

    python -m unittest discover -s local_testing/tests -t local_testing/tests -v

These tests are hermetic: no Gateway, no model, no credentials.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.incident_text import (  # noqa: E402
    MAX_WINDOW_MINUTES,
    SYMPTOM_HTTP_5XX,
    SYMPTOM_LATENCY,
    SYMPTOM_TIMEOUT,
    UNKNOWN,
    IncidentFacts,
    LlmIncidentProposal,
    merge_grounded_proposals,
    parse_incident_text,
    resolve_incident_scope,
)


ACCEPTANCE_TEXT = "payment 服务最近 15 分钟一直报 500，帮我看看"


class AcceptanceExampleTest(unittest.TestCase):
    """PRD §10.3: "payment 服务最近 15 分钟报 500"."""

    def setUp(self) -> None:
        self.facts = parse_incident_text(ACCEPTANCE_TEXT)

    def test_extracts_service(self) -> None:
        self.assertEqual(self.facts.service, "payment")

    def test_explicit_service_label_after_namespace_has_priority(self):
        for name in ('iam-server', 'frontend', 'redis', 'rcagent-does-not-exist'):
            facts = parse_incident_text(f'cluster: rd2 namespace: kylin-insights service: {name} 最近15分钟有错误')
            self.assertEqual(facts.service, name)
            self.assertEqual(facts.namespace, 'kylin-insights')
            self.assertEqual(facts.cluster, 'rd2')

    def test_extracts_symptom_and_status_code(self) -> None:
        self.assertEqual(self.facts.status_code, 500)
        self.assertEqual(self.facts.symptom, SYMPTOM_HTTP_5XX)

    def test_extracts_time_window(self) -> None:
        self.assertEqual(self.facts.window_minutes, 15)

    def test_does_not_invent_scope(self) -> None:
        """The operator never named a cluster or namespace -> both stay unknown."""
        self.assertEqual(self.facts.cluster, UNKNOWN)
        self.assertEqual(self.facts.namespace, UNKNOWN)
        self.assertIn("cluster", self.facts.missing)
        self.assertIn("namespace", self.facts.missing)

    def test_is_actionable_without_required_gaps(self) -> None:
        self.assertTrue(self.facts.is_actionable)
        self.assertEqual(self.facts.missing_required, [])

    def test_summary_is_serialisable_and_carries_no_secret(self) -> None:
        summary = self.facts.as_summary()
        self.assertEqual(summary["service"], "payment")
        self.assertNotIn("api_key", str(summary).lower())


class ExtractionTest(unittest.TestCase):
    def test_english_incident(self) -> None:
        facts = parse_incident_text(
            "checkout service is slow for the last 30 minutes in cluster prod-a"
        )
        self.assertEqual(facts.service, "checkout")
        self.assertEqual(facts.cluster, "prod-a")
        self.assertEqual(facts.window_minutes, 30)
        self.assertEqual(facts.symptom, SYMPTOM_LATENCY)
        self.assertEqual(facts.namespace, UNKNOWN)

    def test_chinese_cluster_and_namespace(self) -> None:
        facts = parse_incident_text("集群 prod-a 命名空间 payments 的 payment 服务一直 timeout")
        self.assertEqual(facts.service, "payment")
        self.assertEqual(facts.cluster, "prod-a")
        self.assertEqual(facts.namespace, "payments")
        self.assertEqual(facts.symptom, SYMPTOM_TIMEOUT)

    def test_hours_are_converted_to_minutes(self) -> None:
        facts = parse_incident_text("payment 服务过去 2 小时一直报错")
        self.assertEqual(facts.window_minutes, 120)

    def test_window_is_clamped_to_24h(self) -> None:
        facts = parse_incident_text("payment 服务最近 5000 分钟一直报错")
        self.assertEqual(facts.window_minutes, MAX_WINDOW_MINUTES)
        self.assertLessEqual(facts.effective_window_minutes(), MAX_WINDOW_MINUTES)

    def test_sub_500_status_is_not_reported_as_5xx(self) -> None:
        facts = parse_incident_text("payment 服务最近 10 分钟返回 404")
        self.assertEqual(facts.status_code, 404)
        self.assertNotEqual(facts.symptom, SYMPTOM_HTTP_5XX)

    def test_duration_like_numbers_are_not_read_as_status_codes(self) -> None:
        facts = parse_incident_text("payment 服务延迟 500ms")
        self.assertIsNone(facts.status_code)

    def test_default_window_applies_when_text_has_none(self) -> None:
        facts = parse_incident_text("payment 服务一直报错")
        self.assertIsNone(facts.window_minutes)
        self.assertEqual(facts.effective_window_minutes(), 15)

    def test_window_helper_returns_ordered_rfc3339(self) -> None:
        facts = IncidentFacts(service="payment", window_minutes=15)
        now = datetime(2026, 9, 30, 10, 15, tzinfo=timezone(timedelta(hours=8)))
        start, end = facts.window(now)
        self.assertEqual(end, "2026-09-30T10:15:00+08:00")
        self.assertEqual(start, "2026-09-30T10:00:00+08:00")


class InsufficientInformationTest(unittest.TestCase):
    def test_chatter_without_target_is_not_actionable(self) -> None:
        facts = parse_incident_text("帮我看看")
        self.assertFalse(facts.is_actionable)
        self.assertEqual(facts.service, UNKNOWN)
        self.assertIn("service", facts.missing_required)

    def test_empty_text_is_reported_as_insufficient(self) -> None:
        for value in ("", "   ", None):
            facts = parse_incident_text(value)
            self.assertFalse(facts.is_actionable)
            self.assertIn("service", facts.missing_required)

    def test_scope_reports_insufficient_instead_of_guessing(self) -> None:
        scope = resolve_incident_scope({"incident_text": "帮我看看"})
        self.assertTrue(scope.is_insufficient)
        self.assertEqual(scope.service, UNKNOWN)
        self.assertEqual(scope.cluster, UNKNOWN)
        self.assertFalse(scope.needs_discovery)
        self.assertTrue(scope.reasons)

    def test_scope_without_any_input_is_insufficient(self) -> None:
        scope = resolve_incident_scope({})
        self.assertTrue(scope.is_insufficient)
        self.assertTrue(any("incident_text" in reason for reason in scope.reasons))


class StructuredPrecedenceTest(unittest.TestCase):
    """PRD §7: structured fields with explicit values are never rewritten."""

    def test_structured_cluster_and_namespace_override_parsed_text(self) -> None:
        state = {
            "incident_text": "payment 服务在 staging 集群 payments 命名空间报 500",
            "target_cluster": "prod-a",
            "target_namespace": "payments",
        }
        scope = resolve_incident_scope(state)
        self.assertEqual(scope.cluster, "prod-a")
        self.assertEqual(scope.namespace, "payments")
        self.assertIn("target_cluster", scope.structured_overrides)
        self.assertIn("target_namespace", scope.structured_overrides)
        self.assertEqual(scope.source, "incident_text")
        self.assertTrue(scope.needs_discovery)

    def test_structured_pod_selects_the_legacy_path(self) -> None:
        state = {
            "incident_text": ACCEPTANCE_TEXT,
            "target_cluster": "test-cluster",
            "target_namespace": "b01-http-5xx",
            "target_resource": "checkout-0",
        }
        scope = resolve_incident_scope(state)
        self.assertEqual(scope.source, "structured")
        self.assertEqual(scope.target_kind, "pod")
        self.assertEqual(scope.pod, "checkout-0")
        self.assertEqual(scope.cluster, "test-cluster")
        self.assertFalse(scope.needs_discovery)

    def test_parsed_text_fills_only_empty_fields(self) -> None:
        state = {"incident_text": ACCEPTANCE_TEXT, "target_cluster": "prod-a"}
        scope = resolve_incident_scope(state)
        self.assertEqual(scope.cluster, "prod-a")
        self.assertEqual(scope.service, "payment")
        self.assertEqual(scope.namespace, UNKNOWN)

    def test_symptom_survives_structured_target(self) -> None:
        """A structured Pod target must not discard the reported symptom."""
        state = {
            "incident_text": ACCEPTANCE_TEXT,
            "target_cluster": "prod-a",
            "target_namespace": "payments",
            "target_resource": "payment-7d9f-abcde",
        }
        scope = resolve_incident_scope(state)
        self.assertEqual(scope.symptom, SYMPTOM_HTTP_5XX)
        self.assertEqual(scope.status_code, 500)


class LlmProposalGroundingTest(unittest.TestCase):
    """The model may only propose values that literally appear in the text."""

    def test_grounded_proposal_fills_an_unknown_field(self) -> None:
        facts = parse_incident_text(ACCEPTANCE_TEXT)
        merged = merge_grounded_proposals(
            facts, LlmIncidentProposal(namespace="payments"), ACCEPTANCE_TEXT
        )
        # "payments" is not in the text -> rejected, not invented.
        self.assertEqual(merged.namespace, UNKNOWN)

    def test_grounded_value_present_in_text_is_accepted(self) -> None:
        text = "payment 服务在 prod-a 集群报 500"
        facts = parse_incident_text(text)
        merged = merge_grounded_proposals(
            facts, LlmIncidentProposal(cluster="prod-a"), text
        )
        self.assertEqual(merged.cluster, "prod-a")
        self.assertTrue(
            any(note.startswith("llm_proposal_accepted:cluster") for note in merged.notes)
        )

    def test_proposal_cannot_overwrite_a_known_value(self) -> None:
        text = "payment 服务在 prod-a 集群报 500"
        facts = parse_incident_text(text)
        merged = merge_grounded_proposals(
            facts, LlmIncidentProposal(service="checkout"), text
        )
        self.assertEqual(merged.service, "payment")
        self.assertTrue(
            any("llm_proposal_ignored:service" in note for note in merged.notes)
        )

    def test_unknown_symptom_is_rejected(self) -> None:
        facts = parse_incident_text("payment 服务一直有问题")
        merged = merge_grounded_proposals(
            facts, LlmIncidentProposal(symptom="made_up_symptom"), "payment 服务一直有问题"
        )
        self.assertNotEqual(merged.symptom, "made_up_symptom")
        self.assertTrue(
            any("llm_proposal_rejected:symptom" in note for note in merged.notes)
        )

    def test_negative_window_is_rejected(self) -> None:
        facts = parse_incident_text("payment 服务一直报错")
        merged = merge_grounded_proposals(
            facts, LlmIncidentProposal(window_minutes=-5), "payment 服务一直报错"
        )
        self.assertIsNone(merged.window_minutes)

    def test_rejection_never_breaks_actionability(self) -> None:
        facts = parse_incident_text(ACCEPTANCE_TEXT)
        merged = merge_grounded_proposals(
            facts, LlmIncidentProposal(cluster="guessed-cluster"), ACCEPTANCE_TEXT
        )
        self.assertTrue(merged.is_actionable)
        self.assertEqual(merged.service, "payment")


if __name__ == "__main__":
    unittest.main(verbosity=2)
