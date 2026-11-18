"""Partial-page handoffs prove exact coverage and preserve mixed-batch pairing."""
import json
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from workspace import investigations
from workspace.queries import save_query
from workspace.read_tools import read_evidence
from workspace.runtime import bind, invoke_model
from workspace.stage_tools import new_segment_after_commit, commit_investigation_stage
from test_investigation_contracts import ActiveWorkspaceCase


class MaterialRanges(ActiveWorkspaceCase):
    async def seed(self):
        await save_query(self.b, 'evidence_pages', 'get_abnormal_requests', {'cluster': 'c', 'pod_uid': 'u'},
                         {'samples': [{'code': 5}, {'code': 3}, {'code': 0}], 'note': 'abcdefghij'}, 'now', 1, 'ok')
        return await investigations.save_question(self.b, text='Which samples have errors?', owner_task_ids=['task_ranges'])

    async def visible(self, pages):
        calls = [{'id': str(i), 'name': 'read_evidence', 'args': {}} for i in range(len(pages))]
        messages = [AIMessage(content='Review this batch', tool_calls=calls)] + [
            ToolMessage(content=json.dumps(page), tool_call_id=str(i), name='read_evidence') for i, page in enumerate(pages)]
        with bind(self.b):
            await invoke_model(RunnableLambda(lambda _: AIMessage(content='read')), messages)
        return messages

    def proposal(self, question, page, *, span=None, unit=None, value=None):
        span = page['range'] if span is None else span
        unit = unit or ('chars' if 'text' in page else 'items')
        return {'client_commit_key': 'commit-page', 'stage_goal': 'Review only this page', 'completion': 'partial',
                'question_ids': [question['id']], 'observation_proposals': [{'local_id': 'o',
                    'source_ref': {'evidence_id': 'evidence_pages', 'json_pointer': '/' + page['field'],
                                   'range': span, 'range_unit': unit},
                    'expected_value': value if value is not None else page.get('items', page.get('text'))}],
                'material_dispositions': [{'source_id': 'evidence_pages', 'field': page['field'],
                    'range': span, 'range_unit': unit, 'disposition': 'covered_by', 'covered_by': ['o']}],
                'open_items': ['unread source ranges remain'], 'next_focus': ['read remaining pages']}

    async def test_middle_list_page_exits_but_unhandled_reply_stays_paired(self):
        question = await self.seed()
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': 'evidence_pages', 'field': 'samples', 'offset': 1, 'limit': 1})
            other = await read_evidence.ainvoke({'evidence_id': 'evidence_pages', 'field': 'note'})
        messages = await self.visible([page, other])
        result = await investigations.commit_stage(self.b, self.proposal(question, page))
        self.assertTrue(result['accepted'], result)
        observation = await self.b.repository.read(self.b.incident_id, result['observation_ids'][0])
        self.assertEqual(observation['verification']['coverage_status'], 'partial')
        self.assertEqual(observation['field_value'], [{'code': 3}])
        segment = await new_segment_after_commit(self.b, result['checkpoint_id'], SystemMessage(content='rules'), messages=messages)
        replies = {m.tool_call_id: m for m in segment if m.type == 'tool'}
        self.assertEqual(json.loads(replies['0'].content)['status'], 'covered_by_commit')
        self.assertEqual(replies['1'].content, messages[-1].content)
        self.assertTrue(any(m.type == 'ai' and len(m.tool_calls) == 2 for m in segment))
        head = await investigations.active_checkpoint(self.b, 'worker')
        self.assertEqual(head['retired_receipt_ids'], [page['read_receipt_id']])

    async def test_unseen_prefix_cannot_be_claimed_from_a_tail(self):
        question = await self.seed()
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': 'evidence_pages', 'field': 'samples', 'offset': 2})
        await self.visible([page])
        result = await investigations.commit_stage(self.b, self.proposal(question, page, span=[0, 3],
            value=[{'code': 5}, {'code': 3}, {'code': 0}]))
        self.assertFalse(result['accepted'])
        self.assertEqual(result['field_errors'][0]['reason'], 'not_shown_to_model')
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'observation'), [])

    async def test_exact_character_page_and_range_gap_are_distinct(self):
        question = await self.seed()
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': 'evidence_pages', 'field': 'note', 'char_offset': 3})
        await self.visible([page])
        result = await investigations.commit_stage(self.b, self.proposal(question, page))
        self.assertTrue(result['accepted'], result)
        from workspace.material_ranges import replacement_covers
        observation = await self.b.repository.read(self.b.incident_id, result['observation_ids'][0])
        self.assertTrue(replacement_covers([observation], 'evidence_pages', 1, 'note', page['range'], 'chars'))
        self.assertFalse(replacement_covers([observation], 'evidence_pages', 1, 'note', [0, page['range'][1]], 'chars'))
        self.assertFalse(replacement_covers([observation], 'evidence_pages', 1, 'note', page['range'], 'items'))
        ambiguous = {**observation, 'source_ref': {**observation['source_ref'], 'json_pointer': '/a.b', 'range': None}}
        self.assertFalse(replacement_covers([ambiguous], 'evidence_pages', 1, 'a.b'))
        ambiguous['source_ref']['json_pointer'] = '/'
        self.assertFalse(replacement_covers([ambiguous], 'evidence_pages', 1, ''))

    async def test_control_schema_is_bound_before_retrying_preparation(self):
        from agents.stage_requests import stage_request
        from langchain_core.tools import tool
        @tool('oversized_query', description='query docs ' * 3000)
        def query() -> dict:
            return {}
        sent = []
        class Client:
            def bind_tools(inner, tools, **kwargs):
                async def answer(messages):
                    sent.append(([t.name for t in tools], messages))
                    return AIMessage(content='handoff only')
                return RunnableLambda(answer)
        with bind(self.b):
            await stage_request(self.b, Client(), [SystemMessage(content='rules')],
                                [query, commit_investigation_stage], [commit_investigation_stage], phase='worker_step')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], ['commit_investigation_stage'])
        contexts = await self.b.repository.all(self.b.incident_id, 'context')
        self.assertTrue(any(row.get('disposition') == 'rejected_before_send' for row in contexts))
        self.assertLessEqual(contexts[-1]['request_chars_v2'], 24000)
        self.assertEqual(contexts[-1]['phase'], 'checkpoint_control')

    async def test_deferred_read_arguments_survive_when_completed_batch_retires(self):
        question = await self.seed()
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': 'evidence_pages', 'field': 'samples', 'offset': 1, 'limit': 1})
        messages = await self.visible([page])
        deferred = {'id': 'blocked', 'name': 'read_evidence', 'args': {'evidence_id': 'evidence_pages', 'field': 'note'}}
        messages[0].tool_calls.append(deferred)
        messages.append(ToolMessage(name='read_evidence', tool_call_id='blocked', content=json.dumps({
            'status': 'rejected', 'reason': 'checkpoint_required: commit this stage before another read'})))
        result = await investigations.commit_stage(self.b, self.proposal(question, page))
        self.assertTrue(result['accepted'], result)
        segment = await new_segment_after_commit(self.b, result['checkpoint_id'], SystemMessage(content='rules'), messages=messages)
        self.assertFalse(any(m.type in ('ai', 'tool') for m in segment))
        opening = json.loads(next(m.content for m in segment if m.type == 'human'))
        self.assertEqual(opening['deferred_tool_calls'], [{'name': deferred['name'], 'args': deferred['args']}])

    async def test_read_from_another_attempt_cannot_prove_this_attempt_value(self):
        question = await self.seed()
        self.b.attempt_id = 'attempt_first'
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': 'evidence_pages', 'field': 'samples', 'offset': 1, 'limit': 1})
        await self.visible([page])
        self.b.attempt_id = 'attempt_second'
        result = await investigations.commit_stage(self.b, self.proposal(question, page))
        self.assertFalse(result['accepted'])
        self.assertEqual(result['field_errors'][0]['reason'], 'not_shown_to_model')

    async def test_batch_pages_share_the_remaining_allowance(self):
        from agents.rca_agent import execute_calls
        from context.request_budget import tool_schemas
        from workspace.read_tools import READ_TOOLS
        from context.request_budget import measure
        await save_query(self.b, 'evidence_big', 'get_abnormal_requests', {'cluster': 'c', 'pod_uid': 'u'},
                         {'note': 'x' * 14000}, 'now', 1, 'ok')
        calls = [{'id': str(i), 'name': 'read_evidence', 'args': {
            'evidence_id': 'evidence_big', 'field': 'note', 'char_offset': i * 7000, 'max_chars': 8000}}
                 for i in range(2)]
        with bind(self.b):
            # Schema growth must not turn this into a test of two rejected
            # reads. Size the protected input to leave one useful shared page.
            request = {'tools': tool_schemas(READ_TOOLS + [commit_investigation_stage])}
            schema_cost = measure([], request)[0]
            padding = max(0, 17500 - schema_cost)
            await invoke_model(RunnableLambda(lambda _: AIMessage(content='read')), [SystemMessage(content='rules' + 'x' * padding)],
                               request=request)
            replies, _, _, _ = await execute_calls(self.b, calls, [])
        bodies = [json.loads(m.content) for m in replies]
        sizes = [len(json.dumps(body, ensure_ascii=False)) for body in bodies]
        self.assertLess(sizes[1], sizes[0])
        self.assertTrue(bodies[0].get('read_receipt_id'))
        if bodies[1].get('read_receipt_id'):
            self.assertLess(len(bodies[1]['text']), len(bodies[0]['text']))
        else:
            self.assertIn('checkpoint_required', bodies[1]['reason'])

    async def test_compact_committed_pages_keep_counter_and_opposition_facts(self):
        from context.builder import prepare
        from workspace.observations import verify_observation
        await self.seed()
        self.b.task_id, self.b.attempt_id = 'task_counter', 'attempt_counter'
        question = await investigations.save_question(self.b, text='Compare samples', owner_task_ids=[self.b.task_id])
        from workspace.artifacts import write_artifact
        artifact = await write_artifact(self.b, {'long': 'x' * 12000})
        await self.b.repository.save(self.b.incident_id, 'artifact', artifact)
        # Verified records remain stored; projection must not resurrect the
        # large page bodies beside the small facts required for reasoning.
        for index in range(16):
            observation = (await verify_observation(self.b, {'source_ref': {'artifact_id': artifact['id'],
                'json_pointer': '/long', 'range': [index, index + 1000], 'range_unit': 'chars'},
                'expected_value': json.dumps('x' * 12000)[index:index + 1000]},
                question_ids=[question['id']], producer='program'))['accepted']
            await self.b.repository.save(self.b.incident_id, 'observation', observation)
        facts = []
        for position in (0, 1):
            observation = (await verify_observation(self.b, {'source_ref': {'evidence_id': 'evidence_pages',
                'json_pointer': '/samples/%d/code' % position}, 'expected_value': 5 if position == 0 else 3},
                question_ids=[question['id']], producer='program'))['accepted']
            await self.b.repository.save(self.b.incident_id, 'observation', observation)
            facts.append(observation['id'])
        await investigations.save_judgment(self.b, question_id=question['id'], type='interpretation', content='Samples differ',
            supporting_observation_ids=[facts[0]], opposing_observation_ids=[facts[1]],
            limitations=['No causal proof'], status='unresolved')
        visible, _ = await prepare(self.b, [SystemMessage(content='rules')])
        payload = json.loads(visible[0].content.split('\n', 1)[1])['investigation']
        self.assertEqual(payload['committed_pages']['total'], 16)
        self.assertEqual({r['field_value'] for r in payload['observations']}, {5, 3})
        self.assertIn(facts[1], payload['must_handle']['observation_ids'])
        self.assertIn('No causal proof', payload['judgments'][0]['limitations'])
        self.assertNotIn('x' * 1000, visible[0].content)
