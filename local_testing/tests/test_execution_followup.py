"""Terminal behavior across actual gate, persisted run, and preflight boundaries."""
from unittest.mock import patch
from test_workspace import WorkspaceCase
from workspace.runtime import initialize_incident, record_execution
from config import settings
from graph import evidence_gate
from local_testing.live_checks import iam_inventory_check


class ExecutionFollowup(WorkspaceCase):
    def state(self, **extra):
        return {'incident_id': self.b.incident_id, 'run_id': self.b.run_id,
                'workspace_root': str(self.root), 'deadline': self.b.deadline, 'evidence_status': 'available', **extra}

    async def finish_gate(self, **extra):
        result = await evidence_gate(self.state(**extra))
        run = await self.b.repository.read(self.b.incident_id, self.b.run_id, 'run')
        for field in ('execution_status', 'execution_error', 'stop_reason', 'secondary_stop_reasons', 'execution_errors'):
            self.assertEqual(result[field], result['final_report'][field])
            self.assertEqual(result[field], run[field])
        return result

    async def test_two_workers_stable_primary_error_across_completion_order(self):
        one = {'task_id': 'task_a', 'attempt_id': 'attempt_2', 'execution_error': {'code': 'model_timeout', 'message': 'timeout'}}
        two = {'task_id': 'task_b', 'attempt_id': 'attempt_1', 'execution_error': {'code': 'model_rate_limited', 'message': '429'}}
        for analyses in ([one, two], [two, one]):
            result = await self.finish_gate(rca_analyses_list=analyses)
            self.assertEqual(result['stop_reason'], 'model_timeout')
            self.assertEqual(len(result['execution_errors']), 2)

    async def test_parent_error_precedes_workers_and_preserves_old_reason(self):
        result = await self.finish_gate(execution_error={'code': 'parent_error', 'message': 'parent'}, stop_reason='no_progress',
            rca_analyses_list=[{'task_id': 'a', 'execution_error': {'code': 'model_timeout'}}])
        self.assertEqual(result['stop_reason'], 'parent_error')
        self.assertIn('no_progress', result['secondary_stop_reasons'])
        self.assertEqual(len(result['execution_errors']), 2)

    async def test_malformed_error_codes_preserve_original(self):
        for error in ({}, {'code': '  ', 'message': 'original'}, {'code': 42}, 'raw failure'):
            result = await self.finish_gate(execution_error=error)
            self.assertEqual(result['stop_reason'], 'execution_failed')
            self.assertEqual(result['execution_errors'][0]['error'], error)

    async def test_normal_inconclusive_and_real_stops(self):
        for reason in (None, 'not_found', 'no_progress', 'partially_invalid_supervisor_report'):
            result = await self.finish_gate(stop_reason=reason)
            self.assertEqual(result['stop_reason'], reason)
            self.assertIsNone(result['execution_error'])
            self.assertEqual(result['final_report']['answer_status'], 'inconclusive')

    async def test_followup_gate_remains_open(self):
        result = await evidence_gate(self.state(tasks_to_execute_ids=['task_a'], final_report={}))
        self.assertEqual(result, {})
        failed = await evidence_gate(self.state(tasks_to_execute_ids=['task_a'], final_report={}, execution_error={}))
        self.assertEqual(failed['stop_reason'], 'execution_failed')

    async def test_new_run_does_not_inherit_historical_worker_failure(self):
        await record_execution(self.b, 'failed', {'code': 'model_timeout'})
        old = self.state(target_cluster='c', stop_reason='model_timeout', execution_error={'code': 'model_timeout'},
                         rca_analyses_list=[{'run_id': self.b.run_id, 'execution_error': {'code': 'model_timeout'}}])
        with patch.object(settings, 'WORKSPACE_ROOT', self.root):
            fresh = {**old, **await initialize_incident(old)}
        result = await evidence_gate(fresh)
        self.assertIsNone(result['execution_error'])
        self.assertIsNone(result['stop_reason'])
        self.assertNotEqual(fresh['run_id'], self.b.run_id)
        prior = await self.b.repository.read(self.b.incident_id, self.b.run_id, 'run')
        self.assertEqual(prior['stop_reason'], 'model_timeout')

    async def test_old_record_execution_signature_still_works(self):
        run = await record_execution(self.b, 'completed')
        self.assertIsNone(run['stop_reason'])
        self.assertIsNone(run['execution_error'])

    async def test_inventory_error_empty_partial_and_ambiguous_never_generate_identity(self):
        good = {'kind': 'pod', 'name': 'iam', 'uid': 'u', 'cluster': 'rd2', 'namespace': 'kylin-insights'}
        samples = [({'status': 'error', 'error': 'lookup server-internal-api: i/o timeout'}, 'inventory_error'),
                   ({'candidates': []}, 'inventory_not_found'),
                   ({'candidates': [good], 'meta': {'truncated': True}}, 'inventory_partial'),
                   ({'candidates': [good, dict(good, uid='v')]}, 'inventory_identity_unresolved')]
        for raw, expected in samples:
            blocked, identity = iam_inventory_check(raw)
            self.assertIsNone(identity)
            self.assertEqual(blocked['classification'], expected)
            self.assertEqual(blocked['inventory_result'], raw)
            self.assertEqual(blocked['model_attempts'], 0)
        blocked, identity = iam_inventory_check({'candidates': [good]})
        self.assertIsNone(blocked)
        self.assertEqual(identity, good)
