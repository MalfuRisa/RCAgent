"""RCAgent public run events, durable answers and raw MCP sources."""

import json
import time
import uuid
from pathlib import Path

from .native_materials import SourceMaterials, page_counts
from .business_materials import observation
from .query_context import query_context


def public(value):
    if isinstance(value, dict):
        return {
            k: public(v)
            for k, v in value.items()
            if k
            not in {
                "reasoning_content",
                "reasoningText",
                "reasoning",
                "apiKey",
                "api_key",
                "Authorization",
            }
        }
    if isinstance(value, list):
        return [
            public(v)
            for v in value
            if not (
                isinstance(v, dict)
                and v.get("type")
                in {"reasoning", "reasoning_content", "thinking", "redacted_thinking"}
            )
        ]
    return value


class RunOutputs:
    def __init__(self, root, run_id):
        self.root, self.run_id = Path(root), run_id
        self.root.mkdir(parents=True, exist_ok=False)
        (self.root / "sources").mkdir()
        self.conversation_id = "conversation_" + uuid.uuid4().hex
        self.started = time.monotonic()
        self.sources, self.texts, self.paths = {}, {}, {}
        self.errors, self.usage, self.secrets = [], [], []
        self.sequence = 0
        self.materials = SourceMaterials(self)
        self.review_status = "not_started"
        self.control_events = []
        self.task_results = []
        self.draft_producer = None
        self.partial_body = ""
        self.instruction = ""
        self.stop_reason = None
        self.stop_reasons = []
        self.sealed = False
        self.preparation_cache = {}
        self.direct_queries = 0
        self.model_attempts = 0
        self.model_backoffs = []
        self.worker_summary_rounds = 0
        self.closeout_rounds = 0
        self.closeout_status = None
        self.closeout_trigger = None

    def record_stop(self, reason):
        if self.sealed:
            return
        self.stop_reason = self.stop_reason or reason
        if reason not in self.stop_reasons:
            self.stop_reasons.append(reason)
            self.event("stop_observed", "system", {"reason": reason, "first_reason": self.stop_reason})

    def redact(self, text):
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def save(self, name, value):
        if self.sealed:
            return None
        try:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            text = (
                value
                if isinstance(value, str)
                else json.dumps(value, ensure_ascii=False, indent=2, default=str)
            )
            path.write_text(self.redact(text), encoding="utf-8")
            return str(path)
        except OSError as exc:
            self.errors.append("Archive write failed: " + name + ": " + str(exc))
            return None

    def event(self, kind, role, payload=None, **ids):
        if self.sealed:
            return None
        self.sequence += 1
        event = {
            "event_id": uuid.uuid4().hex,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "role": role,
            "timestamp": time.time(),
            "type": kind,
            "elapsed_seconds": time.monotonic() - self.started,
            "payload": public(payload or {}),
        }
        event.update(ids)
        if kind in {"control_result", "task_rejected", "plan_degraded", "control_error"}:
            self.control_events.append(event)
        try:
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    self.redact(json.dumps(event, ensure_ascii=False, default=str))
                    + "\n"
                )
        except OSError as exc:
            self.errors.append("Event write failed: " + str(exc))
        return event

    def save_answer(self, role, text, **ids):
        if self.sealed:
            return
        self.texts[role] = text
        name = "plan.md" if role == "planner" else role + "-answer.md"
        self.paths[role] = self.save(name, text)
        self.event(
            "answer_saved", role, {"path": self.paths[role], "text": text}, **ids
        )

    def answer(self, role):
        return self.texts.get(role, "")

    def tool_result(self, outcome):
        if self.sealed:
            return json.dumps({"status": "cancelled", "source_id": None, "error": "Run already delivered"})
        if outcome.get('cache_hit'):
            sid = outcome['original_source_id']
            self.event('preparation_cache_hit',outcome['role'],{'tool':outcome['tool'],
                'original_source_id':sid,'mcp_call_started':False},call_id=outcome['call_id'])
            view = json.loads(self.materials.model_view(sid,self.materials.raw[sid]))
            view['preparation_reuse'] = {'same_run':True,'original_source_id':sid,
                'observed_at':self.sources[sid]['finished_at'],'not_a_fresh_health_observation':True}
            return json.dumps(view,ensure_ascii=False)
        source_id = "source_" + uuid.uuid4().hex
        raw = outcome.get("raw_result")
        path = self.materials.store(
            source_id,
            raw if raw is not None else {"error": outcome.get("error")},
            outcome["tool"],
        )
        source = {
            "source_id": source_id,
            "call_id": outcome["call_id"],
            "role": outcome["role"],
            **{
                k: outcome.get(k)
                for k in (
                    "task_id",
                    "conversation_id",
                    "model_round_id",
                    "planner_decision_id",
                )
            },
            "tool_name": outcome["tool"],
            "arguments": outcome.get("arguments"),
            "started_at": outcome["started_at"],
            "finished_at": outcome["finished_at"],
            "status": outcome["status"],
            "raw_path": path,
            "request_scope": outcome.get("arguments"),
            "association": "unattributed", "error": outcome.get("error"),
            'mcp_source':outcome.get('mcp_source'), 'source_context':outcome.get('source_context'),
            'query_context':query_context(outcome),
            **observation(outcome['tool'], raw or {}),
            "mcp_call_started": outcome.get("mcp_call_started", False),
            **page_counts(raw or {}, outcome["tool"]),
        }
        self.sources[source_id] = source
        if outcome['tool'] in {'insights_status','insights_catalog'} and outcome['status']=='ok':
            key=(outcome.get('mcp_source'),outcome['tool'],json.dumps(outcome.get('arguments'),sort_keys=True,ensure_ascii=False))
            self.preparation_cache[key] = source_id
        target_data=((outcome.get('raw_result') or {}).get('structuredContent') or {}).get('data') or {}
        nonempty_fact=isinstance(target_data,dict) and any(target_data.get(k) for k in ('paths','pod','node','cluster'))
        if outcome['status']=='ok' and (source.get('business_value_count',0)>0 or nonempty_fact):
            if not getattr(self,'first_fact_source',None):
                self.first_fact_source=source_id
                self.event('first_target_fact',outcome['role'],{'tool':outcome['tool'],'meaning':'Target result received; path/resource is useful without numeric metric'},source_id=source_id)
        if source.get('business_value_count', 0) > 0 and outcome['status'] == 'ok' and not getattr(self, 'first_business_source', None):
            self.first_business_source = source_id
            self.event('first_business_value', outcome['role'], {'business_value_count': source['business_value_count'],
                'resolved_time': source['resolved_time'], 'meaning': 'First non-null original business statistic; zero is valid'}, source_id=source_id)
        self.event(
            "tool_result",
            outcome["role"],
            {
                "status": outcome["status"],
                "error": outcome.get("error"),
                "raw_path": path,
                **page_counts(raw or {}, outcome["tool"]),
            },
            call_id=outcome["call_id"],
            source_id=source_id,
            **{
                k: outcome[k]
                for k in (
                    "task_id",
                    "conversation_id",
                    "model_round_id",
                    "planner_decision_id",
                )
                if outcome.get(k)
            },
        )
        result = (
            self.materials.model_view(source_id, raw)
            if raw is not None
            else json.dumps(
                {
                    "source_id": source_id,
                    "status": outcome["status"],
                    "error": outcome.get("error"),
                },
                ensure_ascii=False,
            )
        )
        if raw is not None:
            view = json.loads(result)
            view.update(status=outcome["status"], error=outcome.get("error"))
            result = json.dumps(view, ensure_ascii=False)
        from .native_checks import source_card

        view = json.loads(result)
        view["scope_card"] = {k:v for k,v in source_card(source, raw).items() if k != 'original_values_preview'}
        if outcome.get("rcagent_hint"):
            view["rcagent_hint"] = outcome["rcagent_hint"]
        result = json.dumps(view, ensure_ascii=False)
        return result

    def save_draft(self, text, **ids):
        if self.sealed:
            return
        self.draft_producer = "planner_synthesis"
        self.paths["draft"] = self.save("draft-answer.md", text)
        self.save_answer("worker", text, producer="planner_synthesis", **ids)
        self.event(
            "draft_saved",
            "planner",
            {"path": self.paths["draft"], "draft_producer": self.draft_producer},
            **ids,
        )

    def count_summary(self):
        return [
            {
                k: s.get(k)
                for k in (
                    "source_id",
                    "tool_name",
                    "arguments",
                    "returned_rows",
                    "backend_total",
                    "pagination",
                )
            }
            for s in self.sources.values()
            if s.get("returned_rows") is not None
        ]

    def checkpoint(self):
        self.save("sources.json", list(self.sources.values()))
        self.save("usage.json", self.usage)
        self.save("tasks.json", self.task_results)
        self.save("archive-status.json", {"errors": self.errors})

    def publish(self, status="completed", limitation=None):
        worker, review = self.answer("worker"), self.answer("review")
        body = review or worker or self.partial_body
        from .published_evidence import checked_history, evidence_text
        checks = getattr(self, 'checked_evidence', None)
        evidence = checks['history_evidence'] if checks is not None else checked_history(self)
        invalid_refs = [ref['source_id'] for ref in (checks or {}).get('references', [])
                        if not ref['exists_in_run'] or not ref['original_saved']]
        program_text = evidence_text(evidence, invalid_refs)
        limitations = list(self.errors) + ([limitation] if limitation else [])
        if worker and not review and self.review_status not in {"program_checked", "focused_completed"}:
            limitations.append("复核未完成；保留调查正文，其中陈述尚未全部核验。")
        dispatch = dict(getattr(self, "dispatch_stats", None) or {})
        direct_queries = getattr(self, "direct_queries", 0)
        dispatch_status = (
            "blocked"
            if dispatch.get("blocked")
            else "direct"
            if direct_queries and not self.task_results
            else "accepted"
            if self.task_results
            else "none"
        )
        counts = {
            "tasks_accepted": dispatch.get("accepted", len(self.task_results)),
            "tasks_executed": len(self.task_results),
            "sources_received": len(self.sources),
            "dispatch_calls": dispatch.get("calls", 0),
            "rejected_items": dispatch.get("rejected", 0),
            "corrections_used": dispatch.get("corrections", 0),
            "worker_model_rounds": sum(
                1 for item in self.usage if item.get("role") == "worker"
            ),
            "planner_model_rounds": sum(
                1 for item in self.usage if item.get("role") == "planner"
            ),
            "model_rounds": len(
                {item.get("model_round_id") for item in self.usage if item.get("model_round_id")}
            ),
            "transport_attempts": getattr(self, "model_attempts", 0),
            "backoff_seconds": sum(getattr(self, "model_backoffs", [])),
            "direct_queries": direct_queries,
            "worker_summary_rounds": getattr(self, "worker_summary_rounds", 0),
            "fact_handoffs": sum(
                1 for item in self.task_results if item.get("handoff_mode") == "facts"
            ),
            "worker_note_handoffs": sum(
                1 for item in self.task_results if item.get("handoff_mode") != "facts"
            ),
            "closeout_rounds": getattr(self, "closeout_rounds", 0),
            "closeout_status": getattr(self, "closeout_status", None),
            "closeout_trigger": getattr(self, "closeout_trigger", None),
            "zero_worker": (not self.task_results) and direct_queries > 0,
        }
        body = body or "未取得完整模型正文；工具材料和执行错误已保留，调查尚未完成。"
        if dispatch_status == "blocked":
            body = (
                "取证阻断（dispatch_blocked）：本 run 没有合法任务被接受，实际执行任务 "
                + str(counts["tasks_executed"])
                + " 个，收到来源 "
                + str(counts["sources_received"])
                + " 个。以下为按已返回错误整理的说明，不是业务结论。\n\n"
                + body
            )
        report = {
            "runtime": "rcagent",
            "run_id": self.run_id,
            "conversation_id": self.conversation_id,
            "execution_status": status,
            "answer_source": "model"
            if review or worker
            else "program_assembled_model_partials"
            if body
            else "program_partial",
            "answer_status": "completed"
            if (review or worker) and status == "completed"
            else "partial"
            if body
            else "none",
            "review_status": self.review_status,
            "review_mode": getattr(self, "review_mode", "program"),
            "stop_reason": getattr(self, "stop_reason", None),
            "stop_reasons": self.stop_reasons,
            "dispatch_status": dispatch_status,
            "execution_counts": counts,
            "dispatch": dispatch,
            "budget_limited": any(r.startswith("budget_") or r == "deadline" for r in self.stop_reasons),
            "experiment_goal_status": "not_evaluated",
            "execution_receipts": self.control_events,
            "evidence_status": "unchecked",
            "program_history_evidence": evidence,
            "root_cause_status": "unresolved",
            "body": body,
            "worker_answer_path": self.paths.get("worker"),
            "review_answer_path": self.paths.get("review"),
            "draft_answer_path": self.paths.get("draft"),
            "draft_producer": self.draft_producer,
            "body_producer": "supervisor_revision"
            if review
            else self.draft_producer or "program_assembled_model_partials",
            "task_results": self.task_results,
            "review_notes": [
                {
                    "status": self.review_status,
                    "instruction": "数量、归属、窗口、单位和推断边界复核",
                    "response_path": self.paths.get("review"),
                }
            ],
            "sources": list(self.sources.values()),
            "limitations": limitations,
            "usage_if_available": self.usage,
            "elapsed_seconds": time.monotonic() - self.started,
        }
        self.checkpoint()
        if getattr(self, "review_note", None):
            report["body"] += "\n\n复核提示（未自动改写前文）：\n" + self.review_note
        if program_text:
            # The default CLI/user body stays one paragraph; details remain readable.
            self.save("evidence-summary.md", program_text)
        self.save("result.json", report)
        self.save(
            "report.md",
            report["body"]
            + "\n\n## 执行记录\n复核状态："
            + self.review_status
            + "\n派工状态："
            + dispatch_status
            + "；接受任务 "
            + str(counts["tasks_accepted"])
            + "（执行 "
            + str(counts["tasks_executed"])
            + "）· 来源 "
            + str(counts["sources_received"])
            + " · 派工调用 "
            + str(counts["dispatch_calls"])
            + " · 拒绝项 "
            + str(counts["rejected_items"])
            + " · 纠正 "
            + str(counts["corrections_used"])
            + " · 直查 "
            + str(counts["direct_queries"])
            + " · 事实交接 "
            + str(counts["fact_handoffs"])
            + " · 成稿 "
            + str(counts["closeout_rounds"])
            + "（"
            + str(counts["closeout_status"])
            + "，"
            + str(counts["closeout_trigger"])
            + "）"
            + "\n"
            + "\n".join("- " + x for x in limitations)
            + "\n\n[来源索引](sources.json) · [证据摘要](evidence-summary.md)\n",
        )
        for error in self.errors:
            if error not in limitations:
                limitations.append(error)
        return report
