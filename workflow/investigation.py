"""Default product graph: RCAgent owns the model, MCP and role conversation."""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from agents.investigation_budget import ENTRY_BUDGET, PhaseExpired, RunBudget
from agents.investigation_session import InvestigationSession
from agents.investigation_transport import RunCancellation, interruption_reason
from config import settings
from models.native_tasks import explicit_question_window, original_window
from models.states import SreParentState
from workspace.investigation_outputs import RunOutputs

from .investigation_dispatch import Dispatcher
from .investigation_review import assemble_partials, review_draft

_ACTIVE = {}
_ENTRY_BUDGETS = {}


def cancel(run_id):
    event = _ACTIVE.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def initial_instruction(state):
    task = state.get("incident_text") or state.get("task") or state.get("app_summary")
    if not task or not str(task).strip():
        raise ValueError("Provide a natural-language investigation question")
    namespace = state.get("target_namespace")
    if (
        namespace
        and "*" not in settings.ALLOWED_NAMESPACES
        and namespace not in settings.ALLOWED_NAMESPACES
    ):
        raise ValueError("Target namespace is outside configured access scope")
    target = {
        k: state[k]
        for k in (
            "target_cluster",
            "target_namespace",
            "target_resource",
            "query_window",
        )
        if state.get(k)
    }
    if isinstance(target.get("query_window"), dict):
        window = dict(target["query_window"])
        for key in ("start_time", "end_time"):
            if isinstance(window.get(key), str):
                try:
                    value = datetime.fromisoformat(window[key])
                    if value.tzinfo is not None:
                        window[key + "_unix_seconds"] = int(value.timestamp())
                    else:
                        window["normalization_note"] = (
                            "时间未含时区，需要调查时澄清，不能猜测。"
                        )
                except ValueError:
                    window["normalization_note"] = (
                        "保留原始窗口；无法解析的时间需要澄清。"
                    )
        target["query_window"] = window
    return (
        "只读SRE调查。目标与时间窗口（保留原时区）："
        + json.dumps(target, ensure_ascii=False)
        + "\n"
        + str(task)
    )


async def run_roles(session, outputs, state):
    structured = original_window(state.get('query_window'))
    question = state.get('incident_text') or state.get('task') or state.get('app_summary')
    window = structured or explicit_question_window(question)
    if window:
        outputs.event('user_main_window', 'system', {'source':'structured_input' if structured else 'explicit_beijing_question',
                                                     'window':window.card()})
    dispatcher = Dispatcher(
        session,
        window,
        confirmed_main_window=window is not None,
        question=question,
        target_resource=state.get("target_resource"),
    )
    try:
        await dispatcher.run()
    except PhaseExpired as exc:
        session.closing = True
        outputs.record_stop(exc.reason)
        outputs.event("closing_started", "system", {"reason": outputs.stop_reason})
        if exc.reason in {"deadline", "budget_delivery"}:
            raise
        if exc.phase == "investigation" and not session.cancellation.is_set() and session.budget.remaining("generation") > 0:
            try:
                await dispatcher.draft("调查预算结束；依据截止时已经保存的阶段与原文导航，只汇总已有原值、反证和缺口，简洁回答原题，不再调用工具。")
            except PhaseExpired as later:
                outputs.record_stop(later.reason)
    session.closing = True
    if outputs.answer("worker"):
        await review_draft(session, state.get("review_mode", "program"))


async def _investigate(state, runtime_factory=None):
    run_id = state.get("run_id") or "run_" + uuid.uuid4().hex
    root = Path(
        state.get("output_directory") or settings.WORKSPACE_ROOT / "runs" / run_id
    )
    budget = _ENTRY_BUDGETS.pop(state.get("native_budget"), None) or ENTRY_BUDGET.get() or RunBudget(state.get("deadline"))
    outputs = RunOutputs(root, run_id)
    outputs.budget = budget
    outputs.review_mode = state.get("review_mode", "program")
    outputs.stop_reason = None
    outputs.event("request_received", "system")
    outputs.event("budget_created", "system", budget.card())
    outputs.secrets.append(settings.LLM_API_KEY)
    outputs.save(
        "input.json",
        {k: v for k, v in state.items() if k not in {"messages", "final_report", "native_budget"}},
    )
    cancellation = RunCancellation()
    deadline = None
    _ACTIVE[run_id] = cancellation
    status, limitation = "completed", None
    try:
        writer = get_stream_writer()
    except RuntimeError:
        writer = lambda _: None

    def progress(event):
        writer({"run_id": run_id, "runtime": "rcagent", **event})

    try:
        instruction = initial_instruction(state)
        outputs.instruction = instruction
        deadline = None  # User wall deadline was converted once into the monotonic RunBudget.
        if outputs.review_mode not in {"program", "focused"}:
            raise ValueError("review_mode must be program or focused")
        factory = runtime_factory or InvestigationSession
        async with factory(
            outputs,
            task=instruction,
            cancellation=cancellation,
            deadline=deadline,
            progress=progress,
        ) as session:
            await run_roles(session, outputs, state)
    except asyncio.CancelledError:
        reason = interruption_reason(cancellation, deadline) or "task_cancelled"
        outputs.record_stop(reason)
        status, limitation = (
            "interrupted",
            "用户明确指定的deadline已到，保留已有成果。"
            if reason == "deadline"
            else "用户取消；停止新请求，保留已收到正文与材料。"
            if reason == "cancelled"
            else "执行任务被取消，来源未确认；保留已收到正文与材料。",
        )
        outputs.event("run_interrupted", "system", {"reason": reason})
        if outputs.review_status == "unavailable":
            outputs.review_status = "interrupted"
    except PhaseExpired as exc:
        status = "interrupted"
        limitation = "用户期限已到，保留已得材料与阶段结果。" if exc.reason == "deadline" else "产品预算结束，保留已得材料与阶段结果。"
        outputs.record_stop(exc.reason)
    except TimeoutError as exc:
        reason = interruption_reason(cancellation, deadline)
        if reason:
            outputs.record_stop(reason)
            status = "interrupted"
            limitation = (
                "用户明确指定的deadline已到，保留已有成果。"
                if reason == "deadline"
                else "用户取消；停止新请求，保留已收到正文与材料。"
            )
            outputs.event("run_interrupted", "system", {"reason": reason})
        else:
            outputs.record_stop("service_error")
            status = "execution_error"
            limitation = "服务超时；保留已有成果：" + outputs.redact(
                str(exc) or type(exc).__name__
            )
            outputs.event(
                "execution_error",
                "system",
                {"code": "service_timeout", "message": limitation},
            )
    except Exception as exc:  # noqa: BLE001 -- product boundary preserves partial artifacts
        outputs.record_stop("service_error")
        status, limitation = "execution_error", outputs.redact(str(exc))
        outputs.event("execution_error", "system", {"message": limitation})
    finally:
        _ACTIVE.pop(run_id, None)
        if not outputs.answer("worker"):
            assemble_partials(outputs)
    report = outputs.publish(status, limitation)
    outputs.event("result_ready", "system", {"elapsed_seconds": budget.clock()-budget.started})
    outputs.save("result-ready.json", {"elapsed_seconds": budget.clock()-budget.started, "budget": budget.card()})
    report["elapsed_seconds"] = budget.clock()-budget.started
    outputs.sealed = True
    return {
        "run_id": run_id,
        "conversation_id": outputs.conversation_id,
        "runtime": "rcagent",
        "workspace_root": str(root),
        "execution_status": status,
        "execution_error": {"message": limitation} if limitation else None,
        "workflow_stage": "completed" if status == "completed" else status,
        "evidence_status": report["evidence_status"],
        "answer_status": report["answer_status"],
        "final_report": report,
    }


async def investigate(state, runtime_factory=None):
    try:
        return await _investigate(state, runtime_factory)
    except (ValueError, OSError) as exc:
        body = "本地输入或归档初始化失败：" + str(exc)
        return {"runtime": "rcagent", "execution_status": "execution_error", "execution_error": {"message": body},
            "answer_status": "none", "final_report": {"runtime": "rcagent", "execution_status": "execution_error",
            "answer_status": "none", "answer_source": "program_partial", "body": body, "limitations": [body],
            "evidence_status": "unchecked"}}


def build_investigation_graph(*, runtime_factory=None):
    graph = StateGraph(SreParentState)

    async def initialize(state):
        try:
            budget = ENTRY_BUDGET.get() or RunBudget(state.get("deadline"))
        except ValueError:
            budget = None
        key = uuid.uuid4().hex
        if budget is not None: _ENTRY_BUDGETS[key] = budget
        return {"workflow_stage": "initialize", "runtime": "rcagent", "native_budget": key}

    async def run(state):
        return await investigate(state, runtime_factory)

    graph.add_node("initialize", initialize)
    graph.add_node("investigation", run)
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "investigation")
    graph.add_edge("investigation", END)
    return graph.compile()
