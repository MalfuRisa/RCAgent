"""One role's model binding, append-only history, guidance and task ownership."""

import time
import uuid
from contextlib import asynccontextmanager

from langchain_core.messages import HumanMessage, messages_to_dict

from config import settings
from context.investigation_conversation import Conversation
from workflow.native_tool_calls import calls_from, execute_batch, valid_ids, wire_message
from workspace.investigation_outputs import public
from workspace.native_tasks import OutputContext
from workspace.stage_facts import program_handoff
from workspace.skill_knowledge import load

from .investigation_transport import (
    capacity_error,
    content_text,
    controlled,
    stream_response,
)
from .role_guidance import BUSINESS_GUIDANCE, knowledge_names, role_text


class RoleRunner:
    def __init__(self, owner, role, *, execution=None, definitions=None):
        self.owner, self.role, self.execution = owner, role, execution
        self.conversation_id = (
            execution.conversation_id
            if execution
            else "conversation_" + uuid.uuid4().hex
        )
        self.planner_decision_id = None
        self.model_round_id = None
        self.local_handlers = owner.local_handlers
        if definitions is None:
            definitions = dict(owner.local_definitions) if role != "review" else {}
            if execution:

                definitions.update(
                    {
                        n: d
                        for n, d in owner.native.tools.items()
                        if n in execution.spec.effective_tools(owner.native.tools)
                    }
                )
        self.definitions = definitions
        self.bound = (
            owner.model.bind_tools(list(definitions.values()), tool_choice="auto")
            if definitions
            else owner.model
        )
        self.conversation = Conversation(
            execution.spec.question if execution else owner.task,
            list(definitions.values()),
            settings.MODEL_CONTEXT_TOKENS,
            settings.MODEL_OUTPUT_RESERVE_TOKENS,
        )
        self.conversation.active_role = role
        self.last_public_text = ""
        self.first_text_at = None
        self.context_provider = None
        self.conversation.messages.append(HumanMessage(role_text(role), additional_kwargs={'guidance':'role:'+role}))
        if execution and execution.spec.routing_family == 'business':
            self.instruction(BUSINESS_GUIDANCE)
        self.outputs.event("role_tools", role, {"tools": list(definitions),
            'source_family':getattr(execution.spec,'routing_family',None) if execution else None})

    def refresh_binding(self):
        self.bound = self.owner.model.bind_tools(list(self.definitions.values()),tool_choice='auto') if self.definitions else self.owner.model
        self.conversation.definitions = list(self.definitions.values())

    @property
    def ids(self):
        return {
            "conversation_id": self.conversation_id,
            "task_id": self.execution.task_id if self.execution else None,
            "model_round_id": self.model_round_id,
            "planner_decision_id": self.planner_decision_id,
        }

    @property
    def outputs(self):
        return OutputContext(self.owner.outputs, **self.ids)

    def guide(self, name):
        profile = getattr(self.execution.spec, 'routing_family', None) if self.execution else self.owner.native.profile
        profile = profile or self.owner.native.profile
        if profile != 'native':
            for knowledge in knowledge_names(profile, self.role):
                self._load_guide(knowledge)
            return
        self._load_guide(name)

    def _load_guide(self, name):
        if name in self.conversation.skill_loaded:
            return
        try:
            content, _ = load(name, "SKILL.md")
            self.conversation.messages.append(
                HumanMessage("项目方法指导（非证据）：\n" + content, additional_kwargs={"guidance": name})
            )
            self.conversation.skill_loaded.add(name)
            self.outputs.event("skill_delivered", self.role, {"name": name})
        except (ValueError, OSError) as exc:
            self.outputs.event(
                "execution_error", self.role, {"skill_unavailable": str(exc)}
            )

    def instruction(self, text):
        self.conversation.messages.append(
            HumanMessage(text, additional_kwargs={"role_task": self.role})
        )

    def visible_messages(self):
        if self.context_provider is None:
            return self.conversation.messages
        evidence = self.context_provider()
        return self.conversation.recent_turns() + [HumanMessage('当前取证与待答项（程序整理，原文按来源读取）：\n' + evidence,
                                                               additional_kwargs={'evidence_view': True})]

    @asynccontextmanager
    async def model_slot(self, role):
        """One actual transport attempt owns the slot; backoff runs after release."""
        self.outputs.event("model_queued", role)
        async with self.owner.model_semaphore:
            self.owner.check_active()
            self.outputs.event("model_slot_acquired", role)
            yield

    async def request(self, *, tools=True, messages=None, request_role=None):
        self.owner.check_active()
        self.last_public_text = ""
        self.first_text_at = None
        if self.execution:
            self.execution.discovered_tools = self.owner.native.tools
        self.model_round_id = "round_" + uuid.uuid4().hex
        role, events = request_role or self.role, self.outputs
        sent = messages if messages is not None else self.visible_messages()
        events.event('role_input', role, {'message_chars':sum(len(str(m.content)) for m in sent),
            'evidence_chars':sum(len(str(m.content)) for m in sent if m.additional_kwargs.get('evidence_view')),
            'message_count':len(sent)})
        model = self.bound if tools else self.owner.model

        async def generate():
            if settings.NATIVE_TRACE_REQUESTS:
                path = events.save(
                    "requests/" + self.model_round_id + ".json",
                    public(
                        {
                            "kind": "public_chatmodel_input_not_http_wire",
                            **self.ids,
                            "messages": messages_to_dict(sent),
                            "tools": list(self.definitions.values())
                            if tools
                            else [],
                        }
                    ),
                )
                events.event("model_request_snapshot", role, {"path": path})
            return await stream_response(
                model,
                sent,
                events,
                role,
                settings.INVESTIGATION_IDLE_TIMEOUT,
                on_partial=lambda text: setattr(self, "last_public_text", text),
                on_first_text=lambda: setattr(self, "first_text_at", time.monotonic()),
                budget=self.owner.budget,
                phase="generation" if self.owner.closing else "investigation",
                slot=lambda: self.model_slot(role),
            )

        response = await controlled(
            generate(), self.owner.cancellation, self.owner.deadline, budget=self.owner.budget, phase="generation" if self.owner.closing else "investigation"
        )
        if response is not None:
            self.last_public_text = content_text(response.content)
            events.event(
                "model_response",
                role,
                {
                    "text": self.last_public_text,
                    "tool_calls": response.tool_calls,
                    "invalid_tool_calls": response.invalid_tool_calls,
                    "response_metadata": response.response_metadata,
                },
                message_id=response.id,
            )
            if response.usage_metadata:
                self.owner.outputs.usage.append(
                    {"role": role, **self.ids, **response.usage_metadata}
                )
                events.event("usage", role, response.usage_metadata)
                self.conversation.last_input_tokens = response.usage_metadata.get(
                    "input_tokens", 0
                )
        return response

    async def summarize(self, messages):
        self.guide("continuing-investigation")
        response = await self.request(
            tools=False, messages=messages, request_role="compaction"
        )
        return content_text(response.content) if response else ""

    async def next(self, *, tools=True):
        visible = self.visible_messages()
        if not self.owner.closing and self.conversation.needs_compaction(self.owner.model, visible):
            await self.conversation.compact(self.summarize, self.outputs)
            visible = self.visible_messages()
        try:
            return await self.request(tools=tools, messages=visible)
        except Exception as exc:
            if not self.owner.closing and capacity_error(exc) and await self.conversation.compact(
                self.summarize, self.outputs
            ):
                return await self.request(tools=tools)
            raise

    def closeout_messages(self, instruction, material=None, *, context=None):
        """Bounded synthesis view: original task plus compact verified material."""
        messages = list(self.conversation.messages[:2])
        guidance = next(
            (
                message
                for message in self.conversation.messages
                if getattr(message, "additional_kwargs", {}).get("guidance")
                == "role:" + self.role
            ),
            None,
        )
        if guidance is not None:
            messages.append(guidance)
        messages.append(
            HumanMessage(instruction, additional_kwargs={"role_task": self.role})
        )
        if material is not None:
            messages.append(
                HumanMessage(
                    material.render(),
                    additional_kwargs={"closeout_material": True},
                )
            )
        if context is None and material is None and self.context_provider is not None:
            context = self.context_provider()
        if context:
            messages.append(
                HumanMessage(
                    "当前取证与待答项（程序整理，原文按来源读取）：\n"
                    + context,
                    additional_kwargs={"evidence_view": True},
                )
            )
        return messages

    async def one_text(self, instruction, *, messages=None):
        if messages is None:
            self.instruction(instruction)
            response = await self.next(tools=False)
        else:
            response = await self.request(tools=False, messages=messages)
        text = content_text(response.content) if response else ""
        finish = (response.response_metadata if response else {}).get("finish_reason")
        calls = calls_from(response) if response else []
        if response:
            self.conversation.messages.append(response)
        if calls or finish in {"length", "content_filter"} or not text.strip():
            self.outputs.event(
                "protocol_error",
                self.role,
                {
                    "no_tools": True,
                    "calls_executed": 0,
                    "finish_reason": finish,
                    "has_tool_calls": bool(calls),
                },
            )
            return text, "partial" if text else "unavailable"
        return text, "completed"

    async def work(self):
        self.guide("submitting-observations")
        empty = 0
        while True:
            response = await self.next()
            calls = calls_from(response) if response else []
            text = content_text(response.content) if response else ""
            finish = (response.response_metadata if response else {}).get(
                "finish_reason"
            )
            if calls:
                if finish == "length" or not valid_ids(calls):
                    self.outputs.event(
                        "protocol_error",
                        self.role,
                        {"invalid_ids": True, "calls_executed": 0},
                    )
                    return text, "protocol_error"
                self.conversation.messages.append(wire_message(response, calls))
                settled = await execute_batch(calls, self)
                self.owner.progress(
                    {
                        "role": self.role,
                        "task_id": self.execution.task_id,
                        "event": "tools_settled",
                        "count": len(calls),
                    }
                )
                if self.execution.scope_changed or (
                    self.has_target_result(settled)
                    and self.execution.spec.return_mode == "first_result"
                ):
                    reason = (
                        "scope_change_required"
                        if self.execution.scope_changed
                        else "first_result_settled"
                    )
                    self.execution.status = "summarizing"
                    self.outputs.event(
                        "task_forced_return", self.role, {"reason": reason}
                    )
                    facts, handoff_reason = program_handoff(self.owner.outputs, self.execution)
                    self.execution.handoff_facts = facts
                    self.execution.handoff_reason = handoff_reason
                    self.outputs.event('task_handoff', self.role, {
                        'mode': 'facts' if facts else 'worker_note', 'reason': handoff_reason})
                    if facts:
                        return '程序已整理该阶段的原值与缺口，详见阶段事实。', reason
                    self.owner.outputs.worker_summary_rounds += 1
                    self.outputs.event(
                        "worker_summary_round",
                        self.role,
                        {"reason": handoff_reason, "tools": False},
                    )
                    answer, status = await self.one_text(
                        "本阶段已结算，停止取证。无工具总结本任务的实际事实、source_id、错误与缺口，交回Planner；不要写全面报告。"
                    )
                    return answer, reason if status == "completed" else "protocol_error"
                continue
            if response:
                self.conversation.messages.append(response)
            if text.strip() and finish not in {"length", "content_filter"}:
                self.execution.handoff_reason = 'answer_uses_worker_note' if self.execution.spec.return_mode == 'answer' else 'worker_note_without_target_receipt'
                self.owner.outputs.worker_summary_rounds += 1
                self.outputs.event(
                    "worker_summary_round",
                    self.role,
                    {"reason": self.execution.handoff_reason, "tools": False},
                )
                return text, "model_returned"
            empty += 1
            if empty > 1 or finish in {"length", "content_filter"}:
                return text, "protocol_error"
            self.instruction("请围绕当前小任务继续，或交回实际缺口。")

    def has_target_result(self, settled):
        targets = self.execution.spec.target_tools.intersection(self.definitions)
        return any(result and result['tool'] in targets and not (
            result['tool'] == 'insights_generate_dashboard' and
            isinstance(result.get('arguments'), dict) and result['arguments'].get('render') is False
        ) for result in settled)
