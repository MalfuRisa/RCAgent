"""Deterministic investigator for archived acceptance; reads visible messages only.

No repository, fixture file or Binding is accessible here. The read plan is
part of the task input; every proposed value and official ID comes from the
actual input or earlier actual tool replies. This is not a real LLM evaluation.
"""
import json
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda


def workbench(messages):
    from context.exact_representation import expand
    return expand(next(json.loads(m.content.split('\n', 1)[1]) for m in messages
                       if str(m.content).startswith('Incident workbench')))


def call(name, args, key):
    return {'name': name, 'args': args, 'id': key}


class LongInvestigator:
    def __init__(self):
        self.queue = None
        self.seen_replies = set()
        self.pages = []
        self.commits = []
        self.calls = []
        self.rechecked = False
        self.expectations = {}
        self.network_id = None

    def bind_tools(self, tools, **kwargs):
        allowed = {t.name for t in tools}
        async def answer(messages):
            self.calls.append({'tools': sorted(allowed), 'messages': [m.model_dump(mode='json') for m in messages]})
            view = workbench(messages)
            for row in view.get('evidence', []) + view.get('evidence_directory', []):
                if row.get('tool_name') == 'get_pod_network_summary':
                    self.network_id = row['evidence_id']
            opening = next((json.loads(m.content) for m in reversed(messages) if m.type == 'human'
                            and str(m.content).startswith('{"stage":')), {})
            if opening.get('checkpoint_id') and opening['checkpoint_id'] not in self.commits:
                self.commits.append(opening['checkpoint_id'])
            if self.queue is None:
                task = next(r for r in view['tasks'] if 'acceptance_checks' in r)
                self.queue = []
                for text in task['acceptance_checks']:
                    marker, source_id, source_tool = text.split(':', 2)
                    self.queue.append({'name': 'read_evidence' if marker == 'evidence_id' else 'read_artifact',
                        'source_tool': source_tool, 'args': {marker: source_id, 'view': 'raw',
                                                           'char_offset': 0, 'max_chars': 8000}})
                for entry in self.queue:
                    if entry.get('source_tool') == 'get_pod_network_summary':
                        self.network_id = entry['args']['evidence_id']
            for message in messages:
                if message.type != 'tool' or message.tool_call_id in self.seen_replies:
                    continue
                body = json.loads(message.content)
                if message.name == 'commit_investigation_stage':
                    if body.get('status') != 'accepted':
                        raise AssertionError('stage rejected: ' + str(body))
                    if body['checkpoint_id'] not in self.commits:
                        self.commits.append(body['checkpoint_id'])
                elif body.get('read_receipt_id'):
                    if body.get('page_limit_too_small') or body.get('read_page_limit_too_small'):
                        raise AssertionError('page made no progress: ' + str(body))
                    self.pages.append(body)
                    position = body.get('next_char_offset')
                    if body.get('truncated') and position is not None:
                        marker = ('artifact_id' if body.get('artifact_id') else 'evidence_id')
                        self.queue.append({'name': message.name, 'args': {
                            marker: body[marker], 'field': body.get('field') or '',
                            'char_offset': position, 'max_chars': 8000}})
                elif body.get('status') == 'rejected':
                    if str(body.get('reason', '')).startswith('checkpoint_required'):
                        original = next(c for m in messages if m.type == 'ai'
                                        for c in m.tool_calls if c['id'] == message.tool_call_id)
                        self.queue.insert(0, {'name': original['name'], 'args': original['args']})
                    else:
                        raise AssertionError('tool rejected: ' + str(body))
                self.seen_replies.add(message.tool_call_id)
            if self.pages:
                question = view['investigation']['questions'][0]['question_id']
                opening = next((json.loads(m.content) for m in reversed(messages) if m.type == 'human'
                                and str(m.content).startswith('{"stage":')), {})
                proposal = {'client_commit_key': 'long-stage-' + str(len(self.commits)),
                    'expected_checkpoint_id': opening.get('checkpoint_id'),
                    'expected_revision': opening.get('checkpoint_revision', 0),
                    'stage_goal': 'Verify this exact recorded page batch; retain limits', 'completion': 'partial',
                    'question_ids': [question], 'observation_proposals': [], 'material_dispositions': [],
                    'open_items': ['Root cause unproven; raw records have source-specific gaps'],
                    'next_focus': ['Continue remaining ranges, then recheck the network counter']}
                for index, body in enumerate(self.pages):
                    marker = 'artifact_id' if body.get('source') == 'artifact_content' else 'evidence_id'
                    if not body.get(marker):
                        marker = 'artifact_id' if body.get('artifact_id') and not body.get('evidence_id') else 'evidence_id'
                    source_id = body[marker]
                    field = body.get('field') or ''
                    pointer = '/' + '/'.join(p.replace('~', '~0').replace('/', '~1') for p in field.split('.')) if field else ''
                    unit = 'chars' if 'text' in body else 'items'
                    local = 'page-' + str(index)
                    proposal['observation_proposals'].append({'local_id': local, 'source_ref': {
                        marker: source_id, 'json_pointer': pointer, 'range': body['range'], 'range_unit': unit},
                        'expected_value': body.get('text') if unit == 'chars' else body['items'],
                        'limitations': ['Exact recorded range only; not proof of a cause']})
                    proposal['material_dispositions'].append({'source_id': source_id, 'field': field,
                        'range': body['range'], 'range_unit': unit, 'disposition': 'covered_by', 'covered_by': [local]})
                    if field == 'summary.rst_count' and unit == 'items':
                        proposal['observation_proposals'].append({'local_id': 'counter', 'source_ref': {
                            marker: source_id, 'json_pointer': pointer}, 'expected_value': body['items'][0],
                            'limitations': ['Recorded resets; no DNS/database causation established']})
                        proposal['judgments'] = [{'question_id': question, 'type': 'interpretation',
                            'content': 'Reset counter observed; DNS configuration/database causation remain unverified.',
                            'supporting_observation_ids': ['counter'], 'status': 'unresolved',
                            'limitations': ['Archived samples do not establish request-level causality']}]
                self.pages = []
                if not self.queue and self.rechecked:
                    return AIMessage(content='', tool_calls=[call('submit_final_diagnosis', {
                        'diagnosis': 'Recorded resets observed; cause unresolved',
                        'reasoning': 'All archived ranges reviewed; exact counter re-read after multiple handoffs.',
                        'supporting_evidence_ids': [self.network_id], 'stage_proposal': proposal,
                        'answer_status': 'inconclusive',
                        'gaps': ['DNS configuration and database causation remain unverified']}, 'finish')])
                return AIMessage(content='', tool_calls=[call('commit_investigation_stage', proposal,
                                                              'commit-' + str(len(self.commits)))])
            if self.queue:
                if len(self.queue) <= 2 and len(self.commits) >= 2 and not self.rechecked:
                    self.rechecked = True
                    self.queue.append({'name': 'read_evidence', 'args': {'evidence_id': self.network_id,
                        'field': 'summary.rst_count', 'max_chars': 3000}})
                count = 3 if len(self.queue) == 3 and self.queue[-1]['args'].get('field') == 'summary.rst_count' else 2
                batch = self.queue[:count]
                self.queue = self.queue[count:]
                return AIMessage(content='', tool_calls=[call(r['name'], r['args'],
                    'read-%d-%d' % (len(self.calls), i)) for i, r in enumerate(batch)])
            assert self.network_id, 'network evidence must have appeared in a visible directory'
            if not self.rechecked:
                self.rechecked = True
                return AIMessage(content='', tool_calls=[call('read_evidence', {
                    'evidence_id': self.network_id, 'field': 'summary.rst_count', 'max_chars': 3000}, 'recheck-counter')])
            return AIMessage(content='', tool_calls=[call('submit_final_diagnosis', {
                'diagnosis': 'Recorded resets observed; cause unresolved',
                'reasoning': 'Reviewed archived page ranges and re-read the exact network counter after handoff.',
                'supporting_evidence_ids': [self.network_id], 'checkpoint_id': self.commits[-1],
                'answer_status': 'inconclusive',
                'gaps': ['Archived network/application samples do not prove DNS configuration or database causation']}, 'finish')])
        return RunnableLambda(answer)
