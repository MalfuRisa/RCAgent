"""Packet facts, opposition, stale detection and program-expanded report text."""
from workspace import investigations
from workspace.report_packets import prepare_packet, current_packet, expand_packet
from workspace.repository import WorkspaceError
import test_investigation_contracts as contracts


class ReportPackets(contracts.ActiveWorkspaceCase):
    seed_question_and_source = contracts.StageCommitTests.seed_question_and_source
    show = contracts.StageCommitTests.show
    proposal = contracts.StageCommitTests.proposal

    async def seed_packet(self):
        from workspace.resources import register
        target = await register(self.b, 'pod', 'iam', 'app')
        target.update(uid='u', cluster='c', identity_status='resolved')
        await self.b.repository.save(self.b.incident_id, 'resource', target, target['revision'])
        question, source = await self.seed_question_and_source()
        commit = await investigations.commit_stage(self.b, self.proposal(question, source))
        observation = commit['observation_ids'][0]
        resource = (await self.b.repository.read(self.b.incident_id, source, 'evidence'))['resource_ids']
        self.assertEqual(resource, [target['id']])
        self.b.role = 'supervisor'
        result = await prepare_packet(self.b, [{'claim_id': 'c', 'type': 'observation',
            'resource_id': resource[0], 'content': '已确认 Redis DNS 记录缺失',
            'supporting_evidence_ids': [source], 'limitations': ['Only this source/window']}],
            {'c': [observation]}, question_dispositions={question['id']: 'Unresolved; request correlation missing'})
        self.assertEqual(result['status'], 'accepted', result)
        return question, source, observation, result

    async def test_program_text_cannot_inherit_an_invented_dns_cause(self):
        question, _, _, result = await self.seed_packet()
        expanded = await expand_packet(self.b, result['packet_id'], list(result['statement_ids'].values()),
                                        'inconclusive', [question['id']])
        report = expanded['final_report']
        self.assertNotIn('Redis', report['evidence_summary'])
        self.assertIn('/summary/rst_count = 3', report['evidence_summary'])
        self.assertEqual(report['root_cause'], 'Unresolved')
        with self.assertRaisesRegex(WorkspaceError, 'existing causal rule'):
            await expand_packet(self.b, result['packet_id'], list(result['statement_ids'].values()),
                                'supported', [question['id']])

    async def test_unrelated_event_keeps_packet_but_new_opposition_invalidates_it(self):
        question, _, observation, result = await self.seed_packet()
        await self.b.repository.transact(lambda db: self.b.repository.event(
            db, self.b.incident_id, self.b.run_id, 'unrelated_debug_event', {'value': 1}))
        self.assertEqual((await current_packet(self.b, result['packet_id']))['id'], result['packet_id'])
        await investigations.save_judgment(self.b, question_id=question['id'], type='interpretation',
            content='New counter evidence', opposing_observation_ids=[observation], limitations=['not resolved'])
        with self.assertRaisesRegex(WorkspaceError, 'packet_stale'):
            await current_packet(self.b, result['packet_id'])

    async def test_source_change_and_omitted_unresolved_direction_are_refused(self):
        question, source, _, result = await self.seed_packet()
        with self.assertRaisesRegex(WorkspaceError, 'cannot be omitted'):
            await expand_packet(self.b, result['packet_id'], list(result['statement_ids'].values()),
                                'inconclusive', [])
        evidence = await self.b.repository.read(self.b.incident_id, source, 'evidence')
        evidence['source_status'] = 'partial'
        await self.b.repository.save(self.b.incident_id, 'evidence', evidence, evidence['revision'])
        with self.assertRaisesRegex(WorkspaceError, 'packet_stale'):
            await current_packet(self.b, result['packet_id'])

    async def test_packet_selection_context_retains_linked_registered_tasks(self):
        from models.schemas import RCATask
        from workspace.tasks import register_tasks
        from context.builder import prepare
        from test_investigation_contracts import workbench
        question, _, observation, result = await self.seed_packet()
        task = (await register_tasks(self.b, [RCATask(priority=1,
            investigation_goal='Verify the recorded counter and retain missing mechanism',
            resource_type='pod', target_resource='iam', question_ids=[question['id']])]))[0]
        old = await self.b.repository.read(self.b.incident_id, result['packet_id'], 'report_packet')
        claim = dict(old['statements'][0]['claim'], claim_id='reviewed-counter')
        result = await prepare_packet(self.b, [claim], {'reviewed-counter': [observation]},
            question_dispositions={question['id']: 'Unresolved mechanism'})
        self.b.active_packet_id = result['packet_id']
        visible, manifest = await prepare(self.b, [], phase='packet_submission')
        self.assertIn(task.task_id, [row['id'] for row in workbench(visible)['tasks']])
        self.assertNotIn(task.task_id, [row['id'] for row in manifest['omitted_references_and_reasons']])
