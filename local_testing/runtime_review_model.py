"""Offline report continuation from actual visible facts; no hidden storage reads."""
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from .long_investigation_model import workbench
from .long_review_model import LongReviewer


class RuntimeReviewer(LongReviewer):
    def bind_tools(self, tools, **kwargs):
        async def answer(messages):
            self.calls.append({'tools': [tool.name for tool in tools],
                               'messages': [message.model_dump(mode='json') for message in messages]})
            view = workbench(messages)
            investigation = view['investigation']
            observation = next(row for row in investigation['observations']
                if row.get('field') == 'json_pointer:/summary/rst_count' and row.get('kind') == 'field')
            resource = observation['scope']['resource_refs'][0]['id']
            questions = investigation['questions']
            return AIMessage(content='', tool_calls=[{'name': 'prepare_report_packet', 'id': 'runtime-packet',
                'args': {'claims': [{'claim_id': 'visible-counter', 'type': 'observation',
                    'resource_id': resource, 'content': 'Expand the exact verified counter',
                    'limitations': ['Historical recorded window only; no request-level causal alignment']}],
                    'observation_refs': {'visible-counter': [observation['observation_id']]},
                    'question_dispositions': {question['question_id']:
                        'Unresolved: recorded signal and scope gaps do not establish a cause' for question in questions}}}])
        return RunnableLambda(answer)
