"""Shared contract and local Repair through actual role/tool/send/commit paths."""
import asyncio
import copy
import json
from unittest.mock import patch
from pydantic import ValidationError
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from config import settings
from models.schemas import StageProposal, SymptomList, RCATaskList, SupervisorDecision
from models.stage_repair import StageRepair
from context.request_budget import structured_request, tool_schemas, canonical_request, measure
from workspace import investigations
from workspace.runtime import bind, invoke_model
from workspace.queries import save_query
from workspace.read_tools import read_evidence
from workspace.stage_tools import commit_investigation_stage, new_segment_after_commit
from workspace.repository import WorkspaceError
from agents.stage_repair import submit_stage
from test_investigation_contracts import ActiveWorkspaceCase


class VisibleRepair:
    """No repository or test answer access; chooses from actual semantic input."""
    def __init__(self, mutate=None, cannot=False):
        self.calls = []
        self.mutate = mutate
        self.cannot = cannot

    def with_structured_output(self, schema):
        async def answer(messages):
            assert schema == StageRepair
            payload = json.loads(messages[-1].content)
            self.calls.append(payload)
            # Question text and concrete observation values, not fixture IDs.
            candidate = next(r['id'] for r in payload['candidate_questions'] if 'reset' in r['text'].lower())
            result = {'proposal_id': payload['proposal_id'], 'proposal_version': payload['proposal_version'],
                'status': 'cannot_repair' if self.cannot else 'patched',
                'changes': [] if self.cannot else [{'path': p, 'value': candidate} for p in payload['allowed']],
                'reason': 'Ambiguous evidence' if self.cannot else None}
            if self.mutate:
                await self.mutate(result, payload)
            return result
        return RunnableLambda(answer)


class RepairTests(ActiveWorkspaceCase):
    async def setup_proposal(self, *, role='worker', missing=True):
        self.b.role = role
        question = await investigations.save_question(self.b, text='Do resets explain failures?',
            owner_task_ids=[self.b.task_id] if self.b.task_id else [])
        arguments = {'cluster': 'c', 'pod_uid': 'u'}
        if self.b.resource_id:
            resource = await self.b.repository.read(self.b.incident_id, self.b.resource_id, 'resource')
            resource.update(identity_status='resolved', cluster='c', namespace='app', uid='u')
            await self.b.repository.save(self.b.incident_id, 'resource', resource, resource['revision'])
            arguments.update(namespace='app', pod=resource['name'])
        await save_query(self.b, 'evidence_repair', 'get_pod_network_summary', arguments,
                         {'summary': {'rst_count': 3, 'error_ratio': 0}}, 'now', 1, 'ok')
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': 'evidence_repair', 'field': 'summary'})
            messages = [SystemMessage(content='Investigate IAM to MySQL resets; preserve limited normal counter'),
                AIMessage(content='', tool_calls=[{'id': 'shown', 'name': 'read_evidence', 'args': {}}]),
                ToolMessage(content=json.dumps(page), tool_call_id='shown', name='read_evidence')]
            await invoke_model(RunnableLambda(lambda _: AIMessage(content='propose')), messages)
        self.original_context = self.b.last_context_id
        proposal = {'client_commit_key': 'original-key', 'stage_goal': 'Verify IAM request failure correlation',
            'completion': 'partial', 'question_ids': [question['id']],
            'observation_proposals': [{'local_id': 'rst', 'source_ref': {'evidence_id': 'evidence_repair',
                'json_pointer': '/summary/rst_count'}, 'expected_value': 3},
                {'local_id': 'normal', 'source_ref': {'evidence_id': 'evidence_repair',
                 'json_pointer': '/summary/error_ratio'}, 'expected_value': 0, 'limitations': ['Observed traffic only']}],
            'judgments': [{'content': 'Resets observed, request failure not established',
                'supporting_observation_ids': ['rst'], 'opposing_observation_ids': ['normal'],
                'limitations': ['Counter cannot prove causality']}, {'content': 'Identity and scope remain limited',
                'limitations': ['Historical window only']}],
            'material_dispositions': [{'source_id': 'evidence_repair', 'field': 'summary.rst_count',
                'disposition': 'covered_by', 'covered_by': ['rst']}],
            'open_items': ['Unread request samples remain'], 'next_focus': ['Read actual request samples']}
        if not missing:
            for row in proposal['judgments']:
                row['question_id'] = question['id']
        return proposal, messages

    async def test_patch_preserves_original_visibility_and_all_other_fields_then_continues(self):
        proposal, messages = await self.setup_proposal()
        original = copy.deepcopy(proposal)
        model = VisibleRepair()
        with bind(self.b):
            result = await submit_stage(self.b, model, proposal)
        self.assertTrue(result['accepted'], result)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(proposal, original)
        pending = (await self.b.repository.all(self.b.incident_id, 'stage_pending'))[0]
        self.assertEqual(pending['source_context_id'], self.original_context)
        self.assertNotEqual(self.b.last_context_id, self.original_context)
        commit = await self.b.repository.read(self.b.incident_id, result['commit_id'])
        self.assertEqual(commit['source_context_id'], self.original_context)
        self.assertEqual(len(commit['observation_ids']), 2)
        self.assertEqual(commit['client_commit_key'], original['client_commit_key'])
        self.assertIn('Observed traffic only', json.dumps(model.calls))
        segment = await new_segment_after_commit(self.b, result['checkpoint_id'], SystemMessage(content='rules'), messages=messages)
        with bind(self.b):
            await invoke_model(RunnableLambda(lambda _: AIMessage(content='continue')), segment)
        self.assertEqual(self.b.model_steps, 3)
        contexts = await self.b.repository.all(self.b.incident_id, 'context')
        repair = next(r for r in contexts if r['phase'] == 'stage_repair')
        self.assertLessEqual(repair['request_chars_v2'], 12000)
        self.assertEqual(sum(repair['component_chars'].values()), repair['request_chars_v2'])
        self.assertEqual(repair['injected_evidence_ids'], [])
        with bind(self.b):
            replay = await submit_stage(self.b, model, original)
        self.assertEqual(replay['commit_id'], result['commit_id'])
        self.assertEqual(len(model.calls), 1)

    async def test_legal_proposal_uses_no_repair(self):
        proposal, _ = await self.setup_proposal(missing=False)
        model = VisibleRepair()
        with bind(self.b):
            result = await submit_stage(self.b, model, proposal)
        self.assertTrue(result['accepted'])
        self.assertEqual(model.calls, [])

    async def test_wrong_value_or_unknown_core_field_does_not_get_format_repair(self):
        proposal, _ = await self.setup_proposal()
        for bad in ('value', 'extra'):
            draft = copy.deepcopy(proposal)
            if bad == 'value':
                draft['observation_proposals'][0]['expected_value'] = 99
            else:
                draft['judgments'][0]['delete_opposition'] = True
            model = VisibleRepair()
            with bind(self.b):
                result = await submit_stage(self.b, model, draft)
            self.assertFalse(result['accepted'])
            self.assertEqual(result['reason'], 'stage_repair_not_applicable')
            self.assertEqual(model.calls, [])

    async def test_cannot_repair_consumes_shared_stage_slot_even_with_new_key(self):
        proposal, _ = await self.setup_proposal()
        model = VisibleRepair(cannot=True)
        with bind(self.b):
            first = await submit_stage(self.b, model, proposal)
            second = await submit_stage(self.b, model, {**proposal, 'client_commit_key': 'new-key'}, phase='another-entry')
        self.assertEqual(first['reason'], 'stage_repair_cannot_repair')
        self.assertEqual(second['reason'], 'stage_repair_budget_exhausted')
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'checkpoint'), [])

    async def test_illegal_patch_is_atomic(self):
        proposal, _ = await self.setup_proposal()
        async def illegal(result, payload):
            result['changes'].append({'path': '/judgments/0/content', 'value': 'Confirmed cause'})
        model = VisibleRepair(mutate=illegal)
        with bind(self.b):
            result = await submit_stage(self.b, model, proposal)
        self.assertEqual(result['reason'], 'stage_repair_invalid_patch')
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'observation'), [])

    async def test_related_change_rejects_but_unrelated_audit_does_not(self):
        proposal, _ = await self.setup_proposal()
        async def changed(result, payload):
            row = await self.b.repository.read(self.b.incident_id, 'evidence_repair')
            await self.b.repository.save(self.b.incident_id, 'evidence', row, row['revision'])
        with bind(self.b):
            result = await submit_stage(self.b, VisibleRepair(mutate=changed), proposal)
        self.assertEqual(result['reason'], 'stage_repair_conflict')
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_commit'), [])

    async def test_unrelated_question_added_during_repair_is_not_global_revision_conflict(self):
        proposal, _ = await self.setup_proposal()
        async def unrelated(result, payload):
            await investigations.save_question(self.b, text='An unrelated question')
        with bind(self.b):
            result = await submit_stage(self.b, VisibleRepair(mutate=unrelated), proposal)
        self.assertTrue(result['accepted'], result)

    async def test_concurrent_entries_share_one_slot(self):
        proposal, _ = await self.setup_proposal()
        model = VisibleRepair()
        with bind(self.b):
            outcomes = await asyncio.gather(submit_stage(self.b, model, proposal),
                submit_stage(self.b, model, {**proposal, 'client_commit_key': 'parallel'}))
        self.assertEqual(len(model.calls), 1)
        self.assertLessEqual(sum(r['accepted'] for r in outcomes), 1)

    async def test_worker_last_step_has_no_free_repair(self):
        proposal, _ = await self.setup_proposal()
        self.b.model_steps = 16
        model = VisibleRepair()
        with bind(self.b):
            result = await submit_stage(self.b, model, proposal)
        self.assertEqual(result['reason'], 'stage_repair_budget_exhausted')
        self.assertEqual(model.calls, [])

    async def test_worker_remaining_sixteenth_step_is_used_by_repair(self):
        proposal, _ = await self.setup_proposal()
        self.b.model_steps = 15
        with bind(self.b):
            result = await submit_stage(self.b, VisibleRepair(), proposal)
        self.assertTrue(result['accepted'], result)
        self.assertEqual(self.b.model_steps, 16)

    async def test_repair_limit_refuses_without_send(self):
        proposal, _ = await self.setup_proposal()
        proposal['judgments'][0]['content'] += 'x' * 13000
        model = VisibleRepair()
        with bind(self.b), patch.object(settings, 'STAGE_REPAIR_MAX_CHARS', 12000), \
                patch.object(settings, 'CONTEXT_BUDGET_POLICY', 'soft'), patch.object(settings, 'CONTEXT_HARD_MAX_CHARS', 48000):
            result = await submit_stage(self.b, model, proposal)
        self.assertEqual(result['reason'], 'repair_context_unsatisfiable')
        self.assertEqual(model.calls, [])

    async def test_raw_tool_validation_uses_same_repair(self):
        proposal, _ = await self.setup_proposal()
        with bind(self.b), patch.object(settings, 'get_chat_model', return_value=VisibleRepair()):
            result = await commit_investigation_stage.ainvoke(proposal)
        self.assertEqual(result['status'], 'accepted', result)
        self.assertTrue(result['repair_pending_id'])

    async def test_outer_payload_preserved_when_stage_parse_fails(self):
        from agents.stage_outputs import preserve_outer
        proposal, _ = await self.setup_proposal(role='triage')
        value = {'symptoms': [{'potential_symptom': 'reset', 'resource_type': 'pod',
                              'affected_resource': 'iam', 'evidence': 'rst=3'}], 'stage_proposal': proposal}
        response = {'raw': AIMessage(content='', tool_calls=[{'id': 'raw', 'name': 'SymptomList', 'args': value}]),
                    'parsed': None, 'parsing_error': 'missing question_id'}
        output = await preserve_outer(self.b, response, SymptomList)
        self.assertEqual(output.symptoms[0].evidence, 'rst=3')
        with bind(self.b):
            result = await submit_stage(self.b, VisibleRepair(), output.stage_proposal)
        self.assertTrue(result['accepted'], result)

    async def test_each_role_preserves_outer_business_fields_with_same_patch_path(self):
        from agents.stage_outputs import preserve_outer
        from agents.stage_handoff import accept_role_stage
        for role, schema, business in [
            ('triage', SymptomList, {'symptoms': [{'potential_symptom': 'reset', 'resource_type': 'pod',
                'affected_resource': 'iam', 'evidence': 'rst=3'}]}),
            ('planner', RCATaskList, {'rca_tasks': [{'priority': 1, 'investigation_goal': 'Correlate resets',
                'target_resource': 'iam', 'resource_type': 'pod', 'acceptance_checks': ['Preserve opposition']}]}),
            ('supervisor', SupervisorDecision, {'tasks_to_execute_ids': ['task_actual']})]:
            with self.subTest(role=role):
                proposal, _ = await self.setup_proposal(role=role)
                value = {**business, 'stage_proposal': proposal}
                raw = {'raw': AIMessage(content='', tool_calls=[{'id': role, 'name': schema.__name__, 'args': value}]),
                       'parsed': None, 'parsing_error': 'question_id missing'}
                result = await preserve_outer(self.b, raw, schema)
                with bind(self.b):
                    outcome = await accept_role_stage(self.b, VisibleRepair(), result.stage_proposal,
                                                     owner=role, phase=role)
                self.assertTrue(outcome['accepted'], outcome)
                for key, expected in business.items():
                    actual = result.model_dump(exclude={'stage_proposal'})[key]
                    for index, item in enumerate(expected):
                        if isinstance(item, dict):
                            for field, entry in item.items():
                                self.assertEqual(actual[index][field], entry)
                        else:
                            self.assertEqual(actual[index], item)

    async def test_worker_final_embedded_stage_preserves_assessment_and_patches(self):
        from agents.rca_agent import execute_calls
        await self.begin_task()
        proposal, _ = await self.setup_proposal()
        call = {'name': 'submit_final_diagnosis', 'id': 'final', 'args': {'diagnosis': 'Unresolved resets',
            'reasoning': 'Only archived counters', 'supporting_evidence_ids': ['evidence_repair'],
            'gaps': ['request samples unread'], 'stage_proposal': proposal}}
        model = VisibleRepair()
        with bind(self.b), patch.object(settings, 'get_chat_model', return_value=model):
            replies, assessment, fatal, _ = await execute_calls(self.b, [call], [])
        self.assertIsNone(fatal)
        self.assertIsNotNone(assessment, [r.content for r in replies])
        self.assertEqual(assessment['diagnosis'], 'Unresolved resets')
        self.assertIn('request samples unread', assessment['gaps'])
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].tool_call_id, 'final')

    async def test_rejected_optional_worker_stage_keeps_valid_assessment_with_gap(self):
        from agents.rca_agent import execute_calls
        await self.begin_task()
        await self.setup_proposal()
        call = {'name': 'submit_final_diagnosis', 'id': 'final-rejected-stage', 'args': {
            'diagnosis': 'Reset signal; cause unresolved', 'reasoning': 'Actual evidence only',
            'supporting_evidence_ids': ['evidence_repair'], 'answer_status': 'supported',
            'stage_proposal': {'client_commit_key': 'bad', 'stage_goal': 'handoff', 'question_ids': []}}}
        with bind(self.b), patch.object(settings, 'get_chat_model', return_value=object()):
            replies, assessment, fatal, _ = await execute_calls(self.b, [call], [])
        self.assertIsNone(fatal)
        self.assertIsNotNone(assessment, [message.content for message in replies])
        self.assertEqual(assessment['supporting_evidence_ids'], ['evidence_repair'])
        self.assertEqual(assessment['answer_status'], 'inconclusive')
        self.assertTrue(any('Optional final stage was not accepted' in gap for gap in assessment['gaps']))
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_commit'), [])

    async def test_supervisor_local_final_slot_has_no_free_repair(self):
        proposal, _ = await self.setup_proposal(role='supervisor')
        self.b.local_model_limit = self.b.model_steps
        model = VisibleRepair()
        with bind(self.b):
            result = await submit_stage(self.b, model, proposal)
        self.assertEqual(result['reason'], 'stage_repair_budget_exhausted')
        self.assertEqual(model.calls, [])

    async def test_patch_wrong_identity_and_duplicate_paths_are_atomic(self):
        from workspace.stage_pending import merge
        proposal = {'judgments': [{'content': 'Keep unchanged', 'limitations': ['limited']} ]}
        pending = {'id': 'pending_x', 'proposal_version': 1, 'allowed': {'/judgments/0/question_id': ['q']}}
        base = {'proposal_id': 'pending_x', 'proposal_version': 1, 'status': 'patched',
                'changes': [{'path': '/judgments/0/question_id', 'value': 'q'}]}
        variants = [{**base, 'proposal_id': 'other'}, {**base, 'proposal_version': 2},
                    {**base, 'changes': base['changes'] * 2},
                    {**base, 'changes': [{'path': '/judgments/-1/question_id', 'value': 'q'}]},
                    {**base, 'changes': [{'path': '/judgments/0/question_id', 'value': 'other'}]}]
        for value in variants:
            with self.assertRaises(WorkspaceError):
                merge(pending, proposal, value)
        self.assertNotIn('question_id', proposal['judgments'][0])

    async def test_429_stops_this_run_and_future_repair_without_extra_send(self):
        from workspace.model_requests import ModelRequestError
        proposal, _ = await self.setup_proposal()
        sends = []
        class Model:
            def with_structured_output(inner, schema):
                async def respond(messages):
                    sends.append(messages)
                    class RateLimit(Exception):
                        status_code = 429
                    raise RateLimit('synthetic offline 429')
                return RunnableLambda(respond)
        with bind(self.b), self.assertRaises(ModelRequestError) as error:
            await submit_stage(self.b, Model(), proposal)
        self.assertEqual(error.exception.detail['code'], 'model_rate_limited')
        self.assertEqual(len(sends), 1)
        run = await self.b.repository.read(self.b.incident_id, self.b.run_id, 'run')
        self.assertEqual(run['stop_reason'], 'model_rate_limited')
        with bind(self.b):
            refused = await submit_stage(self.b, Model(), {**proposal, 'client_commit_key': 'after429'})
        self.assertEqual(refused['reason'], 'stage_repair_budget_exhausted')
        self.assertEqual(len(sends), 1)

    async def test_timeout_and_cancellation_consume_slot_without_committing(self):
        proposal, _ = await self.setup_proposal()
        entered = asyncio.Event()
        class Model:
            def with_structured_output(inner, schema):
                async def wait(messages):
                    entered.set()
                    await asyncio.Event().wait()
                return RunnableLambda(wait)
        with bind(self.b):
            task = asyncio.create_task(submit_stage(self.b, Model(), proposal))
            await asyncio.wait_for(entered.wait(), 20)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        pending = (await self.b.repository.all(self.b.incident_id, 'stage_pending'))[-1]
        self.assertEqual(pending['status'], 'cancelled')
        self.assertTrue(pending['repair_used'])
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_commit'), [])

    async def test_repair_failure_archives_pair_but_leaves_mixed_live_read_batch(self):
        from workspace.stage_messages import archive_stage_pairs
        proposal, live = await self.setup_proposal()
        failed = AIMessage(content='', tool_calls=[{'name': 'commit_investigation_stage', 'id': 'failed', 'args': proposal}])
        reply = ToolMessage(content=json.dumps({'status': 'rejected', 'reason': 'stage_repair_cannot_repair'}),
                            name='commit_investigation_stage', tool_call_id='failed')
        messages = await archive_stage_pairs(self.b, live + [failed, reply])
        self.assertEqual(messages[:len(live)], live)
        self.assertFalse(any(getattr(m, 'tool_call_id', None) == 'failed' for m in messages))
        self.assertIn('pair_artifact_id', messages[-1].content)
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'checkpoint'), [])

    async def test_deadline_rejects_before_repair_send(self):
        import time
        proposal, _ = await self.setup_proposal()
        self.b.deadline = time.time() - 1
        model = VisibleRepair()
        with bind(self.b), self.assertRaises(TimeoutError):
            await submit_stage(self.b, model, proposal)
        self.assertEqual(model.calls, [])

    async def test_related_question_version_change_is_a_conflict(self):
        proposal, _ = await self.setup_proposal()
        async def changed(result, payload):
            row = await self.b.repository.read(self.b.incident_id, proposal['question_ids'][0], 'question')
            row['text'] += ' Changed while waiting'
            await self.b.repository.save(self.b.incident_id, 'question', row, row['revision'])
        with bind(self.b):
            result = await submit_stage(self.b, VisibleRepair(mutate=changed), proposal)
        self.assertEqual(result['reason'], 'stage_repair_conflict')
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'checkpoint'), [])

    async def test_stage_opening_change_and_pending_version_change_are_conflicts(self):
        proposal, _ = await self.setup_proposal()
        async def changed(result, payload):
            row = await self.b.repository.read(self.b.incident_id, result['proposal_id'], 'stage_pending')
            row['proposal_version'] += 1
            await self.b.repository.save(self.b.incident_id, 'stage_pending', row, row['revision'])
        with bind(self.b):
            result = await submit_stage(self.b, VisibleRepair(mutate=changed), proposal)
        self.assertEqual(result['reason'], 'stage_repair_conflict')
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_commit'), [])

    async def test_observe_and_legacy_never_invoke_repair(self):
        proposal, _ = await self.setup_proposal()
        model = VisibleRepair()
        for mode in ('observe', 'legacy'):
            with bind(self.b), patch.object(settings, 'CONTEXT_LIFECYCLE_MODE', mode):
                self.assertIsNone(await submit_stage(self.b, model, proposal))
        self.assertEqual(model.calls, [])
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_repair_slot'), [])

    async def test_outer_invalid_json_wrong_tool_and_business_error_do_not_get_stage_repair(self):
        from agents.stage_outputs import preserve_outer
        proposal, _ = await self.setup_proposal()
        for response in ({'raw': AIMessage(content='{broken json'), 'parsed': None},
                         {'raw': AIMessage(content='', tool_calls=[{'name': 'Wrong', 'id': 'wrong',
                                                                  'args': {'stage_proposal': proposal}}]), 'parsed': None},
                         {'symptoms': [{'resource_type': 'illegal'}], 'stage_proposal': proposal}):
            with self.assertRaises((WorkspaceError, ValidationError)):
                await preserve_outer(self.b, response, SymptomList)
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_pending'), [])

    async def test_unseen_original_value_does_not_get_repaired(self):
        proposal, _ = await self.setup_proposal()
        await save_query(self.b, 'evidence_hidden', 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': 'u'},
                         {'secret': {'value': 7}}, 'now', 1, 'ok')
        proposal['observation_proposals'][0].update(source_ref={'evidence_id': 'evidence_hidden',
            'json_pointer': '/secret/value'}, expected_value=7)
        with bind(self.b):
            await invoke_model(RunnableLambda(lambda _: AIMessage(content='different request')), [SystemMessage(content='No facts here')])
            model = VisibleRepair()
            result = await submit_stage(self.b, model, proposal)
        self.assertEqual(result['reason'], 'stage_repair_not_applicable')
        self.assertEqual(model.calls, [])


class SchemaTests(__import__('unittest').TestCase):
    def test_actual_sdk_schemas_share_nested_required_judgment_fields(self):
        schemas = [tool_schemas([commit_investigation_stage])[0]]
        schemas.extend(structured_request(s) for s in (SymptomList, RCATaskList, SupervisorDecision))
        from tools.rca_tools import submit_final_diagnosis
        schemas += tool_schemas([submit_final_diagnosis])
        def find(node):
            if isinstance(node, dict):
                if 'question_id' in node.get('properties', {}) and 'content' in node.get('properties', {}):
                    return {'question_id', 'content'} <= set(node.get('required', []))
                return any(find(v) for v in node.values())
            return isinstance(node, list) and any(find(v) for v in node)
        for schema in schemas:
            self.assertTrue(find(schema), json.dumps(schema)[:300])

    def test_null_fragment_aggregate_and_strict_ids(self):
        base = {'client_commit_key': 'a', 'stage_goal': 'b', 'question_ids': ['q']}
        obs = {'local_id': 'o', 'source_ref': {'evidence_id': 'e'}, 'expected_value': None}
        StageProposal.model_validate({**base, 'observation_proposals': [obs]})
        for extra in ({'text_ref': {'char_range': [0, 2]}, 'fragments': ['ab']},
                      {'aggregate': {'operation': 'count', 'end_index': 2}}):
            StageProposal.model_validate({**base, 'observation_proposals': [{'local_id': 'o', 'source_ref': {'evidence_id': 'e'}, **extra}]})
        for invalid in (True, 1, ''):
            with self.assertRaises(ValidationError):
                StageProposal.model_validate({**base, 'judgments': [{'question_id': invalid, 'content': 'fact'}]})

    def test_long_ai_tool_arguments_are_visible_in_part_meter(self):
        from context.parts import message_size
        message = AIMessage(content='', tool_calls=[{'name': 'commit_investigation_stage', 'id': 'x',
                                                     'args': {'long': 'x' * 8707}}])
        self.assertGreater(message_size(message), 8707)
        total, components = measure([message], structured_request(StageRepair))
        self.assertEqual(sum(components.values()), total)
