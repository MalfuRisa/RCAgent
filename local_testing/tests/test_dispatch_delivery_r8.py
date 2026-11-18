"""PRD29 normal-graph dispatch, closeout and source boundaries (scripted)."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.investigation_budget import RunBudget
from agents.investigation_session import InvestigationSession
from graph import build_parent_graph
from local_testing.tests.test_task_convergence_r7 import (
    END,
    START,
    Model,
    Native,
    chunk,
    dispatch,
    query,
    spec,
)
from models.native_tasks import TaskSpec
from workspace.native_checks import references


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions=[]

    def tearDown(self):
        self.tmp.cleanup()

    async def product(self, model, *, name='run', **state):
        def factory(out, **kw):
            native=Native(out, cancellation=kw['cancellation'], deadline=kw['deadline'])
            session=InvestigationSession(out, model=model, native=native, **kw)
            self.sessions.append(session)
            return session
        return await build_parent_graph(runtime_factory=factory).ainvoke({'incident_text':'读取目标Pod资源，不查流量',
            'query_window':{'start_time':START,'end_time':END},'output_directory':str(self.root/name), **state})

    async def test_resource_new_and_legacy_proposals_reach_send_without_business(self):
        for index, kwargs in enumerate([{'tools':['insights_k8s_resource']},{'business_tools':['insights_k8s_resource']} ]):
            model=Model([dispatch(spec(**kwargs)),chunk(calls=[('resource','insights_k8s_resource',{'query':{'pod_id':259}})]),
                chunk('资源阶段'),chunk('资源原值与未知项')])
            result=await self.product(model,name=str(index))
            session=self.sessions[-1]
            self.assertEqual([name for name,args in session.native.sent],['insights_k8s_resource'])
            self.assertEqual(result['final_report']['review_status'],'program_checked')
            self.assertEqual(len(model.requests),4)
            self.assertNotIn('insights_query_list',{d['function']['name'] for d in model.requests[1]['tools']})
            self.assertEqual(result['final_report']['task_results'][0]['end_reason'],'first_result_settled')
            self.assertTrue(session.native.closed)

    async def test_same_problem_correction_no_default_query(self):
        model=Model([dispatch({'question':'读取Pod'}),dispatch(spec('读取Pod',tools=['insights_k8s_resource'])),
            chunk(calls=[('r','insights_k8s_resource',{'query':{'pod_id':259}})]),chunk('阶段'),chunk('草稿')])
        result=await self.product(model)
        self.assertEqual(len(self.sessions[0].native.sent),1)
        self.assertTrue(result['final_report']['execution_receipts'])
        self.assertIn('只纠正原问题一次',str(model.requests[1]['messages']))

    async def test_two_invalid_plans_stop_without_changing_question(self):
        model=Model([dispatch({'question':'读取Pod'}),dispatch({'question':'读取Pod'}),chunk('未开始有效取证，派工参数不完整')])
        result=await self.product(model)
        self.assertEqual(self.sessions[0].native.sent,[])
        self.assertEqual(result['final_report']['task_results'],[])
        self.assertIn('派工参数',result['final_report']['body'])

    async def test_later_task_can_correct_after_first_task_already_recovered(self):
        first = spec('先读资源', tools=['insights_k8s_resource'])
        follow_up = spec('另查资源归属', tools=['insights_k8s_resource'])
        model = Model([
            dispatch(dict(first, window={'start':'now-30m','end':'now'})), dispatch(first),
            chunk(calls=[('first','insights_k8s_resource',{'query':{'pod_id':259}})]), chunk('第一阶段'),
            dispatch(dict(follow_up, context_source_ids_note='wrong field')), dispatch(follow_up),
            chunk(calls=[('follow-up','insights_k8s_resource',{'query':{'pod_id':260}})]), chunk('第二阶段'),
            chunk('汇总两个阶段'),
        ])
        result = await self.product(model)
        self.assertEqual([args['query']['pod_id'] for _, args in self.sessions[0].native.sent],[259,260])
        self.assertEqual(len(result['final_report']['task_results']),2)
        self.assertEqual(result['final_report']['body'],'汇总两个阶段')

    async def test_no_default_query_and_strict_reference_format(self):
        for legacy in (False,True):
            raw=spec(**({'business_tools':[]} if legacy else {}))
            task=TaskSpec.model_validate(raw).normalize(seed=True,window=None,tools=Native.tools if hasattr(Native,'tools') else {},source_ids=[])
            self.assertFalse(task.business_tools)
        self.assertEqual(references('source_id source_cards source_previews source_'+'a'*32),['source_'+'a'*32])

    async def test_budget_short_graph_closes_and_keeps_sources(self):
        count = 0
        def scripted(request):
            nonlocal count
            count += 1
            if not request['tools']: return chunk('已有材料的有限草稿')
            if count == 1: return dispatch(spec(tools=['insights_query_list']))
            return chunk(calls=[('slow','insights_list_databases',{})])
        model=Model(scripted)
        real_budget=RunBudget
        with patch('workflow.investigation.RunBudget',side_effect=lambda deadline=None:real_budget(deadline,total=.9)):
            started=time.monotonic()
            result=await self.product(model)
            elapsed=time.monotonic()-started
        self.assertLess(elapsed,2)
        self.assertTrue(self.sessions[0].native.closed)
        self.assertIn(result['execution_status'],['completed','interrupted'],result)
        closeout=[r for r in model.requests if not r['tools']]
        if closeout:
            self.assertIn('截止时已经保存的阶段与原文导航',str(closeout[-1]['messages']))

    def test_budget_defaults_and_deadline_validation(self):
        self.assertEqual(RunBudget().total,300)
        for invalid in (True,float('nan'),float('inf'),'soon'):
            with self.assertRaises(ValueError):RunBudget(invalid)
        now=[0.0]
        budget=RunBudget(clock=lambda:now[0])
        now[0]=201
        self.assertEqual(budget.remaining('investigation'),0)
        self.assertEqual(budget.remaining('generation'),69)

    async def test_continue_new_request_does_not_inherit_previous_public_output(self):
        model=Model([dispatch(spec('first',tools=[])),chunk('旧阶段正文'),
            chunk('完整草稿')])
        await self.product(model)
        worker=next(r for r in self.sessions[0].runners.values() if r.execution)
        worker.owner.closing=False
        worker.owner.model.script=[RuntimeError('failed without output')]
        with self.assertRaises(RuntimeError):await worker.request()
        self.assertEqual(worker.last_public_text,'')

    async def test_targeted_helper_hints_and_unmodified_arguments(self):
        from agents.native_validation import validate
        from local_testing.tests.test_task_convergence_r7 import DEFINITIONS
        from platforms.insights_v5.native_task_scope import LocalRejection
        schema=DEFINITIONS['insights_describe_query']['function']['parameters']
        self.assertIsNone(validate('insights_describe_query',{'endpoint':'list'},schema))
        bad={'endpoint':'usage'}
        with self.assertRaises(LocalRejection) as caught:validate('insights_describe_query',bad,schema)
        self.assertEqual(caught.exception.error['rcagent_hint']['skeleton'],{'endpoint':'list','section':'usage'})
        self.assertEqual(bad,{'endpoint':'usage'})

    async def test_program_and_focused_keep_one_draft_and_receipts(self):
        model=Model([dispatch({'question':'resource'}),dispatch(spec(tools=[])),chunk('阶段'),chunk('唯一完整草稿'),chunk('短提示：材料不足以证明健康')])
        result=await self.product(model,review_mode='focused')
        report=result['final_report']
        self.assertTrue(report['body'].startswith('唯一完整草稿'))
        self.assertEqual(report['review_status'],'focused_completed')
        self.assertIn('event_id',str(model.requests[-1]['messages']))
        self.assertEqual(model.requests[-1]['tools'],[])
        self.assertEqual(self.sessions[-1].native.sent,[])

    async def test_actual_send_denies_unselected_resource_and_unknown_context(self):
        from platforms.insights_v5.native_task_scope import TaskExecution
        from workspace.investigation_outputs import RunOutputs
        out=RunOutputs(self.root/'guard','r');native=Native(out)
        task=TaskSpec.model_validate(spec(tools=[])).normalize(seed=False,window=None,tools=native.tools,source_ids=[])
        context=TaskExecution('t','c',task,status='running');native.authority.register(context)
        for execution in (None,context):
            outcome=await native.call('id','insights_k8s_resource',{'query':{'pod_id':259}},'worker',execution=execution)
            self.assertEqual(outcome['status'],'tool_error')
        self.assertEqual(native.sent,[])

    async def test_graph_invalid_inputs_return_readable_error(self):
        for index, state in enumerate([{'deadline':True},{'deadline':float('nan')},{'review_mode':'full'}]):
            result=await self.product(Model([]),name='bad'+str(index),**state)
            self.assertEqual(result['execution_status'],'execution_error')
            self.assertTrue(result['final_report']['body'])

    async def test_initialization_hang_exits_on_owner_and_budget(self):
        from agents.investigation_budget import RunBudget as RealBudget
        from workspace.investigation_outputs import RunOutputs
        class HangingNative(Native):
            async def __aenter__(self):
                self.entered_on=asyncio.current_task()
                await asyncio.Event().wait()
        out=RunOutputs(self.root/'init','r');out.budget=RealBudget(total=.15)
        session=InvestigationSession(out,task='q',model=Model([]),native=HangingNative(out))
        from agents.investigation_budget import PhaseExpired
        started=time.monotonic()
        with self.assertRaises(PhaseExpired):
            async with session:pass
        self.assertLess(time.monotonic()-started,.5)

    async def test_cleanup_hang_is_bounded_and_not_claimed_success(self):
        from workspace.investigation_outputs import RunOutputs
        class HangingExit(Native):
            async def __aexit__(self,*args):
                self.closed=False
                await asyncio.Event().wait()
        out=RunOutputs(self.root/'cleanup','r');out.budget=RunBudget(total=.2)
        native=HangingExit(out)
        async with InvestigationSession(out,task='q',model=Model([]),native=native):pass
        self.assertTrue(out.errors)
        self.assertFalse(native.closed)

    async def test_answer_resources_allow_short_association_chain(self):
        model=Model([dispatch(spec(tools=['insights_k8s_resource'],return_mode='answer',multi_step_reason='Pod后查关联节点')),
            chunk(calls=[('pod','insights_k8s_resource',{'query':{'pod_id':259}})]),
            chunk(calls=[('node','insights_k8s_resource',{'query':{'node_id':1}})]),chunk('关联阶段'),chunk('简洁正文')])
        result=await self.product(model)
        self.assertEqual(len(self.sessions[0].native.sent),2)
        self.assertEqual(result['final_report']['task_results'][0]['end_reason'],'model_returned')

    def test_legacy_conflict_and_internal_fields_not_model_settable(self):
        for extra in ({'tools':[],'business_tools':['insights_k8s_resource']},{'legacy_default':['insights_query_list']}):
            with self.assertRaises(ValueError):TaskSpec.model_validate(spec(**extra))

    async def test_resource_scope_cannot_send_business_and_expanded_window(self):
        model=Model([dispatch(spec(tools=['insights_query_list'])),chunk(calls=[
            ('ok','insights_query_list',query()),('table','insights_query_list',query('other')),
            ('window','insights_query_list',query(start=START-1))]),chunk('范围拒绝已交回'),chunk('已有数据与范围缺口')])
        report=(await self.product(model))['final_report']
        self.assertEqual(len(self.sessions[0].native.sent),1)
        self.assertEqual(report['task_results'][0]['end_reason'],'scope_change_required')
        self.assertEqual(sum(s['status']=='tool_error' for s in report['sources']),2)

    async def test_retry_after_beyond_budget_not_waited_or_resent(self):
        class Limited(Exception):
            status_code=429
            response=type('Response',(),{'headers':{'retry-after':'500'}})()
        model=Model([Limited('rate limited')])
        started=time.monotonic()
        result=await self.product(model)
        self.assertLess(time.monotonic()-started,2)
        self.assertEqual(len(model.requests),1)
        self.assertEqual(result['execution_status'],'execution_error')
        self.assertEqual(self.sessions[0].native.sent,[])

    async def test_model_queue_covered_by_same_phase_budget(self):
        from agents.investigation_budget import PhaseExpired
        from workspace.investigation_outputs import RunOutputs
        out=RunOutputs(self.root/'queue','r');out.budget=RunBudget(total=.15)
        session=InvestigationSession(out,task='q',model=Model([]),native=Native(out))
        async with session:
            await session.model_semaphore.acquire()
            runner=session.runner('planner')
            try:
                with self.assertRaises(PhaseExpired):await runner.request()
            finally:session.model_semaphore.release()

    async def test_user_cancel_prevents_closeout_model_request(self):
        from workflow.investigation import cancel
        signal=asyncio.Event()
        class HeldModel(Model):
            async def generate(self,messages,definitions):
                if any(d['function']['name'].startswith('dispatch_') for d in definitions):
                    async for x in super().generate(messages,definitions):yield x
                else:
                    yield chunk('阶段部分文字')
                    signal.set()
                    await asyncio.Event().wait()
        model=HeldModel([dispatch(spec(tools=[]))])
        job=asyncio.create_task(self.product(model,run_id='cancel-r8'))
        await asyncio.wait_for(signal.wait(),3)
        self.assertTrue(cancel('cancel-r8'))
        result=await job
        self.assertEqual(result['execution_status'],'interrupted')
        self.assertIn('阶段部分文字',result['final_report']['body'])
        self.assertTrue(self.sessions[0].native.closed)


if __name__=='__main__':unittest.main()
