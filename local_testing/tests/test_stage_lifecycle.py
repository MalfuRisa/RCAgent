"""Lifecycle boundaries observed through persisted records and actual visible inputs."""
import asyncio
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda

from config import settings
from context.builder import prepare
from context.request_budget import measure, tool_schemas
from workspace import investigations, run_stops
from workspace.artifacts import read_artifact_raw
from workspace.model_requests import ModelRequestError
from workspace.read_tools import read_investigation, read_evidence
from workspace.runtime import bind, invoke_model
from workspace.stage_tools import new_segment_after_commit
from workspace.stage_tools import commit_investigation_stage
from test_workspace import WorkspaceCase
import test_investigation_contracts as contracts


class LifecycleModes(WorkspaceCase):
    async def test_observe_saves_candidate_without_committing_or_injecting_it(self):
        question = await investigations.save_question(self.b, text='unresolved DNS direction')
        with patch.object(settings, 'CONTEXT_LIFECYCLE_MODE', 'observe'):
            visible, manifest = await prepare(self.b, [HumanMessage(content='check')])
            outcome = await investigations.apply_stage_proposal(self.b, {'client_commit_key': 'candidate'})
        self.assertIsNone(outcome)
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_commit'), [])
        self.assertNotIn(question['id'], ''.join(str(m.content) for m in visible))
        candidate = await read_artifact_raw(self.b, manifest['lifecycle_candidate_artifact_id'])
        self.assertIn(question['id'], json.dumps(candidate))

    async def test_persisted_stop_survives_loss_of_in_memory_registry(self):
        await run_stops.stop_run(self.b, 'model_rate_limited')
        run_stops.clear(self.b)
        sends = []
        async def model(messages):
            sends.append(messages)
            return AIMessage(content='unexpected')
        with bind(self.b), self.assertRaises(ModelRequestError) as error:
            await invoke_model(RunnableLambda(model), [])
        self.assertEqual(error.exception.detail['code'], 'model_rate_limited')
        self.assertEqual(sends, [])


class ActiveLifecycle(contracts.ActiveWorkspaceCase):
    seed_question_and_source = contracts.StageCommitTests.seed_question_and_source
    show = contracts.StageCommitTests.show
    proposal = contracts.StageCommitTests.proposal

    async def test_task_heads_and_idempotency_keys_are_isolated(self):
        question, evidence_id = await self.seed_question_and_source()
        first = await investigations.commit_stage(self.b, self.proposal(question, evidence_id))
        self.b.task_id, self.b.attempt_id = 'task_other', 'attempt_other'
        self.assertIsNone(await investigations.active_checkpoint(self.b, 'worker'))
        await self.show(evidence_id)
        second = await investigations.commit_stage(self.b, self.proposal(question, evidence_id))
        self.assertTrue(second['accepted'], second)
        self.assertNotEqual(second['checkpoint_id'], first['checkpoint_id'])
        self.assertFalse(second.get('duplicate'))
        usage = await investigations.materials(self.b, owner='worker')
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]['task_id'], 'task_other')

    async def test_concurrent_same_key_is_idempotent(self):
        question, evidence_id = await self.seed_question_and_source()
        proposal = self.proposal(question, evidence_id)
        outcomes = await asyncio.gather(*(investigations.commit_stage(self.b, proposal) for _ in range(2)))
        self.assertTrue(all(row['accepted'] for row in outcomes), outcomes)
        self.assertEqual(outcomes[0]['checkpoint_id'], outcomes[1]['checkpoint_id'])
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'stage_commit')), 1)

    async def test_unknown_replacement_and_missing_head_cannot_retire_material(self):
        question, evidence_id = await self.seed_question_and_source()
        proposal = self.proposal(question, evidence_id)
        proposal['material_dispositions'][0]['covered_by'] = ['observation_missing']
        rejected = await investigations.commit_stage(self.b, proposal)
        self.assertFalse(rejected['accepted'])
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'observation'), [])
        first = await investigations.commit_stage(self.b, self.proposal(question, evidence_id))
        rejected = await investigations.commit_stage(self.b, self.proposal(question, evidence_id, key='new'))
        self.assertFalse(rejected['accepted'])
        self.assertEqual(rejected['retained_checkpoint_id'], first['checkpoint_id'])

    async def test_partial_read_and_unhandled_batch_survive_a_commit(self):
        question, evidence_id = await self.seed_question_and_source()
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': evidence_id, 'field': 'samples', 'limit': 1})
        messages = [AIMessage(content='', tool_calls=[{'id': 'page', 'name': 'read_evidence', 'args': {}}]),
                    ToolMessage(content=json.dumps(page), tool_call_id='page', name='read_evidence')]
        counters = (self.b.model_steps, self.b.mcp_calls, self.b.reads, self.b.deadline)
        result = await investigations.commit_stage(self.b, self.proposal(question, evidence_id))
        segment = await new_segment_after_commit(self.b, result['checkpoint_id'], SystemMessage(content='rules'),
                                                 messages=messages)
        self.assertTrue(any(getattr(m, 'tool_call_id', None) == 'page' for m in segment))
        head = await investigations.active_checkpoint(self.b, 'worker')
        archived = await read_artifact_raw(self.b, head['segment_archive_artifact_id'])
        self.assertEqual(len(archived['messages']), 2)
        self.assertEqual(counters, (self.b.model_steps, self.b.mcp_calls, self.b.reads, self.b.deadline))

    async def test_investigation_page_is_bounded_and_listing_snapshot_is_stable(self):
        first = await investigations.save_question(self.b, text='long question ' * 1000)
        await investigations.save_question(self.b, text='second')
        with bind(self.b):
            page = await read_investigation.ainvoke({'kind': 'question', 'limit': 1, 'max_chars': 512})
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 512)
            self.assertTrue(page['has_more'])
            await investigations.save_question(self.b, text='added after snapshot')
            tail = await read_investigation.ainvoke({'kind': 'question', 'limit': 1,
                                                     'cursor': page['next_cursor'], 'max_chars': 512})
            self.assertEqual(tail['total'], 2)
            self.assertFalse(tail['has_more'])
            text_page = await read_investigation.ainvoke({'record_id': first['id'], 'field': 'text',
                                                         'char_offset': 0, 'max_chars': 512})
        self.assertLessEqual(len(json.dumps(text_page, ensure_ascii=False)), 512)
        self.assertTrue(text_page['truncated'])
        self.assertGreater(text_page['next_char_offset'], 0)

    async def test_canonical_budget_includes_tools_and_call_ids(self):
        messages = [ToolMessage(content='\\"\n', tool_call_id='call_x', name='read_investigation')]
        bare, _ = measure(messages)
        total, components = measure(messages, {'tools': tool_schemas([read_investigation])})
        self.assertEqual(sum(components.values()), total)
        self.assertGreater(total, bare)
        self.assertGreater(components['tools'], 500)
        visible, manifest = await prepare(self.b, [HumanMessage(content='check')],
                                           request={'tools': tool_schemas([read_investigation])})
        self.assertEqual(manifest['request_basis'], 'v2')

    async def test_two_real_stage_transactions_retire_only_the_replaced_page_and_allow_reread(self):
        question, evidence_id = await self.seed_question_and_source()
        messages, checkpoints = [], []
        rules = SystemMessage(content='Investigate; counts do not establish a cause')
        async def visible_model(visible):
            # The scripted model gets values/IDs exclusively from its actual input.
            pages = [json.loads(m.content) for m in visible if m.type == 'tool' and m.name == 'read_evidence']
            page = pages[-1]
            payload = contracts.workbench(visible)['investigation']
            questions = payload.get('questions') or []
            qid = questions[0]['question_id'] if questions else payload['overview']['listed'][0]
            opening = next((json.loads(m.content) for m in visible if m.type == 'human'
                            and str(m.content).startswith('{"stage":')), {})
            n = len(checkpoints)
            proposal = {'client_commit_key': 'stage-' + str(n), 'stage_goal': 'Verify exact reset count',
                        'completion': 'partial', 'question_ids': [qid],
                        'expected_checkpoint_id': opening.get('checkpoint_id'),
                        'expected_revision': opening.get('checkpoint_revision', 0),
                        'observation_proposals': [{'local_id': 'value', 'source_ref': {
                            'evidence_id': page['evidence_id'], 'json_pointer': '/summary/rst_count'},
                            'expected_value': page['items'][0]}],
                        'open_items': ['request correlation is still unknown'], 'next_focus': ['Recheck count'],
                        'material_dispositions': [{'source_id': page['evidence_id'], 'field': page['field'],
                                                   'disposition': 'covered_by', 'covered_by': ['value']}]}
            return AIMessage(content='', tool_calls=[{'id': 'commit-' + str(n),
                                                       'name': 'commit_investigation_stage', 'args': proposal}])
        initial_deadline = self.b.deadline
        for index in range(2):
            with bind(self.b):
                page = await read_evidence.ainvoke({'evidence_id': evidence_id, 'field': 'summary.rst_count'})
                call_id = 'read-' + str(index)
                messages += [AIMessage(content='', tool_calls=[{'id': call_id, 'name': 'read_evidence', 'args': {}}]),
                             ToolMessage(content=json.dumps(page), tool_call_id=call_id, name='read_evidence')]
                answer = await invoke_model(RunnableLambda(visible_model), [rules] + messages,
                                             request={'tools': tool_schemas([commit_investigation_stage])})
                messages.append(answer)
                result = await commit_investigation_stage.ainvoke(answer.tool_calls[0]['args'])
            self.assertEqual(result['status'], 'accepted', result)
            checkpoints.append(result['checkpoint_id'])
            messages.append(ToolMessage(content=json.dumps(result), name='commit_investigation_stage',
                                        tool_call_id=answer.tool_calls[0]['id']))
            messages = await new_segment_after_commit(self.b, checkpoints[-1], rules, messages=messages)
            self.assertFalse(any(m.type == 'tool' and m.name == 'read_evidence' for m in messages))
        self.assertNotEqual(*checkpoints)
        self.assertEqual(self.b.deadline, initial_deadline)
        self.assertEqual(self.b.model_steps, 3)  # seed visibility + two actual stage calls
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'stage_commit')), 2)
        receipts = await self.b.repository.all(self.b.incident_id, 'read_receipt')
        self.assertEqual(len([r for r in receipts if r.get('tool_name') == 'read_evidence']), 2)
