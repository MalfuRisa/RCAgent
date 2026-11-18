"""Same-request audit-object sharing never substitutes a unique fact/source."""
import copy
import json
import unittest
from context.exact_representation import represent, expand, TABLE
from workspace.repository import WorkspaceError


class ExactRepresentation(unittest.TestCase):
    def view(self):
        common = {'evidence_id': 'evidence_x', 'artifact_id': 'artifact_x', 'source_revision': 1, 'source_hash': 'a' * 64}
        scope = {'resource_refs': [{'id': 'resource_x', 'uid': 'uid_x', 'revision': 3}],
                 'requested_window': {'start_time': '2026-10-16T00:00:00Z', 'end_time': '2026-10-16T00:15:00Z'}}
        return {'investigation': {'observations': [
            {'observation_id': 'observation_positive', 'question_ids': ['q1', 'q2'],
             'source_ref': {**common, 'json_pointer': '/summary/rst_count'}, 'field_value': 3, 'scope': scope},
            {'observation_id': 'observation_opposition', 'question_ids': ['q1'],
             'source_ref': {**common, 'json_pointer': '/summary/error_ratio'}, 'field_value': 0, 'scope': copy.deepcopy(scope),
             'limitations': ['Observed traffic only, not proof of absence']}],
            'judgments': [{'question_id': 'q1', 'opposing_observation_ids': ['observation_opposition']}]}}

    def test_shared_objects_restore_exact_fact_relations_and_limits(self):
        original = self.view()
        visible, audit = represent(original)
        restored = expand(json.loads(json.dumps(visible)))
        self.assertEqual(restored, original)
        self.assertTrue(audit)
        self.assertIn(TABLE, visible)
        rows = restored['investigation']['observations']
        self.assertEqual(rows[0]['question_ids'], ['q1', 'q2'])
        self.assertEqual(rows[1]['field_value'], 0)
        self.assertIn('not proof of absence', rows[1]['limitations'][0])

    def test_different_version_source_and_range_remain_distinct(self):
        for field, value in [('source_revision', 2), ('evidence_id', 'evidence_other')]:
            original = self.view()
            original['investigation']['observations'][1]['source_ref'][field] = value
            visible, _ = represent(original)
            self.assertEqual(expand(visible), original)
            self.assertNotIn('shared_source_identity_ref', visible['investigation']['observations'][0]['source_ref'])
        original = self.view()
        original['investigation']['observations'][0]['source_ref'].update(range=[0, 2], range_unit='items')
        original['investigation']['observations'][1]['source_ref'].update(range=[2, 3], range_unit='items')
        visible, _ = represent(original)
        self.assertEqual(expand(visible), original)
        self.assertEqual(visible['investigation']['observations'][1]['source_ref']['range'], [2, 3])

    def test_missing_retained_copy_cannot_prove_content(self):
        visible, _ = represent(self.view())
        visible.pop(TABLE)
        with self.assertRaisesRegex(WorkspaceError, 'missing'):
            expand(visible)

    def test_original_json_using_reserved_spelling_is_not_interpreted_as_a_reference(self):
        original = self.view()
        original['investigation']['observations'][0]['field_value'] = {'shared_audit_object_ref': 'literal source value'}
        visible, audit = represent(original)
        self.assertEqual(expand(visible), original)
        self.assertEqual(audit, [])

    def test_duplicate_task_question_requires_current_owner_version_and_complete_body(self):
        from context.duplicate_task_views import share_task_questions
        from workspace.receipts import source_hash
        from types import SimpleNamespace
        from langchain_core.messages import ToolMessage
        b = SimpleNamespace(role='supervisor', run_id='run1', task_id=None, attempt_id=None)
        question = 'A complete recorded investigation goal, preserved exactly. ' * 7
        body = {'read_receipt_id': 'receipt1', 'task': {'investigation_goal': question}}
        message = ToolMessage(content=json.dumps(body), tool_call_id='call1', name='read_task')
        receipt = {'id': 'receipt1', 'role': 'supervisor', 'run_id': 'run1', 'task_id': None, 'attempt_id': None,
                   'source_id': 'task1', 'source_kind': 'task', 'source_revision': 2, 'result_hash': source_hash(body)}
        payload = {'tasks': [{'task_id': 'task1', 'question': question}]}
        snapshots = {'task': [{'id': 'task1', 'revision': 2}]}
        visible, audit = share_task_questions(b, payload, [message], {'receipt1': receipt}, snapshots)
        self.assertEqual(visible['tasks'][0]['question']['active_tool_question_ref'], 'call1')
        self.assertEqual(json.loads(message.content)['task']['investigation_goal'], question)
        self.assertEqual(len(audit), 1)
        for updated in ({**receipt, 'role': 'worker'}, {**receipt, 'source_revision': 1},
                        {**receipt, 'result_hash': 'different'}, {**receipt, 'source_id': 'other-task'}):
            restored, audit = share_task_questions(b, payload, [message], {'receipt1': updated}, snapshots)
            self.assertEqual(restored, payload)
            self.assertEqual(audit, [])
        # If the retained message was reduced later, rebuilding from that
        # final candidate cancels the sharing rather than pointing at a ghost.
        reduced = message.model_copy(update={'content': json.dumps({'read_receipt_id': 'receipt1', 'preview': 'not complete'})})
        restored, audit = share_task_questions(b, payload, [reduced], {'receipt1': receipt}, snapshots)
        self.assertEqual(restored, payload)
        self.assertEqual(audit, [])
