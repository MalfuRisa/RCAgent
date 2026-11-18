"""Triage entry-point routing tests.

Verifies the three input paths required by PRD §7 / §10.3 at the Triage agent
level itself:

1. structured Pod input -> the original path, with discovery never invoked;
2. ``incident_text`` only -> Gateway target discovery;
3. ambiguous or too-thin input -> stop and ask, never guess.

The Gateway and the model are both replaced by fakes, so this needs no network.

Run from the RCAgent repository root::

    python -m unittest discover -s local_testing/tests -t local_testing/tests -v
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# config/ builds ChatOpenAI clients at import time. These tests never call a
# model, so a placeholder key keeps construction working without any secret.
os.environ.setdefault("OPENAI_API_KEY", "test-placeholder-not-a-real-key")
os.environ.setdefault("RCAGENT_MCP_MODE", "kylin_gateway")
os.environ.setdefault("MCP_GATEWAY_URL", "http://127.0.0.1:1/mcp")


from tools import observability_adapter  # noqa: E402


def _load_triage_module():
    from agents import triage_agent as module

    return module


triage = _load_triage_module()


SERVICE_CANDIDATE = {
    "kind": "service",
    "name": "payment",
    "uid": "svc-prod-a-payments-payment",
    "id": 12,
    "cluster": "prod-a",
    "namespace": "payments",
    "match": "exact",
    "type": "ClusterIP",
    "cluster_ip": "10.96.0.11",
}


class RaisingModel:
    """Fails loudly if Triage tries to extract symptoms from an unresolved target."""

    def with_structured_output(self, _schema):
        raise AssertionError("the symptom model must not run for an unresolved target")

    def __getattr__(self, item):
        raise AssertionError(f"the symptom model must not be used ({item})")


class FakeGateway:
    def __init__(self, responses: dict) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, name: str, arguments) -> dict:
        self.calls.append((name, dict(arguments)))
        response = self.responses.get(name)
        if response is None:
            raise AssertionError(f"unexpected Gateway tool call: {name}")
        return response(dict(arguments)) if callable(response) else response

    @property
    def called_tools(self) -> list[str]:
        return [name for name, _ in self.calls]


def base_state(**overrides):
    state = {
        "app_name": "RCAgent routing test",
        "app_summary": "incident under test",
        "target_namespace": "",
        "target_cluster": "",
        "target_resource": "",
        "trace_service_starting_point": "",
        "incident_text": "",
        "problematic_pods": {},
        "slow_traces": {},
        "problematic_metrics": {},
        "problematic_traces": {},
        "symptoms": [],
        "prompts_config": {},
        "triage_status": "",
        "clarification": {},
        "discovery_record": {},
    }
    state.update(overrides)
    return state


class StructuredInputPrecedenceTest(unittest.TestCase):
    """PRD §7: structured fields with explicit values are preserved."""

    def setUp(self) -> None:
        self.gateway = FakeGateway(
            {
                "resolve_pod": {
                    "ambiguous": False,
                    "pods": [
                        {
                            "pod_uid": "uid-b01-http-5xx-checkout-0",
                            "name": "checkout-0",
                            "namespace": "b01-http-5xx",
                            "cluster": "test-cluster",
                        }
                    ],
                },
                "get_pod_runtime_status": {"status": "ok"},
                "get_pod_network_summary": {"status": "ok", "summary": {}},
                "get_pod_application_summary": {"status": "ok"},
            }
        )

    def _gather(self, state):
        with mock.patch.object(observability_adapter, "call_gateway_tool", self.gateway):
            return asyncio.run(triage.get_triage_data(state))

    def test_structured_pod_wins_over_parsed_service(self) -> None:
        state = base_state(
            incident_text="payment 服务在 staging 集群 payments 命名空间报 500",
            target_cluster="test-cluster",
            target_namespace="b01-http-5xx",
            target_resource="checkout-0",
        )
        result = self._gather(state)

        self.assertEqual(result["problematic_pods"]["target"]["pod"], "checkout-0")
        self.assertEqual(result["problematic_pods"]["target"]["cluster"], "test-cluster")
        self.assertNotIn("search_k8s_targets", self.gateway.called_tools)
        self.assertIn("resolve_pod", self.gateway.called_tools)

    def test_structured_path_keeps_views_and_program_status(self) -> None:
        state = base_state(
            target_cluster="test-cluster",
            target_namespace="b01-http-5xx",
            target_resource="checkout-0",
        )
        result = self._gather(state)

        self.assertEqual(
            sorted(result),
            [
                "evidence_status",
                "problematic_metrics",
                "problematic_pods",
                "problematic_traces",
                "slow_traces",
                "triage_status",
            ],
        )
        self.assertEqual(result["triage_status"], "discovered")
        self.assertEqual(result["evidence_status"], "available")
        self.assertNotIn("discovery_record", result)

    def test_structured_incomplete_still_raises_the_original_error(self) -> None:
        state = base_state(target_cluster="test-cluster")
        result = self._gather(state)
        self.assertIn("error", result["problematic_pods"])
        self.assertIn("target_namespace", result["problematic_pods"]["error"])
        self.assertEqual(self.gateway.called_tools, [])

    def test_no_incident_text_never_calls_discovery_tools(self) -> None:
        state = base_state(
            target_cluster="test-cluster",
            target_namespace="b01-http-5xx",
            target_resource="checkout-0",
        )
        self._gather(state)
        for tool in observability_adapter.DISCOVERY_TOOL_NAMES:
            self.assertNotIn(tool, self.gateway.called_tools)


class DiscoveryInputTest(unittest.TestCase):
    """PRD §10.3 case 1: natural language with a unique target."""

    def _gateway(self, ambiguous: bool = False) -> FakeGateway:
        candidates = [SERVICE_CANDIDATE]
        if ambiguous:
            candidates.append(
                {
                    **SERVICE_CANDIDATE,
                    "uid": "svc-staging-payments-payment",
                    "id": 13,
                    "cluster": "staging",
                }
            )
        return FakeGateway(
            {
                "get_pod_runtime_status": {"status": "ok", "containers": []},
                "get_pod_network_summary": {"status": "ok", "summary": {"tcp_errors": 2}},
                "get_pod_application_summary": {"status": "ok", "summary": {"error_ratio": 0.42}},
                "search_k8s_targets": {
                    "status": "ambiguous" if ambiguous else "ok",
                    "candidates": candidates,
                    "candidates_by_kind": {"service": len(candidates), "pod": 0},
                    "warnings": [],
                    "truncated": False,
                },
                "get_service_backends": {
                    "status": "ok",
                    "ready_status": "unknown",
                    "pods": [
                        {
                            "name": "payment-7d9f-abcde",
                            "uid": "u1",
                            "cluster": "prod-a",
                            "namespace": "payments",
                            "state": "Running",
                        }
                    ],
                    "service": {"name": "payment", "selector_raw": '{"app":"payment"}'},
                    "matching_basis": "inventory_selector",
                    "warnings": [],
                    "truncated": False,
                },
                "query_metrics": {
                    "status": "ok",
                    "metric_key": "error_rate",
                    "unit": "ratio",
                    "warnings": [],
                    "truncated": False,
                    "series": [
                        {
                            "labels": {"pod": "payment-7d9f-abcde"},
                            "points": [{"t": "2026-09-30T10:15:00+08:00", "v": 0.42}],
                        }
                    ],
                },
                "search_traces": {
                    "status": "ok",
                    "warnings": [],
                    "truncated": False,
                    "trace_count": 0,
                    "traces": [],
                    "service_tag": "resource.service.name",
                },
            }
        )

    def test_incident_text_drives_discovery(self) -> None:
        gateway = self._gateway()
        state = base_state(incident_text="payment 服务最近 15 分钟一直报 500，帮我看看")
        with mock.patch.object(observability_adapter, "call_gateway_tool", gateway):
            result = asyncio.run(triage.get_triage_data(state))

        self.assertIn("search_k8s_targets", gateway.called_tools)
        self.assertIn("get_service_backends", gateway.called_tools)
        self.assertEqual(result["triage_status"], "discovered")
        record = result["discovery_record"]
        self.assertTrue(record["facts"])
        self.assertEqual(record["inferences"], [])
        self.assertEqual(record["attributed_pods"], [])
        self.assertEqual(record["coverage"]["checked"][0]["pod"]["name"], "payment-7d9f-abcde")
        self.assertNotIn("query_metrics", gateway.called_tools)
        self.assertNotIn("search_traces", gateway.called_tools)

    def test_discovery_records_data_gaps_for_empty_traces(self) -> None:
        gateway = self._gateway()
        state = base_state(incident_text="payment 服务最近 15 分钟一直报 500")
        with mock.patch.object(observability_adapter, "call_gateway_tool", gateway):
            result = asyncio.run(triage.get_triage_data(state))
        gaps = result["discovery_record"]["data_gaps"]
        self.assertTrue(any(gap["reason"] == "unsupported_scope" for gap in gaps))
        self.assertNotIn("search_traces", gateway.called_tools)


class AmbiguousTargetTest(unittest.TestCase):
    """PRD §10.3 case 2: two same-named Services -> ask, do not pick."""

    def _gateway(self) -> FakeGateway:
        return FakeGateway(
            {
                "search_k8s_targets": {
                    "status": "ambiguous",
                    "candidates": [
                        SERVICE_CANDIDATE,
                        {
                            **SERVICE_CANDIDATE,
                            "uid": "svc-staging-payments-payment",
                            "id": 13,
                            "cluster": "staging",
                        },
                    ],
                    "candidates_by_kind": {"service": 2, "pod": 0},
                    "warnings": [],
                    "truncated": False,
                },
                # Deliberately absent: any of these being called means a Pod was chosen.
            }
        )

    def test_ambiguous_sets_awaiting_confirmation(self) -> None:
        gateway = self._gateway()
        state = base_state(incident_text="payment 服务最近 15 分钟一直报 500")
        with mock.patch.object(observability_adapter, "call_gateway_tool", gateway):
            result = asyncio.run(triage.get_triage_data(state))

        self.assertEqual(result["triage_status"], "awaiting_confirmation")
        self.assertTrue(result["clarification"]["question"])

    def test_no_pod_is_ever_selected(self) -> None:
        gateway = self._gateway()
        state = base_state(incident_text="payment 服务最近 15 分钟一直报 500")
        with mock.patch.object(observability_adapter, "call_gateway_tool", gateway):
            asyncio.run(triage.get_triage_data(state))

        self.assertEqual(gateway.called_tools, ["search_k8s_targets"])
        self.assertNotIn("get_service_backends", gateway.called_tools)

    def test_ambiguous_is_not_investigated_by_the_planner(self) -> None:
        gateway = self._gateway()
        state = base_state(incident_text="payment 服务最近 15 分钟一直报 500")
        with mock.patch.object(observability_adapter, "call_gateway_tool", gateway):
            gathered = asyncio.run(triage.get_triage_data(state))

        resolved = {**state, **gathered}
        with mock.patch.object(triage, "GPT5_MINI", RaisingModel()):
            outcome = asyncio.run(triage.triage_agent(resolved))

        self.assertEqual(outcome["symptoms"], [])
        self.assertEqual(outcome["triage_status"], "awaiting_confirmation")

    def test_router_ends_the_run_while_unresolved(self) -> None:
        self.assertEqual(
            triage.triage_router({"triage_status": "awaiting_confirmation"}),
            "needs_clarification",
        )
        self.assertEqual(
            triage.triage_router({"triage_status": "insufficient"}),
            "needs_clarification",
        )

    def test_router_continues_for_resolved_targets(self) -> None:
        self.assertEqual(triage.triage_router({"triage_status": "discovered"}), "investigate")
        # The legacy structured path emits no triage_status and must still run.
        self.assertEqual(triage.triage_router({}), "investigate")


class InsufficientInputTest(unittest.TestCase):
    """PRD §10.3 case 4: say "not enough information", never guess."""

    def test_chatter_only_input_asks_for_a_target(self) -> None:
        gateway = FakeGateway({})
        state = base_state(incident_text="帮我看看")
        with mock.patch.object(observability_adapter, "call_gateway_tool", gateway):
            result = asyncio.run(triage.get_triage_data(state))

        self.assertEqual(result["triage_status"], "insufficient")
        self.assertEqual(gateway.called_tools, [])
        self.assertEqual(result["clarification"]["reason"], "insufficient_information")
        self.assertIn("service", result["clarification"]["missing"])

    def test_insufficient_is_not_investigated(self) -> None:
        gateway = FakeGateway({})
        state = base_state(incident_text="帮我看看")
        with mock.patch.object(observability_adapter, "call_gateway_tool", gateway):
            gathered = asyncio.run(triage.get_triage_data(state))

        with mock.patch.object(triage, "GPT5_MINI", RaisingModel()):
            outcome = asyncio.run(triage.triage_agent({**state, **gathered}))

        self.assertEqual(outcome["symptoms"], [])
        self.assertEqual(outcome["triage_status"], "insufficient")

    def test_empty_incident_text_falls_back_to_the_structured_path(self) -> None:
        """No text at all keeps the original error semantics."""
        gateway = FakeGateway({})
        state = base_state(incident_text="")
        with mock.patch.object(observability_adapter, "call_gateway_tool", gateway):
            result = asyncio.run(triage.get_triage_data(state))

        self.assertIn("error", result["problematic_pods"])
        self.assertEqual(result["triage_status"], "insufficient")
        self.assertEqual(result["evidence_status"], "unavailable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
