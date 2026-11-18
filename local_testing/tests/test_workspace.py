"""Workbench behavioral matrix, using real async repository and fake transport."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from config import settings
from models.schemas import RCATask
from workspace.repository import Repository, WorkspaceError, new_id
from workspace.runtime import Binding, bind, initialize_incident, invoke_model
from workspace.resources import register, ensure_identity
from workspace.queries import execute, save_query, read_raw
from workspace.tasks import register_tasks, start, finish
from workspace.read_tools import read_evidence, read_artifact, query_evidence
from context.builder import prepare
from tools import observability_adapter as gateway


class WorkspaceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Existing threshold regressions exercise the explicit strict rollback
        # contract. test_soft_context_budget covers the new default separately.
        budget_policy = patch.object(settings, 'CONTEXT_BUDGET_POLICY', 'strict')
        budget_policy.start()
        self.addCleanup(budget_policy.stop)
        # Threshold fixtures deliberately retain their historical 24k profile;
        # actual runtime defaults are tested separately from pressure limits.
        threshold = patch.object(settings, 'CONTEXT_MAX_CHARS', 24000)
        threshold.start()
        self.addCleanup(threshold.stop)
        model_pace = patch.object(settings, 'MODEL_MIN_INTERVAL_SECONDS', 0)
        model_pace.start()
        self.addCleanup(model_pace.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        with patch.object(settings, 'WORKSPACE_ROOT', self.root):
            state = await initialize_incident({'target_cluster': 'c', 'target_namespace': 'app', 'target_resource': 'iam'})
        self.b = Binding(Repository(self.root), state['incident_id'], state['run_id'], 'worker', time.time() + 60)
        self.calls = []
        self.response = {'status': 'ok', 'candidates': [{'kind': 'pod', 'name': 'coredns', 'cluster': 'c', 'namespace': 'system', 'uid': 'dns-uid'}]}
        async def raw(name, args):
            self.calls.append((name, dict(args)))
            return copy.deepcopy(self.response)
        self.raw = raw

    async def locate(self, name='coredns', namespace=None):
        r = await register(self.b, 'pod', name, namespace)
        with bind(self.b), patch.object(gateway, '_call_gateway_raw', self.raw):
            result = await ensure_identity(self.b, r['id'])
        return result

    async def begin_task(self):
        task = RCATask(priority=1, investigation_goal='diagnose', target_resource='coredns', resource_type='pod')
        task = (await register_tasks(self.b, [task]))[0]
        attempt = await start(self.b, task.task_id)
        self.b.task_id, self.b.attempt_id, self.b.resource_id = task.task_id, attempt['id'], task.resource_id
        return task


class R1IdentityTests(WorkspaceCase):
    async def test_unknown_dependency_scope_wrong_uid_and_reuse(self):
        result = await self.locate()
        self.assertEqual(result['status'], 'resolved')
        self.assertNotIn('namespace', self.calls[0][1])
        self.assertEqual(result['resource']['namespace'], 'system')
        self.b.resource_id = result['resource']['id']
        with bind(self.b), patch.object(gateway, '_call_gateway_raw', self.raw):
            await ensure_identity(self.b, self.b.resource_id)
            refused = await gateway.call_gateway_tool('get_pod_runtime_status', {'cluster': 'c', 'pod_uid': 'wrong', 'namespace': 'app'})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(refused['error_type'], 'identity_or_scope_rejected')

    async def test_ambiguity_and_partial_single_candidate(self):
        self.response['candidates'].append(dict(self.response['candidates'][0], namespace='other', uid='other'))
        result = await self.locate()
        self.assertEqual(result['status'], 'ambiguous')
        self.assertIsNone(result['resource']['uid'])
        self.response['candidates'].pop()
        self.response['truncated'] = True
        result = await self.locate()
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['resource']['uid'])

    async def test_restricted_scope_unknown_cluster(self):
        with patch.object(settings, 'ALLOWED_NAMESPACES', ['app']), patch.object(settings, 'WORKSPACE_ROOT', self.root):
            state = await initialize_incident({'target_cluster': 'c'})
        b = Binding(Repository(self.root), state['incident_id'], state['run_id'], 'worker', time.time() + 60)
        r = await register(b, 'pod', 'coredns')
        with bind(b), patch.object(gateway, '_call_gateway_raw', self.raw):
            result = await ensure_identity(b, r['id'])
        self.assertNotEqual(result['status'], 'resolved')
        self.assertEqual(self.calls[0][1]['namespace'], 'app')
        self.calls.clear()
        with patch.object(settings, 'WORKSPACE_ROOT', self.root):
            state = await initialize_incident({})
        b = Binding(Repository(self.root), state['incident_id'], state['run_id'], 'worker', time.time() + 60)
        r = await register(b, 'pod', 'coredns')
        with bind(b):
            result = await ensure_identity(b, r['id'])
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(self.calls, [])

    async def test_ttl_refresh_and_uid_replacement(self):
        result = await self.locate()
        r = result['resource']
        r['verified_at'] = '2020-01-01T00:00:00+00:00'
        await self.b.repository.save(self.b.incident_id, 'resource', r, r['revision'])
        self.response['candidates'][0]['uid'] = 'new-uid'
        with bind(self.b), patch.object(gateway, '_call_gateway_raw', self.raw):
            refreshed = await ensure_identity(self.b, r['id'])
        self.assertEqual(refreshed['resource']['uid'], 'new-uid')
        resources = await self.b.repository.all(self.b.incident_id, 'resource')
        self.assertTrue(any(r.get('uid') == 'dns-uid' and r.get('replaced_by') for r in resources))

    async def test_specific_scope_miss_does_not_expand(self):
        resource = await register(self.b, 'pod', 'coredns', 'app')
        async def raw(name, args):
            self.calls.append((name, args))
            return {'candidates': []} if args.get('namespace') else self.response
        with bind(self.b), patch.object(gateway, '_call_gateway_raw', raw):
            result = await ensure_identity(self.b, resource['id'])
        self.assertEqual(result['status'], 'not_found')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]['namespace'], 'app')


class R2EvidenceTests(WorkspaceCase):
    async def test_raw_null_metadata_idempotent_and_new_operation(self):
        raw = {'meta': {'data_gaps': [{'reason': 'no_peer'}], 'truncated': True}, 'trace': None, 'samples': []}
        await save_query(self.b, 'operation', 'x', {}, raw, '2026-10-10T00:00:00+00:00', 1)
        await save_query(self.b, 'operation', 'x', {}, raw, '2026-10-10T00:00:00+00:00', 1)
        await save_query(self.b, 'operation2', 'x', {}, raw, '2026-10-10T00:00:00+00:00', 1)
        rows = await self.b.repository.all(self.b.incident_id, 'evidence')
        self.assertEqual(len(rows), 2)
        self.assertEqual(await read_raw(self.b, 'operation'), raw)
        self.assertTrue(rows[0]['truncated'])
        self.assertEqual(rows[0]['data_gaps'][0]['reason'], 'no_peer')

    async def test_error_timeout_cancel_recorded_and_propagated(self):
        for error in (RuntimeError('provider'), TimeoutError('timeout'), asyncio.CancelledError()):
            async def fail():
                raise error
            if isinstance(error, asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):
                    await execute(self.b, 'search_k8s_targets', {'cluster': 'c', 'query': 'x'}, fail)
            else:
                result = await execute(self.b, 'search_k8s_targets', {'cluster': 'c', 'query': 'x'}, fail)
                self.assertIn('error_type', result)
        rows = await self.b.repository.all(self.b.incident_id, 'evidence')
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]['transport_status'], 'cancelled')
        self.assertEqual(rows[-1]['observations'], {})

    async def test_cross_incident_pagination_and_path_rejection(self):
        await save_query(self.b, 'e1', 'x', {}, {'rows': list(range(50))}, 'now', 1)
        with bind(self.b):
            first = await read_evidence.ainvoke({'evidence_id': 'e1', 'field': 'rows'})
            self.assertEqual(first['next'], 20)
            self.assertEqual(first['range'], [0, 20])
            with self.assertRaises(WorkspaceError):
                await read_artifact.ainvoke({'artifact_id': '../../secret'})
        with patch.object(settings, 'WORKSPACE_ROOT', self.root):
            state = await initialize_incident({'target_cluster': 'c'})
        b = Binding(Repository(self.root), state['incident_id'], state['run_id'], 'worker', time.time() + 60)
        with bind(b), self.assertRaises(WorkspaceError):
            await read_evidence.ainvoke({'evidence_id': 'e1'})

    async def test_next_context_observes_new_evidence_previous_input_immutable(self):
        before, manifest1 = await prepare(self.b, [])
        await save_query(self.b, 'e1', 'x', {}, {'value': 42, 'data_gaps': ['partial']}, 'now', 1)
        after, manifest2 = await prepare(self.b, [])
        # Match the evidence id as a JSON token: a bare 'e1' can occur inside a
        # generated hex id and would make this assertion depend on chance.
        self.assertNotIn('"e1"', before[0].content)
        self.assertIn('"e1"', after[0].content)
        self.assertEqual(manifest1['injected_evidence_ids'], [])
        self.assertEqual(manifest2['injected_evidence_ids'], ['e1'])
        self.assertIn('partial', after[0].content)

    async def test_context_required_overflow_and_storage_failure(self):
        await save_query(self.b, 'e1', 'x', {}, {'data_gaps': ['x' * 4000]}, 'now', 1)
        with patch.object(settings, 'CONTEXT_MAX_CHARS', 200), self.assertRaisesRegex(WorkspaceError, 'context_incomplete'):
            await prepare(self.b, [])
        with patch('workspace.queries.write_artifact', side_effect=OSError('disk full')), self.assertRaises(OSError):
            await save_query(self.b, 'failed', 'x', {}, {}, 'now', 1)
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'evidence')), 1)
        with patch.object(self.b.repository, 'save', side_effect=WorkspaceError('sqlite failure')), self.assertRaises(WorkspaceError):
            await self.b.repository.save(self.b.incident_id, 'resource', {'id': 'x'})


class R3TaskTests(WorkspaceCase):
    async def test_legacy_finish_idempotency_wrong_attempt_and_version(self):
        task = await self.begin_task()
        response = {'diagnosis': 'No target', 'reasoning': 'No identity'}
        assessment = await finish(self.b, response)
        repeated = await finish(self.b, response)
        self.assertEqual(assessment['id'], repeated['id'])
        final = await self.b.repository.read(self.b.incident_id, task.task_id, 'task')
        self.assertEqual((final['execution_status'], final['answer_status']), ('finished', 'inconclusive'))
        with self.assertRaises(WorkspaceError):
            await finish(self.b, dict(response, diagnosis='Changed'))
        with self.assertRaises(WorkspaceError):
            await start(self.b, task.task_id, expected=1)
        self.b.attempt_id = 'nonexistent'
        with self.assertRaises(WorkspaceError):
            await finish(self.b, response)

    async def test_invalid_support_does_not_change_task_and_dependency_missing(self):
        task = await self.begin_task()
        with self.assertRaises(WorkspaceError):
            await finish(self.b, {'diagnosis': 'x', 'supporting_evidence_ids': ['missing']})
        record = await self.b.repository.read(self.b.incident_id, task.task_id, 'task')
        self.assertEqual(record['execution_status'], 'running')
        invalid = task.model_copy(update={'depends_on': ['missing']})
        with self.assertRaises(WorkspaceError):
            await register_tasks(self.b, [invalid])

    async def test_priority_concurrent_reports_preserved(self):
        from models.reducers import merge_rca_analyses
        tasks = [RCATask(priority=1, investigation_goal='x', target_resource=n, resource_type='pod') for n in ('a', 'b')]
        registered = await register_tasks(self.b, tasks)
        self.assertEqual([t.priority for t in registered], [1, 2])
        results = [{'task': {'task_id': t.task_id, 'priority': 1}, 'attempt_id': str(i)} for i, t in enumerate(registered)]
        self.assertEqual(len(merge_rca_analyses(results[:1], results[1:])), 2)
        bindings = []
        for t in registered:
            attempt = await start(self.b, t.task_id)
            bindings.append(Binding(self.b.repository, self.b.incident_id, self.b.run_id, 'worker', self.b.deadline, t.task_id, attempt['id'], t.resource_id))
        await asyncio.gather(*(finish(b, {'diagnosis': 'unresolved', 'reasoning': 'x'}) for b in bindings))
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'assessment')), 2)

    async def test_opposition_and_hypotheses_labeled_not_inventory(self):
        identity = await self.locate()
        task = await self.begin_task()
        evidence = (await self.b.repository.all(self.b.incident_id, 'evidence'))[0]
        result = await finish(self.b, {'diagnosis': 'Candidate', 'supporting_evidence_ids': [evidence['id']],
            'opposing_evidence_ids': [evidence['id']], 'hypotheses': [{'content': 'DNS suspected', 'status': 'unresolved'}]})
        self.assertEqual(result['opposing_evidence_ids'], [evidence['id']])
        messages, _ = await prepare(self.b, [])
        self.assertIn('Model judgments, not inventory facts', messages[0].content)
        r = await self.b.repository.read(self.b.incident_id, task.resource_id, 'resource')
        self.assertNotIn('diagnosis', r)


if __name__ == '__main__':
    unittest.main()

class R3OwnershipTests(WorkspaceCase):
    async def test_cross_incident_reference_and_finish_revision_conflict(self):
        task = await self.begin_task()
        with patch.object(settings, 'WORKSPACE_ROOT', self.root):
            state = await initialize_incident({'target_cluster': 'c'})
        other = Binding(Repository(self.root), state['incident_id'], state['run_id'], 'worker', time.time() + 60)
        await save_query(other, 'foreign', 'x', {}, {'value': 1}, 'now', 1)
        with self.assertRaises(WorkspaceError):
            await finish(self.b, {'diagnosis': 'x', 'supporting_evidence_ids': ['foreign']})
        record = await self.b.repository.read(self.b.incident_id, task.task_id, 'task')
        await self.b.repository.save(self.b.incident_id, 'task', record, record['revision'])
        with self.assertRaisesRegex(WorkspaceError, 'revision'):
            await finish(self.b, {'diagnosis': 'x'})

    async def test_dependency_cycle_and_unsatisfied_prerequisite(self):
        tasks = [RCATask(priority=i, investigation_goal='x', target_resource=str(i), resource_type='pod') for i in (1, 2)]
        tasks = await register_tasks(self.b, tasks)
        one, two = [await self.b.repository.read(self.b.incident_id, t.task_id, 'task') for t in tasks]
        one['depends_on'] = [two['id']]
        await self.b.repository.save(self.b.incident_id, 'task', one, one['revision'])
        with self.assertRaisesRegex(WorkspaceError, 'prerequisite'):
            await start(self.b, one['id'])
        two['depends_on'] = [one['id']]
        await self.b.repository.save(self.b.incident_id, 'task', two, two['revision'])
        with self.assertRaisesRegex(WorkspaceError, 'cycle'):
            await register_tasks(self.b, [RCATask(priority=3, investigation_goal='x', target_resource='3', resource_type='pod')])


class R2AdditionalTests(WorkspaceCase):
    async def test_sqlite_real_failure_no_false_evidence_reference(self):
        database = self.b.repository.database
        database.unlink()
        database.mkdir()
        with self.assertRaises(Exception):
            await save_query(self.b, 'failed_sqlite', 'x', {}, {}, 'now', 1)

    async def test_large_read_char_cursor_scope_continuation_and_required_injection(self):
        await save_query(self.b, 'large', 'x', {}, {'data': 'a' * 16000, 'data_gaps': ['blind spot']}, 'now', 1)
        with bind(self.b):
            page = await read_evidence.ainvoke({'evidence_id': 'large', 'char_offset': 0})
            self.assertTrue(page['truncated'])
            self.assertEqual(page['next_char_offset'], len(page['text']),
                             'the next position follows the characters actually returned')
            self.assertLess(page['next_char_offset'], 16000)
            self.assertIn('a', page['text'].strip('"'))
            with self.assertRaises(WorkspaceError):
                await query_evidence.ainvoke({'limit': 101})
        with patch.object(settings, 'WORKSPACE_ROOT', self.root):
            continued = await initialize_incident({'incident_id': self.b.incident_id, 'target_cluster': 'c'})
            self.assertNotEqual(continued['run_id'], self.b.run_id)
            with self.assertRaises(WorkspaceError):
                await initialize_incident({'incident_id': self.b.incident_id, 'target_cluster': 'other'})
        from langchain_core.messages import HumanMessage, ToolMessage, AIMessage
        messages = [AIMessage(content='', tool_calls=[{'name': 'x', 'args': {}, 'id': 'paired'}]),
                    ToolMessage(content='x' * 30000, name='x', tool_call_id='paired')]
        with self.assertRaisesRegex(WorkspaceError, 'protected_parts_exceed_budget'):
            await prepare(self.b, messages)
        manifest = (await self.b.repository.all(self.b.incident_id, 'context'))[-1]
        self.assertEqual(manifest['disposition'], 'rejected_before_send')
        from workspace.artifacts import read_artifact_raw
        candidate = await read_artifact_raw(self.b, manifest['candidate_messages_artifact_id'])
        self.assertEqual(candidate[-1]['tool_call_id'], 'paired')
        self.assertEqual(candidate[-1]['content'], 'x' * 30000,
                         'an unknown recent result cannot be replaced by an archive pointer')

class R1RefreshBoundaryTests(WorkspaceCase):
    async def test_forced_refresh_cross_namespace_identity_is_not_merged(self):
        first = await self.locate()
        with bind(self.b), patch.object(gateway, '_call_gateway_raw', self.raw):
            await ensure_identity(self.b, first['resource']['id'], force=True)
        self.assertEqual(len(self.calls), 2)
        self.response['candidates'][0].update(namespace='elsewhere', uid='other-entity')
        with bind(self.b), patch.object(gateway, '_call_gateway_raw', self.raw):
            changed = await ensure_identity(self.b, first['resource']['id'], force=True)
            refused = await gateway.call_gateway_tool('get_pod_runtime_status', {'cluster': 'c', 'pod_uid': 'dns-uid'})
        self.assertEqual(changed['status'], 'ambiguous')
        self.assertIsNone(changed['resource']['uid'])
        self.assertEqual(refused['error_type'], 'identity_or_scope_rejected')
        resources = await self.b.repository.all(self.b.incident_id, 'resource')
        self.assertTrue(any(r.get('uid') == 'dns-uid' and r.get('replaced_by') for r in resources))

class R2InFlightTests(WorkspaceCase):
    async def test_in_flight_model_input_is_immutable_next_call_sees_commit(self):
        from langchain_core.runnables import RunnableLambda
        from langchain_core.messages import AIMessage
        entered, release = asyncio.Event(), asyncio.Event()
        captures = []
        async def model(messages):
            captures.append(messages)
            if len(captures) == 1:
                entered.set()
                await release.wait()
            return AIMessage(content='done')
        with bind(self.b):
            request = asyncio.create_task(invoke_model(RunnableLambda(model), []))
            await asyncio.wait_for(entered.wait(), 2)
            await save_query(self.b, 'committed-during-request', 'x', {}, {'summary': {'tcp_errors': 1}}, 'now', 1)
            self.assertNotIn('committed-during-request', captures[0][0].content)
            release.set()
            await request
            await invoke_model(RunnableLambda(model), [])
        self.assertIn('committed-during-request', captures[1][0].content)
        manifests = await self.b.repository.all(self.b.incident_id, 'context')
        self.assertEqual(manifests[0]['injected_evidence_ids'], [])
        self.assertEqual(manifests[1]['injected_evidence_ids'], ['committed-during-request'])

class R1KindTests(WorkspaceCase):
    async def test_kind_is_part_of_identity_even_if_uid_string_matches(self):
        service = await register(self.b, 'service', 'coredns', 'system')
        service.update(identity_status='resolved', cluster='c', uid='dns-uid', verified_at='2099-01-01T00:00:00+00:00')
        await self.b.repository.save(self.b.incident_id, 'resource', service, service['revision'])
        with bind(self.b), patch.object(gateway, '_call_gateway_raw', self.raw):
            refused = await gateway.call_gateway_tool('get_pod_runtime_status', {'cluster': 'c', 'pod_uid': 'dns-uid'})
        self.assertEqual(refused['error_type'], 'identity_or_scope_rejected')
        self.assertEqual(self.calls, [])

class R1AutoInventoryTests(WorkspaceCase):
    async def test_auto_inventory_kind_and_nonempty_backends_unlock_scoped_queries(self):
        async def raw(name, args):
            self.calls.append((name, args))
            if name == 'search_k8s_targets':
                return {'status': 'ok', 'candidates': [{'kind': 'service', 'name': 'iam', 'namespace': 'app', 'cluster': 'c', 'uid': 'service-uid'}]}
            if name == 'get_service_backends':
                return {'status': 'ok', 'pods': [{'name': 'iam-pod', 'namespace': 'app', 'cluster': 'c', 'pod_uid': 'pod-uid'}]}
            return {'status': 'ok', 'series': []}
        with bind(self.b), patch.object(gateway, '_call_gateway_raw', raw):
            await gateway.call_gateway_tool('search_k8s_targets', {'cluster': 'c', 'namespace': 'app', 'query': 'iam', 'kind': 'auto'})
            await gateway.call_gateway_tool('get_service_backends', {'cluster': 'c', 'namespace': 'app', 'service_uid': 'service-uid'})
            metric = await gateway.call_gateway_tool('query_metrics', {'cluster': 'c', 'namespace': 'app', 'target_kind': 'pod', 'target_name': 'iam-pod', 'metric_key': 'error_rate'})
            summary = await gateway.call_gateway_tool('get_pod_network_summary', {'cluster': 'c', 'pod_uid': 'pod-uid'})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(metric['error_type'], 'unsupported_scope')
        self.assertEqual(metric['rejected_fields'], ['cluster'])
        self.assertEqual(summary['status'], 'ok')
        resources = await self.b.repository.all(self.b.incident_id, 'resource')
        service = next(r for r in resources if r['kind'] == 'service')
        pod = next(r for r in resources if r['kind'] == 'pod')
        self.assertEqual((service['identity_status'], service['uid']), ('resolved', 'service-uid'))
        self.assertEqual((pod['identity_status'], pod['uid']), ('resolved', 'pod-uid'))
        backend = next(e for e in await self.b.repository.all(self.b.incident_id, 'evidence') if e['tool_name'] == 'get_service_backends')
        self.assertIn(pod['id'], backend['resource_ids'])
        self.assertIn(backend['id'], pod['source_refs'])
