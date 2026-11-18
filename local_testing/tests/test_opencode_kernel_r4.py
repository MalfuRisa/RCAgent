"""Native product integration checks with a deterministic CLI, not a model experiment."""
import asyncio
import json
import os
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch
from agents.opencode_session import OpenCodeSession
from workspace.opencode_experiment_outputs import RunOutputs, returned_page
from graph import build_parent_graph

FAKE = r'''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
if args==['--version']:
 print('fake-cli-for-deterministic-tests');sys.exit()
conf=json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
if args[:2]==['debug','config']:
 print(json.dumps(conf));sys.exit()
assert conf['tools']['*'] is False
assert conf['tools']['insights_insights_query_list'] is True
assert conf['permission']['*']=='deny'
work=Path.cwd();marker=work/'prior-session'
session='test-native-session'
def emit(kind,part):
 print(json.dumps({'type':kind,'sessionID':session,'part':dict(part,sessionID=session)},ensure_ascii=False),flush=True)
if '--session' in args:
 assert args[args.index('--session')+1]==session
 assert marker.exists()
 if marker.read_text()=='review_fail':sys.exit(3)
 assert '90' in args[-1]
 # Re-emitting an old event is not a new call.
 emit('tool_use',json.loads((work/'old-event').read_text()))
 emit('text',{'id':'review-text','messageID':'review-message','text':'## 复核意见\n实际返回90条，total=0不代表没有数据；仅host关联，不能确认Pod健康。\n## 修订后的分析\n有90条关联host采样，日志含403是业务事实，影响未决。'})
else:
 marker.write_text('review_fail' if 'review_fail' in args[-1] else 'normal')
 rows=[{'time':'2026-11-01T12:00:00+08:00','value':i,'log':'业务403 '+('x'*1000)} for i in range(90)]
 event={'id':'tool-result','messageID':'worker-message','callID':'call-data','tool':'insights_insights_query_list','state':{'status':'completed','input':{'body':{'queries':[{'where':"tag.host='node-a'"}]}},'output':json.dumps({'data':{'list':rows,'total':0}})}}
 (work/'old-event').write_text(json.dumps(event))
 emit('tool_use',event)
 emit('tool_use',{'id':'tool-error','messageID':'worker-message','callID':'call-error','tool':'insights_insights_query_metric','state':{'status':'error','error':'backend_error','input':{}}})
 emit('text',{'id':'worker-text','messageID':'worker-message','text':'原始调查正文：约46条host记录，403日志不等于客户端鉴权失败。'})
 if 'cancel_after_text' in args[-1]:time.sleep(60)
emit('step_finish',{'id':'step-finish-review' if '--session' in args else 'step-finish-worker','messageID':'review-message' if '--session' in args else 'worker-message','tokens':{'input':10,'output':10}})
'''


class NativeKernelR4(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root/'fake-opencode'
        self.binary.write_text(FAKE)
        self.binary.chmod(0o700)
        self.connections = []

    @asynccontextmanager
    async def connect(self, output):
        self.connections.append('open')
        try:
            yield 'http://127.0.0.1/fake', 'test-credential', ['insights_query_list','insights_query_metric']
        finally:
            self.connections.append('closed')

    def factory(self, outputs, **kwargs):
        original = kwargs['progress']
        def progress(event):
            original(event)
            if getattr(self, 'cancel_after_text', False) and event.get('event')=='text' and event.get('role')=='worker':
                kwargs['cancellation'].set()
        kwargs['progress'] = progress
        return OpenCodeSession(outputs, binary=str(self.binary), connect=self.connect, **kwargs)

    async def run_graph(self, task='observe Pod'):
        state={'incident_text':task,'target_cluster':'rd2','target_namespace':'kylin-insights',
               'target_resource':'pod-a','output_directory':str(self.root/'run')}
        with patch('context.investigation_preparation.prepare',side_effect=AssertionError('old workbench called')):
            return await build_parent_graph(runtime_factory=self.factory).ainvoke(state)

    async def test_same_session_partial_tool_failure_and_count_review(self):
        result=await self.run_graph()
        report=result['final_report']
        self.assertEqual(report['answer_source'],'model')
        self.assertEqual(report['answer_status'],'completed')
        self.assertIn('90条',report['body'])
        self.assertEqual(report['session_id'],'test-native-session')
        self.assertEqual(len(report['sources']),2) # replay does not repeat source call
        self.assertEqual(report['sources'][0]['returned_rows'],90)
        self.assertEqual(report['sources'][0]['backend_total'],0)
        self.assertIn('unattributed',report['sources'][0]['association'])
        self.assertEqual(self.connections,['open','closed'])
        runtime=json.loads((self.root/'run/runtime.json').read_text())
        self.assertIsNone(runtime['application_deadline'])
        self.assertNotIn('observation_ids',report)
        self.assertTrue((self.root/'run/worker-answer.md').exists())
        self.assertTrue((self.root/'run/report.md').exists())

    async def test_review_failure_preserves_original_body(self):
        result=await self.run_graph('review_fail')
        report=result['final_report']
        self.assertEqual(report['execution_status'],'execution_error')
        self.assertEqual(report['answer_status'],'partial')
        self.assertIn('原始调查正文',report['body'])
        self.assertTrue(any('复核未完成' in x for x in report['limitations']))
        self.assertEqual(self.connections,['open','closed'])

    async def test_cancel_stops_owned_process_and_preserves_received_text(self):
        self.cancel_after_text=True
        result=await self.run_graph('cancel_after_text')
        report=result['final_report']
        self.assertEqual(report['execution_status'],'interrupted')
        self.assertIn('原始调查正文',report['body'])
        self.assertFalse((self.root/'run/review-instruction.txt').exists())
        self.assertTrue(json.loads((self.root/'run/cleanup.json').read_text())['own_process_exited'])

    def test_plain_counts_and_archive_error_do_not_erase_answer(self):
        self.assertEqual(returned_page('{"data":{"list":[1,2],"total":0}}')['returned_rows'],2)
        out=RunOutputs(self.root/'disk-error','r')
        out.texts={'worker':{'p':'已有正文'}}
        with patch.object(Path,'write_text',side_effect=OSError('disk full')):
            report=out.publish('execution_error')
        self.assertEqual(report['body'],'已有正文')
        self.assertTrue(any('disk full' in x for x in report['limitations']))

    async def test_missing_kernel_is_explicit_not_a_legacy_fallback(self):
        def missing(outputs, **kwargs):
            return OpenCodeSession(outputs,binary='/does/not/exist',connect=self.connect,**kwargs)
        state={'incident_text':'observe','output_directory':str(self.root/'missing')}
        result=await build_parent_graph(runtime_factory=missing).ainvoke(state)
        self.assertEqual(result['final_report']['answer_source'],'program_partial')
        self.assertEqual(result['execution_status'],'execution_error')
        self.assertEqual(self.connections,['open','closed'])

    async def test_outer_task_cancellation_keeps_answer(self):
        # Actual asyncio cancellation, in addition to the explicit cancel event.
        task = asyncio.create_task(self.run_graph('cancel_after_text'))
        for _ in range(200):
            if (self.root/'run/worker-answer.md').exists():
                break
            await asyncio.sleep(0.01)
        self.assertTrue((self.root/'run/worker-answer.md').exists())
        task.cancel()
        result = await task
        self.assertEqual(result['execution_status'],'interrupted')
        self.assertIn('原始调查正文', result['final_report']['body'])

    def test_native_tool_output_is_archived_and_hidden_reasoning_not_recorded(self):
        import uuid
        native = Path('/tmp/opencode/tool-output') / ('tool-test-'+uuid.uuid4().hex)
        native.parent.mkdir(parents=True,exist_ok=True)
        native.write_text('complete native raw output')
        self.addCleanup(lambda: native.unlink(missing_ok=True))
        out=RunOutputs(self.root/'external-output','r')
        out.accept({'type':'reasoning','text':'hidden contents'},'worker')
        out.accept({'type':'tool_use','sessionID':'s','part':{'id':'p','callID':'c',
            'tool':'read','state':{'status':'completed','input':{},'metadata':{'outputPath':str(native)},
                'output':'saved '+str(native)}}},'worker')
        saved=Path(out.sources['c']['external_outputs'][0])
        self.assertEqual(saved.read_text(),'complete native raw output')
        self.assertNotIn('hidden contents',(out.root/'events.jsonl').read_text())

    async def test_deprecated_alias_keeps_an_existing_native_result(self):
        from workflow.role_compat import invoke_role
        state={'final_report':{'runtime':'opencode','body':'existing answer'}}
        result=await invoke_role(state,'supervisor')
        self.assertIs(result,state)

if __name__=='__main__':unittest.main()
