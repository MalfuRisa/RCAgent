"""Real visible cards must point back to the exact archived values."""
import json
from pathlib import Path
import unittest

from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableLambda

from test_investigation_contracts import ActiveWorkspaceCase, register_pod, workbench
from workspace.queries import save_query
from workspace.runtime import bind, invoke_model
from workspace.observations import verify_observation


class ProjectionPathAudit(unittest.TestCase):
    def test_model_stage_requires_named_observations_and_preserves_judgment_content(self):
        from models.schemas import StageProposal
        from pydantic import ValidationError
        base = {'client_commit_key': 'stage', 'stage_goal': 'facts', 'question_ids': ['q1']}
        with self.assertRaises(ValidationError):
            StageProposal(**base, observation_proposals=[{'source_ref': {}, 'expected_value': 3}])
        with self.assertRaises(ValidationError):
            StageProposal(**base, judgments=[{'question_id': 'q1', 'text': 'lost', 'limitations': 'not a list'}])
        with self.assertRaises(ValidationError):
            StageProposal(**base, material_dispositions=[{'evidence_id': 'e1', 'status': 'covered_by'}])
        with self.assertRaises(ValidationError):
            StageProposal(**base, material_dispositions=[{'source_id': 'e1', 'disposition': 'ignored'}])
        valid = StageProposal(**base,
            observation_proposals=[{'local_id': 'o1', 'source_ref': {'evidence_id': 'e1'}, 'expected_value': 3}],
            judgments=[{'question_id': 'q1', 'content': 'Unresolved',
                        'supporting_observation_ids': ['o1'], 'limitations': ['No causation']}]).model_dump()
        self.assertEqual(valid['judgments'][0]['content'], 'Unresolved')

    def test_thirteen_tool_scalar_paths_match_stored_samples(self):
        from context.tool_views import project
        from context.projection_paths import card_paths
        from workspace.observations import resolve_pointer, values_match
        samples = json.loads((Path(__file__).parents[1] / 'fixtures/workspace_tool_samples.json').read_text('utf-8-sig'))['samples']
        self.assertEqual(len(samples), 13)
        for name, sample in samples.items():
            raw = sample['observations']
            with self.subTest(tool=name):
                for _aliases, canonical, value in card_paths(project(name, raw)):
                    if isinstance(value, (dict, list)):
                        continue
                    actual, found, _ = resolve_pointer(raw, canonical)
                    self.assertTrue(found, (name, canonical))
                    self.assertTrue(values_match(value, actual), (name, canonical, value, actual))

    def test_representative_indices_and_escaped_keys(self):
        from context.projection_paths import card_paths, resolve_card_pointer
        card = {'source': {'samples': {'response_code': 'samples[].response_code'}, 'extra': 'summary'},
                'observations': {'samples': {'samples': [{'source_index': 5, 'response_code': 3}],
                                             'shown': 1, 'total': 8},
                                 'extra': {'a/b': {'c~d': 7}}}}
        self.assertEqual(resolve_card_pointer(card, '/observations/samples/samples/0/response_code'),
                         '/samples/5/response_code')
        self.assertIsNone(resolve_card_pointer(card, '/samples/0/response_code'))
        self.assertTrue(any(path == '/summary/a~1b/c~0d' and value == 7
                            for _aliases, path, value in card_paths(card)))


class ProjectionReferenceTests(ActiveWorkspaceCase):
    async def seed(self, tool='get_pod_network_summary', raw=None):
        await register_pod(self.b, 'iam', 'app')
        raw = raw if raw is not None else {'summary': {'rst_count': 3, 'anomalies': [
            {'metric': 'tcp_rst_fail', 'value': 3, 'peer': {'name': 'mysql'}}]}}
        await save_query(self.b, 'evidence_projection', tool, {'cluster': 'c', 'pod_uid': 'iam-uid'},
                         raw, 'now', 1, 'ok', 'projection-query')
        self.visible = None

        async def respond(messages):
            self.visible = workbench(messages)
            return AIMessage(content='ok')

        with bind(self.b):
            await invoke_model(RunnableLambda(respond), [SystemMessage(content='Inspect shown facts')], phase='triage')

    async def verify(self, pointer, value, **source):
        proposal = {'source_ref': {'evidence_id': 'evidence_projection', 'json_pointer': pointer, **source},
                    'expected_value': value}
        with bind(self.b):
            return await verify_observation(self.b, proposal, owner='worker')

    async def test_original_list_leaf_is_provably_shown(self):
        await self.seed()
        result = await self.verify('/summary/anomalies/0/metric', 'tcp_rst_fail')
        self.assertIn('accepted', result, result)

    async def test_exact_recorded_hybrid_path_is_normalized_and_audited(self):
        await self.seed()
        result = await self.verify('/summary/anomalies/items/0/metric', 'tcp_rst_fail')
        self.assertIn('accepted', result, result)
        observation = result['accepted']
        self.assertEqual(observation['source_ref']['json_pointer'], '/summary/anomalies/0/metric')
        self.assertEqual(observation['source_pointer_resolution']['submitted_pointer'],
                         '/summary/anomalies/items/0/metric')

    async def test_resolve_pod_mapping_and_recorded_hybrid_path(self):
        await self.seed('resolve_pod', {'ambiguous': False, 'pods': [
            {'pod_uid': 'iam-uid', 'name': 'iam', 'namespace': 'app', 'cluster': 'c'}]})
        card = self.visible['evidence'][0]
        self.assertEqual(card['source']['candidates'], 'pods')
        result = await self.verify('/pods/candidates/0/pod_uid', 'iam-uid')
        self.assertIn('accepted', result, result)
        self.assertEqual(result['accepted']['source_ref']['json_pointer'], '/pods/0/pod_uid')

    async def test_display_path_and_nested_field_are_resolved(self):
        await self.seed()
        result = await self.verify('/observations/anomalies/items/0/peer/name', 'mysql')
        self.assertIn('accepted', result, result)
        self.assertEqual(result['accepted']['source_ref']['json_pointer'], '/summary/anomalies/0/peer/name')

    async def test_wrong_value_still_rejected(self):
        await self.seed()
        result = await self.verify('/summary/anomalies/items/0/value', 300)
        self.assertEqual(result['rejected']['reason'], 'value_mismatch', result)

    async def test_unknown_prefix_and_metadata_are_not_facts(self):
        await self.seed()
        for pointer, value in [('/invented/anomalies/items/0/value', 3),
                               ('/summary/anomalies/shown', 1),
                               ('/summary/anomalies/items/9/value', 3)]:
            result = await self.verify(pointer, value)
            self.assertIn('rejected', result, result)

    async def test_unshown_original_row_is_not_recovered(self):
        await self.seed(raw={'summary': {'anomalies': [{'value': i} for i in range(8)]}})
        result = await self.verify('/summary/anomalies/6/value', 6)
        self.assertEqual(result['rejected']['reason'], 'not_shown_to_model', result)

    async def test_changed_source_is_rejected_before_normalization(self):
        await self.seed()
        result = await self.verify('/summary/anomalies/items/0/value', 3, source_hash='stale')
        self.assertEqual(result['rejected']['reason'], 'source_changed', result)

    async def test_explicitly_empty_returned_list_is_visible(self):
        await self.seed('get_pod_application_summary', {'summary': {'abnormal_endpoints': []}})
        result = await self.verify('/summary/abnormal_endpoints', [])
        self.assertIn('accepted', result, result)

    async def test_truncated_list_does_not_prove_whole_list(self):
        rows = [{'value': i} for i in range(8)]
        await self.seed(raw={'summary': {'anomalies': rows}})
        result = await self.verify('/summary/anomalies', rows)
        self.assertEqual(result['rejected']['reason'], 'not_shown_to_model', result)

    async def test_correction_suggests_visible_empty_list_not_display_count(self):
        from workspace.observation_visibility import correction_examples
        await self.seed('get_pod_application_summary', {'summary': {'abnormal_endpoints': []}})
        result = await self.verify('/summary/abnormal_endpoints/total', 0)
        self.assertIn('rejected', result)
        examples = await correction_examples(self.b, [result['rejected']])
        self.assertIn({'source_ref': {'evidence_id': 'evidence_projection',
                                    'json_pointer': '/summary/abnormal_endpoints'}, 'expected_value': []}, examples)
        self.assertFalse(any(item['source_ref']['json_pointer'].endswith('/total') for item in examples))
