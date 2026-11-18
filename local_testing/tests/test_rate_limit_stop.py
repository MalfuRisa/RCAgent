"""R0 behaviour: one 429 stops the whole run (PRD §19.10).

The tests observe real sends: how many times the model was actually invoked,
what the run record and events say, whether a queued request was terminated
before sending, whether the Supervisor still calls the model, and that another
incident's run is untouched. They never assert a private helper name.
"""

import asyncio
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from config import settings
from workspace import run_stops
from workspace.execution import INTERNAL_REASON_STATES, internal_reason_state, normalize_execution
from workspace.model_requests import ModelRequestError
from workspace.repository import WorkspaceError
from workspace.runtime import bind, invoke_model
from test_workspace import WorkspaceCase


class RateLimit(Exception):
    status_code = 429
    response = type("Response", (), {"headers": {}})()


class Unavailable(Exception):
    status_code = 503
    response = type("Response", (), {"headers": {}})()


class CountingModel:
    """Records every real invocation; fails the first `limit_failures` calls."""

    def __init__(self, failure=None, limit_failures=1):
        self.calls = []
        self.failure = failure
        self.limit_failures = limit_failures

    def runnable(self):
        async def call(messages):
            self.calls.append(messages)
            if self.failure is not None and len(self.calls) <= self.limit_failures:
                raise self.failure
            return AIMessage(content="ok")

        return RunnableLambda(call)


class StopRunTests(WorkspaceCase):
    async def test_rate_limit_stops_the_run_and_sends_nothing_more(self):
        self.assertEqual(settings.RATE_LIMIT_POLICY, "stop_run", "new paths default to stop_run")
        model = CountingModel(failure=RateLimit("account request limit"))
        with bind(self.b):
            with self.assertRaises(ModelRequestError) as caught:
                await invoke_model(model.runnable(), [])
            self.assertEqual(caught.exception.detail["code"], "model_rate_limited")
            self.assertEqual(caught.exception.detail["http_status"], 429)
            # A second call in the same run must not reach the model at all.
            with self.assertRaises(ModelRequestError) as second:
                await invoke_model(model.runnable(), [])
            self.assertEqual(second.exception.detail["code"], "model_rate_limited")
        self.assertEqual(
            len(model.calls), 1, "the rate-limited request is not retried and nothing else is sent"
        )
        attempts = await self.b.repository.all(self.b.incident_id, "model_attempt")
        sent = [row for row in attempts if row.get("send_started_at")]
        self.assertEqual(len(sent), 1, "exactly one request was ever sent")
        self.assertTrue(all(row["error_type"] == "model_rate_limited" for row in attempts))
        self.assertTrue(
            all(row["retry_wait_seconds"] is None for row in attempts),
            "no retry wait is scheduled for a rate limit",
        )
        blocked = [row for row in attempts if not row.get("send_started_at")]
        self.assertTrue(blocked, "the request refused after the stop is still audited")
        run = await self.b.repository.read(self.b.incident_id, self.b.run_id, "run")
        self.assertEqual(run["stop_reason"], "model_rate_limited")
        events = await self.b.repository.transact(
            lambda db: [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT body FROM events WHERE incident=?", (self.b.incident_id,)
                )
            ]
        )
        stopped = [event for event in events if event.get("reason") == "model_rate_limited"]
        self.assertTrue(stopped, "the stop is audited with its reason")
        self.assertEqual(stopped[0]["rate_limit_policy"], "stop_run")
        calls = await self.b.repository.all(self.b.incident_id, "model_call")
        self.assertTrue(all(row.get("rate_limit_policy") == "stop_run" for row in calls))

    async def test_queued_request_is_terminated_before_sending(self):
        model = CountingModel(failure=RateLimit("burst"), limit_failures=1)
        with (
            patch.object(settings, "MODEL_MAX_IN_FLIGHT", 1),
            patch.object(settings, "MODEL_MIN_INTERVAL_SECONDS", 0),
        ):
            with bind(self.b):
                results = await asyncio.gather(
                    invoke_model(model.runnable(), []),
                    invoke_model(model.runnable(), []),
                    return_exceptions=True,
                )
        self.assertEqual(
            len(model.calls), 1, "the queued request never reaches the model after the 429"
        )
        codes = sorted(
            getattr(item, "detail", {}).get("code")
            for item in results
            if isinstance(item, ModelRequestError)
        )
        self.assertEqual(codes, ["model_rate_limited", "model_rate_limited"])
        attempts = await self.b.repository.all(self.b.incident_id, "model_attempt")
        sent = [row for row in attempts if row.get("send_started_at")]
        self.assertEqual(len(sent), 1, "only one attempt was ever sent")

    async def test_supervisor_and_fallback_do_not_call_the_model_after_the_stop(self):
        from agents.supervisor_agent import supervisor_agent

        await self.begin_task()
        self.b.role = "supervisor"
        model = CountingModel()
        stub = type(
            "Stub",
            (),
            {
                "bind_tools": lambda _self, tools, **kwargs: model.runnable(),
                "with_structured_output": lambda _self, schema, **kwargs: model.runnable(),
            },
        )()
        run_stops.stop(self.b, "model_rate_limited")
        with bind(self.b), patch("config.settings.get_chat_model", return_value=stub):
            update = await supervisor_agent({})
        self.assertEqual(model.calls, [], "no model request is made once the run is stopped")
        self.assertEqual(update["execution_status"], "failed")
        self.assertEqual(update["execution_error"]["code"], "model_rate_limited")
        self.assertTrue(
            update["final_report"].get("fallback"), "the program report is marked as fallback"
        )

    async def test_parent_flow_stops_at_the_first_429_under_the_default_policy(self):
        """R2: preserve this safety outcome through the one current role contract."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(
            fault="planner_429", initial_evidence=True
        )
        self.assertEqual(sum(role == "planner" for role, phase, messages in model.calls), 1)
        self.assertFalse(any(role == "worker" for role, phase, messages in model.calls))
        self.assertEqual(state["execution_error"]["code"], "model_rate_limited")
        self.assertTrue(rows["evidence"])
        self.assertTrue(any(e["action"] == "run_stopped" for e in events))
        self.assertEqual(rows["run"][0]["stop_reason"], "model_rate_limited")
        self.assertFalse([a for a in rows["model_attempt"] if a.get("retry_wait_seconds")])

    async def test_another_run_is_unaffected(self):
        from workspace.repository import Repository
        from workspace.runtime import initialize_incident
        from pathlib import Path

        other = await asyncio.to_thread(lambda: None)  # keep the flow readable
        with patch.object(settings, "WORKSPACE_ROOT", Path(self.temp.name)):
            state = await initialize_incident(
                {"target_cluster": "c", "target_namespace": "app", "target_resource": "iam"}
            )
        from workspace.runtime import Binding
        import time

        second = Binding(
            Repository(self.temp.name),
            state["incident_id"],
            state["run_id"],
            "worker",
            time.time() + 60,
        )
        run_stops.stop(self.b, "model_rate_limited")
        model = CountingModel()
        with bind(second):
            await invoke_model(model.runnable(), [])
        self.assertEqual(len(model.calls), 1, "a stop belongs to one run only")

    async def test_connection_retry_contract_is_kept_and_legacy_policy_retries_429(self):
        model = CountingModel(failure=Unavailable("temporary"), limit_failures=1)
        with patch.object(settings, "MODEL_MIN_INTERVAL_SECONDS", 0), bind(self.b):
            await invoke_model(model.runnable(), [])
        self.assertEqual(
            len(model.calls), 2, "a 503 is still retried once under the default policy"
        )
        run_stops.clear(self.b)
        legacy = CountingModel(failure=RateLimit("legacy retry"), limit_failures=2)
        with (
            patch.object(settings, "RATE_LIMIT_POLICY", "legacy"),
            patch.object(settings, "MODEL_MIN_INTERVAL_SECONDS", 0),
            bind(self.b),
        ):
            await invoke_model(legacy.runnable(), [])
        self.assertEqual(len(legacy.calls), 3, "the legacy policy keeps its bounded 429 retry")
        policy_calls = await self.b.repository.all(self.b.incident_id, "model_call")
        self.assertTrue(
            any(row.get("rate_limit_policy") == "legacy" for row in policy_calls),
            "the policy actually used is recorded on the call",
        )

    async def test_sdk_client_does_not_resend_a_429_by_itself(self):
        model = settings._build_chat_model("test-model")
        self.assertEqual(
            model.max_retries,
            0,
            "the SDK must not quietly resend before the controller sees the 429",
        )

    async def test_internal_reasons_map_to_existing_states(self):
        legal_execution = {"incomplete", "failed", "completed", "cancelled"}
        legal_answers = {"inconclusive", "supported", "contradicted", "unanswered"}
        for code, row in INTERNAL_REASON_STATES.items():
            self.assertIn(row["execution_status"], legal_execution, code)
            self.assertIn(row["answer_status"], legal_answers, code)
            normalized = normalize_execution(
                "failed", error={"code": code}, stop_reason="model_timeout"
            )
            self.assertEqual(
                normalized["stop_reason"], code, "the internal reason is the first cause"
            )
            self.assertIn("model_timeout", normalized["secondary_stop_reasons"])
            self.assertEqual(
                internal_reason_state(code)["execution_status"], row["execution_status"]
            )
        self.assertEqual(internal_reason_state("something_new")["execution_status"], "failed")

    async def test_a_timeout_keeps_its_tokens_unknown_and_does_not_override_the_first_cause(self):
        async def slow(messages):
            await asyncio.sleep(5)
            return AIMessage(content="late")

        with patch.object(settings, "MODEL_TIMEOUT_SECONDS", 1), bind(self.b):
            with self.assertRaises(ModelRequestError) as caught:
                await invoke_model(RunnableLambda(slow), [])
        self.assertEqual(caught.exception.detail["code"], "model_timeout")
        attempts = await self.b.repository.all(self.b.incident_id, "model_attempt")
        self.assertIsNone(attempts[-1]["token_usage"], "an unknown token count stays null")
        self.assertEqual(attempts[-1]["usage_status"], "unavailable")
