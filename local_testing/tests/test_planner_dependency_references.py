"""Externalizing dependency facts must not turn them into an absence claim."""

import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from models.schemas import RCATaskList, Symptom


class PlannerDependencyReferences(unittest.IsolatedAsyncioTestCase):
    async def test_archived_dependencies_are_referenced_without_false_absence(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario(
            initial_evidence=True,
            raw={"dependencies": [{"name": "redis"}], "data_gaps": ["CoreDNS runtime not checked"]},
        )
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        messages = next(messages for role, phase, messages in model.calls if role == "planner")
        text = "\n".join(str(m.content) for m in messages)
        self.assertIn(rows["evidence"][0]["id"], text)
        self.assertIn("CoreDNS runtime not checked", text)
        self.assertIn("redis", text)
        self.assertNotIn("No data dependencies found", text)
        self.assertNotIn("No infrastructure dependencies found", text)
        self.assertTrue(rows["task"])
