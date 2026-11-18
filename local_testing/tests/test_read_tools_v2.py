"""Round-3 read-tool contract: filters, cursors, bounded views and audit.

Every assertion is about the visible result or the recorded audit trail: what a
page contains, what it refuses, and whether a page can be traced back to the
read that produced it.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from workspace.repository import WorkspaceError
from workspace.read_tools import (CURRENT_TOOL_CALL, get_resource, query_evidence, read_artifact,
                                  read_assessment, read_evidence, read_task)
from workspace.queries import save_query
from workspace.tasks import finish
from workspace.runtime import bind
from test_workspace import WorkspaceCase


def window(start_hours=0, hours=1):
    start = datetime(2026, 5, 1, tzinfo=timezone.utc) + timedelta(hours=start_hours)
    return {'start_time': start.isoformat(), 'end_time': (start + timedelta(hours=hours)).isoformat()}


class QueryEvidenceTests(WorkspaceCase):
    async def seed(self):
        identity = await self.locate()
        uid = identity['resource']['uid']
        win = window()
        raw = {'status': 'ok', 'summary': {'tcp_errors': 1},
               'meta': {'time_range': win}}
        for index in range(6):
            await save_query(self.b, 'e%d' % index, 'get_pod_network_summary',
                             {**win, 'cluster': 'c', 'pod_uid': uid}, raw, 'now', 1)
        await save_query(self.b, 'err', 'get_pod_runtime_status', {'cluster': 'c', 'pod_uid': uid},
                         {'status': 'error', 'data_gaps': ['boom']}, 'now', 1)
        return identity, win

    async def collect(self, args):
        """Walk every cursor page and return (ids, total, page_count)."""
        ids, page_count, cursor = [], 0, ''
        while True:
            page = await query_evidence.ainvoke({**args, 'cursor': cursor})
            page_count += 1
            ids.extend(row['evidence_id'] for row in page['items'])
            cursor = page.get('next_cursor') or ''
            if not cursor or page_count > 50:
                return ids, page.get('total'), page_count

    async def test_filters_match_exactly_and_unknown_window_is_not_a_match(self):
        identity, win = await self.seed()
        network_ids = {'e%d' % i for i in range(6)}
        all_ids = {row['id'] for row in await self.b.repository.all(self.b.incident_id, 'evidence')}
        with bind(self.b):
            by_tool = await self.collect({'tool_name': 'get_pod_network_summary'})
            self.assertEqual(set(by_tool[0]), network_ids)
            self.assertEqual(by_tool[1], 6)
            by_status = await self.collect({'source_status': 'error'})
            self.assertEqual(by_status[0], ['err'])
            by_resource = await self.collect({'resource_id': identity['resource']['id']})
            self.assertEqual(set(by_resource[0]), all_ids)
            self.assertEqual(len(by_resource[0]), len(set(by_resource[0])), 'cursor pages must not repeat rows')
            overlapping = await self.collect({'start_time': window(0)['start_time'],
                                              'end_time': window(2)['end_time']})
            self.assertEqual(set(overlapping[0]), network_ids,
                             'a record without a recorded window is not a window match')
            outside = await query_evidence.ainvoke({'start_time': window(10)['start_time'],
                                                    'end_time': window(12)['end_time']})
            self.assertEqual(outside['total'], 0, 'no window overlap must not match silently')

    async def test_illegal_filters_and_pages_are_explicit_errors(self):
        await self.seed()
        with bind(self.b):
            for args in ({'start_time': window()['start_time']}, {'offset': -1}, {'limit': 0}, {'limit': 101},
                         {'view': 'everything'}, {'max_chars': 100}, {'cursor': 'not-base64', 'offset': 3}):
                with self.subTest(args=args), self.assertRaises(WorkspaceError):
                    await query_evidence.ainvoke(args)

    async def test_cursor_pages_stay_on_the_original_snapshot(self):
        await self.seed()
        with bind(self.b):
            first = await query_evidence.ainvoke({'limit': 2, 'max_chars': 8000})
            self.assertTrue(first['has_more'])
            self.assertEqual(first['total'], 8)
            self.assertEqual(len(first['items']), 2)
            await save_query(self.b, 'late', 'get_pod_network_summary', {}, {'status': 'ok'}, 'now', 1)
            # Continue the original scan: 'late' must stay invisible on this cursor.
            ids, cursor, pages = [], first['next_cursor'], 1
            while cursor:
                page = await query_evidence.ainvoke({'limit': 2, 'max_chars': 8000, 'cursor': cursor})
                ids.extend(row['evidence_id'] for row in page['items'])
                cursor = page.get('next_cursor') or ''
                pages += 1
                self.assertLessEqual(pages, 50)
            self.assertNotIn('late', ids, 'a cursor is bound to its scan snapshot')
            self.assertEqual(len(ids), 6, 'the snapshot keeps exactly its remaining rows')
            self.assertGreaterEqual(pages, 4)
            fresh = await query_evidence.ainvoke({'limit': 100})
            self.assertEqual(fresh['total'], 9, 'a fresh query counts the newly added record')
            with bind(self.b):
                all_ids, total, _ = await self.collect({'view': 'minimal', 'max_chars': 8000})
            self.assertIn('late', all_ids, 'minimal pages can enumerate every row including the new one')
            with self.assertRaises(WorkspaceError):
                await query_evidence.ainvoke({'limit': 2, 'cursor': first['next_cursor'], 'tool_name': 'get_pod_runtime_status'})

    async def test_payload_limit_reports_minimum_instead_of_looping(self):
        identity, win = await self.seed()
        with bind(self.b):
            tight = await query_evidence.ainvoke({'limit': 5, 'max_chars': 512})
            if tight.get('page_limit_too_small'):
                self.assertEqual(tight['items'], [])
                self.assertTrue(tight['minimum_chars'] >= 512)
                self.assertIn('retry', json.dumps(tight).lower())
                self.assertIsNone(tight['next'], 'a too-small page must not claim progress')
            else:
                self.assertLessEqual(len(json.dumps(tight, ensure_ascii=False)), 512,
                                     'the whole returned page is what the limit measures')
            full = await query_evidence.ainvoke({'limit': 5, 'max_chars': 8000})
            self.assertLessEqual(len(json.dumps(full, ensure_ascii=False)), 8000)

    async def test_every_page_row_hides_archive_noise_and_stays_brief(self):
        await self.seed()
        with bind(self.b):
            page = await query_evidence.ainvoke({'tool_name': 'get_pod_runtime_status'})
            row = page['items'][0]
            self.assertEqual(row['evidence_id'], 'err')
            self.assertIn('observations', row)
            self.assertNotIn('duration_ms', row)
            self.assertNotIn('schema_fingerprint', row)
            minimal = await query_evidence.ainvoke({'view': 'minimal', 'limit': 100})
            self.assertNotIn('observations', minimal['items'][0])


class DetailReadTests(WorkspaceCase):
    async def seed(self):
        identity = await self.locate()
        uid = identity['resource']['uid']
        await save_query(self.b, 'net', 'get_pod_network_summary', {'cluster': 'c', 'pod_uid': uid},
                         {'status': 'ok', 'summary': {'tcp_errors': 7, 'impact_note': 'i' * 9000},
                          'meta': {'time_range': window(), 'data_gaps': [{'reason': 'limited'}]}}, 'now', 1)
        return identity

    async def test_brief_default_metadata_and_field_views(self):
        await self.seed()
        with bind(self.b):
            brief = await read_evidence.ainvoke({'evidence_id': 'net'})
            self.assertEqual(brief['evidence_id'], 'net')
            self.assertIn('observations', brief['brief'])
            self.assertTrue(brief['readable_fields'])
            metadata = await read_evidence.ainvoke({'evidence_id': 'net', 'view': 'metadata'})
            self.assertIn('observations_keys', metadata['metadata'])
            self.assertEqual(metadata['artifact_id'], brief['brief'] and metadata['artifact_id'])
            field = await read_evidence.ainvoke({'evidence_id': 'net', 'field': 'summary.tcp_errors'})
            self.assertEqual(field['items'], [7])
            self.assertTrue(field.get('limitations', {}).get('data_gaps'))
            self.assertEqual(field['requested_window_ref'], 'w1')
            self.assertEqual(field['returned_window_ref'], 'w2', 'the returned window stays separate from the request')
            self.assertIn('w2', field['windows'], 'every w-ref comes with its window table')
            raw = await read_evidence.ainvoke({'evidence_id': 'net', 'field': 'summary.impact_note', 'char_offset': 0})
            self.assertTrue(raw['truncated'], 'a long field is paged, not dumped')
            self.assertEqual(raw['range'], [0, len(raw['text'])])
            self.assertEqual(raw['next_char_offset'], len(raw['text']))
            serialized = json.dumps('i' * 9000, ensure_ascii=False)
            self.assertEqual(raw['text'], serialized[:len(raw['text'])],
                             'the page is a slice of the serialized field, nothing else')
            self.assertLess(len(raw['text']), len(serialized))

    async def test_unknown_and_cross_incident_ids_are_rejected(self):
        await self.seed()
        with bind(self.b):
            with self.assertRaises(WorkspaceError):
                await read_evidence.ainvoke({'evidence_id': 'missing'})
            with self.assertRaises(WorkspaceError):
                await read_evidence.ainvoke({'evidence_id': 'net', 'field': 'summary.nope'})
        from workspace.runtime import initialize_incident
        with patch.object(__import__('config.settings', fromlist=['settings']), 'WORKSPACE_ROOT', self.root):
            other = await initialize_incident({'target_cluster': 'c'})
        other_binding = type(self.b)(self.b.repository, other['incident_id'], other['run_id'], 'worker', self.b.deadline)
        with bind(other_binding):
            with self.assertRaises(WorkspaceError):
                await read_evidence.ainvoke({'evidence_id': 'net'})

    async def test_artifact_auto_view_is_a_directory_not_a_dump(self):
        await self.seed()
        with bind(self.b):
            page = await read_artifact.ainvoke({'artifact_id': (await read_evidence.ainvoke({'evidence_id': 'net', 'view': 'metadata'}))['artifact_id']})
            self.assertEqual(page['content_type'], 'object')
            self.assertTrue(page['top_level_fields'])
            self.assertGreater(page['serialized_chars'], 9000)
            self.assertNotIn('status', page, 'auto view must not expand the payload')

    async def test_task_and_assessment_reads_are_bounded_and_complete(self):
        identity = await self.seed()
        from models.schemas import RCATask
        from workspace.tasks import register_tasks, start
        await self.locate()
        custom = RCATask(priority=1, investigation_goal='diagnose core dns',
                         target_resource='coredns', resource_type='pod',
                         acceptance_checks=['tcp errors explained', 'no unsupported causes'],
                         suggested_tools=['get_pod_network_summary'])
        task = (await register_tasks(self.b, [custom]))[0]
        attempt = await start(self.b, task.task_id)
        self.b.task_id, self.b.attempt_id, self.b.resource_id = task.task_id, attempt['id'], task.resource_id
        await finish(self.b, {'diagnosis': 'd' * 800, 'reasoning': 'r' * 50, 'answer_status': 'inconclusive',
                              'gaps': ['missing logs']})
        assessment_id = (await self.b.repository.all(self.b.incident_id, 'assessment'))[-1]['id']
        with bind(self.b):
            task_view = await read_task.ainvoke({'task_id': task.task_id})
            self.assertEqual(task_view['task']['investigation_goal'], 'diagnose core dns')
            self.assertIn('acceptance_checks', task_view['task'])
            judgment = await read_assessment.ainvoke({'assessment_id': assessment_id})
            self.assertEqual(judgment['assessment']['assessment_id'], assessment_id)
            self.assertEqual(len(judgment['assessment']['diagnosis']), 800)
            self.assertIn('missing logs', judgment['assessment']['gaps'])
            with self.assertRaises(WorkspaceError):
                await read_task.ainvoke({'task_id': 'missing'})
            with self.assertRaises(WorkspaceError):
                await read_assessment.ainvoke({'assessment_id': 'missing'})

    async def test_reads_are_audited_with_source_field_page_and_tool_call(self):
        await self.seed()
        with bind(self.b):
            token = CURRENT_TOOL_CALL.set('call-42')
            try:
                await read_evidence.ainvoke({'evidence_id': 'net', 'field': 'summary'})
            finally:
                CURRENT_TOOL_CALL.reset(token)
        events = await self.b.repository.transact(lambda db: [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE action='local_read'")])
        entry = json.loads(events[-1]['body'])
        self.assertEqual(entry['reference'], 'net')
        self.assertEqual(entry['tool_call_id'], 'call-42')
        self.assertEqual(entry['field'], 'summary')
        self.assertGreater(entry['chars'], 0)

    async def test_catalog_page_can_locate_its_evidence_ids(self):
        await self.seed()
        with bind(self.b):
            page = await query_evidence.ainvoke({'limit': 100})
        events = await self.b.repository.transact(lambda db: [dict(r) for r in db.execute(
            "SELECT * FROM events WHERE action='local_read_catalog'")])
        self.assertTrue(events, 'catalog pages must be audited as pages, not as reference=null noise')
        entry = json.loads(events[-1]['body'])
        self.assertEqual(entry['evidence_ids'], [row['evidence_id'] for row in page['items']],
                         'the page audit must name the evidence it returned')


if __name__ == '__main__':
    unittest.main()