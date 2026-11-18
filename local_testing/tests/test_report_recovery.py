"""Finalization uses verified stages and a packet within a finite model budget."""
from unittest.mock import patch
import json
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from config import settings
from agents.report_recovery import recover_report
from workspace.runtime import bind
from workspace.repository import WorkspaceError
from test_investigation_contracts import ActiveWorkspaceCase
import test_stage_repair as repair_support
from local_testing.runtime_review_model import RuntimeReviewer
from local_testing.long_investigation_model import workbench


class ReportRecoveryTests(ActiveWorkspaceCase):
    async def prepare_source(self):
        await self.begin_task()
        self.b.task_id = self.b.attempt_id = None
        await repair_support.RepairTests.setup_proposal(self, role='supervisor')

    async def test_no_registered_observations_can_commit_then_submit_packet(self):
        await self.prepare_source()
        seen = []
        from agents.report_recovery import copyable_fields
        pool = await copyable_fields(self.b)
        self.assertTrue(any(row.get('expected_value') == 3 for row in pool), repr(pool))
        class Reviewer(RuntimeReviewer):
            def bind_tools(self, tools, **kwargs):
                base = super().bind_tools(tools, **kwargs)
                async def answer(messages):
                    seen.extend(message.content for message in messages if message.type == 'tool')
                    view = workbench(messages)
                    if view['investigation']['observations']:
                        return await base.ainvoke(messages)
                    evidence = next(row for row in view['evidence'] if row['tool_name'] == 'get_pod_network_summary')
                    question = view['investigation']['questions'][0]['question_id']
                    value = evidence['observations']['rst_count']
                    payload = next(json.loads(message.content) for message in messages
                        if message.type == 'human' and message.content.startswith('{')
                        and 'copyable_observation_candidates' in message.content)
                    candidate = next(row for row in payload['copyable_observation_candidates']
                        if row['expected_value'] == value and row['source_ref']['json_pointer'] == '/summary/rst_count')
                    return AIMessage(content='', tool_calls=[{'name': 'register_report_observations', 'id': 'final-facts',
                        'args': {'question_ids': [question], 'candidate_ids': [candidate['local_id']]}}])
                return RunnableLambda(answer)
        before = self.b.model_steps
        with bind(self.b), patch.object(settings, 'CONTEXT_BUDGET_POLICY', 'soft'):
            try:
                result = await recover_report(self.b, Reviewer(), SystemMessage(content='Review actual facts and limits'))
            except WorkspaceError as exc:
                self.fail(str(exc) + ': ' + json.dumps(seen[-4:]))
        self.assertTrue(result['report_packet_id'])
        self.assertTrue(result['report_claims'])
        self.assertLessEqual(self.b.model_steps - before, settings.SUPERVISOR_FINALIZATION_MAX_STEPS)
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'stage_commit')), 1)
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'report_proposal')), 1)

    async def test_bad_finalization_is_bounded_without_accepting_invalid_facts(self):
        await self.prepare_source()
        calls = []
        class Bad:
            def bind_tools(self, tools, **kwargs):
                async def answer(messages):
                    calls.append(messages)
                    return AIMessage(content='', tool_calls=[{'name': 'register_report_observations', 'id': 'bad',
                        'args': {'candidate_ids': ['not-a-visible-candidate'], 'question_ids': ['unknown']}}])
                return RunnableLambda(answer)
        with bind(self.b), patch.object(settings, 'CONTEXT_BUDGET_POLICY', 'soft'), \
                self.assertRaisesRegex(WorkspaceError, 'report_recovery_budget_exhausted'):
            await recover_report(self.b, Bad(), SystemMessage(content='Review actual facts'))
        self.assertEqual(len(calls), settings.SUPERVISOR_FINALIZATION_MAX_STEPS)
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_commit'), [])

    async def test_hidden_source_cannot_become_a_copyable_candidate(self):
        await self.prepare_source()
        from agents.report_recovery import copyable_fields
        from workspace.queries import save_query
        await save_query(self.b, 'hidden_counter', 'get_pod_network_summary',
            {'cluster': 'c', 'pod_uid': 'u'}, {'summary': {'rst_count': 999}}, 'now', 1, 'ok')
        candidates = await copyable_fields(self.b)
        self.assertFalse(any(row['source_ref']['evidence_id'] == 'hidden_counter' for row in candidates))
        self.assertTrue(any(row['expected_value'] == 3 for row in candidates))
