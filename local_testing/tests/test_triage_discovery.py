"""Unit tests for Triage target discovery.

Covers the scenarios PRD §8/§10 and the design spec §5 require to be covered:

* unique target -> the full discovery chain runs;
* two same-named Services -> ``ambiguous``, and **no** Pod is selected;
* structured input wins over parsed text;
* selector without matching Pods / empty metrics / empty traces -> recorded as
  data gaps, never as "the service is healthy".

Everything runs against an injected fake Gateway caller, so the tests need no
Gateway, no credentials and no model.

Run from the RCAgent repository root::

    python -m unittest discover -s local_testing/tests -t local_testing/tests -v
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.incident_text import parse_incident_text, resolve_incident_scope  # noqa: E402
from tools.observability_adapter import (  # noqa: E402
    call_gateway_tool,
    classify_tool_error,
    target_from_state,
    tool_not_published,
)
from tools.triage_discovery import (  # noqa: E402
    DISCOVERY_AMBIGUOUS,
    DISCOVERY_ERROR,
    DISCOVERY_NOT_FOUND,
    DISCOVERY_OK,
    DISCOVERY_PARTIAL,
    discover_service_target,
)


ACCEPTANCE_TEXT = "payment 服务最近 15 分钟一直报 500，帮我看看"


# --------------------------------------------------------------------------
# Fake Gateway, mirroring the Mock fixtures in design spec §4.
# --------------------------------------------------------------------------


class FakeGateway:
    """Records calls and replays fixture responses keyed by tool name."""

    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name: str, arguments) -> dict:
        self.calls.append((name, dict(arguments)))
        response = self.responses.get(name, {"status": "ok"})
        if callable(response):
            return response(dict(arguments))
        return response

    @property
    def called_tools(self) -> list[str]:
        return [name for name, _ in self.calls]

    def zero_arg_calls(self, prefix: str) -> int:
        return sum(1 for name, _ in self.calls if name.startswith(prefix))


def service_candidate(name="payment", cluster="prod-a", namespace="payments"):
    return {
        "kind": "service",
        "name": name,
        "uid": f"svc-{cluster}-{namespace}-{name}",
        "id": 12,
        "cluster": cluster,
        "namespace": namespace,
        "match": "exact",
        "match_field": "name",
        "type": "ClusterIP",
        "cluster_ip": "10.96.0.11",
    }


def search_response(candidates, status="ok", **extra):
    return {
        "status": status,
        "data_source": "server_inventory",
        "warnings": [],
        "truncated": False,
        "freshness": "unknown",
        "query": "payment",
        "kind": "auto",
        "scanned": {"clusters": 2, "namespaces": 4, "services": 6, "pods": 33},
        "candidates": candidates,
        "candidates_by_kind": {
            "service": len([c for c in candidates if c["kind"] == "service"]),
            "pod": len([c for c in candidates if c["kind"] == "pod"]),
        },
        **extra,
    }


def backends_response(pods, status="ok", matching_note=None, **extra):
    return {
        "status": status,
        "data_source": "server_inventory",
        "warnings": [],
        "truncated": False,
        "freshness": "unknown",
        "matching_basis": "inventory_selector",
        "ready_status": "unknown",
        "service": {
            "uid": "svc-prod-a-payments-payment",
            "id": 12,
            "name": "payment",
            "cluster": "prod-a",
            "namespace": "payments",
            "type": "ClusterIP",
            "cluster_ip": "10.96.0.11",
            "selector": {"app": "payment"},
            "selector_raw": '{"app":"payment"}',
        },
        "ports": [{"name": "http", "protocol": "TCP", "port": 80,
                   "target_port": 8080, "node_port": 0}],
        "pods": pods,
        "matching_note": matching_note or (
            "候选 Pod 来自 Controller/MySQL 资源清单的 selector 匹配，"
            "不等于 kube-apiserver 实时 EndpointSlice 的 Ready 后端，也不代表正在接流量"
        ),
        **extra,
    }


def pod(name, labels=None, state="Running"):
    return {
        "uid": f"uid-rcagent-payment-5xx-{name}",
        "name": name,
        "state": state,
        "node_id": 3,
        "namespace": "payments",
        "cluster": "prod-a",
        "matched_labels": labels or {"app": "payment"},
    }


def metrics_response(series, metric_key="error_rate", status="ok", **extra):
    return {
        "status": status,
        "data_source": "querier_prom",
        "warnings": [],
        "truncated": False,
        "freshness": "unknown",
        "metric_key": metric_key,
        "unit": "ratio",
        "query_range_mode": "range",
        "generated_query": "sum(rate(kylin_insights_l7_error_total[60s]))",
        "window": {"start": "2026-09-30T10:00:00+08:00",
                   "end": "2026-09-30T10:15:00+08:00", "step": "60s"},
        "series": series,
        "series_count": len(series),
        "point_count": sum(len(s["points"]) for s in series),
        "truncated_reason": "",
        **extra,
    }


def series(pod_name, value):
    return {
        "labels": {"pod": pod_name},
        "points": [
            {"t": "2026-09-30T10:14:00+08:00", "v": round(value * 0.5, 3)},
            {"t": "2026-09-30T10:15:00+08:00", "v": value},
        ],
    }


def traces_response(traces, status="ok", **extra):
    return {
        "status": status,
        "data_source": "querier_tempo",
        "warnings": [],
        "truncated": False,
        "freshness": "unknown",
        "service_tag": "resource.service.name",
        "tags_applied": ['resource.service.name="payment"', 'http.status_code="500"'],
        "traces": traces,
        "trace_count": len(traces),
        **extra,
    }


def trace(trace_id, status="error", duration_ms=3200.0):
    return {
        "trace_id": trace_id,
        "start_time": "2026-09-30T10:12:00+08:00",
        "duration_ms": duration_ms,
        "root_service": "payment",
        "root_span": "GET /pay",
        "span_count": 4,
        "status": status,
    }


def trace_detail_response(trace_id="4bf92f3577b34da6a3ce929d0e0e4736"):
    return {
        "status": "ok",
        "data_source": "querier_tempo",
        "warnings": [],
        "truncated": False,
        "freshness": "unknown",
        "trace_id": trace_id,
        "spans": [
            {"span_id": "a1", "parent_span_id": "", "service": "payment",
             "operation": "GET /pay", "start_time": "2026-09-30T10:12:00+08:00",
             "duration_ms": 3200.0, "status": "error", "depth": 0,
             "tags": {"http.status_code": "500"}},
            {"span_id": "b1", "parent_span_id": "a1", "service": "payment-db",
             "operation": "SELECT", "start_time": "2026-09-30T10:12:00+08:00",
             "duration_ms": 3000.0, "status": "error", "depth": 1, "tags": {}},
        ],
        "span_count": 2,
        "max_depth": 1,
        "critical_path": ["a1", "b1"],
        "truncated_reason": "",
    }


def happy_gateway(**overrides) -> FakeGateway:
    responses = {
        "search_k8s_targets": search_response([service_candidate()]),
        "get_service_backends": backends_response([
            pod("payment-7d9f-abcde"), pod("payment-7d9f-fghij"),
        ]),
        "query_metrics": metrics_response([
            series("payment-7d9f-abcde", 0.42),
            series("payment-7d9f-fghij", 0.01),
        ]),
        "search_traces": traces_response([
            trace("4bf92f3577b34da6a3ce929d0e0e4736", "error"),
            trace("0af7651916cd43dd8448eb211c80319c", "ok", 45.0),
        ]),
        "get_trace": trace_detail_response(),
    }
    responses.update(overrides)
    return FakeGateway(responses)


def scope_for(text=ACCEPTANCE_TEXT, **structured):
    return resolve_incident_scope({"incident_text": text, **structured})


def run_discovery(gateway: FakeGateway, **kwargs):
    return asyncio.run(discover_service_target(scope_for(**kwargs.pop("structured", {})),
                                               call_tool=gateway, **kwargs))


# --------------------------------------------------------------------------
# Scenario 1: unique target
# --------------------------------------------------------------------------


class SupportedScopeRules(unittest.TestCase):
    """Historical metric/trace interpretation with a hypothetical scoped API.

    Current Gateway capability rejection and Pod fallback are tested separately
    in test_live_fixes; this fixture explicitly overrides capability only.
    """
    def setUp(self):
        capability = mock.patch('tools.contracts.unsupported_scope', return_value=[])
        capability.start()
        self.addCleanup(capability.stop)


class UniqueTargetTest(SupportedScopeRules):
    def setUp(self) -> None:
        super().setUp()
        self.gateway = happy_gateway()
        self.outcome = run_discovery(self.gateway)

    def test_runs_the_full_discovery_chain_in_order(self) -> None:
        self.assertEqual(
            self.gateway.called_tools,
            [
                "search_k8s_targets",
                "get_service_backends",
                "query_metrics",
                "search_traces",
                "get_trace",
            ],
        )
        self.assertEqual(
            self.outcome.tool_calls,
            [
                "search_k8s_targets",
                "get_service_backends",
                "query_metrics:error_rate",
                "search_traces",
                "get_trace",
            ],
        )

    def test_status_is_ok_when_nothing_is_missing(self) -> None:
        self.assertEqual(self.outcome.status, DISCOVERY_OK)
        self.assertFalse(self.outcome.requires_confirmation)

    def test_attributes_the_anomalous_pod_from_metric_evidence(self) -> None:
        self.assertEqual(
            [entry["pod"] for entry in self.outcome.attributed_pods],
            ["payment-7d9f-abcde"],
        )
        self.assertEqual(self.outcome.attributed_pods[0]["epistemic_status"], "agent_inference")

    def test_reports_facts_separately_from_inferences(self) -> None:
        self.assertTrue(self.outcome.facts)
        self.assertTrue(self.outcome.inferences)
        self.assertTrue(
            all("mcp_tool:" in fact["source"] for fact in self.outcome.facts)
        )

    def test_ready_status_is_always_flagged_unknown(self) -> None:
        self.assertTrue(
            any(gap["reason"] == "ready_status_unknown" for gap in self.outcome.data_gaps)
        )
        self.assertEqual(self.outcome.backends["ready_status"], "unknown")

    def test_candidate_pods_are_not_reported_as_the_single_fault_target(self) -> None:
        note = self.outcome.service["resolved_from"]
        self.assertEqual(note, "search_k8s_targets")
        rendered = str(self.outcome.as_summary())
        self.assertNotIn("root cause", rendered.lower())

    def test_deepest_failing_span_is_reported_as_an_inference(self) -> None:
        self.assertTrue(
            any("payment-db" in item["statement"] for item in self.outcome.inferences)
        )


# --------------------------------------------------------------------------
# Scenario 3: two same-named Services -> ambiguous
# --------------------------------------------------------------------------


class AmbiguousTargetTest(SupportedScopeRules):
    def setUp(self) -> None:
        super().setUp()
        self.gateway = happy_gateway(
            search_k8s_targets=search_response(
                [
                    service_candidate("payment", "prod-a", "payments"),
                    service_candidate("payment", "staging", "payments"),
                ],
                status="ambiguous",
            )
        )
        self.outcome = run_discovery(self.gateway)

    def test_status_is_ambiguous_and_needs_confirmation(self) -> None:
        self.assertEqual(self.outcome.status, DISCOVERY_AMBIGUOUS)
        self.assertTrue(self.outcome.requires_confirmation)

    def test_returns_every_candidate_identity(self) -> None:
        self.assertEqual(len(self.outcome.candidates), 2)
        locations = {candidate["location"] for candidate in self.outcome.candidates}
        self.assertEqual(locations, {"prod-a/payments", "staging/payments"})

    def test_never_selects_the_first_candidate(self) -> None:
        self.assertIsNone(self.outcome.service)
        self.assertEqual(self.outcome.attributed_pods, [])
        self.assertEqual(self.outcome.metrics, [])
        self.assertEqual(self.outcome.traces, None)

    def test_no_pod_is_ever_investigated(self) -> None:
        self.assertNotIn("get_service_backends", self.gateway.called_tools)
        self.assertNotIn("query_metrics", self.gateway.called_tools)
        self.assertNotIn("search_traces", self.gateway.called_tools)

    def test_clarification_asks_for_scope_instead_of_guessing(self) -> None:
        clarification = self.outcome.clarification
        self.assertEqual(clarification["reason"], "ambiguous_target_candidates")
        self.assertTrue(clarification["question"])
        self.assertEqual(sorted(clarification["missing"]), ["cluster", "namespace"])

    def test_explicit_scope_resolves_the_ambiguity(self) -> None:
        scoped = happy_gateway()
        scoped.responses["search_k8s_targets"] = search_response([service_candidate()])
        outcome = asyncio.run(
            discover_service_target(
                scope_for(cluster="prod-a", namespace="payments"),
                call_tool=scoped,
            )
        )
        self.assertEqual(outcome.status, DISCOVERY_OK)
        self.assertEqual(outcome.service["cluster"], "prod-a")


# --------------------------------------------------------------------------
# Scenario 9 / failure paths
# --------------------------------------------------------------------------


class NotFoundTest(unittest.TestCase):
    def test_no_candidates_is_not_found(self) -> None:
        gateway = happy_gateway(search_k8s_targets=search_response([], status="not_found"))
        outcome = run_discovery(gateway)
        self.assertEqual(outcome.status, DISCOVERY_NOT_FOUND)
        self.assertTrue(
            any(gap["reason"] == "not_found" for gap in outcome.data_gaps)
        )

    def test_not_found_never_claims_the_service_is_healthy(self) -> None:
        """The word may only appear inside an explicit denial of health."""
        gateway = happy_gateway(search_k8s_targets=search_response([], status="not_found"))
        outcome = run_discovery(gateway)
        for statement in [f["statement"] for f in outcome.facts] + [
            i["statement"] for i in outcome.inferences
        ]:
            self.assertNotIn("healthy", statement.lower())
        gap_text = " ".join(gap["impact"] for gap in outcome.data_gaps).lower()
        self.assertIn("says nothing about whether the service is healthy", gap_text)
        self.assertEqual(outcome.attributed_pods, [])

    def test_not_found_text_does_not_claim_health(self) -> None:
        gateway = happy_gateway(search_k8s_targets=search_response([], status="not_found"))
        outcome = run_discovery(gateway)
        gap_text = " ".join(gap["impact"] for gap in outcome.data_gaps).lower()
        self.assertIn("nothing about", gap_text)


class MissingToolTest(unittest.TestCase):
    """The five tools may not be published yet; that must be an explicit gap."""

    def test_missing_tool_is_an_error_with_a_gap(self) -> None:
        gateway = happy_gateway(search_k8s_targets=tool_not_published("search_k8s_targets"))
        outcome = run_discovery(gateway)
        self.assertEqual(outcome.status, DISCOVERY_ERROR)
        self.assertTrue(
            any(gap["reason"] == "tool_not_published" for gap in outcome.data_gaps)
        )

    def test_missing_tool_is_never_reported_as_healthy(self) -> None:
        gateway = happy_gateway(search_k8s_targets=tool_not_published("search_k8s_targets"))
        outcome = run_discovery(gateway)
        impacts = " ".join(gap["impact"] for gap in outcome.data_gaps)
        self.assertIn("not evidence that the target is healthy", impacts)

    def test_raising_tool_is_classified_without_crashing(self) -> None:
        def boom(_args):
            raise RuntimeError("Connection refused by gateway")

        gateway = happy_gateway(search_k8s_targets=boom)
        # The fake raises rather than returning, so simulate the adapter contract.
        outcome = asyncio.run(
            discover_service_target(
                scope_for(),
                call_tool=lambda name, args: _adapter_like_call(gateway, name, args),
            )
        )
        self.assertEqual(outcome.status, DISCOVERY_ERROR)
        self.assertTrue(
            any(gap["reason"] == "gateway_unreachable" for gap in outcome.data_gaps)
        )


async def _adapter_like_call(gateway, name, args):
    try:
        return await gateway(name, args)
    except Exception as exc:
        return classify_tool_error(name, exc)


# --------------------------------------------------------------------------
# Scenarios 4, 5, 6, 7, 8: missing observation data
# --------------------------------------------------------------------------


class MissingObservationTest(SupportedScopeRules):
    def test_selector_without_matching_pods_is_a_documented_caveat(self) -> None:
        gateway = happy_gateway(
            get_service_backends=backends_response(
                [],
                matching_note=(
                    "该 Service 没有 selector 或 selector 无法映射到 Pod；"
                    "无法按 selector 映射 Pod，不得解释为“Service 没有后端”或“服务已宕机”"
                ),
            )
        )
        outcome = run_discovery(gateway)
        self.assertTrue(
            any(gap["reason"] == "no_selector_match" for gap in outcome.data_gaps)
        )
        note = next(
            gap["impact"] for gap in outcome.data_gaps if gap["reason"] == "no_selector_match"
        )
        self.assertIn("无法按 selector 映射 Pod", note)
        self.assertIn("服务已宕机", note)

    def test_truncated_backends_are_reported(self) -> None:
        gateway = happy_gateway(
            get_service_backends=backends_response(
                [pod("payment-7d9f-abcde")], status="partial", truncated=True,
                truncated_reason="pod_scan_limit reached",
            )
        )
        outcome = run_discovery(gateway)
        self.assertTrue(any(gap["reason"] == "truncated" for gap in outcome.data_gaps))
        self.assertEqual(outcome.status, DISCOVERY_PARTIAL)

    def test_empty_metrics_are_a_gap_not_good_health(self) -> None:
        gateway = happy_gateway(query_metrics=metrics_response([]))
        outcome = run_discovery(gateway)
        gap = next(g for g in outcome.data_gaps if g["reason"] == "empty_result")
        self.assertIn("must not be read as the service being healthy", gap["impact"])
        self.assertEqual(outcome.attributed_pods, [])

    def test_empty_traces_are_a_gap_not_good_health(self) -> None:
        gateway = happy_gateway(search_traces=traces_response([]))
        outcome = run_discovery(gateway)
        impacts = " ".join(g["impact"] for g in outcome.data_gaps)
        self.assertIn("does not mean the service has no fault", impacts)

    def test_no_anomalous_pod_means_no_pod_is_named(self) -> None:
        """With several healthy Pods, Triage must not fall back to the first one."""
        gateway = happy_gateway(
            query_metrics=metrics_response([
                series("payment-7d9f-abcde", 0.01),
                series("payment-7d9f-fghij", 0.01),
            ])
        )
        outcome = run_discovery(gateway)
        self.assertEqual(outcome.attributed_pods, [])
        self.assertEqual(
            [p["name"] for p in outcome.backends["pods"]],
            ["payment-7d9f-abcde", "payment-7d9f-fghij"],
        )

    def test_missing_metric_template_is_reported_as_unsupported(self) -> None:
        gateway = happy_gateway(
            query_metrics={
                "status": "error",
                "error_type": "unsupported_metric",
                "warnings": ["supported: request_rate, error_rate, latency_p95"],
            }
        )
        outcome = run_discovery(gateway)
        self.assertIn(outcome.status, (DISCOVERY_ERROR, DISCOVERY_PARTIAL))
        self.assertTrue(outcome.data_gaps)


class LatencySymptomTest(SupportedScopeRules):
    def test_latency_symptom_also_queries_latency_p95(self) -> None:
        gateway = happy_gateway()
        outcome = asyncio.run(
            discover_service_target(
                scope_for("payment 服务最近 15 分钟延迟很高"),
                call_tool=gateway,
            )
        )
        metric_keys = [call for call in outcome.tool_calls if call.startswith("query_metrics")]
        self.assertEqual(
            metric_keys, ["query_metrics:error_rate", "query_metrics:latency_p95"]
        )

    def test_http_5xx_symptom_does_not_query_latency(self) -> None:
        gateway = happy_gateway()
        outcome = run_discovery(gateway)
        metric_keys = [call for call in outcome.tool_calls if call.startswith("query_metrics")]
        self.assertEqual(metric_keys, ["query_metrics:error_rate"])


class InsufficientInputTest(unittest.TestCase):
    def test_no_target_is_insufficient_and_queries_nothing(self) -> None:
        gateway = happy_gateway()
        outcome = asyncio.run(
            discover_service_target(scope_for("帮我看看"), call_tool=gateway)
        )
        self.assertEqual(outcome.status, "insufficient")
        self.assertTrue(outcome.requires_confirmation)
        self.assertEqual(gateway.called_tools, [])
        self.assertEqual(outcome.tool_calls, [])

    def test_insufficient_result_explains_itself(self) -> None:
        gateway = happy_gateway()
        outcome = asyncio.run(
            discover_service_target(scope_for(""), call_tool=gateway)
        )
        self.assertEqual(outcome.clarification["reason"], "insufficient_information")
        self.assertTrue(outcome.clarification["missing"])


class DirectPodTargetTest(unittest.TestCase):
    def test_single_pod_candidate_skips_service_expansion(self) -> None:
        gateway = happy_gateway(
            search_k8s_targets=search_response(
                [{"kind": "pod", "name": "checkout-0", "uid": "uid-b01-http-5xx-checkout-0",
                  "id": 7, "cluster": "test-cluster", "namespace": "b01-http-5xx",
                  "match": "exact", "match_field": "name"}]
            )
        )
        outcome = asyncio.run(
            discover_service_target(
                scope_for("checkout-0 这个 pod 报 500"), call_tool=gateway
            )
        )
        self.assertNotIn("get_service_backends", gateway.called_tools)
        self.assertEqual(outcome.attributed_pods[0]["pod"], "checkout-0")

    def test_multiple_pod_candidates_are_ambiguous(self) -> None:
        gateway = happy_gateway(
            search_k8s_targets=search_response([
                {"kind": "pod", "name": "checkout-0", "uid": "u1", "id": 7,
                 "cluster": "prod-a", "namespace": "checkout", "match": "fuzzy"},
                {"kind": "pod", "name": "checkout-1", "uid": "u2", "id": 8,
                 "cluster": "prod-a", "namespace": "checkout", "match": "fuzzy"},
            ])
        )
        outcome = asyncio.run(
            discover_service_target(
                scope_for("checkout pod 报 500"), call_tool=gateway
            )
        )
        self.assertEqual(outcome.status, DISCOVERY_AMBIGUOUS)
        self.assertNotIn("get_service_backends", gateway.called_tools)


# --------------------------------------------------------------------------
# Adapter-level regressions for the preserved structured path
# --------------------------------------------------------------------------


class StructuredPathRegressionTest(unittest.TestCase):
    """PRD §10.3: the original structured Pod path must keep working."""

    def test_target_from_state_returns_the_original_shape(self) -> None:
        target = target_from_state({
            "target_cluster": "test-cluster",
            "target_namespace": "b01-http-5xx",
            "target_resource": "checkout-0",
        })
        self.assertEqual(
            target,
            {"cluster": "test-cluster", "namespace": "b01-http-5xx", "pod": "checkout-0"},
        )

    def test_target_from_state_still_raises_when_incomplete(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            target_from_state({"target_cluster": "test-cluster"})
        message = str(ctx.exception)
        self.assertIn("target_namespace", message)
        self.assertIn("target_resource", message)

    def test_target_from_state_ignores_incident_text(self) -> None:
        """Parsed text must never satisfy the structured contract."""
        with self.assertRaises(ValueError):
            target_from_state({"incident_text": ACCEPTANCE_TEXT})

    def test_call_gateway_tool_reports_an_unpublished_tool(self) -> None:
        result = asyncio.run(_call_with_no_tools("search_k8s_targets", {}))
        self.assertEqual(result["error_type"], "tool_not_published")
        self.assertIn("error", result)

    def test_error_classification_distinguishes_timeout(self) -> None:
        result = classify_tool_error("query_metrics", asyncio.TimeoutError())
        self.assertEqual(result["error_type"], "timeout")
        self.assertEqual(result["status"], "error")

    def test_error_classification_distinguishes_unreachable_gateway(self) -> None:
        result = classify_tool_error("query_metrics", RuntimeError("Connection refused"))
        self.assertEqual(result["error_type"], "gateway_unreachable")

    def test_unloadable_tool_registry_is_not_reported_as_a_tool_failure(self) -> None:
        """If the tool list cannot load, no RPC happened -- say exactly that."""
        from tools import observability_adapter

        def boom():
            raise RuntimeError("Gateway handshake failed")

        with mock.patch.object(observability_adapter, "_tools_by_name", boom):
            result = asyncio.run(call_gateway_tool("search_k8s_targets", {}))

        self.assertEqual(result["error_type"], "gateway_unreachable")
        self.assertEqual(result["data_gaps"][0]["source"], "mcp_gateway")
        self.assertIn("never called", result["warnings"][0])


async def _call_with_no_tools(name, args):
    """Call the real adapter with an empty tool registry (no Gateway)."""
    from tools import observability_adapter

    original = observability_adapter._TOOLS_BY_NAME
    observability_adapter._TOOLS_BY_NAME = {}
    try:
        return await call_gateway_tool(name, args)
    finally:
        observability_adapter._TOOLS_BY_NAME = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
