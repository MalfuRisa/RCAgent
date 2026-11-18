"""PRD42 short dispatch, per-call receipts, bounded recovery and first evidence.

The archived samples in ``fixtures/prd42_archived_dispatch_calls.json`` are the
two real calls from the 2026-11-17 live replay: one SDK-unparsed argument
string and one task mixing business and native tools.
"""

import asyncio
import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessageChunk, ToolMessage

from agents.investigation_session import InvestigationSession
from graph import build_parent_graph
from platforms.business_policy import BUSINESS_READS
from platforms.insights_v5.diagnostic_policy import NATIVE_READS
from platforms.insights_v5.native_session import NativeMcpSession
from workflow.investigation_dispatch import dispatch_definitions

HERE = Path(__file__).resolve().parent
FIXTURES = HERE.parent / "fixtures"
ARCHIVED = json.loads(
    (FIXTURES / "prd42_archived_dispatch_calls.json").read_text(encoding="utf-8")
)
NATIVE_DEFS = {
    item["function"]["name"]: item
    for item in json.loads(
        (FIXTURES / "native_phase5_tools.json").read_text(encoding="utf-8-sig")
    )
}
BUSINESS_DEFS = {
    "insights_catalog": {
        "type": "function",
        "function": {
            "name": "insights_catalog",
            "description": "business catalog",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"query": {"type": "string"}},
            },
        },
    },
    "insights_query_metrics": {
        "type": "function",
        "function": {
            "name": "insights_query_metrics",
            "description": "business metric rows",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["plans"],
                "properties": {
                    "plans": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["datasetId"],
                            "properties": {
                                "datasetId": {"type": "string"},
                                "metrics": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "filters": {
                                    "type": "array",
                                    "items": {"type": "object"},
                                },
                                "limit": {"type": "integer"},
                            },
                        },
                    },
                    "time": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start", "end"],
                        "properties": {
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                        },
                    },
                    "chartFormat": {"type": "string"},
                },
            },
        },
    },
    "insights_metric_history": {
        "type": "function",
        "function": {
            "name": "insights_metric_history",
            "description": "business minute history",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["datasetId", "client", "server", "metrics"],
                "properties": {
                    "datasetId": {"type": "string"},
                    "client": {"type": "string"},
                    "server": {"type": "string"},
                    "metrics": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "time": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["start", "end"],
                        "properties": {
                            "start": {"type": "integer"},
                            "end": {"type": "integer"},
                        },
                    },
                    "chartFormat": {"type": "string"},
                },
            },
        },
    },
}
START, END = ARCHIVED["main_window"]["start"], ARCHIVED["main_window"]["end"]
NODE = "k8s-10-44-56-107"


def chunk(text="", calls=(), finish="stop"):
    return AIMessageChunk(
        content=text,
        tool_call_chunks=[
            {"name": name, "args": json.dumps(args), "id": cid, "index": i}
            for i, (cid, name, args) in enumerate(calls)
        ],
        response_metadata={"finish_reason": finish},
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )


def sdk_unparsed(name, payload, cid):
    """Exactly the archived shape: the SDK could not turn arguments into an object."""
    inner = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": name, "args": json.dumps(inner), "id": cid, "index": 0}
        ],
        response_metadata={"finish_reason": "tool_calls"},
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )


def truncated(name, payload, cid):
    inner = json.dumps(payload, ensure_ascii=False)
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": name, "args": json.dumps(inner)[:90], "id": cid, "index": 0}
        ],
        response_metadata={"finish_reason": "tool_calls"},
        usage_metadata={"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    )


def business_args():
    return {
        "plans": [
            {
                "datasetId": "app-path",
                "metrics": ["server_response_time", "request_rate"],
                "filters": [
                    {"field": "auto_service_0", "op": "eq", "values": [NODE]}
                ],
                "limit": 10,
            }
        ],
        "chartFormat": "none",
    }


def native_args(table="network", start=START + 1, end=END):
    return {
        "body": {
            "database": "flow_metrics",
            "table": table,
            "time_start": start,
            "time_end": end,
            "queries": [{"query_id": "q", "select": "Sum(byte)"}],
            "total": False,
        }
    }


class Model:
    """Scripted ChatModel: records the public request and returns one chunk."""

    def __init__(self, script):
        self.script = script if callable(script) else list(script)
        self.requests = []

    def bind_tools(self, definitions, **kw):
        parent, bound = self, copy.deepcopy(definitions)

        class Bound:
            async def astream(self, messages):
                async for item in parent.generate(messages, bound):
                    yield item

        return Bound()

    async def astream(self, messages):
        async for item in self.generate(messages, []):
            yield item

    async def generate(self, messages, definitions):
        request = {
            "messages": copy.deepcopy(messages),
            "tools": copy.deepcopy(definitions),
            "planner": any(
                getattr(m, "additional_kwargs", {}).get("guidance") == "role:planner"
                for m in messages
            ),
            "task": None,
        }
        for message in messages:
            if isinstance(message.content, str) and message.content.startswith("本次小任务："):
                request["task"] = json.loads(message.content.split("：", 1)[1])
        self.requests.append(request)
        assert len(self.requests) < 40, "scripted fixture failed to converge"
        item = self.script(request) if callable(self.script) else self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        yield item


class Native(NativeMcpSession):
    """One run-owned fake capable of both discovered sources without a network."""

    def __init__(self, outputs, **kw):
        super().__init__(outputs, **kw)
        self.tools = copy.deepcopy({**NATIVE_DEFS, **BUSINESS_DEFS})
        self.profile = "integrated"
        self.allowed = BUSINESS_READS | NATIVE_READS
        self.availability = {
            "business": {"status": "available"},
            "native": {"status": "available"},
        }
        self.routes = {
            name: ("business" if name in BUSINESS_READS else "native")
            for name in self.tools
        }
        self.client = self
        self.sent, self.closed = [], False

    async def __aenter__(self):
        self.entered_on = asyncio.current_task()
        return self

    async def __aexit__(self, *args):
        assert asyncio.current_task() is self.entered_on
        self.closed = True

    def payload(self, name, args):
        if name == "insights_query_metrics":
            return {
                "structuredContent": {
                    "data": {
                        "time": args.get("time"),
                        "results": [
                            {
                                "datasetId": "app-path",
                                "metrics": [
                                    {"key": "server_response_time", "unit": "ms"},
                                    {"key": "request_rate", "unit": "count/s"},
                                ],
                                "rows": [
                                    {
                                        "label": f"{NODE} → frontend @ cluster",
                                        "values": {
                                            "server_response_time": {
                                                "avg": 88.0,
                                                "max": 420.0,
                                            },
                                            "request_rate": {"max": 12.5},
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                },
                "isError": False,
            }
        if name == "insights_query_list":
            return {
                "structuredContent": {
                    "data": {"list": [{"v": 123}], "total": 0},
                    "meta": {
                        "pagination": {
                            "count_kind": "business_rows",
                            "total_status": "not_requested",
                        }
                    },
                },
                "isError": False,
            }
        if name == "insights_metric_history":
            points = [[START * 1000 + i * 60000, None if i == 50 else 200.0] for i in range(61)]
            points[58] = [START * 1000 + 58 * 60000, 266.333]
            points[59] = [START * 1000 + 59 * 60000, 100732]
            points[60] = [START * 1000 + 60 * 60000, 242]
            return {
                "structuredContent": {
                    "data": {
                        "time": args.get("time") or {"start": START, "end": END},
                        "subject": f"{NODE} → {args.get('server')}",
                        "metrics": [
                            {
                                "key": "server_response_time",
                                "unit": "µs",
                                "stats": {"last": 242, "avg": 2733, "max": 100732, "min": 142},
                                "peakAt": START + 59 * 60,
                            }
                        ],
                        "charts": [
                            {
                                "metricKey": "server_response_time",
                                "unit": "µs",
                                "series": [{"name": "server_response_time", "points": points}],
                            }
                        ],
                        "queryMeta": {
                            "requested": {
                                "datasetId": args.get("datasetId"),
                                "filters": [],
                                "time": args.get("time"),
                            }
                        },
                    }
                },
                "isError": False,
            }
        return {
            "structuredContent": {"data": {"pod": {"pod_id": 259, "name": NODE}}},
            "isError": False,
        }

    async def call_tool(self, name, args):
        self.sent.append((name, copy.deepcopy(args)))
        raw = self.payload(name, args)
        return SimpleNamespace(
            isError=raw["isError"], model_dump=lambda **kw: raw
        )


def planner_tools(request):
    return {item["function"]["name"] for item in request["tools"]}


def tool_messages(request):
    return [m for m in request["messages"] if isinstance(m, ToolMessage)]


def receipts(request):
    return [json.loads(m.content) for m in tool_messages(request) if m.name and m.name.startswith("dispatch_")]


def planners(model):
    return [request for request in model.requests if request["planner"]]


def all_receipts(model):
    seen, sequence = set(), []
    for request in model.requests:
        for message in request["messages"]:
            if (
                isinstance(message, ToolMessage)
                and message.name
                and message.name.startswith("dispatch_")
                and message.tool_call_id not in seen
            ):
                seen.add(message.tool_call_id)
                sequence.append(json.loads(message.content))
    return sequence


def first_source_id(request):
    for message in request["messages"]:
        match = re.search(r"source_[0-9a-f]{32}", str(message.content))
        if match:
            return match.group(0)
    return None


class Checks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sessions, self.natives = [], []

    def tearDown(self):
        self.tmp.cleanup()

    async def product(self, model, *, name="run", **state):
        def factory(out, **kw):
            native = Native(out, cancellation=kw.get("cancellation"), deadline=kw.get("deadline"))
            session = InvestigationSession(out, model=model, native=native, **kw)
            self.sessions.append(session)
            self.natives.append(native)
            return session

        return await build_parent_graph(runtime_factory=factory).ainvoke(
            {
                "incident_text": "调查该客户端在原题时间窗内的响应延迟",
                "query_window": {"start_time": START, "end_time": END},
                "output_directory": str(self.root / name),
                **state,
            }
        )

    def events(self, name="run"):
        return [
            json.loads(line)
            for line in (self.root / name / "events.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    async def test_archived_bad_json_then_wrong_source_then_legal_short_task(self):
        archived = ARCHIVED["archived_invalid_tool_call"]
        mixed = ARCHIVED["archived_mixed_source_task"]
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return sdk_unparsed(archived["name"], archived["args"], archived["id"])
                if count["planner"] == 2:
                    return chunk(
                        calls=[
                            (
                                "call_mixed",
                                "dispatch_business",
                                {"question": mixed["question"], "tools": mixed["tools"]},
                            )
                        ]
                    )
                if count["planner"] == 3:
                    return chunk(
                        calls=[
                            (
                                "call_legal",
                                "dispatch_business",
                                {
                                    "question": "取原题时间窗内该客户端的路径延迟",
                                    "tools": ["insights_query_metrics"],
                                },
                            )
                        ]
                    )
                source = first_source_id(request)
                return chunk(f"调查正文：{source} 的 app-path 原值显示响应时间升高。")
            if not request["tools"]:
                return chunk("阶段：已结算，见结果。")
            return chunk(calls=[("w1", "insights_query_metrics", business_args())])

        model = Model(script)
        result = await self.product(model)
        report = result["final_report"]
        native = self.natives[0]

        first_tools = planner_tools(model.requests[0])
        self.assertIn("dispatch_business", first_tools)
        self.assertIn("dispatch_native", first_tools)
        self.assertNotIn("dispatch_tasks", first_tools)

        sequence = all_receipts(model)
        bad_json = sequence[0]
        self.assertEqual(bad_json["status"], "rejected")
        error = bad_json["rejected"][0]["error"]
        self.assertEqual(error["code"], "arguments_unparsed")
        self.assertFalse(error["arguments_parsed"])
        self.assertFalse(error["mcp_call_started"])
        self.assertIn("argument_snippet", error)
        self.assertIn("nothing was dispatched", error["message"])
        self.assertEqual(bad_json["rejected"][0]["index"], 0)

        wrong_source = sequence[1]
        error = wrong_source["rejected"][0]["error"]
        self.assertEqual(error["code"], "source_mismatch")
        self.assertEqual(error["path"], "tools[1]")
        self.assertEqual(error["allowed_entry"], "dispatch_native")
        self.assertIn("insights_get_tags", error["message"])
        self.assertFalse(error["mcp_call_started"])

        accepted = sequence[2]
        self.assertEqual(accepted["status"], "accepted")
        item = accepted["accepted"][0]
        self.assertEqual(item["source"], "business")
        self.assertEqual(item["window"], {"start": START, "end": END})
        self.assertIn("insights_query_metrics", item["effective_tools"])
        self.assertNotIn("insights_k8s_resource", item["effective_tools"])

        self.assertEqual([name for name, _ in native.sent], ["insights_query_metrics"])
        self.assertEqual(native.sent[0][1]["time"], {"start": START, "end": END})
        task = report["task_results"][0]
        self.assertEqual(task["end_reason"], "first_result_settled")
        self.assertEqual(task["dispatch_entry"], "dispatch_business")
        self.assertEqual(task["dispatch_call_id"], "call_legal")
        self.assertEqual(report["dispatch_status"], "accepted")
        self.assertEqual(report["execution_counts"]["tasks_executed"], 1)
        self.assertEqual(report["execution_counts"]["sources_received"], 1)
        self.assertEqual(report["execution_counts"]["rejected_items"], 2)
        self.assertEqual(report["execution_counts"]["corrections_used"], 2)
        self.assertRegex(report["body"], r"source_[0-9a-f]{32}")

    def test_valid_json_string_arguments_are_recovered_without_a_second_call(self):
        from workflow.native_tool_calls import calls_from

        payload = {"question": "取延迟", "tools": ["insights_query_metrics"]}
        message = sdk_unparsed(
            "dispatch_business", json.dumps(payload), "call_recoverable"
        )
        calls = calls_from(message)
        self.assertEqual(calls[0]["args"], payload)
        self.assertFalse(calls[0]["invalid"])

    async def test_truncated_json_reports_unparsed_and_pairs_a_wire_safe_call(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return truncated(
                        "dispatch_business",
                        {
                            "question": "取该客户端延迟",
                            "tools": ["insights_query_metrics"],
                        },
                        "call_truncated",
                    )
                if count["planner"] == 2:
                    return chunk(
                        calls=[
                            (
                                "call_ok",
                                "dispatch_business",
                                {
                                    "question": "取原题时间窗内该客户端的路径延迟",
                                    "tools": ["insights_query_metrics"],
                                },
                            )
                        ]
                    )
                return chunk("正文引用 " + str(first_source_id(request)))
            if not request["tools"]:
                return chunk("阶段：已结算，见结果。")
            return chunk(calls=[("w1", "insights_query_metrics", business_args())])

        model = Model(script)
        result = await self.product(model)
        report = result["final_report"]
        self.assertEqual([name for name, _ in self.natives[0].sent], ["insights_query_metrics"])

        follow_up = planners(model)[1]
        receipt = [
            json.loads(m.content)
            for m in tool_messages(follow_up)
            if m.tool_call_id == "call_truncated"
        ][0]
        error = receipt["rejected"][0]["error"]
        self.assertEqual(error["code"], "arguments_unparsed")
        self.assertFalse(error["arguments_parsed"])
        self.assertFalse(error["mcp_call_started"])
        self.assertIn("argument_snippet", error)
        self.assertIn("question", error["example"])
        self.assertIn("nothing was dispatched", error["message"])

        assistant = [
            m
            for m in follow_up["messages"]
            if getattr(m, "tool_calls", None)
            and any(call["id"] == "call_truncated" for call in m.tool_calls)
        ]
        self.assertEqual(assistant[0].tool_calls[0]["args"], {})
        self.assertEqual(report["execution_counts"]["rejected_items"], 1)
        self.assertRegex(report["body"], r"source_[0-9a-f]{32}")

    async def test_later_batch_runs_legal_items_once_and_rejects_only_the_wrong_source(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(
                        calls=[
                            (
                                "seed",
                                "dispatch_native",
                                {
                                    "question": "取原题窗口的流量行",
                                    "tools": ["insights_query_list"],
                                },
                            )
                        ]
                    )
                if count["planner"] == 2:
                    return chunk(
                        calls=[
                            (
                                "business_ok",
                                "dispatch_business",
                                {
                                    "question": "取该客户端的路径延迟与流量",
                                    "tools": ["insights_query_metrics"],
                                    "why_needed": "确认客户端侧是否有延迟升高",
                                },
                            ),
                            (
                                "wrong_source",
                                "dispatch_business",
                                {
                                    "question": "读取该对象的资源详情",
                                    "tools": ["insights_k8s_resource"],
                                },
                            ),
                            (
                                "native_ok",
                                "dispatch_native",
                                {
                                    "question": "取同一窗口另一张表的流量行",
                                    "tools": ["insights_query_list"],
                                    "dataset": {
                                        "database": "flow_metrics",
                                        "table": "application",
                                    },
                                },
                            ),
                        ]
                    )
                return chunk("汇总：" + str(first_source_id(request)))
            if not request["tools"]:
                return chunk("阶段：已结算，见结果。")
            task = request["task"] or {}
            names = task.get("effective_tools") or []
            tool = "insights_query_metrics" if "insights_query_metrics" in names else "insights_query_list"
            table = (task.get("dataset") or {}).get("table", "network")
            return chunk(calls=[("w", tool, business_args() if tool == "insights_query_metrics" else native_args(table))])

        model = Model(script)
        result = await self.product(model)
        report = result["final_report"]
        sends = [name for name, _ in self.natives[0].sent]
        self.assertEqual(sends.count("insights_query_metrics"), 1, report)
        self.assertEqual(sends.count("insights_query_list"), 2, report)

        batch = all_receipts(model)[1:4]
        self.assertEqual([item["call"] for item in batch], [
            "dispatch_business",
            "dispatch_business",
            "dispatch_native",
        ])
        self.assertEqual(batch[0]["status"], "accepted")
        self.assertEqual(batch[1]["status"], "rejected")
        self.assertEqual(batch[1]["rejected"][0]["error"]["code"], "source_mismatch")
        self.assertEqual(batch[1]["rejected"][0]["error"]["path"], "tools[0]")
        self.assertEqual(batch[2]["status"], "accepted")

        results = report["task_results"]
        self.assertEqual([item["submission_index"] for item in results], [0, 1, 2])
        self.assertEqual(len(results), 3)
        self.assertEqual(report["execution_counts"]["tasks_executed"], 3)
        self.assertEqual(report["execution_counts"]["rejected_items"], 1)

    async def test_first_response_accepts_one_seed_and_reports_the_extra_item(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(
                        calls=[
                            (
                                "first",
                                "dispatch_native",
                                {"question": "首个最小问题", "tools": ["insights_query_list"]},
                            ),
                            (
                                "second",
                                "dispatch_business",
                                {
                                    "question": "另一个独立问题",
                                    "tools": ["insights_query_metrics"],
                                },
                            ),
                        ]
                    )
                return chunk("正文 " + str(first_source_id(request)))
            if not request["tools"]:
                return chunk("阶段：已结算，见结果。")
            return chunk(calls=[("w", "insights_query_list", native_args())])

        model = Model(script)
        result = await self.product(model)
        report = result["final_report"]
        self.assertEqual(len(report["task_results"]), 1)
        second_receipt = all_receipts(model)[1]
        self.assertEqual(second_receipt["status"], "rejected")
        self.assertEqual(second_receipt["rejected"][0]["error"]["code"], "seed_limit")
        self.assertIn("only one legal minimal task", second_receipt["rejected"][0]["error"]["message"])
        self.assertEqual(len(self.natives[0].sent), 1)

    async def test_planner_local_reads_do_not_consume_dispatch_corrections(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] == 1:
                    return chunk(
                        calls=[
                            (
                                "seed",
                                "dispatch_business",
                                {
                                    "question": "取原题时间窗内该客户端的路径延迟",
                                    "tools": ["insights_query_metrics"],
                                },
                            )
                        ]
                    )
                if count["planner"] == 2:
                    source = first_source_id(request) or "source_" + "0" * 32
                    return chunk(
                        calls=[
                            (
                                "read",
                                "read_material",
                                {"source_id": source, "pointer": "", "limit": 100},
                            )
                        ]
                    )
                return chunk("最终正文引用 " + str(first_source_id(request)))
            if not request["tools"]:
                return chunk("阶段：已结算，见结果。")
            return chunk(calls=[("w1", "insights_query_metrics", business_args())])

        model = Model(script)
        result = await self.product(model)
        report = result["final_report"]
        self.assertEqual(report["execution_counts"]["corrections_used"], 0)
        self.assertEqual(report["execution_counts"]["dispatch_calls"], 1)
        self.assertFalse(any(e["type"] == "plan_degraded" for e in self.events()))
        self.assertTrue(report["body"].startswith("最终正文"))

    async def test_repeated_rejected_call_stops_early_as_dispatch_blocked(self):
        mixed = ARCHIVED["archived_mixed_source_task"]
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] <= 2:
                    return chunk(
                        calls=[
                            (
                                f"bad{count['planner']}",
                                "dispatch_business",
                                {
                                    "question": mixed["question"],
                                    "tools": mixed["tools"],
                                },
                            )
                        ]
                    )
                return chunk("取证阻断：没有合法任务被接受，不能给出业务结论。")
            raise AssertionError("no Worker should start")

        model = Model(script)
        result = await self.product(model)
        report = result["final_report"]
        self.assertEqual(self.natives[0].sent, [])
        self.assertEqual(report["stop_reason"], "dispatch_blocked")
        self.assertEqual(report["stop_reasons"], ["dispatch_blocked"])
        self.assertEqual(report["dispatch_status"], "blocked")
        self.assertEqual(report["task_results"], [])
        counts = report["execution_counts"]
        for key, value in {
            "tasks_accepted": 0,
            "tasks_executed": 0,
            "sources_received": 0,
            "dispatch_calls": 2,
            "rejected_items": 2,
            "corrections_used": 1,
            "worker_model_rounds": 0,
        }.items():
            self.assertEqual(counts[key], value, key)
        self.assertTrue(report["body"].startswith("取证阻断（dispatch_blocked）"))
        draft = [
            request
            for request in planners(model)
            if not request["tools"]
            and any("停止取证" in str(message.content) for message in request["messages"])
        ][-1]
        draft_text = " ".join(str(message.content) for message in draft["messages"])
        self.assertNotIn("本run实际发现的MCP工具目录", draft_text)
        self.assertIn("当前取证与待答项", draft_text)
        blocked = [event for event in self.events() if event["type"] == "dispatch_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["payload"]["executed_tasks"], 0)
        self.assertEqual(blocked[0]["payload"]["rejected_items"], 2)

    async def test_two_exhausted_corrections_also_block(self):
        count = {"planner": 0}

        def script(request):
            if request["planner"]:
                count["planner"] += 1
                if count["planner"] <= 3:
                    bad = [
                        {"question": "读业务目录", "tools": ["insights_catalog"]},
                        {
                            "question": "读资源再读业务目录",
                            "tools": ["insights_k8s_resource", "insights_catalog"],
                        },
                        {
                            "question": "读资源",
                            "tools": ["insights_k8s_resource"],
                            "unknown": 1,
                        },
                    ][count["planner"] - 1]
                    return chunk(
                        calls=[(f"bad{count['planner']}", "dispatch_native", bad)]
                    )
                return chunk("阻断说明正文")
            raise AssertionError("no Worker should start")

        model = Model(script)
        result = await self.product(model, name="exhausted")
        report = result["final_report"]
        self.assertEqual(report["stop_reason"], "dispatch_blocked")
        self.assertEqual(report["execution_counts"]["corrections_used"], 2)
        self.assertEqual(report["execution_counts"]["rejected_items"], 3)
        self.assertEqual(self.natives[0].sent, [])

    def test_entry_schema_lists_only_that_source_and_single_source_runs_show_one(self):
        discovered = {**NATIVE_DEFS, **BUSINESS_DEFS}
        definitions = dispatch_definitions(discovered)
        self.assertEqual(set(definitions), {"dispatch_business", "dispatch_native"})
        for entry, source in (
            ("dispatch_business", BUSINESS_READS),
            ("dispatch_native", NATIVE_READS),
        ):
            parameters = definitions[entry]["function"]["parameters"]
            self.assertEqual(parameters["required"], ["question", "tools"])
            self.assertEqual(
                parameters["properties"]["tools"]["items"]["enum"],
                sorted(set(discovered) & source),
            )
        only_native = dispatch_definitions(NATIVE_DEFS)
        self.assertEqual(set(only_native), {"dispatch_native"})
        only_business = dispatch_definitions(BUSINESS_DEFS)
        self.assertEqual(set(only_business), {"dispatch_business"})


if __name__ == "__main__":
    unittest.main()