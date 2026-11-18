"""R1 contracts: source verification, questions/judgments, stage handoff, selection.

Behaviour-level checks only: they read what was actually saved, what the model
input carried and what a rejection said. Nothing here asserts a private helper
name or a file layout.
"""
import json
import unittest
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda

from config import settings
from context.builder import prepare
from context.investigations import select_investigation
from workspace import investigations, observations
from workspace.queries import save_query
from workspace.runtime import bind, invoke_model
from test_workspace import WorkspaceCase


class ActiveWorkspaceCase(WorkspaceCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        mode = patch.object(settings, 'CONTEXT_LIFECYCLE_MODE', 'active')
        mode.start()
        self.addCleanup(mode.stop)


async def register_pod(binding, name, namespace):
    from workspace.resources import register
    row = await register(binding, 'pod', name, namespace)
    row['uid'] = 'iam-uid'
    row['cluster'] = binding.query_window.get('cluster') or 'c'
    row['identity_status'] = 'resolved'
    return await binding.repository.save(binding.incident_id, 'resource', row, row['revision'])


def workbench(messages):
    text = next(str(m.content) for m in messages if getattr(m, 'type', '') == 'system'
                and str(m.content).startswith('Incident workbench'))
    from context.exact_representation import expand
    return expand(json.loads(text.split('\n', 1)[1]))


class PointerTests(unittest.TestCase):
    def test_pointers_resolve_nested_escapes_and_indices(self):
        document = {'summary': {'rst_count': 3, 'a/b': 1, 'c~d': 2},
                    'samples': [{'response_code': 5}, {'response_code': 3}]}
        self.assertEqual(observations.resolve_pointer(document, '/summary/rst_count')[0], 3)
        self.assertEqual(observations.resolve_pointer(document, '/summary/a~1b')[0], 1)
        self.assertEqual(observations.resolve_pointer(document, '/summary/c~0d')[0], 2)
        self.assertEqual(observations.resolve_pointer(document, '/samples/1/response_code')[0], 3)
        self.assertFalse(observations.resolve_pointer(document, '/samples/-')[1], 'the end token is not a value')
        self.assertFalse(observations.resolve_pointer(document, '/samples/x')[1])
        self.assertFalse(observations.resolve_pointer(document, '/missing')[1])
        with self.assertRaises(Exception):
            observations.pointer_tokens('summary/rst_count')

    def test_strict_comparison_rejects_type_aliases(self):
        self.assertTrue(observations.values_match(3, 3))
        self.assertTrue(observations.values_match(3, 3.0))
        self.assertFalse(observations.values_match('3', 3))
        self.assertFalse(observations.values_match(True, 1))
        self.assertFalse(observations.values_match(0, False))
        self.assertTrue(observations.values_match(None, None))
        self.assertFalse(observations.values_match([], None))
        self.assertFalse(observations.values_match({'value': True}, {'value': 1}))
        self.assertFalse(observations.values_match([False], [0]))


class VerificationTests(ActiveWorkspaceCase):
    async def seed_network(self, rst_count=3, truncated=False):
        r = await register_pod(self.b, 'iam', 'app')
        evidence_id = 'evidence_net'
        await save_query(self.b, evidence_id, 'get_pod_network_summary',
                         {'cluster': 'c', 'pod_uid': r['uid']},
                         {'status': 'ok', 'summary': {'rst_count': rst_count, 'rtt_avg_ms': 12},
                          'samples': [{'response_code': 5}, {'response_code': 3}],
                          'data_gaps': [{'source': 'l7', 'reason': 'trace_id_unavailable'}]},
                         'now', 1, 'ok', 'net-call')
        record = await self.b.repository.read(self.b.incident_id, evidence_id, 'evidence')
        if truncated:
            record['truncated'] = True
            record = await self.b.repository.save(self.b.incident_id, 'evidence', record, record['revision'])
        return evidence_id, record

    async def show_evidence(self, evidence_id):
        """Make the source provably visible: the model call persists its manifest."""

        async def model(messages):
            return AIMessage(content='ok')

        with bind(self.b):
            await invoke_model(RunnableLambda(model), [SystemMessage(content='Extract symptoms'),
                                                       HumanMessage(content='iam pod 出现 DNS 失败')],
                               phase='triage')

    def proposal(self, evidence_id, pointer='/summary/rst_count', expected=3, **kwargs):
        row = {'source_ref': {'evidence_id': evidence_id, 'json_pointer': pointer}, 'expected_value': expected}
        row.update(kwargs)
        return row

    async def test_verified_observation_keeps_value_scope_and_split_flags(self):
        evidence_id, record = await self.seed_network()
        await self.show_evidence(evidence_id)
        question = await investigations.save_question(self.b, text='网络重传是否与请求失败有关')
        with bind(self.b):
            outcome = await observations.verify_observation(
                self.b, self.proposal(evidence_id), question_ids=[question['id']], owner='worker')
        self.assertIn('accepted', outcome, outcome)
        stored = outcome['accepted']
        self.assertEqual(stored['field_value'], 3)
        self.assertEqual(stored['verification'],
                         {'source_valid': True, 'value_matches': True, 'scope_known': True,
                          'coverage_status': 'complete'})
        self.assertEqual(stored['scope']['resource_refs'][0]['uid'], 'iam-uid')
        self.assertEqual(stored['producer'], 'model')
        self.assertEqual(stored['question_ids'], [question['id']])
        saved = await self.b.repository.save(self.b.incident_id, 'observation', stored)
        self.assertEqual(saved['id'], stored['id'])
        self.assertIn('limitations', saved)

    async def test_wrong_value_is_rejected_with_the_real_difference(self):
        evidence_id, _ = await self.seed_network()
        await self.show_evidence(evidence_id)
        with bind(self.b):
            outcome = await observations.verify_observation(self.b, self.proposal(evidence_id, expected=30))
        self.assertIn('rejected', outcome)
        self.assertEqual(outcome['rejected']['reason'], 'value_mismatch')
        self.assertEqual(outcome['rejected']['expected'], 30)
        self.assertEqual(outcome['rejected']['actual'], 3, 'the real value is reported, not silently corrected')

    async def test_revision_hash_and_visibility_are_enforced(self):
        evidence_id, record = await self.seed_network()
        await self.show_evidence(evidence_id)
        stale = self.proposal(evidence_id)
        stale['source_ref']['source_revision'] = record['revision'] + 5
        with bind(self.b):
            outcome = await observations.verify_observation(self.b, stale)
        self.assertEqual(outcome['rejected']['reason'], 'source_changed')
        self.assertEqual(outcome['rejected']['problems'][0]['actual'], record['revision'])
        artifact_hash = (await self.b.repository.read(self.b.incident_id, record['artifact_id'], 'artifact'))['sha256']
        mismatched = self.proposal(evidence_id)
        mismatched['source_ref']['source_hash'] = artifact_hash[:-1] + ('0' if artifact_hash[-1] != '0' else '1')
        with bind(self.b):
            outcome = await observations.verify_observation(self.b, mismatched)
        self.assertEqual(outcome['rejected']['reason'], 'source_changed')
        # A value the model never saw in this stage cannot be claimed.
        invisible = await self.seed_invisible()
        with bind(self.b):
            outcome = await observations.verify_observation(
                self.b, self.proposal(invisible, pointer='/restart_count', expected=7))
        self.assertEqual(outcome['rejected']['reason'], 'not_shown_to_model')

    async def seed_invisible(self):
        evidence_id = 'evidence_invisible'
        await save_query(self.b, evidence_id, 'get_pod_runtime_status', {'cluster': 'c', 'pod_uid': 'x'},
                         {'status': 'ok', 'status': {'phase': 'Running'}, 'restart_count': 7}, 'now', 1, 'ok')
        return evidence_id

    async def test_visible_evidence_id_does_not_authorize_an_unshown_field(self):
        evidence_id, _ = await self.seed_network()
        await self.show_evidence(evidence_id)
        from workspace.artifacts import read_artifact_raw, write_artifact
        manifest = await self.b.repository.read(self.b.incident_id, self.b.last_context_id, 'context')
        messages = await read_artifact_raw(self.b, manifest['visible_messages_artifact_id'])
        for message in messages:
            if str(message.get('content', '')).startswith('Incident workbench'):
                payload = json.loads(message['content'].split('\n', 1)[1])
                payload['evidence'] = [{'evidence_id': evidence_id, 'observations': {}, 'source': {}}]
                message['content'] = 'Incident workbench\n' + json.dumps(payload)
        artifact = await write_artifact(self.b, messages)
        await self.b.repository.save(self.b.incident_id, 'artifact', artifact)
        manifest['visible_messages_artifact_id'] = artifact['id']
        await self.b.repository.save(self.b.incident_id, 'context', manifest, manifest['revision'])
        result = await observations.verify_observation(self.b, self.proposal(evidence_id))
        self.assertEqual(result['rejected']['reason'], 'not_shown_to_model')

    async def test_pointer_not_found_and_cross_incident_sources_are_rejected(self):
        evidence_id, _ = await self.seed_network()
        await self.show_evidence(evidence_id)
        with bind(self.b):
            outcome = await observations.verify_observation(
                self.b, self.proposal(evidence_id, pointer='/summary/not_there'))
            self.assertEqual(outcome['rejected']['reason'], 'pointer_not_found')
            missing = await observations.verify_observation(
                self.b, {'source_ref': {'evidence_id': 'evidence_other_incident'},
                         'expected_value': 1})
            self.assertEqual(missing['rejected']['reason'], 'source_unavailable')

    async def test_fragment_and_program_aggregate_keep_their_ranges(self):
        evidence_id, _ = await self.seed_network()
        await self.show_evidence(evidence_id)
        raw = json.dumps({'status': 'ok', 'note': 'lookup kylin-insights-redis timed out after 1.4s'},
                         ensure_ascii=False)
        evidence_id2 = 'evidence_text'
        await save_query(self.b, evidence_id2, 'get_pod_application_summary', {'cluster': 'c', 'pod_uid': 'x'},
                         {'status': 'ok', 'note': 'lookup kylin-insights-redis timed out after 1.4s'}, 'now', 1, 'ok')
        await self.show_evidence(evidence_id2)
        from workspace.read_tools import read_evidence
        from langchain_core.messages import ToolMessage
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': evidence_id2, 'view': 'raw'})
            await invoke_model(RunnableLambda(lambda _: AIMessage(content='read')),
                               [SystemMessage(content='verify text'),
                                ToolMessage(content=json.dumps(page), tool_call_id='text', name='read_evidence')])
        start = raw.index('lookup')
        with bind(self.b):
            outcome = await observations.verify_observation(self.b, {
                'source_ref': {'evidence_id': evidence_id2},
                'text_ref': {'source_ref': {'evidence_id': evidence_id2}, 'char_range': [start, start + 40]},
                'fragments': ['lookup kylin-insights']})
            self.assertIn('accepted', outcome, outcome)
            self.assertEqual(outcome['accepted']['kind'], 'fragment')
            self.assertIn('does_not_prove_meaning', outcome['accepted']['interpretation'])
            broken = await observations.verify_observation(self.b, {
                'source_ref': {'evidence_id': evidence_id2},
                'text_ref': {'source_ref': {'evidence_id': evidence_id2}, 'char_range': [start, start + 40]},
                'fragments': ['kylin-insights-redis is the root cause']})
            self.assertEqual(broken['rejected']['reason'], 'fragment_mismatch')
            aggregate = await observations.verify_observation(self.b, {
                'source_ref': {'evidence_id': evidence_id}, 'aggregate': {
                    'operation': 'count', 'json_pointer': '/samples'}})
            self.assertIn('accepted', aggregate, aggregate)
            self.assertEqual(aggregate['accepted']['field_value'], 2)
            self.assertEqual(aggregate['accepted']['producer'], 'program')
            partial = await observations.verify_observation(self.b, {
                'source_ref': {'evidence_id': evidence_id}, 'aggregate': {
                    'operation': 'count', 'json_pointer': '/samples', 'start_index': 0, 'end_index': 1}})
            self.assertEqual(partial['accepted']['field_value'], 1)
            self.assertEqual(partial['accepted']['aggregate']['input_total'], 2)
            self.assertIn('partial range counts only that range', partial['accepted']['aggregate']['note'])
            forbidden = await observations.verify_observation(self.b, {
                'source_ref': {'evidence_id': evidence_id}, 'aggregate': {
                    'operation': 'python_eval', 'json_pointer': '/samples'}})
            self.assertEqual(forbidden['rejected']['reason'], 'aggregate_not_allowed')


class StageCommitTests(ActiveWorkspaceCase):
    async def seed_question_and_source(self):
        evidence_id = 'evidence_stage'
        await save_query(self.b, evidence_id, 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': 'u'},
                         {'status': 'ok', 'summary': {'rst_count': 3}, 'samples': [{'code': 5}, {'code': 3}]},
                         'now', 1, 'ok')
        await self.show(evidence_id)
        question = await investigations.save_question(self.b, text='网络重传是否与失败有关',
                                                      resource_ids=['resource_x'])
        return question, evidence_id

    async def show(self, evidence_id):
        async def model(messages):
            return AIMessage(content='ok')

        with bind(self.b):
            await invoke_model(RunnableLambda(model), [SystemMessage(content='Extract symptoms'),
                                                       HumanMessage(content='check the pod')], phase='triage')

    def proposal(self, question, evidence_id, *, key='commit-1', expected_checkpoint=None, completion='complete',
                 local_id='obs1', rst=3, include_opposition=True):
        proposal = {'client_commit_key': key, 'stage_goal': '网络方向核对', 'completion': completion,
                    'question_ids': [question['id']],
                    'observation_proposals': [{'local_id': local_id,
                                               'source_ref': {'evidence_id': evidence_id,
                                                              'json_pointer': '/summary/rst_count'},
                                               'expected_value': rst}],
                    'judgments': [{'local_id': 'j1', 'question_id': question['id'], 'type': 'interpretation',
                                   'content': '计数本身不能证明根因', 'supporting_observation_ids': [local_id],
                                   'opposing_observation_ids': [], 'limitations': ['仅这一 Pod/窗口']}],
                    'coverage': {'sources': [evidence_id]},
                    'open_items': ['是否对应失败请求'], 'next_focus': ['关联请求日志'],
                    'material_dispositions': [{'source_id': evidence_id, 'field': 'summary.rst_count',
                                               'disposition': 'covered_by', 'covered_by': [local_id],
                                               'reason': '值已由观测接替'}]}
        if expected_checkpoint is not None:
            proposal['expected_checkpoint_id'] = expected_checkpoint
            proposal['expected_revision'] = 1
        return proposal

    async def test_commit_persists_records_checkpoint_and_material_usage(self):
        question, evidence_id = await self.seed_question_and_source()
        with bind(self.b):
            outcome = await investigations.commit_stage(self.b, self.proposal(question, evidence_id), owner='worker')
        self.assertTrue(outcome['accepted'], outcome)
        self.assertTrue(outcome['commit_id'].startswith('stage_commit_'))
        self.assertTrue(outcome['checkpoint_id'].startswith('checkpoint_'))
        self.assertEqual(outcome['id_map']['obs1'], outcome['observation_ids'][0])
        stored = await self.b.repository.read(self.b.incident_id, outcome['commit_id'], 'stage_commit')
        self.assertEqual(stored['question_ids'], [question['id']])
        self.assertEqual(stored['completion'], 'complete')
        self.assertEqual(stored['included_opposition_ids'], [])
        observation = await self.b.repository.read(self.b.incident_id, outcome['observation_ids'][0], 'observation')
        self.assertEqual(observation['field_value'], 3)
        head = await investigations.active_checkpoint(self.b, 'worker')
        self.assertEqual(head['id'], outcome['checkpoint_id'])
        self.assertEqual(head['stage_commit_id'], outcome['commit_id'])
        self.assertTrue(head['stage_id'])
        usage = await investigations.materials(self.b, owner='worker')
        self.assertEqual(usage[0]['state'], 'covered_by_commit')
        self.assertEqual(usage[0]['source_id'], evidence_id)

    async def test_second_commit_chains_the_checkpoint_and_material_stays_isolated(self):
        question, evidence_id = await self.seed_question_and_source()
        with bind(self.b):
            first = await investigations.commit_stage(self.b, self.proposal(question, evidence_id), owner='worker')
            second = await investigations.commit_stage(
                self.b, self.proposal(question, evidence_id, key='commit-2',
                                      expected_checkpoint=first['checkpoint_id'], local_id='obs2'),
                owner='worker')
        self.assertTrue(second['accepted'], second)
        head = await investigations.active_checkpoint(self.b, 'worker')
        self.assertEqual(head['id'], second['checkpoint_id'])
        self.assertEqual(head['previous_checkpoint_id'], first['checkpoint_id'])
        checkpoints = await self.b.repository.all(self.b.incident_id, 'checkpoint')
        self.assertEqual(len([row for row in checkpoints if row.get('is_head')]), 1,
                         'exactly one active head per owner')
        other = await investigations.active_checkpoint(self.b, 'supervisor')
        self.assertIsNone(other, 'another owner keeps its own head')

    async def test_stale_checkpoint_conflict_keeps_the_previous_head(self):
        question, evidence_id = await self.seed_question_and_source()
        with bind(self.b):
            first = await investigations.commit_stage(self.b, self.proposal(question, evidence_id), owner='worker')
            conflicted = await investigations.commit_stage(
                self.b, self.proposal(question, evidence_id, key='commit-3', expected_checkpoint='checkpoint_stale',
                                      local_id='obs3'), owner='worker')
        self.assertFalse(conflicted['accepted'])
        self.assertEqual(conflicted['reason'], 'checkpoint_conflict')
        self.assertEqual(conflicted['retained_checkpoint_id'], first['checkpoint_id'])
        head = await investigations.active_checkpoint(self.b, 'worker')
        self.assertEqual(head['id'], first['checkpoint_id'], 'a rejected handoff changes nothing')

    async def test_idempotent_replay_returns_the_same_result_and_a_new_reason_repeats_nothing(self):
        question, evidence_id = await self.seed_question_and_source()
        proposal = self.proposal(question, evidence_id, key='commit-4')
        with bind(self.b):
            first = await investigations.commit_stage(self.b, proposal, owner='worker')
            replay = await investigations.commit_stage(self.b, proposal, owner='worker')
        self.assertTrue(replay.get('duplicate'))
        self.assertEqual(replay['commit_id'], first['commit_id'])
        stored_observations = await self.b.repository.all(self.b.incident_id, 'observation')
        self.assertEqual(len(stored_observations), 1, 'the replay produced no new fact')
        changed = dict(proposal, stage_goal='不同的目标')
        with bind(self.b):
            conflict = await investigations.commit_stage(self.b, changed, owner='worker')
        self.assertFalse(conflict['accepted'])
        self.assertEqual(conflict['reason'], 'checkpoint_conflict')
        from workspace.artifacts import read_artifact_raw
        archived = await read_artifact_raw(self.b, conflict['proposal_artifact_id'])
        self.assertEqual(archived['kind'], 'stage_proposal_rejected')
        self.assertEqual(archived['reason'], 'checkpoint_conflict')
        self.assertTrue(archived['proposal']['client_commit_key'] == 'commit-4')

    async def test_invalid_observation_rejects_the_whole_handoff(self):
        question, evidence_id = await self.seed_question_and_source()
        bad = self.proposal(question, evidence_id, key='commit-5', rst=30)
        with bind(self.b):
            outcome = await investigations.commit_stage(self.b, bad, owner='worker')
        self.assertFalse(outcome['accepted'])
        self.assertEqual(outcome['reason'], 'checkpoint_invalid')
        self.assertEqual(outcome['field_errors'][0]['reason'], 'value_mismatch')
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'observation'), [],
                         'nothing partial is persisted')
        self.assertIsNone(await investigations.active_checkpoint(self.b, 'worker'))

    async def test_existing_opposition_is_carried_even_when_the_model_omits_it(self):
        question, evidence_id = await self.seed_question_and_source()
        with bind(self.b):
            first = await investigations.commit_stage(self.b, self.proposal(question, evidence_id, key='c-1'),
                                                      owner='worker')
        opposition = await investigations.save_judgment(
            self.b, question_id=question['id'], type='interpretation', content='相反观察',
            opposing_observation_ids=[first['observation_ids'][0]],
            limitations=['反证仍未解释'], status='unresolved')
        second = {'client_commit_key': 'c-2', 'stage_goal': '继续', 'completion': 'partial',
                  'question_ids': [question['id']],
                  'expected_checkpoint_id': first['checkpoint_id'],
                  'expected_revision': 1,
                  'observation_proposals': [], 'judgments': [], 'material_dispositions': [],
                  'open_items': [], 'next_focus': []}
        with bind(self.b):
            outcome = await investigations.commit_stage(self.b, second, owner='worker')
        self.assertEqual(outcome['included_opposition'], [first['observation_ids'][0]],
                         'the stored opposition returns even though the model did not resubmit it')
        self.assertIn(opposition['id'], [row['id'] for row in
                                        await self.b.repository.all(self.b.incident_id, 'judgment')])


class SelectionTests(ActiveWorkspaceCase):
    async def test_worker_input_carries_values_opposition_and_the_unresolved_overview(self):
        evidence_id = 'evidence_sel'
        row = await register_pod(self.b, 'iam', 'app')
        await save_query(self.b, evidence_id, 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': row['uid']},
                         {'status': 'ok', 'summary': {'rst_count': 3}}, 'now', 1, 'ok')
        self.b.task_id = 'task_any'
        question = await investigations.save_question(self.b, text='网络重传是否与失败有关',
                                                      owner_task_ids=[self.b.task_id])
        other = await investigations.save_question(self.b, text='DNS 方向是否仍待核查')
        observation = observations.observation_record(
            self.b, owner='worker', question_ids=[question['id']],
            source_ref={'evidence_id': evidence_id}, field='json_pointer:/summary/rst_count',
            field_value=3, kind='field',
            scope={'resource_refs': [{'id': row['id']}], 'requested_window': None,
                   'returned_window': None, 'scope_known': True, 'coverage_status': 'complete'},
            limitations=['单次计数'], producer='program',
            verification={'source_valid': True, 'value_matches': True, 'scope_known': True,
                          'coverage_status': 'complete'})
        await self.b.repository.save(self.b.incident_id, 'observation', observation)
        judgment = await investigations.save_judgment(
            self.b, question_id=question['id'], type='interpretation', content='计数不能证明根因',
            supporting_observation_ids=[observation['id']], opposing_observation_ids=[observation['id']],
            limitations=['机制未验证'], status='unresolved')
        with bind(self.b):
            visible, manifest = await prepare(self.b, [SystemMessage(content='work'),
                                                       HumanMessage(content='task')], 'worker_step')
        investigation = workbench(visible)['investigation']
        values = [row['field_value'] for row in investigation['observations']]
        self.assertIn(3, values, 'the concrete value travels, not only an id')
        self.assertTrue(any(row['judgment_id'] == judgment['id'] for row in investigation['judgments']))
        self.assertIn('机制未验证', json.dumps(investigation['judgments'], ensure_ascii=False),
                      'limitations travel with the judgment')
        self.assertTrue(investigation['must_handle']['observation_ids'])
        self.assertTrue(investigation['overview']['total_questions'] >= 1,
                        'the unresolved direction stays visible as an overview row')
        self.assertIn('read_investigation', json.dumps(investigation['reading']))
        self.assertIn(observation['id'], manifest['injected_observation_ids'])
        self.assertEqual(manifest['injected_judgment_ids'], [judgment['id']])

    async def test_manifest_records_questions_values_and_three_ranges(self):
        evidence_id = 'evidence_manifest'
        row = await register_pod(self.b, 'iam', 'app')
        await save_query(self.b, evidence_id, 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': row['uid']},
                         {'status': 'ok', 'summary': {'rst_count': 3}}, 'now', 1, 'ok')
        question = await investigations.save_question(self.b, text='一个待核查问题')
        self.b.role = 'triage'
        with bind(self.b):
            visible, manifest = await prepare(self.b, [SystemMessage(content='triage'),
                                                       HumanMessage(content='check iam')], 'triage')
        self.assertEqual(manifest['injected_question_ids'], [question['id']])
        self.assertEqual(manifest['must_handle_ids']['questions'], [question['id']])
        self.assertIn('coverage_three_ranges', manifest)
        usage = manifest['material_usage']
        self.assertIsInstance(usage, list)
        payload_json = workbench(visible)
        self.assertEqual(payload_json['investigation']['questions'][0]['status'], 'open')
