"""Active stage errors use local question patches; never full-stage regeneration."""
import json
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from agents.stage_handoff import accept_role_stage
from models.stage_repair import StageRepair
from workspace.repository import WorkspaceError
from workspace.runtime import bind, invoke_model
from workspace import investigations
from test_investigation_contracts import ActiveWorkspaceCase


class RoleStageCorrection(ActiveWorkspaceCase):
    async def test_rejected_question_assignment_is_patched_once(self):
        calls = []
        question = await investigations.save_question(self.b, text='Reset correlation remains unknown')
        class Model:
            def with_structured_output(inner, schema):
                async def answer(messages):
                    self.assertEqual(schema, StageRepair)
                    payload = json.loads(messages[-1].content)
                    calls.append(payload)
                    return {'proposal_id': payload['proposal_id'], 'proposal_version': payload['proposal_version'],
                        'status': 'patched', 'changes': [{'path': path, 'value': payload['candidate_questions'][0]['id']}
                                                        for path in payload['allowed']]}
                return RunnableLambda(answer)
        draft = {'client_commit_key': 'original', 'stage_goal': 'retain unknowns', 'completion': 'partial',
            'question_ids': [question['id']], 'judgments': [{'content': 'Correlation not established'}],
            'open_items': ['scope unknown']}
        with bind(self.b):
            await invoke_model(RunnableLambda(lambda _: AIMessage(content='draft')), [SystemMessage(content='Triage')])
            result = await accept_role_stage(self.b, Model(), draft, owner='worker', phase='triage')
        self.assertTrue(result['accepted'], result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(await self.b.repository.all(self.b.incident_id, 'stage_commit')), 1)

    async def test_nonwhite_error_preserves_outer_flow_without_accepting_stage(self):
        calls = []
        class Model:
            def with_structured_output(inner, schema):
                calls.append(schema)
                raise AssertionError('Must not regenerate a stage')
        invalid = {'client_commit_key': 'bad', 'stage_goal': 'bad', 'question_ids': []}
        with bind(self.b):
            result = await accept_role_stage(self.b, Model(), invalid, [], owner='worker', phase='triage')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason'], 'stage_repair_not_applicable')
        issues = await self.b.repository.all(self.b.incident_id, 'recovery_issue')
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['action'], 'continue_valid_outer_payload_without_accepting_stage')
        self.assertEqual(calls, [])
        self.assertEqual(await self.b.repository.all(self.b.incident_id, 'stage_commit'), [])
