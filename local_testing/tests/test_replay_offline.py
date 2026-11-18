"""Round-4 offline replay tests: archived samples end to end, plus amplified load.

Archived dumps live outside the repository; the cases skip (with a reason) when
the local archive is absent so the suite stays portable. The amplified-sample and
externalize-then-read-back cases are synthetic and always run.
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from config import settings
from context.builder import prepare
from workspace.repository import Repository, WorkspaceError, new_id, utcnow
from workspace.runtime import Binding, initialize_incident
from workspace.queries import save_query
from workspace.read_tools import read_evidence

def archived(relative):
    """Resolve an archived sample under Windows or WSL mount paths."""
    for base in (Path('D:/Business/Agents/RCAgent'), Path('/mnt/d/Business/Agents/RCAgent')):
        candidate = base / relative
        if candidate.exists():
            return candidate
    return Path('D:/Business/Agents/RCAgent') / relative


OLD_DUMP = archived('test-results/20261012-k8s-redeploy/workspace-records.json')
IAM_OLD = 'incident_945d68a4d51d419d82eb7b913a670011'
FRONTEND = 'incident_7cb6b94d7c29405393cc740682175d4b'
FRONTEND_INPUT = archived('test-results/20261012-k8s-redeploy/workspaces/artifacts/'
                          'incident_7cb6b94d7c29405393cc740682175d4b/artifact_14a9bc27184d4ace8f4181601fc00f2c.json')
LIVE_DUMP = archived('学习材料归档/04-Agent学习/改动优化/'
                     '真实联调五项修复-2026-10-13/acceptance-followup/live-iam/iam-pod/records.json')
LIVE_IAM = 'incident_bfcffd4a914043cea71d137e7da51ad6'


def load_rows(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if isinstance(data, dict) and set(data) >= {'run', 'resource', 'evidence'}:
        rows = []
        for kind, values in data.items():
            for body in values:
                rows.append({'id': body['id'], 'incident': body.get('incident_id'), 'kind': kind,
                             'revision': body.get('revision', 1), 'body': body})
        return rows
    return data


def legacy_history(path):
    """Rebuild the archived input: the old workbench message is regenerated."""
    messages = []
    for row in json.loads(path.read_text(encoding='utf-8')):
        kind, content = row.get('type'), row.get('content', '')
        if kind == 'system':
            if isinstance(content, str) and content.startswith('Incident workbench'):
                continue
            messages.append(SystemMessage(content=content))
        elif kind == 'human':
            messages.append(HumanMessage(content=content))
        elif kind == 'ai':
            messages.append(AIMessage(content=content, tool_calls=row.get('tool_calls') or []))
        elif kind == 'tool':
            call_id = row.get('tool_call_id') or 'replay-' + new_id('call')
            messages.append(ToolMessage(content=content, name=row.get('name') or 'tool', tool_call_id=call_id))
    return messages


class ReplayCase(unittest.IsolatedAsyncioTestCase):
    async def replay(self, rows, incident, *, role='supervisor', phase='initial_submission',
                     mode='compact', budget=True, messages=None, limit=None, task_id=None, attempt_id=None,
                     hook=None):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        repo = Repository(self.temp.name)
        await repo.initialize()
        def seed(db):
            scope = next((row['body'].get('scope') for row in rows
                          if row['incident'] == incident and row['kind'] == 'incident'), None) or {'cluster': 'rd2', 'namespaces': ['*']}
            db.execute("INSERT OR REPLACE INTO incidents VALUES (?,?)", (incident, json.dumps(scope)))
            for row in rows:
                if row['incident'] != incident:
                    continue
                body = dict(row['body'])
                # Mirror Repository.put: the stored body carries its own revision.
                body.setdefault('revision', row.get('revision', 1))
                body.setdefault('incident_id', incident)
                db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?,?)",
                           (row['id'], row['incident'], row['kind'], row.get('revision', 1),
                            json.dumps(body, ensure_ascii=False, default=str)))
        await repo.transact(seed)
        run = await repo.transact(lambda db: [json.loads(r[0])['id'] for r in
                                              db.execute("SELECT body FROM records WHERE incident=? AND kind='run'", (incident,))])
        binding = Binding(repo, incident, run[0] if run else 'replay', role, time.time() + 300,
                          task_id=task_id, attempt_id=attempt_id)
        if hook is not None:
            await hook(repo, binding)
        with patch.object(settings, 'CONTEXT_VIEW_MODE', mode), \
             patch.object(settings, 'CONTEXT_BUDGET_POLICY', 'strict'), \
             patch.object(settings, 'CONTEXT_BUDGET_ENABLED', budget), \
             patch.object(settings, 'CONTEXT_MAX_CHARS', limit or settings.CONTEXT_MAX_CHARS):
            return await prepare(binding, messages or [], phase)

    def payload(self, visible):
        return json.loads(visible[0].content.split('\n', 1)[1])


@unittest.skipUnless(OLD_DUMP.exists(), 'archived 2026-10-12 workspace dump is not available')
class ArchivedIamTests(ReplayCase):
    async def test_first_supervisor_input_fits_and_every_record_stays_discoverable(self):
        rows = load_rows(OLD_DUMP)
        visible, manifest = await self.replay(rows, IAM_OLD, limit=24000)
        self.assertLessEqual(manifest['input_chars'], 24000,
                             'the version-88 supervisor input must fit the engineering limit')
        payload = self.payload(visible)
        self.assertEqual(payload['coverage']['totals']['task'], 6)
        self.assertEqual(payload['coverage']['totals']['evidence'], 14)
        self.assertEqual(payload['coverage']['totals']['assessment'], 3)
        self.assertEqual(payload['coverage']['omitted'], {}, 'nothing of this incident disappears')
        listed_evidence = {row['evidence_id'] for row in payload['evidence']}
        listed_evidence.update(row['evidence_id'] for row in payload.get('evidence_directory', []))
        self.assertEqual(len(listed_evidence), 14)
        self.assertEqual({task['task_id'] for task in payload['tasks']},
                         {row['id'] for row in rows if row['incident'] == IAM_OLD and row['kind'] == 'task'})

    async def test_legacy_same_snapshot_is_longer_or_refused(self):
        rows = load_rows(OLD_DUMP)
        visible, manifest = await self.replay(rows, IAM_OLD, mode='compact', budget=False, limit=24000)
        compact_chars = manifest['input_chars']
        with self.assertRaises(Exception) as caught:
            await self.replay(rows, IAM_OLD, mode='legacy', budget=True, limit=24000)
        self.assertIn('context_incomplete', str(caught.exception))
        self.assertGreater(44015, compact_chars,
                           'the measured old assembly (44,015) must be longer than the compact input')


@unittest.skipUnless(OLD_DUMP.exists() and FRONTEND_INPUT.exists(),
                     'archived frontend input artifact is not available')
class ArchivedFrontendTests(ReplayCase):
    async def test_frontend_history_chain_assembles_without_repeating_raw_text(self):
        rows = load_rows(OLD_DUMP)
        messages = legacy_history(FRONTEND_INPUT)
        visible, manifest = await self.replay(rows, FRONTEND, phase='local_read', messages=messages)
        self.assertLessEqual(manifest['input_chars'], 24000)
        payload = self.payload(visible)
        self.assertEqual(payload['coverage']['totals']['evidence'], 4)
        self.assertEqual(len(payload['evidence']) + len(payload.get('evidence_directory', [])), 4)
        body = '\n'.join(str(m.content) for m in visible)
        self.assertNotIn('"text": "' + 'x' * 200, body, 'raw expansion must not be repeated wholesale')
        requests = [call['id'] for message in visible for call in getattr(message, 'tool_calls', [])]
        replies = [message.tool_call_id for message in visible if message.type == 'tool']
        self.assertEqual(requests, replies)

    async def test_frontend_reading_draft_survives_into_the_structured_stage(self):
        """Second half of the frontend row: the reading phase archives its draft and
        the structured stage starts from that reference, not from the reading history."""
        rows = load_rows(OLD_DUMP)
        messages, draft = [], {}
        async def write_draft(repo, binding):
            from workspace.artifacts import write_artifact, read_artifact_raw
            from workspace.runtime import bind
            with bind(binding):
                artifact = await write_artifact(binding, {'kind': 'model_draft', 'note': 'reading-phase draft',
                                                          'text': 'tentative: no verified root cause'})
                await repo.save(FRONTEND, 'artifact', artifact)
                draft['id'] = artifact['id']
                draft['read_back'] = await read_artifact_raw(binding, artifact['id'])
            messages.append(HumanMessage(content=json.dumps(
                {'incident_id': FRONTEND, 'draft_artifact_id': draft['id'],
                 'note': 'structured submission from the archived reading draft'})))
        visible, manifest = await self.replay(rows, FRONTEND, phase='initial_submission',
                                             messages=messages, hook=write_draft)
        self.assertLessEqual(manifest['input_chars'], 24000)
        self.assertNotIn('context_incomplete', manifest)
        payload = self.payload(visible)
        self.assertTrue(payload['resources'], 'facts stay visible in the structured stage')
        self.assertTrue(payload['scope'], 'scope stays visible in the structured stage')
        self.assertTrue(payload['coverage']['filters'], 'the discovery entry stays available for gaps')
        self.assertEqual([message.type for message in visible if message.type == 'tool'], [],
                         'the reading history is not stacked into the submission input')
        self.assertEqual(draft['read_back']['kind'], 'model_draft', 'the draft stays archived and readable')
        self.assertIn(draft['id'], '\n'.join(str(message.content) for message in visible))


@unittest.skipUnless(LIVE_DUMP.exists(), 'archived 2026-10-13 live IAM dump is not available')
class ArchivedLiveIamTests(ReplayCase):
    async def test_compact_is_shorter_than_legacy_on_the_same_snapshot_and_stage(self):
        rows = load_rows(LIVE_DUMP)
        _, compact = await self.replay(rows, LIVE_IAM, phase='local_read', mode='compact', budget=False)
        legacy_visible, legacy = await self.replay(rows, LIVE_IAM, phase='local_read', mode='legacy', budget=False)
        self.assertLess(compact['input_chars'], legacy['input_chars'],
                        'compact must shorten the same snapshot and stage')
        self.assertLessEqual(compact['input_chars'], 24000)
        # The legacy 53,533-char live input is the baseline this batch replaces.
        self.assertGreater(legacy['input_chars'], 24000)


def amplified_sample(tasks, evidence):
    """A synthetic incident far past the fixed samples: `tasks` resources/tasks and
    `evidence` tool records, so discovery -- not expansion -- has to carry it."""
    rows, incident = [], new_id('incident')
    rows.append({'id': incident, 'incident': incident, 'kind': 'incident',
                 'revision': 1, 'body': {'id': incident, 'scope': {'cluster': 'c', 'namespaces': ['*']}}})
    run_id = new_id('run')
    rows.append({'id': run_id, 'incident': incident, 'kind': 'run', 'revision': 1,
                 'body': {'id': run_id, 'scope': {'cluster': 'c'}, 'execution_status': 'incomplete'}})
    task_ids, resource_ids = [], []
    for index in range(tasks):
        resource_id = new_id('resource')
        resource_ids.append(resource_id)
        rows.append({'id': resource_id, 'incident': incident, 'kind': 'resource', 'revision': 1,
                     'body': {'id': resource_id, 'kind': 'pod', 'name': 'pod-%d' % index, 'cluster': 'c',
                              'namespace': 'app', 'uid': 'uid-%d' % index, 'identity_status': 'resolved',
                              'source_refs': []}})
        task_id = new_id('task')
        task_ids.append(task_id)
        rows.append({'id': task_id, 'incident': incident, 'kind': 'task', 'revision': 1,
                     'body': {'id': task_id, 'priority': index, 'target_resource_ids': [resource_id],
                              'investigation_goal': 'goal %d' % index, 'question': 'goal %d' % index,
                              'execution_status': 'finished', 'answer_status': 'inconclusive',
                              'latest_attempt_id': None}})
    for index in range(evidence):
        evidence_id = new_id('evidence')
        rows.append({'id': evidence_id, 'incident': incident, 'kind': 'evidence', 'revision': 1,
                     'body': {'id': evidence_id, 'tool_name': 'get_pod_runtime_status', 'run_id': run_id,
                              'resource_ids': [resource_ids[index % tasks]], 'resource_identities': [],
                              'transport_status': 'ok', 'source_status': 'ok', 'observations': {'status': 'ok'},
                              'data_gaps': [], 'time_range': None, 'returned_time_range': None,
                              'arguments': {}, 'producer_task_id': task_ids[index % tasks], 'finished_at': utcnow()}})
    return rows, incident


class AmplifiedSampleTests(ReplayCase):
    async def test_large_incident_fits_by_discovery_without_recursion(self):
        tasks, evidence = 12, 48
        rows, incident = amplified_sample(tasks, evidence)
        visible, manifest = await self.replay(rows, incident, limit=24000)
        payload = self.payload(visible)
        self.assertLessEqual(manifest['input_chars'], 24000)
        self.assertEqual(payload['coverage']['totals']['task'], tasks)
        self.assertEqual(payload['coverage']['totals']['evidence'], evidence)
        self.assertTrue(payload['coverage']['filters']['query_evidence'])
        self.assertNotIn('context_incomplete', manifest)
        listed = {row['task_id'] for row in payload['tasks']}
        listed_evidence = {row['evidence_id'] for row in payload['evidence']}
        listed_evidence.update(row['evidence_id'] for row in payload.get('evidence_directory', []))
        wanted_tasks = {row['id'] for row in rows if row['incident'] == incident and row['kind'] == 'task'}
        wanted_evidence = {row['id'] for row in rows if row['incident'] == incident and row['kind'] == 'evidence'}
        audited = {row['id'] for row in manifest['omitted_references_and_reasons'] if row.get('kind')}
        self.assertEqual(wanted_tasks - listed - audited, set(), 'every unlisted task must carry an audit reason')
        self.assertEqual(wanted_evidence - listed_evidence - audited, set(),
                         'every unlisted evidence must carry an audit reason')
        # Nothing recurses: the payload stays one level deep.
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('"observations": {"observations"', text)

    async def test_fifty_tasks_two_hundred_evidence_refuses_before_send_when_it_cannot_fit(self):
        rows, incident = amplified_sample(50, 200)
        with self.assertRaises(WorkspaceError) as caught:
            await self.replay(rows, incident, limit=24000)
        self.assertIn('context_incomplete', str(caught.exception))
        repo = Repository(self.temp.name)
        stored = await repo.transact(lambda db: [json.loads(r[0]) for r in db.execute(
            "SELECT body FROM records WHERE incident=? AND kind='context' ORDER BY rowid", (incident,))])
        refused = [row for row in stored if row.get('context_incomplete')]
        self.assertTrue(refused, 'a refused assembly must still be saved for explanation')
        self.assertEqual(refused[-1]['disposition'], 'rejected_before_send')
        self.assertTrue(refused[-1]['candidate_messages_artifact_id'])

    async def test_externalized_value_reads_back_and_is_not_hidden_elsewhere(self):
        """The §12.4 'externalize then read back' case: the value exists only in
        the archived record, and the local read tool must recover it."""
        rows, incident = [], new_id('incident')
        rows.append({'id': incident, 'incident': incident, 'kind': 'incident', 'revision': 1,
                     'body': {'id': incident, 'scope': {'cluster': 'c', 'namespaces': ['*']}}})
        run_id = new_id('run')
        rows.append({'id': run_id, 'incident': incident, 'kind': 'run', 'revision': 1,
                     'body': {'id': run_id, 'scope': {'cluster': 'c'}}})
        resource_id, evidence_id = new_id('resource'), new_id('evidence')
        marker = 'UNIQUE-MARKER-42'
        hidden = 'A' * 430 + marker  # beyond the 400-char text limit: only the archive keeps it whole
        rows.append({'id': resource_id, 'incident': incident, 'kind': 'resource', 'revision': 1,
                     'body': {'id': resource_id, 'kind': 'pod', 'name': 'target', 'cluster': 'c',
                              'namespace': 'app', 'uid': 'u1', 'identity_status': 'resolved', 'source_refs': []}})
        rows.append({'id': evidence_id, 'incident': incident, 'kind': 'evidence', 'revision': 1,
                     'body': {'id': evidence_id, 'tool_name': 'get_pod_network_summary', 'run_id': run_id,
                              'resource_ids': [resource_id], 'resource_identities': [],
                              'transport_status': 'ok', 'source_status': 'ok',
                              'observations': {'status': 'ok', 'summary': {'rtt_avg_ms': 12, 'rare_field': hidden}},
                              'data_gaps': [], 'time_range': None, 'returned_time_range': None,
                              'arguments': {}, 'tool_call_id': 'call-old', 'finished_at': utcnow()}})
        # The earlier raw expansion as the model saw it: large enough to externalize.
        payload = {'status': 'ok', 'summary': {'rtt_avg_ms': 12, 'rare_field': hidden},
                   'samples': [{'detail': 'x' * 2000}]}
        history = [AIMessage(content='', tool_calls=[{'name': 'get_pod_network_summary', 'args': {}, 'id': 'call-old'}]),
                   ToolMessage(content=json.dumps(payload), name='get_pod_network_summary', tool_call_id='call-old')]
        for i in range(3):
            history.append(AIMessage(content='', tool_calls=[{'name': 'read_task', 'args': {}, 'id': 'call-%d' % i}]))
            history.append(ToolMessage(content=json.dumps({'note': 'filler', 'i': i}), name='read_task',
                                       tool_call_id='call-%d' % i))
        # The earlier result is externalized in the *next* assembly.
        visible, manifest = await self.replay(rows, incident, phase='local_read', messages=history)
        body = '\n'.join(str(m.content) for m in visible)
        self.assertIn(evidence_id, body, 'the archived evidence stays referenced')
        decision = next((item for item in manifest.get('history_replacements') or []
                         if item.get('tool_call_id') == 'call-old'), None)
        self.assertIsNotNone(decision, 'the earlier raw result must be externalized')
        self.assertEqual(decision['strategy'], 'evidence_card')
        self.assertNotIn('x' * 2000, body, 'the raw expansion must not stay verbatim')
        self.assertNotIn(marker, body, 'the over-limit value stays only in the archive; the brief keeps its preview')
        repo_binding = Binding(Repository(self.temp.name), incident, run_id, 'worker', time.time() + 60)
        from workspace.read_tools import CURRENT_TOOL_CALL
        from workspace.artifacts import write_artifact
        from workspace.runtime import bind
        with bind(repo_binding):
            artifact = await write_artifact(repo_binding, payload)
            await repo_binding.repository.save(incident, 'artifact', artifact)
            evidence = await repo_binding.repository.read(incident, evidence_id, 'evidence')
            evidence['artifact_id'] = artifact['id']
            await repo_binding.repository.save(incident, 'evidence', evidence, evidence['revision'])
            token = CURRENT_TOOL_CALL.set('read-back')
            try:
                page = await read_evidence.ainvoke({'evidence_id': evidence_id, 'field': 'summary.rare_field',
                                                    'char_offset': 0})
            finally:
                CURRENT_TOOL_CALL.reset(token)
        self.assertIn(marker, page['text'], 'the value must be recoverable through the local read tool')


if __name__ == '__main__':
    unittest.main()
