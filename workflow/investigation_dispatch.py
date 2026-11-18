"""Planner control protocol and finite-concurrency independent task lifecycle."""

import asyncio
import json
import uuid

from langchain_core.messages import ToolMessage

from agents.investigation_budget import PhaseExpired
from agents.investigation_transport import content_text, controlled, interruption_reason
from agents.role_guidance import BUSINESS_GUIDANCE
from config import settings
from models.dispatch_tasks import (
    DispatchRejection,
    build_spec,
    classify_rejection,
    correction_prompt,
    entry_description,
    legacy_spec,
    normalize_spec,
    planner_schema,
    rejection_item,
    rejection_signature,
    truncation_item,
    unparsed_call_rejection,
)
from models.native_capabilities import entry_name
from platforms.business_policy import BUSINESS_READS
from platforms.insights_v5.diagnostic_policy import NATIVE_READS
from platforms.insights_v5.native_task_scope import TaskExecution
from workflow.direct_query import DIRECT_TOOL, DirectQuery, direct_definition
from workflow.native_tool_calls import calls_from, execute_batch, valid_ids, wire_message
from workspace.catalog_preparation import catalog_cards
from workspace.investigation_projection import decision_materials
from workspace.native_tasks import save_task
from workspace.stage_handoff import source_cards, stage_handoff
from context.concept_hints import concept_hint

MAX_DISPATCH_CORRECTIONS = 2
LEGACY_DISPATCH = "dispatch_tasks"
CLOSEOUT_TOOL = "close_investigation"
SOURCE_NAMES = {'business', 'native'}
CLOSEOUT_INSTRUCTION = (
    "停止取证并直接回答原题。只输出一段中文正文：第一句直接回答；"
    "随后给支撑结论的时间、关键数值和单位；最后说明会影响结论的覆盖缺口。"
    "不要分节、不要罗列工具或来源，不要编造。详细原文按 source_id 回查。"
)


def closeout_definition():
    return {
        "type": "function",
        "function": {
            "name": CLOSEOUT_TOOL,
            "description": (
                "Stop evidence gathering and answer now from archived facts and explicit gaps. "
                "Use once when the current evidence can answer the original question or the "
                "investigation margin is tight. Do not combine it with new evidence calls "
                "(dispatch, directed read or material read) in the same response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the current evidence is enough or must be closed.",
                    }
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


def available_entries(discovered):
    """Only sources actually discovered in this run get a Planner entry."""
    found = []
    if set(discovered) & BUSINESS_READS:
        found.append("business")
    if set(discovered) & NATIVE_READS:
        found.append("native")
    return {entry_name(source): source for source in found}


def dispatch_definitions(discovered):
    return {
        entry: {
            "type": "function",
            "function": {
                "name": entry,
                "description": entry_description(source, entry),
                "parameters": planner_schema(source, discovered),
            },
        }
        for entry, source in available_entries(discovered).items()
    }


class Dispatcher:
    def __init__(self, session, window=None, *, confirmed_main_window=False,
                 question=None, target_resource=None):
        self.session, self.outputs, self.window = session, session.outputs, window
        self.confirmed_main_window = confirmed_main_window
        self.question = question or session.task
        self.target_resource = target_resource
        self.tasks, self.worker_runners, self.seen = {}, {}, {}
        self.latest, self.reserved = {}, set()
        self.entries = available_entries(session.native.tools)
        self.any_settled = False
        self.corrections = 0
        self.last_rejections = None
        self.closeout_requested = False
        self.closeout_started = False
        self.direct_available = (
            session.native.profile in {"business", "integrated"}
            and "insights_query_metrics" in session.native.tools
            and window is not None
        )
        self.stats = {
            "calls": 0,
            "accepted": 0,
            "rejected": 0,
            "corrections": 0,
            "correction_limit": MAX_DISPATCH_CORRECTIONS,
            "blocked": False,
            "entries": sorted(self.entries),
            "entries_unavailable": sorted(
                set(SOURCE_NAMES) - set(self.entries.values())
            ),
        }
        self.outputs.dispatch_stats = self.stats
        definitions = {**session.local_definitions, **dispatch_definitions(session.native.tools)}
        definitions[CLOSEOUT_TOOL] = closeout_definition()
        if self.direct_available:
            definitions[DIRECT_TOOL] = direct_definition()
        self.planner = session.runner("planner", definitions=definitions)
        self.direct = DirectQuery(self) if self.direct_available else None
        self.planner.context_provider = lambda: json.dumps(decision_materials(self.outputs, self.window), ensure_ascii=False)
        self.outputs.conversation_id = self.planner.conversation_id
        self.planner.guide("planning-investigation")
        hint = concept_hint(session.task, session.budget)
        self.planner.instruction('同源概念提示（不是现场事实）：'+json.dumps(hint,ensure_ascii=False))
        self.outputs.event('concept_hint', 'planner', hint)
        if not self.outputs.sources:
            self.planner.instruction('程序确认：当前run材料集为空；未挂载历史库。现在无需search_materials，可依据实际工具直接派工。后续有来源后仍可检索，关键词没命中不代表全库为空。')
            self.planner.definitions.pop('search_materials',None)
            self.planner.refresh_binding()
        self.planner.instruction(
            "本run实际发现的MCP工具目录（Planner自身不能调用远程工具）："
            + json.dumps({n:d["function"]["description"] for n,d in session.native.tools.items()}, ensure_ascii=False)
        )
        if 'insights_generate_dashboard' in session.native.tools:
            self.planner.instruction(BUSINESS_GUIDANCE)
        if session.native.profile == 'integrated':
            availability = {family: info['status'] for family, info in session.native.availability.items()}
            self.planner.instruction('当前integrated双来源：每个Task只选一个来源家庭；换源另派到另一个派工入口。业务按名称/词典起查，paths给TopN路径、anomalies仅阈值命中。原生深入元数据/查询/资源需本run可证实ID，服务ID不能作pod_id；同名或版本相同不证明跨源同一环境。可用性：'+json.dumps(availability,ensure_ascii=False))
        if session.native.profile == 'business':
            self.planner.instruction('当前仅business工具。用一个业务小任务按问题选insights_generate_dashboard等；可查少量预设数据集，不受原生单表限制。'
                'dashboard必须scopePolicy=require-target；可用相对时间起步，随后复用返回resolved time的绝对窗。不要调用原native工具。')
        if self.direct_available:
            self.planner.instruction(
                "如果原题只是查一个已确认对象的响应时延、请求率等窗口原值，且一个 query_business_metrics 调用能回答，"
                "先直接调用它；不要先派 Worker 或查目录。程序填写已确认的绝对窗和客户端(auto_service_0)过滤，零Worker；"
                "对象必须逐字来自原题。app-path 常用指标：server_response_time、percentile95、percentile99、request_rate。"
                "需要消歧、分步选择查询、跨来源或证据性质需研判时仍用派工。"
                "对照路径补查是可选增益：只有原题需要同期比较、现有同口径样本不足、已给出有价值候选且剩余预算够补查和成稿时才做；补查不保证形成同步性结论。"
            )
        self.planner.instruction(
            "证据够用或调查余量不足时，可调用 close_investigation 主动结束取证；"
            "它不要和新的取证调用放在同一响应。执行后只用一段正文回答原题："
            "第一句给结论，随后时间/关键数值/单位，最后说明会影响结论的覆盖缺口。"
        )
        entries = "、".join(sorted(self.entries)) or "无（本run未发现可派工来源）"
        self.planner.instruction(
            "持续主控调查：可用派工入口：" + entries + "。首轮只派一个最小问题，不强制业务探测。"
            "每个入口一次只提交一个小任务，只需question和tools；"
            "程序继承已确认的对象范围、绝对时间窗和来源，并填写机械交付与停止条件。"
            "需要不同来源时分别调用对应入口，不要把两个来源的工具放进同一个任务。"
            "首轮只接一个合法最小任务，结算后再接受多个独立补查；同一响应可用多个派工调用提交独立任务。"
            "资源/元数据任务不要求取得未授权业务值。"
            "纯取数子问题用first_result，并把影响判断的指标放在同一批：同一路径的延迟、P95/P99和请求率可一次取回；"
            "程序facts交回后不再写小结。需要根据中间结果选择查询、消歧、跨来源或业务研判时才用answer。"
            "first_result在首批目标调用结算后交回，包括失败；不要要求当前阶段在首次失败后继续换写法。"
            "收到阶段结果后决定继续、独立问题并行或交付。"
            "需要改变结论的缺口才派工。最终默认一段：第一句回答原题，随后关键时间、原值/单位和影响结论的缺口；"
            "过程留归档，不罗列每个工具和所有source。不要编造健康结论，无需报告包。"
        )

    def stop_reason(self):
        explicit = interruption_reason(self.session.cancellation, self.session.deadline)
        if explicit: return explicit
        if self.session.budget and self.session.budget.remaining("investigation") <= 0:
            return self.session.budget.phase_reason("investigation")
        return "task_cancelled"

    def version(self):
        return len(self.outputs.sources), len(self.outputs.task_results)

    def window_kind(self, spec):
        window, main = spec.window, self.window
        if not window:
            return None
        if not main or window == main:
            return 'main'
        if main.start <= window.start and window.end <= main.end:
            return 'subwindow'
        if spec.routing_family == 'business' and (
                window.start < main.end and main.start < window.end):
            raise ValueError('Comparison window must be separate from the main window; a focused subwindow must stay inside it')
        return 'comparison'

    def _seed_error(self):
        return DispatchRejection(
            "seed_limit",
            "First dispatch accepts only one legal minimal task; independent tasks start after its result",
            fix="Wait for this task's result in the same response before adding independent tasks.",
        )

    def _register(self, spec, raw, *, seed, entry, call_id):
        predecessor = None
        if spec.continue_task_id:
            predecessor = self.tasks.get(spec.continue_task_id)
            if not predecessor or predecessor.status != "returned":
                raise ValueError("Continue requires a returned task in this run")
            cid = predecessor.conversation_id
            if cid in self.reserved or self.latest.get(cid) is not predecessor:
                raise ValueError(
                    "Continue requires the latest returned task; conversation already continued/reserved"
                )
            for key in ("dataset", "window", "tools", "window_reason"):
                if key not in spec.model_fields_set:
                    setattr(
                        spec,
                        key,
                        predecessor.dataset
                        if key == "dataset"
                        else getattr(predecessor.spec, key),
                    )
        if predecessor and "tools" not in spec.model_fields_set:
            spec.legacy_default = predecessor.spec.legacy_default
        normalize_spec(
            spec,
            seed=seed,
            main_window=self.window,
            discovered=self.session.native.tools,
            source_ids=self.outputs.sources,
            entry=entry,
        )
        if predecessor and (
            spec.dataset != predecessor.dataset
            or spec.window != predecessor.spec.window
            or spec.effective_tools(self.session.native.tools) != predecessor.spec.effective_tools(self.session.native.tools)
        ):
            raise ValueError(
                "Changed dataset/window/family requires a new independent conversation"
            )
        if self.seen.get(spec.signature()) == self.version():
            raise ValueError("Exact repeated task without new input")
        window_kind = self.window_kind(spec)
        if seed and self.confirmed_main_window and window_kind == 'comparison':
            raise ValueError(f'First task must use confirmed main window {self.window.start}–{self.window.end} or its subwindow')
        context = TaskExecution(
            "task_" + uuid.uuid4().hex,
            predecessor.conversation_id
            if predecessor
            else "conversation_" + uuid.uuid4().hex,
            spec,
            seed=seed,
            submission_index=len(self.tasks),
            window_kind=window_kind,
        )
        if self.session.native.profile in {'business','integrated'} and not self.window and spec.window:
            self.window = spec.window
        context.discovered_tools = self.session.native.tools
        context.dispatch_entry = entry
        context.dispatch_call_id = call_id
        self.session.native.authority.register(context)
        self.tasks[context.task_id] = context
        self.latest[context.conversation_id] = context
        self.reserved.add(context.conversation_id)
        self.outputs.event(
            "task_accepted",
            "planner",
            {"original_proposal": raw, "spec": spec.model_dump(), "effective_tools": sorted(spec.effective_tools(self.session.native.tools)), "seed": seed, "window_kind": window_kind, "dispatch_entry": entry},
            task_id=context.task_id,
            conversation_id=context.conversation_id,
            planner_decision_id=self.planner.planner_decision_id,
            call_id=call_id,
        )
        return context

    def _accept_item(self, raw, *, entry, index, call_id):
        seed = not self.any_settled
        if seed and self.tasks:
            raise self._seed_error()
        if entry == LEGACY_DISPATCH:
            spec = legacy_spec(raw, index=index, discovered=self.session.native.tools)
        else:
            spec = build_spec(raw, entry=entry, seed=seed, discovered=self.session.native.tools)
        return self._register(spec, raw, seed=seed, entry=entry, call_id=call_id)

    def _call_items(self, call):
        raw = call.get("raw_args") if call.get("invalid") else call.get("args")
        if call.get("name") != LEGACY_DISPATCH:
            return [(0, raw)]
        if not isinstance(raw, dict):
            return [(0, raw)]
        tasks = raw.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise DispatchRejection(
                "schema_error",
                "tasks must be a non-empty array of short task objects",
                path="tasks",
                fix="Submit one dispatch call per task, or a non-empty tasks array.",
            )
        return list(enumerate(tasks))

    def accept(self, raw_tasks):
        """Archived batch entry: accept raw full-form tasks in submission order."""
        accepted, rejected = [], []
        self.reserved = set()
        for index, raw in enumerate(raw_tasks):
            try:
                context = self._accept_item(
                    raw, entry=LEGACY_DISPATCH, index=index, call_id=None
                )
                accepted.append(context)
                self.stats["accepted"] += 1
            except DispatchRejection as exc:
                item = rejection_item(index, exc)
                rejected.append(item)
                self.stats["rejected"] += 1
                self.outputs.event("task_rejected", "planner", item)
            except (ValueError, TypeError) as exc:
                item = rejection_item(
                    index,
                    classify_rejection(
                        exc, discovered=self.session.native.tools, entry=LEGACY_DISPATCH
                    ),
                )
                rejected.append(item)
                self.stats["rejected"] += 1
                self.outputs.event("task_rejected", "planner", item)
        return accepted, rejected

    def accept_calls(self, calls):
        """Accept/reject every dispatch call in wire order; pair each by its own call id."""
        accepted_all, rejected_all = [], []
        self.reserved = set()
        for call in calls:
            name = call.get("name")
            entry = name if name in self.entries else LEGACY_DISPATCH if name == LEGACY_DISPATCH else None
            if entry is None:
                item = rejection_item(
                    0,
                    DispatchRejection(
                        "unknown_entry",
                        f"{name} is not an available dispatch entry in this run",
                        allowed_entry=sorted(self.entries),
                        fix="Use the dispatch entry named in the Planner tool list.",
                    ),
                )
                rejected_all.append(item)
                self.stats["rejected"] += 1
                self.outputs.event("task_rejected", "planner", item, call_id=call.get("id"))
                continue
            self.stats["calls"] += 1
            try:
                items = (
                    [(0, unparsed_call_rejection(
                        call.get("raw_args"), entry=entry,
                        discovered=self.session.native.tools,
                    ))]
                    if call.get("invalid")
                    else self._call_items(call)
                )
            except DispatchRejection as exc:
                items = [(0, exc)]
            accepted, rejected = [], []
            for index, raw in items:
                try:
                    if isinstance(raw, DispatchRejection):
                        raise raw
                    context = self._accept_item(raw, entry=entry, index=index, call_id=call.get("id"))
                    accepted.append(context)
                    self.stats["accepted"] += 1
                except DispatchRejection as exc:
                    item = rejection_item(index, exc)
                    rejected.append(item)
                    self.stats["rejected"] += 1
                    self.outputs.event("task_rejected", "planner", item, call_id=call.get("id"))
                except (ValueError, TypeError) as exc:
                    item = rejection_item(
                        index,
                        classify_rejection(
                            exc, discovered=self.session.native.tools, entry=entry
                        ),
                    )
                    rejected.append(item)
                    self.stats["rejected"] += 1
                    self.outputs.event("task_rejected", "planner", item, call_id=call.get("id"))
            accepted_all.extend(accepted)
            rejected_all.extend(rejected)
            self._pair(call, self._receipt(call, accepted, rejected))
        return accepted_all, rejected_all

    def _accepted_view(self, context):
        spec = context.spec
        return {
            "task_id": context.task_id,
            "conversation_id": context.conversation_id,
            "source": spec.routing_family,
            "window": spec.window.model_dump() if spec.window else None,
            "effective_tools": sorted(spec.effective_tools(self.session.native.tools)),
            "submission_index": context.submission_index,
            "seed": context.seed,
        }

    def _receipt(self, call, accepted, rejected):
        payload = {
            "call": call.get("name"),
            "status": "accepted" if accepted and not rejected else "partial" if accepted else "rejected",
            "accepted": [self._accepted_view(context) for context in accepted],
            "rejected": rejected,
        }
        if rejected and not accepted:
            payload["detail"] = (
                "No remote MCP call was made for any rejected item. "
                "Fix each listed field/path and resubmit only the failed item."
            )
        return payload

    def _pair(self, call, payload):
        self.planner.conversation.messages.append(
            ToolMessage(
                json.dumps(payload, ensure_ascii=False),
                tool_call_id=call["id"],
                name=call.get("name"),
            )
        )
        self.outputs.event("control_result", "planner", payload, call_id=call["id"])

    async def run_task(self, context):
        runner = None
        text, reason, status, errors = "", "service_error", "failed", []
        try:
            async with self.session.worker_semaphore:
                self.session.check_active()
                context.status = "running"
                runner = self.worker_runners.get(context.conversation_id)
                if runner:
                    runner.execution = context
                    runner.last_public_text = ""
                    runner.guide("continuing-investigation")
                else:
                    runner = self.session.runner("worker", execution=context)
                    self.worker_runners[context.conversation_id] = runner
                runner.planner_decision_id = self.planner.planner_decision_id
                prior = next(
                    (
                        r
                        for r in self.outputs.task_results
                        if r["task_id"] == context.spec.continue_task_id
                    ),
                    None,
                )
                runner.instruction(
                    "本次小任务："
                    + json.dumps(
                        {
                            "task_id": context.task_id,
                            **context.spec.model_dump(),
                            "seed": context.seed,
                            "context_cards": source_cards(
                                self.outputs, context.spec.context_source_ids
                            ),
                            "previous_stage": stage_handoff(self.outputs, prior) if prior else None,
                            "effective_tools": sorted(context.spec.effective_tools(self.session.native.tools)),
                            "source_family": context.spec.routing_family,
                            "window_kind": context.window_kind,
                            "main_window": self.window.card() if self.window else None,
                            "prepared_catalog": catalog_cards(self.outputs, context.spec),
                        },
                        ensure_ascii=False,
                    )
                )
                runner.instruction("只完成本小问题；明确本任务能做的能力、实际结果与需Planner另派的缺口。"
                    "first_result首批目标调用结算即交回，失败后的调整留给Planner；纯元数据任务不要求业务值。")
                runner.outputs.event(
                    "task_started", "worker", {"tools": list(runner.definitions),
                        "dispatch_entry": getattr(context, "dispatch_entry", None),
                        "dispatch_call_id": getattr(context, "dispatch_call_id", None),
                        "active_workers": sum(t.status in {'running','summarizing'} for t in self.tasks.values())}
                )
                self.session.progress(
                    {"role": "worker", "task_id": context.task_id, "event": "started"}
                )
                text, reason = await runner.work()
                status = "failed" if reason == "protocol_error" else "returned"
        except asyncio.CancelledError:
            status, reason = (
                "cancelled",
                self.stop_reason(),
            )
            raise
        except PhaseExpired as exc:
            status, reason = "cancelled", exc.reason
            raise
        except TimeoutError as exc:
            interrupted = interruption_reason(
                self.session.cancellation, self.session.deadline
            )
            if interrupted:
                status, reason = "cancelled", interrupted
                raise
            errors.append(self.outputs.redact(str(exc) or type(exc).__name__))
        except Exception as exc:
            errors.append(self.outputs.redact(str(exc)))
            if self.session.native.auth_failed or getattr(exc, "status_code", None) in (
                401,
                403,
            ):
                self.session.native.auth_failed = True
                raise
        finally:
            context.status = status
            text = text or (runner.last_public_text if runner else "")
            result = save_task(
                self.outputs,
                context,
                text,
                status=status,
                reason=reason,
                errors=errors,
                planner_decision_id=self.planner.planner_decision_id,
            )
        return result

    async def run_batch(self, accepted):
        for context in accepted:
            self.outputs.event('task_queued', 'worker', task_id=context.task_id,
                               conversation_id=context.conversation_id)
        jobs = [asyncio.create_task(self.run_task(context)) for context in accepted]
        try:
            results = await controlled(
                asyncio.gather(*jobs, return_exceptions=True),
                self.session.cancellation,
                self.session.deadline,
                budget=self.session.budget,
            )
        finally:
            # All children must settle before run owner exits the anyio MCP scope.
            for job in jobs:
                if not job.done():
                    job.cancel()
            if jobs:
                _, pending = await asyncio.wait(jobs, timeout=min(3, self.session.budget.remaining("delivery")) if self.session.budget else 3)
                if pending:
                    self.outputs.errors.append("Worker cancellation cleanup unconfirmed")
                    for context, job in zip(accepted, jobs):
                        if job in pending:
                            context.status = "cancelled"
                            save_task(self.outputs, context, "", status="cancelled", reason=self.stop_reason(),
                                      errors=["Worker cancellation cleanup unconfirmed"],
                                      planner_decision_id=self.planner.planner_decision_id)
                            context.revoked = True
                            self.outputs.event("task_cleanup_unconfirmed", "system", {"task_id": context.task_id})
                            job.cancel()
            for context in accepted:
                if context.status == "queued":
                    context.status = "cancelled"
                    save_task(
                        self.outputs,
                        context,
                        "",
                        status="cancelled",
                        reason=self.stop_reason(),
                        planner_decision_id=self.planner.planner_decision_id,
                    )
        self.session.check_active()
        if self.session.native.profile in {'business','integrated'}:
            self.window = self.window or self.session.native.resolved_window
        for context, result in zip(accepted, results):
            if isinstance(result, BaseException):
                raise result
            self.seen[context.spec.signature()] = self.version()
        self.any_settled = True
        if self.outputs.sources and 'search_materials' not in self.planner.definitions:
            self.planner.definitions['search_materials'] = self.session.local_definitions['search_materials']
            self.planner.refresh_binding()

    def _closeout_conflict(self, call):
        error = DispatchRejection(
            "closeout_conflict",
            "close_investigation cannot be combined with new evidence calls in the same response",
            fix="Let the accepted evidence calls settle, then decide in the next response.",
        )
        self._pair(
            call,
            {
                "call": CLOSEOUT_TOOL,
                "status": "rejected",
                "rejected": [rejection_item(0, error)],
                "detail": "Accepted evidence calls in this response still run and are archived.",
            },
        )

    async def control(self, calls):
        """Pair every call in wire order; evidence and closeout keep their own rules."""
        accepted, rejected, dispatched = [], [], False
        handled = False
        names = [call.get("name") for call in calls]
        evidence_names = set(self.entries) | {LEGACY_DISPATCH, DIRECT_TOOL, "read_material"}
        has_evidence = any(name in evidence_names for name in names)
        closeout_seen = False
        for call in calls:
            name = call.get("name")
            if name == CLOSEOUT_TOOL:
                handled = True
                if has_evidence:
                    self._closeout_conflict(call)
                elif self.closeout_started or closeout_seen:
                    error = DispatchRejection(
                        "closeout_already_started",
                        "Only one dedicated closeout round is allowed per investigation",
                        fix="Answer from the archived facts instead of requesting another closeout.",
                    )
                    self._pair(
                        call,
                        {
                            "call": CLOSEOUT_TOOL,
                            "status": "rejected",
                            "rejected": [rejection_item(0, error)],
                        },
                    )
                else:
                    closeout_seen = True
                    self.closeout_requested = True
                    self._pair(
                        call,
                        {
                            "call": CLOSEOUT_TOOL,
                            "status": "accepted",
                            "detail": "Closeout accepted; no further tools will run.",
                        },
                    )
                continue
            if name == DIRECT_TOOL:
                dispatched = True
                handled = True
                if self.direct is None:
                    self._pair(
                        call,
                        {
                            "call": DIRECT_TOOL,
                            "status": "rejected",
                            "rejected": [
                                rejection_item(
                                    0,
                                    DispatchRejection(
                                        "direct_unavailable",
                                        "The directed business read is unavailable in this run",
                                        fix="Use the existing dispatch entries.",
                                    ),
                                )
                            ],
                        },
                    )
                else:
                    self._pair(call, await self.direct.run(call))
                continue
            is_dispatch = name in self.entries or name == LEGACY_DISPATCH
            if not is_dispatch:
                if not dispatched:
                    await execute_batch([call], self.planner)
                else:
                    self._pair(
                        call,
                        {
                            "error": "Local reads after a dispatch are not executed; read materials in a separate response before dispatching",
                            "source": "rcagent",
                        },
                    )
                continue
            dispatched = True
            batch_accepted, batch_rejected = self.accept_calls([call])
            accepted.extend(batch_accepted)
            rejected.extend(batch_rejected)
        if accepted:
            await self.run_batch(accepted)
        return dispatched, bool(accepted) or handled, rejected

    def should_closeout_after_batch(self):
        """Once evidence exists and one more investigation round cannot finish, close out."""
        if self.closeout_started or not self.outputs.sources:
            return False
        if not any(source.get("status") == "ok" for source in self.outputs.sources.values()):
            return False
        if self.session.budget is None:
            return False
        return (
            self.session.budget.remaining("investigation")
            < settings.CLOSEOUT_MIN_INVESTIGATION_MARGIN
        )

    async def closeout(self, reason, *, trigger):
        """The single dedicated generation round; normal answer, deadline and fallback share it."""
        if self.closeout_started:
            return
        self.closeout_started = True
        self.session.closing = True
        budget = self.session.budget
        self.outputs.closeout_rounds += 1
        self.outputs.closeout_trigger = trigger
        self.outputs.event(
            "closeout_started",
            "planner",
            {
                "reason": str(reason),
                "trigger": trigger,
                "elapsed_seconds": budget.clock() - budget.started if budget else None,
                "investigation_remaining": budget.remaining("investigation") if budget else None,
                "generation_remaining": budget.remaining("generation") if budget else None,
                "min_investigation_margin": settings.CLOSEOUT_MIN_INVESTIGATION_MARGIN,
                "sources": len(self.outputs.sources),
                "tasks": len(self.outputs.task_results),
            },
        )
        if trigger == "planner":
            self.outputs.record_stop("planner_closeout")
        elif trigger == "program":
            self.outputs.record_stop("investigation_margin")
        if budget:
            budget.check("generation")
        instruction = f"现在停止取证（{reason}）。" + CLOSEOUT_INSTRUCTION
        text, status = await self.planner.one_text(
            instruction, messages=self.planner.closeout_messages(instruction)
        )
        self.outputs.closeout_status = status
        self.outputs.event(
            "closeout_finished",
            "planner",
            {"status": status, "chars": len(text or ""), "trigger": trigger},
        )
        if status == "completed":
            self.outputs.save_draft(text, **self.planner.ids)
        elif text:
            self.outputs.save("closeout-partial.md", text)

    async def draft(self, reason):
        self.planner.outputs.event(
            "plan_degraded", "planner", {"error": str(reason), "action": "save_draft"}
        )
        await self.closeout(reason, trigger="fallback")

    def _block(self, rejected, reason):
        if self.tasks:
            return
        self.stats["blocked"] = True
        self.outputs.record_stop("dispatch_blocked")
        self.outputs.event(
            "dispatch_blocked",
            "planner",
            {
                "reason": reason,
                "accepted_tasks": len(self.tasks),
                "executed_tasks": len(self.outputs.task_results),
                "sources_received": len(self.outputs.sources),
                "rejected_items": self.stats["rejected"],
                "corrections_used": self.corrections,
                "correction_limit": MAX_DISPATCH_CORRECTIONS,
                "blocking_errors": [
                    item.get("error") for item in rejected[:4]
                ],
            },
        )

    async def run(self):
        failures = 0
        while True:
            self.planner.planner_decision_id = "decision_" + uuid.uuid4().hex
            try:
                response = await self.planner.next()
            except PhaseExpired:
                raise
            except Exception as exc:
                self.session.check_active()
                if self.session.native.auth_failed or getattr(
                    exc, "status_code", None
                ) in (401, 403):
                    raise
                self.planner.outputs.event("planner_service_failed", "planner", {"error": str(exc)})
                raise
            calls = calls_from(response) if response else []
            text = content_text(response.content) if response else ""
            finish = (response.response_metadata if response else {}).get(
                "finish_reason"
            )
            if calls and valid_ids(calls):
                self.planner.conversation.messages.append(wire_message(response, calls))
                if finish == "length":
                    rejected = [truncation_item(call, index) for index, call in enumerate(calls)]
                    self.stats["rejected"] += len(rejected)
                    for call, item in zip(calls, rejected):
                        self._pair(
                            call,
                            {
                                "call": call.get("name"),
                                "status": "rejected",
                                "accepted": [],
                                "rejected": [item],
                                "detail": "No remote MCP call was made; the output ended with finish_reason=length.",
                            },
                        )
                    self.outputs.event(
                        "control_error",
                        "planner",
                        {"calls_executed": 0, "finish_reason": finish},
                    )
                    dispatched, accepted_any = True, False
                else:
                    dispatched, accepted_any, rejected = await self.control(calls)
                if self.closeout_requested:
                    await self.closeout("Planner 主动结束取证", trigger="planner")
                    return
                if self.should_closeout_after_batch():
                    await self.closeout(
                        "调查余量不足，无法再发起一轮正常调查", trigger="program"
                    )
                    return
                if not dispatched:
                    # Local original reads are allowed before a dispatch and are not
                    # a failed dispatch attempt; the Planner decides the next move.
                    continue
                if accepted_any:
                    failures, self.corrections, self.last_rejections = 0, 0, None
                    continue
                if dispatched and rejected:
                    signature = rejection_signature(rejected)
                    repeated = signature == self.last_rejections
                    failures += 1
                    exhausted = failures > MAX_DISPATCH_CORRECTIONS
                    if exhausted or repeated:
                        self._block(
                            rejected,
                            (
                                "Two correction requests produced the same rejected call"
                                if repeated
                                else "Dispatch corrections exhausted; no task was accepted"
                            ),
                        )
                        await self.draft(
                            "派工纠正两次后仍无合法任务"
                            if not repeated
                            else "重复提交相同错误、没有新依据"
                        )
                        return
                    self.corrections += 1
                    self.stats["corrections"] = self.corrections
                    self.last_rejections = signature
                    self.planner.instruction(
                        correction_prompt(
                            rejected, MAX_DISPATCH_CORRECTIONS - failures + 1
                        )
                    )
                    continue
                if dispatched:
                    failures += 1
                    if failures > MAX_DISPATCH_CORRECTIONS:
                        self._block(rejected, "Dispatch corrections exhausted; no task was accepted")
                        await self.draft("派工纠正两次后仍无合法任务")
                        return
                    self.corrections += 1
                    self.stats["corrections"] = self.corrections
                    self.planner.instruction(
                        "派工未执行。请提交当前入口的一个合法短任务：question和tools；"
                        "tools只从本入口已列出的工具选择。"
                    )
                    continue
            elif (
                not calls
                and text.strip()
                and finish not in {"length", "content_filter"}
            ):
                self.planner.conversation.messages.append(response)
                if self.tasks or self.outputs.sources:
                    self.outputs.save_draft(text, **self.planner.ids)
                    return
                self.outputs.save("initial-plan.md", text)
            else:
                self.planner.outputs.event(
                    "control_error",
                    "planner",
                    {"calls_executed": 0, "invalid_ids_or_response": True},
                )
            failures += 1
            if failures > MAX_DISPATCH_CORRECTIONS:
                self._block([], "Dispatch corrections exhausted; no task was accepted")
                await self.draft("派工纠正两次后仍无合法任务")
                return
            self.corrections += 1
            self.stats["corrections"] = self.corrections
            self.planner.instruction(
                "派工未执行。请依据上一ToolMessage的具体错误纠正本次派工；"
                "tools从本入口已列出的工具选择。保持原目标、禁查项、窗口和交付物，不换题。"
                "若仍无法执行，请直接说明阻断。"
            )