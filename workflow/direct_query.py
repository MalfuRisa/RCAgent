"""One controlled Planner business read; the program fills the confirmed scope."""

import json
import time
import uuid

from agents.investigation_tools import definition
from models.dispatch_tasks import DispatchRejection, rejection_item
from models.native_tasks import TaskSpec
from platforms.insights_v5.native_task_scope import TaskExecution

DIRECT_TOOL = "query_business_metrics"
DIRECT_MCP_TOOL = "insights_query_metrics"
DIRECT_DATASETS = frozenset({"app-path", "network-path"})


def direct_definition():
    return definition(
        DIRECT_TOOL,
        "One directed business metric read. Use only when the object, client direction, "
        "dataset and metrics are already clear from the user question and one batch can answer "
        "the current sub-question. The program fills the confirmed absolute window and the "
        "client-side filter, and archives the real result as a source. Typical app-path metrics "
        "are server_response_time, percentile95, percentile99 and request_rate. Do not use it "
        "for identity disambiguation, choosing queries from intermediate results, or cross-source joins. "
        "It returns window/path aggregates; for minute series, spike shape or same-time comparisons, "
        "dispatch an insights_metric_history first_result task instead.",
        {
            "question": {
                "type": "string",
                "description": "What this directed read must answer.",
            },
            "object": {
                "type": "string",
                "description": "Client object name exactly as written in the user question.",
            },
            "metrics": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 6,
                "description": "Metrics needed together, for example latency, P95/P99 and request rate.",
            },
            "server": {
                "type": "string",
                "description": "Optional server-side endpoint already confirmed by the question or a returned path list.",
            },
            "dataset_id": {
                "type": "string",
                "enum": sorted(DIRECT_DATASETS),
                "default": "app-path",
                "description": "Path dataset; the pilot is limited to app-path and network-path.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10,
                "default": 10,
                "description": "Returned-row limit; the business contract allows 1..10.",
            },
            "chart_format": {
                "type": "string",
                "enum": ["auto", "none", "echarts", "text"],
                "default": "auto",
            },
        },
        ["question", "object", "metrics"],
    )


def _reject(code, message, *, path=None, fix=None, allowed=None, snippet=None, parsed=None):
    return DispatchRejection(
        code,
        message,
        path=path,
        fix=fix,
        allowed=allowed,
        snippet=snippet,
        parsed=parsed,
    )


class DirectQuery:
    """A run-issued, single-use Planner execution bound to one business tool."""

    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.session = dispatcher.session
        self.outputs = dispatcher.outputs
        self.window = dispatcher.window
        self.question = dispatcher.question or ""
        self.target_resource = dispatcher.target_resource or ""
        self.planner = dispatcher.planner

    @property
    def available(self):
        return (
            self.session.native.profile in {"business", "integrated"}
            and DIRECT_MCP_TOOL in self.session.native.tools
            and self.window is not None
        )

    def confirmed_object(self, value):
        text = value.strip()
        return bool(text) and (
            text in self.question
            or (self.target_resource and text == self.target_resource)
        )

    def confirmed_server(self, value):
        text = value.strip()
        if not text:
            return False
        if text in self.question:
            return True
        for source_id, source in self.outputs.sources.items():
            if source.get("tool_name") != "insights_list_paths":
                continue
            raw = self.outputs.materials.raw.get(source_id) or {}
            data = (raw.get("structuredContent") or {}).get("data") or {}
            for path in data.get("paths") or []:
                server = path.get("server") or {}
                if server.get("name") == text:
                    return True
        return False

    def validate(self, raw):
        if not self.available:
            raise _reject(
                "direct_unavailable",
                "The directed business read needs the business source, insights_query_metrics and a confirmed absolute window",
                fix="Use the existing dispatch entries for this question.",
            )
        if not isinstance(raw, dict):
            raise _reject(
                "arguments_unparsed",
                "Directed read arguments could not be parsed as a JSON object; nothing was sent",
                parsed=False,
                fix='Send an object with question, object and metrics.',
                snippet=str(raw)[:300],
            )
        allowed = {
            "question",
            "object",
            "metrics",
            "server",
            "dataset_id",
            "limit",
            "chart_format",
        }
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise _reject(
                "unknown_field",
                f"{unknown[0]} is not a directed read field",
                path=unknown[0],
                fix="Use only the declared fields; the window and client filter are filled by the program.",
            )
        question = raw.get("question")
        if not isinstance(question, str) or not question.strip():
            raise _reject(
                "schema_error",
                "question must be a non-empty string",
                path="question",
            )
        objective = raw.get("object")
        if not isinstance(objective, str) or not objective.strip():
            raise _reject(
                "schema_error",
                "object must be a non-empty string",
                path="object",
            )
        if not self.confirmed_object(objective):
            raise _reject(
                "unconfirmed_object",
                f"{objective} does not appear verbatim in the original question; a direct read cannot introduce a new target",
                path="object",
                fix="Use the exact client object name from the question, or dispatch a Worker task to resolve identity.",
            )
        server = raw.get("server")
        if server is not None:
            if not isinstance(server, str) or not server.strip():
                raise _reject(
                    "schema_error", "server must be a non-empty string", path="server"
                )
            server = server.strip()
            if server == objective.strip():
                raise _reject(
                    "schema_error",
                    "server must differ from the client object",
                    path="server",
                )
            if not self.confirmed_server(server):
                raise _reject(
                    "unconfirmed_server",
                    f"{server} is not confirmed by the question or a returned path list; a direct read cannot invent the other endpoint",
                    path="server",
                    fix="Use a server name from the question or an insights_list_paths result, or dispatch a Worker task.",
                )
        metrics = raw.get("metrics")
        if (
            not isinstance(metrics, list)
            or not metrics
            or any(not isinstance(item, str) or not item.strip() for item in metrics)
        ):
            raise _reject(
                "schema_error",
                "metrics must be a non-empty array of metric names",
                path="metrics",
            )
        metrics = list(dict.fromkeys(item.strip() for item in metrics))
        dataset = raw.get("dataset_id", "app-path")
        if dataset not in DIRECT_DATASETS:
            raise _reject(
                "unconfirmed_dataset",
                f"{dataset} is outside the directed-read pilot",
                path="dataset_id",
                allowed=sorted(DIRECT_DATASETS),
                fix="Use app-path or network-path, or dispatch a Worker task for another dataset.",
            )
        limit = raw.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 10:
            raise _reject(
                "limit_exceeds_contract",
                "limit must be an integer 1..10 for insights_query_metrics",
                path="limit",
                allowed=list(range(1, 11)),
                fix="Use limit<=10 or omit it; do not raise the business contract limit.",
            )
        chart = raw.get("chart_format", "auto")
        if chart not in {"auto", "none", "echarts", "text"}:
            raise _reject(
                "schema_error",
                "chart_format must be auto, none, echarts or text",
                path="chart_format",
            )
        return {
            "question": question.strip(),
            "object": objective.strip(),
            "server": server,
            "metrics": metrics,
            "dataset_id": dataset,
            "limit": limit,
            "chart_format": chart,
        }

    def arguments(self, item):
        filters = [
            {"field": "auto_service_0", "op": "eq", "values": [item["object"]]}
        ]
        if item["server"]:
            filters.append(
                {"field": "auto_service_1", "op": "eq", "values": [item["server"]]}
            )
        return {
            "plans": [
                {
                    "datasetId": item["dataset_id"],
                    "metrics": item["metrics"],
                    "filters": filters,
                    "limit": item["limit"],
                }
            ],
            "time": {"start": self.window.start, "end": self.window.end},
            "chartFormat": item["chart_format"],
        }

    def _context(self, item, call_id):
        spec = TaskSpec(
            question=item["question"],
            deliverable="交回这次定向业务读的原值、覆盖和缺口",
            completion_criteria="一次定向业务读结算",
            why_needed="对象、方向和绝对窗已由原题确认",
            decision_impact="直接回答原题或决定是否另派 Worker",
            tools=[DIRECT_MCP_TOOL],
            window=self.window,
            return_mode="first_result",
        )
        spec.normalize(
            seed=False,
            window=self.window,
            tools=self.session.native.tools,
            source_ids=self.outputs.sources,
            source="business",
        )
        context = TaskExecution(
            "direct_" + uuid.uuid4().hex,
            self.planner.conversation_id,
            spec,
            seed=False,
            status="running",
            role="planner",
            window_kind="main",
        )
        context.discovered_tools = self.session.native.tools
        context.dispatch_entry = DIRECT_TOOL
        context.dispatch_call_id = call_id
        return context

    def _payload(self, item, call_id, outcome, view):
        return {
            "call": DIRECT_TOOL,
            "status": "executed",
            "source_status": outcome.get("status"),
            "source_id": view.get("source_id"),
            "object": item["object"],
            "server": item["server"],
            "dataset_id": item["dataset_id"],
            "metrics": item["metrics"],
            "window": {"start": self.window.start, "end": self.window.end},
            "business_value_count": view.get("business_value_count"),
            "returned_rows": view.get("returned_rows"),
            "error": view.get("error"),
            "limitation": (
                "Window/path aggregate only; it cannot establish minute spike shape or same-time "
                "cross-path changes. Use a first_result Worker task with insights_metric_history "
                "for minute P95/P99/request_rate."
            ),
            "result": view,
            "detail": "Program filled the confirmed absolute window and client-side filter; the Planner execution is single-use.",
        }

    async def run(self, call):
        raw = call.get("raw_args") if call.get("invalid") else call.get("args")
        try:
            item = self.validate(raw)
        except DispatchRejection as exc:
            item = rejection_item(0, exc)
            self.outputs.event(
                "direct_rejected", "planner", item, call_id=call.get("id")
            )
            return {
                "call": DIRECT_TOOL,
                "status": "rejected",
                "source_status": None,
                "rejected": [item],
                "detail": "No remote MCP call was made for this directed read.",
            }
        context = self._context(item, call.get("id"))
        self.session.native.authority.register(context)
        arguments = self.arguments(item)
        try:
            async with self.session.tool_semaphore:
                self.session.check_active()
                outcome = await self.session.native.call(
                    call.get("id"),
                    DIRECT_MCP_TOOL,
                    arguments,
                    "planner",
                    execution=context,
                    trace_ids={
                        "model_round_id": self.planner.model_round_id,
                        "planner_decision_id": self.planner.planner_decision_id,
                    },
                )
        except Exception as exc:  # noqa: BLE001 -- direct read failure stays an outcome
            outcome = {
                "call_id": call.get("id"),
                "tool": DIRECT_MCP_TOOL,
                "arguments": arguments,
                "role": "planner",
                "started_at": self._now(),
                "finished_at": self._now(),
                "status": "tool_error",
                "raw_result": None,
                "mcp_call_started": False,
                "error": {
                    "code": type(exc).__name__,
                    "message": self.outputs.redact(str(exc)),
                    "source": "rcagent",
                    "stage": "direct",
                },
            }
        finally:
            context.status = "returned"
            context.revoked = True
        outcome.update(
            task_id=context.task_id,
            conversation_id=context.conversation_id,
            model_round_id=self.planner.model_round_id,
            planner_decision_id=self.planner.planner_decision_id,
            dispatch_entry=DIRECT_TOOL,
            dispatch_call_id=call.get("id"),
        )
        self.outputs.direct_queries += 1
        result = self.outputs.tool_result(outcome)
        view = json.loads(result)
        payload = self._payload(item, call.get("id"), outcome, view)
        self.outputs.event("direct_result", "planner", {
            "source_id": payload["source_id"],
            "source_status": payload["source_status"],
            "object": item["object"],
            "server": item["server"],
            "dataset_id": item["dataset_id"],
            "metrics": item["metrics"],
            "window": payload["window"],
        }, call_id=call.get("id"), source_id=payload["source_id"])
        return payload

    @staticmethod
    def _now():
        return time.time()