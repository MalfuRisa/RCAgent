"""Round-1 presentation contract: fixed projections, task dedup, windows, caps.

These tests read the real-or-documented sample per Gateway tool
(`fixtures/workspace_tool_samples.json`) and assert that the model-facing brief
keeps key observations, provenance and limitations while dropping archive-only
fields. They never assert private helper names, only the visible contract.
"""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from config import settings
from workspace.repository import Repository, utcnow
from workspace.runtime import Binding, initialize_incident
from context import tool_views, views
from context.assembly import build_payload
from context.views import CONTEXT_VIEW_VERSION

FIXTURES = Path(__file__).parents[1] / 'fixtures'
SAMPLES = json.loads((FIXTURES / 'workspace_tool_samples.json').read_text(encoding='utf-8-sig'))


class ProjectionTests(unittest.TestCase):
    def test_every_tool_brief_keeps_a_traced_observation_and_origin(self):
        for name, sample in SAMPLES['samples'].items():
            with self.subTest(tool=name):
                brief = tool_views.project(name, sample['observations'])
                self.assertFalse(brief.get('unknown_shape'), brief)
                self.assertTrue(brief['observations'], 'projection dropped every observation')
                for key, path in brief['source'].items():
                    self.assertTrue(path, 'observation %s has no source path' % key)

    def test_archived_shapes_keep_the_fields_models_reason_about(self):
        def brief(name):
            return tool_views.project(name, SAMPLES['samples'][name]['observations'])

        self.assertIn('ambiguous', brief('resolve_pod')['observations'])
        self.assertTrue(brief('search_k8s_targets')['observations']['candidates']['candidates'])
        service = brief('get_service_backends')['observations']['service']
        self.assertTrue(service.get('name'), 'service identity must survive')
        self.assertIn('phase', brief('get_pod_runtime_status')['observations'])
        network = brief('get_pod_network_summary')['observations']
        self.assertTrue({'retrans_ratio', 'rtt_avg_ms', 'rst_count'} & set(network), network)
        application = brief('get_pod_application_summary')['observations']
        self.assertTrue({'error_ratio', 'p95_ms', 'total_rps'} & set(application), application)
        self.assertIn('samples', brief('get_abnormal_requests')['observations'])
        self.assertIn('single_pod_abnormal', brief('compare_pod_with_peers')['observations'])
        self.assertIn('metric_key', brief('query_metrics')['observations'])
        self.assertIn('trace_count', brief('search_traces')['observations'])
        self.assertIn('span_count', brief('get_trace')['observations'])
        self.assertIn('bottleneck_span', brief('trace_request')['observations'])
        diagnosed = brief('diagnose_pod')
        self.assertIn('inferences', diagnosed)
        self.assertEqual(diagnosed['inferences']['kind'], 'tool_candidate_diagnosis_not_confirmed_fact')

    def test_zero_null_and_not_found_are_never_interchanged(self):
        raw = {'status': 'ok', 'summary': {'rst_count': 0, 'anomalies': None, 'rtt_avg_ms': 0.0}}
        brief = tool_views.project('get_pod_network_summary', raw)
        self.assertEqual(brief['observations']['rst_count'], 0)
        self.assertEqual(brief['observations']['rtt_avg_ms'], 0.0)
        self.assertIsNone(brief['observations']['anomalies'])
        not_found = tool_views.project('resolve_pod', {'status': 'not_found', 'pods': []})
        self.assertEqual(not_found['result']['status'], 'not_found')
        self.assertNotIn('zero_window', json.dumps(not_found['observations']))

    def test_missing_expected_fields_are_unknown_not_invented(self):
        brief = tool_views.project('get_pod_runtime_status', {'status': 'ok'})
        self.assertIn('status.phase', brief['unknown_fields'])
        self.assertNotIn('phase', brief['observations'])

    def test_unrecognised_shape_keeps_directory_and_limitations_only(self):
        raw = {'weird': {'nested': ['x'] * 50}, 'data_gaps': ['shape changed; no mapping'],
               'warnings': ['schema drift']}
        brief = tool_views.project('get_pod_runtime_status', raw)
        self.assertTrue(brief['unknown_shape'])
        self.assertTrue(brief['detail_required'])
        self.assertEqual(brief['detail_fields'], ['data_gaps', 'warnings', 'weird'])
        self.assertNotIn('nested', json.dumps(brief['observations']))
        self.assertEqual(brief['data_gaps'], ['shape changed; no mapping'])
        self.assertEqual(brief['warnings'], ['schema drift'])

    def test_long_values_become_marked_previews_and_gaps_stay_structured(self):
        raw = {'status': 'ok', 'summary': {'impact_note': 'y' * 5000},
               'data_gaps': [{'source': 'prom', 'reason': 'no sample', 'impact': 'z' * 3000},
                             {'source': 'prom', 'reason': 'no sample', 'impact': 'z' * 3000}]}
        brief = tool_views.project('get_pod_network_summary', raw)
        preview = brief['observations']['impact_note']
        self.assertTrue(preview['preview_incomplete'])
        self.assertTrue(preview['detail_required'])
        self.assertEqual(preview['chars'], 5000)
        self.assertEqual(len(brief['data_gaps']), 1, 'identical gaps collapse to one reference')
        gap = brief['data_gaps'][0]
        self.assertEqual(gap['source'], 'prom')
        self.assertEqual(gap['reason'], 'no sample')
        self.assertTrue(gap['impact']['preview_incomplete'])
        self.assertTrue(gap['detail_preserved'])

    def test_bulky_lists_are_capped_with_counts_and_detail(self):
        rows = [{'status_code': code, 'endpoint': '/x', 'trace_id': 't%d' % i}
                for i, code in enumerate([500, 500, 502, 503, 504, 500, 500, 500])]
        brief = tool_views.project('get_abnormal_requests', {'status': 'ok', 'samples': rows})
        block = brief['observations']['samples']
        self.assertEqual(block['total'], 8)
        self.assertLessEqual(block['shown'], tool_views.MAX_SAMPLE_ITEMS)
        codes = {row['status_code'] for row in block['samples']}
        self.assertTrue({500, 502, 503}.issubset(codes), 'distinct codes must be represented: %s' % codes)
        self.assertTrue(block['detail_required'])

    def test_sample_without_grouping_field_says_it_is_only_a_sample(self):
        rows = [{'endpoint': '/a'}, {'endpoint': '/b'}, {'endpoint': '/c'}, {'endpoint': '/d'}]
        brief = tool_views.project('get_abnormal_requests', {'status': 'ok', 'samples': rows})
        self.assertEqual(brief['observations']['samples']['ordering'], 'original_order_sample_only')

    def test_projection_does_not_mutate_the_archived_response(self):
        for name, sample in SAMPLES['samples'].items():
            with self.subTest(tool=name):
                before = copy.deepcopy(sample['observations'])
                tool_views.project(name, sample['observations'])
                self.assertEqual(sample['observations'], before)


class RecordViewTests(unittest.TestCase):
    def test_task_question_and_goal_are_shown_once_when_identical(self):
        task = {'id': 'task_1', 'priority': 1, 'target_resource_ids': ['r1'],
                'question': 'same text', 'investigation_goal': 'same text',
                'execution_status': 'finished', 'answer_status': 'inconclusive'}
        worker = views.task_view(task, role='worker', main_task_id='task_1')
        self.assertEqual(worker['investigation_goal'], 'same text')
        self.assertNotIn('question', worker)
        self.assertNotIn('question_supplement', worker)
        self.assertFalse(worker['question_conflict'])
        supervisor = views.task_view(task, role='supervisor')
        self.assertEqual(supervisor['question'], 'same text')
        self.assertNotIn('investigation_goal', supervisor)

    def test_task_conflict_keeps_goal_as_requirement_and_marks_conflict(self):
        task = {'id': 'task_1', 'priority': 2, 'target_resource_ids': ['r1'],
                'question': 'older wording', 'investigation_goal': 'executing requirement',
                'execution_status': 'pending', 'answer_status': 'unanswered'}
        worker = views.task_view(task, role='worker', main_task_id='task_1')
        self.assertTrue(worker['question_conflict'])
        self.assertEqual(worker['investigation_goal'], 'executing requirement')
        self.assertIn('older wording', worker['question_supplement'])
        supervisor = views.task_view(task, role='supervisor')
        self.assertTrue(supervisor['question_conflict'])
        self.assertIn('executing requirement', supervisor['question'])

    def test_long_question_becomes_marked_preview_with_read_entry(self):
        task = {'id': 'task_9', 'priority': 1, 'target_resource_ids': [],
                'question': 'q' * 900, 'investigation_goal': 'g' * 900,
                'execution_status': 'pending', 'answer_status': 'unanswered'}
        view = views.task_view(task, role='supervisor')
        self.assertTrue(view['question']['preview_incomplete'])
        self.assertTrue(view['detail_required'])
        self.assertEqual(view['detail'], 'read_task(task_id=task_9)')

    def test_resource_identity_keeps_unknowns_and_history(self):
        resource = {'id': 'r1', 'kind': 'pod', 'name': 'iam', 'cluster': None, 'namespace': None,
                    'uid': None, 'identity_status': 'pending', 'source_refs': ['e1', 'e2']}
        view = views.resource_view(resource)
        self.assertEqual(view['unknown_fields'], ['cluster', 'namespace', 'uid'])
        self.assertEqual(view['source_evidence_ids'], ['e1', 'e2'])
        old = dict(resource, id='r0', cluster='c', namespace='app', uid='u-old',
                   identity_status='resolved', replaced_by='r1')
        historical = views.resource_view(old)
        self.assertEqual((historical['uid'], historical['namespace']), ('u-old', 'app'))
        self.assertEqual(historical['replaced_by'], 'r1')

    def test_windows_are_centralised_and_mismatch_visible(self):
        evidence = [
            {'id': 'e1', 'time_range': {'start_time': 'S1', 'end_time': 'E1'},
             'returned_time_range': {'start_time': 'S2', 'end_time': 'E2'}},
            {'id': 'e2', 'time_range': {'start_time': 'S1', 'end_time': 'E1'}},
        ]
        windows, refs = views.window_table(evidence, {})
        self.assertEqual(len(windows), 2)
        first = views.evidence_view(evidence[0], refs)
        self.assertEqual(first['requested_window_ref'], refs[('S1', 'E1')])
        self.assertEqual(first['window_ref'], refs[('S2', 'E2')])
        self.assertTrue(first['window_mismatch'])
        second = views.evidence_view(evidence[1], refs)
        self.assertNotIn('window_ref', second)
        unknown = views.evidence_view({'id': 'e3', 'tool_name': 'resolve_pod'}, refs)
        self.assertTrue(unknown['window_unknown'])


class AssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        with patch.object(settings, 'WORKSPACE_ROOT', Path(self.temp.name)):
            state = await initialize_incident({'target_cluster': 'c', 'target_namespace': 'app',
                                               'target_resource': 'iam'})
        self.binding = Binding(Repository(self.temp.name), state['incident_id'], state['run_id'],
                               'supervisor', time.time() + 60)

    def snapshot(self, *, resources=(), tasks=(), evidence=(), assessments=()):
        return {'resource': list(resources), 'task': list(tasks), 'evidence': list(evidence),
                'assessment': list(assessments), 'scope': {'cluster': 'c', 'namespaces': ['*']},
                'workspace_revision': 88, 'query_window': {'start_time': 's', 'end_time': 'e'},
                'mcp_budget': 8}

    def resource(self, rid, name='iam'):
        return {'id': rid, 'kind': 'pod', 'name': name, 'cluster': 'c', 'namespace': 'app',
                'uid': 'uid-' + rid, 'identity_status': 'resolved', 'source_refs': []}

    def task(self, tid, resource_id, *, goal=None, question=None):
        return {'id': tid, 'priority': int(tid[-1]) if tid[-1].isdigit() else 1,
                'target_resource_ids': [resource_id], 'investigation_goal': goal or 'goal ' + tid,
                'question': question if question is not None else (goal or 'goal ' + tid),
                'execution_status': 'finished', 'answer_status': 'inconclusive',
                'latest_attempt_id': 'att_' + tid, 'resource_id': resource_id}

    def evidence(self, eid, resource_id, *, tool='get_pod_runtime_status', status='ok', run='run_x'):
        return {'id': eid, 'tool_name': tool, 'run_id': run, 'resource_ids': [resource_id],
                'resource_identities': [{'id': resource_id, 'revision': 1}],
                'transport_status': status, 'source_status': status, 'observations': {'status': status},
                'data_gaps': [], 'time_range': None, 'returned_time_range': None,
                'arguments': {}, 'reuse_key': None, 'finished_at': '2026-01-01T00:00:00+00:00'}

    def test_supervisor_gets_global_table_and_latest_judgments(self):
        resources = [self.resource('r1'), self.resource('r2', 'other')]
        tasks = [self.task('t1', 'r1'), self.task('t2', 'r2')]
        evidence = [self.evidence('e1', 'r1'), self.evidence('e2', 'r2')]
        assessments = [
            {'id': 'a1', 'task_id': 't1', 'attempt_id': 'att_t1', 'producer': 'worker',
             'answer_status': 'inconclusive', 'diagnosis': 'candidate', 'supporting_evidence_ids': ['e1'],
             'opposing_evidence_ids': [], 'gaps': ['missing logs'], 'hypotheses': []},
            {'id': 'a2', 'task_id': 't2', 'attempt_id': 'att_t2', 'producer': 'worker',
             'answer_status': 'supported', 'diagnosis': 'other', 'supporting_evidence_ids': ['e2'],
             'opposing_evidence_ids': [], 'gaps': [], 'hypotheses': []},
        ]
        payload = build_payload(self.binding, self.snapshot(resources=resources, tasks=tasks,
                                                            evidence=evidence, assessments=assessments),
                                'initial_submission')
        self.assertEqual(payload['context_view_version'], CONTEXT_VIEW_VERSION)
        self.assertEqual([t['task_id'] for t in payload['tasks']], ['t1', 't2'])
        self.assertEqual([a['assessment_id'] for a in payload['assessments']], ['a1', 'a2'])
        self.assertTrue(all(a['is_model_judgment'] for a in payload['assessments']))
        self.assertEqual(payload['coverage']['totals']['evidence'], 2)
        self.assertEqual(payload['role'], 'supervisor')

    def test_worker_sees_own_full_task_and_referenced_opposition_only(self):
        resources = [self.resource('r1'), self.resource('r2', 'other')]
        tasks = [self.task('t1', 'r1', goal='Check DNS'), self.task('t2', 'r2')]
        evidence = [self.evidence('e1', 'r1'), self.evidence('e2', 'r2'), self.evidence('e3', 'r2')]
        assessments = [{'id': 'a1', 'task_id': 't1', 'attempt_id': 'att_t1', 'producer': 'worker',
                        'answer_status': 'inconclusive', 'diagnosis': 'x',
                        'supporting_evidence_ids': ['e1'], 'opposing_evidence_ids': ['e2'],
                        'gaps': ['need samples'], 'hypotheses': []}]
        self.binding.role, self.binding.task_id, self.binding.attempt_id = 'worker', 't1', 'att_t1'
        self.binding.mcp_calls = 3
        payload = build_payload(self.binding, self.snapshot(resources=resources, tasks=tasks,
                                                            evidence=evidence, assessments=assessments), 'request')
        self.assertEqual([t['task_id'] for t in payload['tasks']], ['t1'])
        self.assertEqual(payload['tasks'][0]['investigation_goal'], 'Check DNS')
        briefed = {row['evidence_id'] for row in payload['evidence']}
        self.assertIn('e1', briefed)
        self.assertIn('e2', briefed, 'opposition evidence referenced by the judgment must be visible')
        self.assertEqual(payload['budget'], {'mcp_calls_used': 3, 'mcp_calls_budget': 8})
        unrelated = next(row for row in payload['tasks'] if row['task_id'] == 't2') if any(
            row['task_id'] == 't2' for row in payload['tasks']) else None
        self.assertIsNone(unrelated, 'the Worker must not expand unrelated tasks')

    def test_failures_and_gaps_stay_in_the_overview(self):
        resources = [self.resource('r1')]
        tasks = [self.task('t1', 'r1')]
        evidence = [self.evidence('e1', 'r1', status='error'), self.evidence('e2', 'r1', status='not_found')]
        self.binding.role, self.binding.task_id = 'worker', 't1'
        payload = build_payload(self.binding, self.snapshot(resources=resources, tasks=tasks, evidence=evidence), 'request')
        statuses = {row.get('result_transport_status') for row in payload['evidence']}
        self.assertEqual(statuses, {'error', 'not_found'})

    def test_no_focus_lists_this_runs_evidence_not_the_whole_database(self):
        resources = [self.resource('r1'), self.resource('r2', 'other')]
        evidence = [self.evidence('e1', 'r1', run=self.binding.run_id),
                    self.evidence('e2', 'r2', run='old_run')]
        payload = build_payload(self.binding, self.snapshot(resources=resources, evidence=evidence), 'local_read')
        listed = {row['evidence_id'] for row in payload['evidence'] + payload.get('evidence_directory', [])}
        self.assertEqual(listed, {'e1'})
        self.assertEqual(payload['target_focus']['resource_ids'], ['r1'])
        # The old run's resource is outside this run's evidence, so it is omitted too.
        self.assertEqual(payload['coverage']['omitted'], {'resource': 1, 'evidence': 1})

    def test_related_but_uncapped_evidence_becomes_a_directory_row(self):
        resources = [self.resource('r1')]
        tasks = [self.task('t1', 'r1')]
        evidence = [self.evidence('e%d' % i, 'r1') for i in range(30)]
        assessments = [{'id': 'a1', 'task_id': 't1', 'attempt_id': 'att_t1', 'producer': 'worker',
                        'answer_status': 'unanswered', 'diagnosis': '', 'supporting_evidence_ids': ['e0'],
                        'opposing_evidence_ids': [], 'gaps': [], 'hypotheses': []}]
        self.binding.role, self.binding.task_id = 'worker', 't1'
        payload = build_payload(self.binding, self.snapshot(resources=resources, tasks=tasks,
                                                            evidence=evidence, assessments=assessments), 'request')
        self.assertLess(len(payload['evidence']), 30)
        self.assertTrue(payload['evidence_directory'], 'uncapped related evidence must stay discoverable')
        self.assertEqual(payload['coverage']['totals']['evidence'], 30)
        self.assertEqual(payload['coverage']['shown']['evidence'], 30)


if __name__ == '__main__':
    unittest.main()