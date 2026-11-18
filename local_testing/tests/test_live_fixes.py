"""2026-10-13 PRD: real-contract behavior, controlled transports and visible inputs."""
import asyncio
import json
import time
from unittest.mock import patch
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from test_workspace import WorkspaceCase
import test_acceptance_rework as acceptance
from workspace.runtime import bind, invoke_model
from workspace.queries import execute, read_raw
from workspace.repository import WorkspaceError
from workspace.model_requests import ModelRequestError, controller
from workspace.report_checks import normalize_proposal, inspect_proposal
from workspace.reports import adjudicate
from context.builder import prepare
from config import settings


class LiveFixes(WorkspaceCase):
    resolved = acceptance.AcceptanceBoundaries.resolved
    task_for = acceptance.AcceptanceBoundaries.task_for
    oom = acceptance.AcceptanceBoundaries.oom
    claim = acceptance.AcceptanceBoundaries.claim

    async def test_empty_statement_is_rejected_and_cannot_erase_observations(self):
        await self.setup_pod()
        result = await execute(self.b, 'diagnose_pod', {'cluster': 'c', 'namespace': 'system', 'pod': 'coredns'}, self.query)
        claim = {'claim_id': 'empty', 'type': 'observation', 'resource_id': self.resource['id'],
                 'supporting_evidence_ids': [result['_rcagent_query']['evidence_id']], 'limitations': ['this Pod and window']}
        for content in (None, '', '  '):
            proposal = dict(claim)
            if content is not None:
                proposal['content'] = content
            claims, items, _ = await normalize_proposal(self.b, [proposal], [], {})
            report = await adjudicate(self.b, {}, claims, report_items=items)
            self.assertTrue(report['report_errors'])
            self.assertEqual(report['statements'], [])
            self.assertTrue(report['evidence_summary'].strip())
            self.assertEqual(report['answer_status'], 'inconclusive')

    async def setup_pod(self):
        self.resource = await self.resolved('coredns', 'system', 'dns-uid')
        self.b.resource_id = self.resource['id']
        self.b.query_window = {'start_time': '2026-10-13T00:00:00+00:00', 'end_time': '2026-10-13T00:15:00+00:00'}
        self.remote = []
        async def raw(args):
            self.remote.append(dict(args))
            return {'status': 'ok', 'summary': {'tcp_errors': 7}, 'meta': {'time_range': {
                'start_time': args['start_time'], 'end_time': args['end_time']}}}
        self.query = raw

    async def test_B1_B2_budget_switch_preserves_long_messages_and_records_size(self):
        self.b.role = 'triage'
        messages = [SystemMessage(content='instruction'), HumanMessage(content='x' * 30000)]
        with patch.object(settings, 'CONTEXT_BUDGET_ENABLED', False):
            visible, manifest = await prepare(self.b, messages)
        self.assertEqual(visible[-1].content, messages[-1].content)
        self.assertTrue(manifest['would_exceed_budget'])
        self.assertFalse(manifest['budget_enabled'])
        self.assertNotIn('context_incomplete', manifest)
        with patch.object(settings, 'CONTEXT_BUDGET_ENABLED', True), self.assertRaisesRegex(WorkspaceError, 'context_incomplete'):
            await prepare(self.b, messages)

    async def test_Q1_Q3_diagnose_name_first_call_and_reuse_original_evidence(self):
        await self.setup_pod()
        args = {'cluster': 'c', 'namespace': 'system', 'pod': 'coredns'}
        first = await execute(self.b, 'diagnose_pod', args, self.query)
        again = await execute(self.b, 'diagnose_pod', args, self.query)
        self.assertEqual(len(self.remote), 1)
        self.assertNotIn('pod_uid', self.remote[0])
        self.assertEqual(first['_rcagent_query']['evidence_id'], again['_rcagent_query']['evidence_id'])
        self.assertEqual(again['_rcagent_query']['mode'], 'reused')
        self.assertEqual(self.b.mcp_calls, 1)
        self.assertEqual(self.b.reuse_calls, 1)
        e = await self.b.repository.read(self.b.incident_id, first['_rcagent_query']['evidence_id'], 'evidence')
        self.assertEqual(e['resource_ids'], [self.resource['id']])

    async def test_Q2_wrong_namespace_and_changed_uid_not_sent(self):
        await self.setup_pod()
        wrong = await execute(self.b, 'diagnose_pod', {'cluster': 'c', 'namespace': 'other', 'pod': 'coredns'}, self.query)
        self.assertEqual(wrong['status'], 'error')
        self.assertEqual(self.remote, [])
        self.resource['verified_at'] = '2020-01-01T00:00:00+00:00'
        await self.b.repository.save(self.b.incident_id, 'resource', self.resource)
        self.response['candidates'][0]['uid'] = 'new-uid'
        from tools import observability_adapter
        with bind(self.b), patch.object(observability_adapter, '_call_gateway_raw', self.raw):
            stale = await execute(self.b, 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': 'dns-uid'}, self.query)
        self.assertEqual(stale['status'], 'error')
        self.assertIn('new-uid', stale['reason'])
        self.assertEqual(self.remote, [])

    async def test_Q4_Q5_defaults_windows_options_partial_and_failed_results(self):
        await self.setup_pod()
        args = {'cluster': 'c', 'pod_uid': 'dns-uid'}
        await execute(self.b, 'get_pod_network_summary', args, self.query)
        await execute(self.b, 'get_pod_network_summary', dict(args, direction='both', top_n=20), self.query)
        self.assertEqual(len(self.remote), 1)
        await execute(self.b, 'get_pod_network_summary', dict(args, top_n=10), self.query)
        await execute(self.b, 'get_pod_network_summary', args, self.query, force_refresh=True)
        await execute(self.b, 'get_pod_network_summary', dict(args, end_time='2026-10-13T00:16:00+00:00', start_time=self.b.query_window['start_time']), self.query)
        self.assertEqual(len(self.remote), 4)
        async def partial(params):
            return dict(await self.query(params), status='partial', truncated=True)
        await execute(self.b, 'get_pod_application_summary', args, partial)
        await execute(self.b, 'get_pod_application_summary', args, partial)
        self.assertEqual(len(self.remote), 6)
        reused = await execute(self.b, 'get_pod_application_summary', args, partial, allow_partial=True)
        self.assertEqual(len(self.remote), 6)
        self.assertTrue(reused['truncated'])

    async def test_Q5_unknown_window_or_unreadable_artifact_is_not_reused(self):
        await self.setup_pod()
        args = {'cluster': 'c', 'pod_uid': 'dns-uid'}
        async def unknown(params):
            self.remote.append(params)
            return {'status': 'ok', 'summary': {'value': 1}}
        await execute(self.b, 'get_pod_network_summary', args, unknown)
        await execute(self.b, 'get_pod_network_summary', args, unknown)
        self.assertEqual(len(self.remote), 2)
        first = await execute(self.b, 'get_pod_runtime_status', args, self.query)
        evidence = await self.b.repository.read(self.b.incident_id, first['_rcagent_query']['evidence_id'], 'evidence')
        artifact = await self.b.repository.read(self.b.incident_id, evidence['artifact_id'], 'artifact')
        (self.root / artifact['storage_reference']).write_text('corrupt')
        await execute(self.b, 'get_pod_runtime_status', args, self.query)
        self.assertEqual(len(self.remote), 4)

    async def test_Q6_join_cancel_one_waiter_keeps_operation_and_retry_after_failure(self):
        await self.setup_pod()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(args):
            entered.set()
            await release.wait()
            return await self.query(args)
        args = {'cluster': 'c', 'pod_uid': 'dns-uid'}
        first = asyncio.create_task(execute(self.b, 'get_pod_network_summary', args, slow))
        await entered.wait()
        second = asyncio.create_task(execute(self.b, 'get_pod_network_summary', args, slow))
        await asyncio.sleep(0.03)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        result = await second
        self.assertEqual(len(self.remote), 1)
        self.assertEqual(result['_rcagent_query']['mode'], 'joined')
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'evidence')), 1)

    async def test_M1_all_roles_share_controller_and_permit_is_released(self):
        active, maximum = 0, 0
        starts = []
        async def model(messages):
            nonlocal active, maximum
            starts.append(time.monotonic())
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.015)
            active -= 1
            return AIMessage(content='done')
        from workspace.runtime import Binding
        async def role_call(role):
            b = Binding(self.b.repository, self.b.incident_id, self.b.run_id, role, self.b.deadline)
            with bind(b):
                return await invoke_model(RunnableLambda(model), [])
        with patch.object(settings, 'MODEL_MIN_INTERVAL_SECONDS', 0.02):
            await asyncio.gather(*(role_call(role) for role in ('triage', 'planner', 'worker', 'supervisor')))
        self.assertEqual(maximum, 1)
        self.assertTrue(all(b - a >= 0.019 for a, b in zip(starts, starts[1:])))

    async def test_M2_rate_limit_retry_and_logical_step_once(self):
        """The bounded 429 retry is the explicit legacy policy (PRD 19.10)."""
        class RateLimit(Exception):
            status_code = 429
            response = type('Response', (), {'headers': {'retry-after': '0'}})()
        count = 0
        async def model(messages):
            nonlocal count
            count += 1
            if count <= 2:
                raise RateLimit('account request limit')
            return AIMessage(content='success')
        with patch.object(settings, 'RATE_LIMIT_POLICY', 'legacy'), bind(self.b):
            await invoke_model(RunnableLambda(model), [])
        self.assertEqual(count, 3)
        self.assertEqual(self.b.model_steps, 1)
        calls = await self.b.repository.all(self.b.incident_id, 'model_call')
        self.assertTrue(any(row.get('rate_limit_policy') == 'legacy' for row in calls),
                        'the legacy policy is recorded on the call')
        attempts = await self.b.repository.all(self.b.incident_id, 'model_attempt')
        self.assertEqual(len(attempts), 3)
        self.assertTrue(all(a['token_usage'] is None for a in attempts))

    async def test_M3_timeout_no_retry_and_queue_cancel_releases_permission(self):
        entered = asyncio.Event()
        async def model(messages):
            entered.set()
            await asyncio.Event().wait()
        with bind(self.b), patch.object(settings, 'MODEL_TIMEOUT_SECONDS', 0.02), self.assertRaises(ModelRequestError) as caught:
            await invoke_model(RunnableLambda(model), [])
        self.assertEqual(caught.exception.detail['code'], 'model_timeout')
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'model_attempt')), 1)
        self.assertEqual(controller().semaphore._value, settings.MODEL_MAX_IN_FLIGHT)

    async def test_R1_R2_R3_independent_explanations_keep_correct_references(self):
        await self.setup_pod()
        queried = await execute(self.b, 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': 'dns-uid'}, self.query)
        ref = queried['_rcagent_query']['evidence_id']
        claims = [{'claim_id': 'fact', 'type': 'observation', 'resource_id': self.resource['id'], 'content': 'Observed TCP errors: 7',
                   'supporting_evidence_ids': [ref], 'limitations': ['This Pod/window only']},
                  {'claim_id': 'candidate', 'type': 'hypothesis', 'resource_id': self.resource['id'], 'content': 'DNS configuration is a candidate; logs/config were not obtained',
                   'supporting_evidence_ids': [ref], 'limitations': ['Mechanism and configuration unverified']},
                  {'claim_id': 'wrong', 'type': 'observation', 'resource_id': ref, 'content': 'wrong identity', 'supporting_evidence_ids': [ref], 'limitations': ['unknown']}]
        items = [{'section': 'observations', 'content': claims[0]['content'], 'claim_ids': ['fact']},
                 {'section': 'hypotheses', 'content': claims[1]['content'], 'claim_ids': ['candidate']},
                 {'section': 'observations', 'content': 'wrong entry', 'claim_ids': ['wrong']}]
        normalized, rows, _ = await normalize_proposal(self.b, claims, items, {})
        report = await adjudicate(self.b, {'root_cause': 'definitely DNS'}, normalized, report_items=rows)
        self.assertIn('TCP errors: 7', report['evidence_summary'])
        self.assertIn('logs/config were not obtained', report['investigation_summary'])
        self.assertNotIn('wrong entry', report['evidence_summary'])
        self.assertIsNone(report['localization'])
        self.assertEqual(report['answer_status'], 'inconclusive')

    async def test_R4_true_reference_cannot_smuggle_redis_dns_confirmation(self):
        await self.setup_pod()
        result = await execute(self.b, 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': 'dns-uid'}, self.query)
        claim = {'claim_id': 'tcp', 'type': 'observation', 'resource_id': self.resource['id'],
                 'content': '观察到 TCP 错误', 'supporting_evidence_ids': [result['_rcagent_query']['evidence_id']],
                 'limitations': ['仅此 Pod 的网络观测，未取得 DNS 配置']}
        claims, items, _ = await normalize_proposal(self.b, [claim], [{
            'section': 'observations', 'content': '已确认 Redis DNS 记录缺失', 'claim_ids': ['tcp']}], {})
        report = await adjudicate(self.b, {}, claims, report_items=items)
        self.assertTrue(report['report_errors'])
        self.assertIn('观察到 TCP 错误', report['evidence_summary'])
        self.assertNotIn('已确认 Redis DNS', report['evidence_summary'])
        self.assertNotIn('已确认 Redis DNS', report['investigation_summary'])
        self.assertIsNone(report['localization'])
        self.assertEqual(report['answer_status'], 'inconclusive')

    async def test_S1_S2_current_capability_skips_and_bounds_sorted_backend_pods(self):
        from tools.triage_discovery import discover_service_target
        from tools.incident_text import resolve_incident_scope
        calls = []
        async def gateway(name, args):
            calls.append((name, dict(args)))
            if name == 'search_k8s_targets':
                return {'status': 'ok', 'candidates': [{'kind': 'service', 'name': 'frontend', 'cluster': 'c', 'namespace': 'app', 'uid': 'svc'}]}
            if name == 'get_service_backends':
                return {'status': 'ok', 'pods': [{'name': name, 'uid': name + '-uid', 'cluster': 'c', 'namespace': 'app'} for name in ('z', 'b', 'a', 'c', 'b')]}
            return {'status': 'ok', 'summary': {'errors': 0}}
        outcome = await discover_service_target(resolve_incident_scope({'incident_text': 'service: frontend cluster: c namespace: app 最近15分钟有错误'}), call_tool=gateway)
        self.assertEqual([p['name'] for p in outcome.coverage['selected']], ['a', 'b', 'c'])
        self.assertEqual([p['name'] for p in outcome.coverage['unchecked']], ['z'])
        self.assertEqual(len(outcome.coverage['checked']), 3)
        self.assertEqual(len(calls), 11)
        self.assertEqual(len(outcome.skipped_tools), 2)
        self.assertNotIn('query_metrics', [n for n, _ in calls])
        self.assertNotIn('search_traces', [n for n, _ in calls])
        self.assertTrue(outcome.coverage['ready_and_actual_traffic_unknown'])

    async def test_S3_meta_partial_requires_independent_exact_identity(self):
        from tools.service_fallback import coarse_pods
        from tools.triage_discovery import DiscoveryOutcome
        from tools.incident_text import resolve_incident_scope
        outcome = DiscoveryOutcome(scope=resolve_incident_scope({'incident_text': 'service: frontend'}), backends={'meta': {'truncated': True}})
        calls = []
        async def gateway(name, args):
            calls.append((name, args))
            return {'status': 'ok', 'pods': [{'name': 'frontend-0', 'pod_uid': 'new-uid', 'cluster': 'c', 'namespace': 'app'}]}
        await coarse_pods(outcome, [{'name': 'frontend-0', 'uid': 'old-uid'}], gateway, 'c', 'app')
        self.assertEqual([n for n, _ in calls], ['resolve_pod'])
        self.assertFalse(outcome.coverage['inventory_complete'])
        self.assertEqual(outcome.evidence_status, 'unavailable')
        self.assertTrue(outcome.coverage['failed'])

    async def test_S3_empty_ambiguous_and_all_failed_are_unavailable(self):
        from tools.service_fallback import coarse_pods
        from tools.triage_discovery import DiscoveryOutcome
        from tools.incident_text import resolve_incident_scope
        for candidates in ([], [{'name': 'p', 'uid': 'one'}, {'name': 'p', 'uid': 'two'}], [{'name': 'p', 'uid': 'one'}]):
            outcome = DiscoveryOutcome(scope=resolve_incident_scope({'incident_text': 'service: frontend'}), backends={'status': 'ok'})
            calls = []
            async def fail(name, args):
                calls.append(name)
                return {'status': 'error', 'error_type': 'timeout'}
            await coarse_pods(outcome, candidates, fail, 'c', 'app')
            self.assertEqual(outcome.evidence_status, 'unavailable')
            self.assertEqual(outcome.coverage['checked'], [])
            self.assertEqual(len(calls), 3 if len(candidates) == 1 else 0)

    async def test_M2_retry_after_exceeds_deadline_records_failed_logical_call(self):
        class Limited(Exception):
            status_code = 429
            response = type('Response', (), {'headers': {'Retry-After': '600'}})()
        async def model(messages):
            raise Limited('limit')
        with patch.object(settings, 'RATE_LIMIT_POLICY', 'legacy'),              bind(self.b), self.assertRaises(ModelRequestError) as caught:
            await invoke_model(RunnableLambda(model), [])
        self.assertEqual(caught.exception.detail['code'], 'investigation_deadline')
        calls = await self.b.repository.all(self.b.incident_id, 'model_call')
        self.assertEqual(calls[0]['status'], 'failed')
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'model_attempt')), 1)

    async def test_M3_auth_and_context_failure_are_not_retried(self):
        for status, message, code in ((403, 'denied', 'model_authentication_failed'), (400, 'maximum context length', 'provider_context_limit')):
            error = type('ProviderError', (Exception,), {'status_code': status})(message)
            async def fail(messages):
                raise error
            before = len(await self.b.repository.all(self.b.incident_id, 'model_attempt'))
            with bind(self.b), self.assertRaises(ModelRequestError) as caught:
                await invoke_model(RunnableLambda(fail), [])
            self.assertEqual(caught.exception.detail['code'], code)
            self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'model_attempt')), before + 1)

    async def test_M3_queue_and_send_cancellation_release_permits_and_audit(self):
        control = controller()
        await control.semaphore.acquire()
        async def immediate(messages):
            return AIMessage(content='ok')
        with bind(self.b):
            queued = asyncio.create_task(invoke_model(RunnableLambda(immediate), []))
            await asyncio.sleep(0.03)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
        self.assertEqual(control.semaphore._value, 0)
        control.semaphore.release()
        entered = asyncio.Event()
        async def block(messages):
            entered.set()
            await asyncio.Event().wait()
        with bind(self.b):
            sent = asyncio.create_task(invoke_model(RunnableLambda(block), []))
            await entered.wait()
            sent.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await sent
        self.assertEqual(control.semaphore._value, 1)
        self.assertTrue(all(a['status'] == 'cancelled' for a in await self.b.repository.all(self.b.incident_id, 'model_attempt')))

    async def test_R_duplicate_proposal_ids_reject_both_and_preserve_other_claim(self):
        await self.setup_pod()
        response = await execute(self.b, 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': 'dns-uid'}, self.query)
        fact = {'claim_id': 'dup', 'type': 'observation', 'resource_id': self.resource['id'], 'content': 'TCP errors 7',
                'supporting_evidence_ids': [response['_rcagent_query']['evidence_id']], 'limitations': ['this window']}
        claims, items, errors = await normalize_proposal(self.b, [fact, fact, dict(fact, claim_id='unique')],
            [{'section': 'observations', 'content': 'TCP errors 7', 'claim_ids': ['dup']}], {})
        self.assertEqual(len(claims), 1)
        self.assertEqual(len(errors), 2)
        checks, accepted, errors = await inspect_proposal(self.b, claims, items)
        self.assertEqual(accepted, [])
        self.assertTrue(checks[0]['retained'])

    async def test_M1_fake_clock_pacing_and_cross_incident_shared_controller(self):
        from workspace.model_requests import Controller
        from workspace.runtime import Binding, initialize_incident
        tick, delays = [100.0], []
        async def sleep(seconds):
            delays.append(seconds)
            tick[0] += seconds
        control = Controller(1, 2, clock=lambda: tick[0], sleep=sleep)
        await control.acquire(lambda: 60)
        control.semaphore.release()
        await control.acquire(lambda: 60)
        control.semaphore.release()
        self.assertEqual(delays, [2.0])
        with patch.object(settings, 'WORKSPACE_ROOT', self.root):
            other = await initialize_incident({'target_cluster': 'c', 'target_namespace': 'app', 'target_resource': 'other'})
        other_binding = Binding(self.b.repository, other['incident_id'], other['run_id'], 'planner', self.b.deadline)
        with bind(self.b):
            first = controller()
        with bind(other_binding):
            self.assertIs(first, controller())

    async def test_Q6_all_waiters_cancel_audits_actual_operation_and_new_request_runs(self):
        await self.setup_pod()
        entered = asyncio.Event()
        async def blocked(args):
            entered.set()
            await asyncio.Event().wait()
        args = {'cluster': 'c', 'pod_uid': 'dns-uid'}
        task = asyncio.create_task(execute(self.b, 'get_pod_network_summary', args, blocked))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        records = await self.b.repository.all(self.b.incident_id, 'evidence')
        self.assertEqual(records[0]['transport_status'], 'cancelled')
        await execute(self.b, 'get_pod_network_summary', args, self.query)
        self.assertEqual(len(self.remote), 1)

    async def test_R2_unverified_lookup_keeps_scoped_observation_not_cause(self):
        from workspace.queries import save_query
        await save_query(self.b, 'lookup', 'search_k8s_targets', {'cluster': 'c', 'namespace': 'app', 'query': 'missing', 'kind': 'service'},
                         {'status': 'not_found', 'candidates': []}, 'now', 1)
        claim = {'claim_id': 'lookup_fact', 'type': 'observation', 'lookup_target': {'kind': 'service', 'name': 'missing', 'cluster': 'c', 'namespace': 'app'},
                 'content': '在 c/app 指定范围的库存查询未命中 missing；不能据此证明它不存在或健康',
                 'supporting_evidence_ids': ['lookup'], 'limitations': ['库存覆盖和更新时效未验证']}
        claims, items, _ = await normalize_proposal(self.b, [claim], [{'section': 'observations', 'content': claim['content'], 'claim_ids': ['lookup_fact']}], {})
        report = await adjudicate(self.b, {}, claims, report_items=items)
        self.assertIn('指定范围', report['evidence_summary'])
        self.assertEqual(report['report_errors'], [])
        self.assertIsNone(report['localization'])
