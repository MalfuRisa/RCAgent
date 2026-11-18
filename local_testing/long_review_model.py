"""Visible-input-only review of the long offline Worker's actual outcome."""
import json
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from .long_investigation_model import workbench, call


class LongReviewer:
    def __init__(self):
        self.calls = []
        self.read_started = False

    def bind_tools(self, tools, **kwargs):
        async def answer(messages):
            self.calls.append({'tools': [t.name for t in tools],
                               'messages': [m.model_dump(mode='json') for m in messages]})
            view = workbench(messages)
            question = view['investigation']['questions'][0]['question_id']
            counter = next(r for r in view['investigation']['observations']
                           if r['source_ref']['json_pointer'] == '/summary/rst_count' and r['kind'] == 'field')
            evidence_id = counter['source_ref']['evidence_id']
            if 'read_evidence' not in {t.name for t in tools}:
                if not any(m.type == 'human' and 'continued_after_handoff' in str(m.content) for m in messages):
                    return AIMessage(content='', tool_calls=[call('commit_investigation_stage', {
                        'client_commit_key': 'review-visible-counter', 'stage_goal': 'Review verified facts and gaps',
                        'completion': 'partial', 'question_ids': [question],
                        'judgments': [{'question_id': question, 'type': 'interpretation',
                            'content': 'Recorded reset counter verified; causal mechanism remains unproven.',
                            'supporting_observation_ids': [counter['observation_id']],
                            'limitations': ['DNS configuration/database causation unverified'], 'status': 'unresolved'}],
                        'open_items': ['No request-level causal correlation'], 'next_focus': ['Prepare limited observations']}, 'review-visible')])
            opening = next((json.loads(m.content) for m in reversed(messages) if m.type == 'human'
                            and str(m.content).startswith('{"stage":')), {})
            if opening:
                return AIMessage(content='', tool_calls=[call('prepare_report_packet', {
                    'claims': [{'claim_id': 'recorded-reset', 'type': 'observation',
                                'resource_id': counter['scope']['resource_refs'][0]['id'],
                                'content': 'Program must generate this observation from the verified counter',
                                'limitations': ['Archived counter; DNS configuration/database causation unverified']}],
                    'observation_refs': {'recorded-reset': [counter['observation_id']]},
                    'question_dispositions': {question: 'Unresolved: no request-level causal correlation'}}, 'packet')])
            page = next((json.loads(m.content) for m in reversed(messages) if m.type == 'tool'
                         and m.name == 'read_evidence'), None)
            if page:
                assert page.get('read_receipt_id'), page
                proposal = {'client_commit_key': 'review-recorded-counter', 'stage_goal': 'Independently re-read counter',
                    'completion': 'partial', 'question_ids': [question],
                    'observation_proposals': [{'local_id': 'counter', 'source_ref': {
                        'evidence_id': evidence_id, 'json_pointer': '/summary/rst_count'},
                        'expected_value': page['items'][0], 'limitations': ['Archived sample only']}],
                    'material_dispositions': [{'source_id': evidence_id, 'field': 'summary.rst_count',
                        'disposition': 'covered_by', 'covered_by': ['counter']}],
                    'open_items': ['DNS configuration and database causation remain unresolved'],
                    'next_focus': ['Prepare observations with their limits, retaining the unresolved question']}
                return AIMessage(content='', tool_calls=[call('commit_investigation_stage', proposal, 'review-stage')])
            self.read_started = True
            return AIMessage(content='', tool_calls=[call('read_evidence', {'evidence_id': evidence_id,
                'field': 'summary.rst_count', 'max_chars': 2000}, 'review-counter')])
        return RunnableLambda(answer)

    def with_structured_output(self, schema):
        async def select(messages):
            from models.schemas import PacketSelection
            assert schema == PacketSelection
            self.calls.append({'schema': schema.__name__, 'messages': [m.model_dump(mode='json') for m in messages]})
            packet = workbench(messages)['report_packet']
            return PacketSelection(packet_id=packet['packet_id'],
                selected_statement_ids=[r['statement_id'] for r in packet['statements']],
                conclusion_status='inconclusive', unresolved_question_ids=packet['unresolved_question_ids'])
        return RunnableLambda(select)
