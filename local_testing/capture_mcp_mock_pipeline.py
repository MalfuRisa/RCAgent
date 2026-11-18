"""Capture an auditable RCAgent -> Gateway -> mock Querier experiment.

This is an experiment harness. It does not change the agent graph or tool logic.
The trace contains visible model messages and tool I/O, not private model reasoning.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import Lock
from time import perf_counter
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

from graph import parent_graph
from local_testing.run_mcp_mock_pipeline import INPUT
from tools import observability_adapter


REDACTED_KEY = re.compile(r"(token|api[_-]?key|password|secret|authorization)", re.I)
REDACTED_VALUE = re.compile(r"sk-[A-Za-z0-9._-]{20,}")
REDACTED_BEARER = re.compile(r"(?i)\bbearer\s+[^\s\"']+")
REDACTED_INLINE = re.compile(
    r"(?i)(\b(?:authorization|cookie|token|api[_-]?key|password|secret)\b\s*[:=]\s*[\"']?)[^\s\"',;}]+"
)


def redact_text(value: str) -> str:
    value = REDACTED_VALUE.sub("[REDACTED]", value)
    value = REDACTED_BEARER.sub("Bearer [REDACTED]", value)
    return REDACTED_INLINE.sub(r"\1[REDACTED]", value)


def plain(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        try:
            return plain(value.model_dump(mode="json"))
        except (TypeError, ValueError):
            return plain(value.model_dump())
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if REDACTED_KEY.search(str(key)) else plain(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return redact_text(value) if isinstance(value, str) else value
    return redact_text(str(value))


class Recorder(BaseCallbackHandler):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = Lock()
        self.seq = 0
        self.names: dict[str, str] = {}
        self.started: dict[str, float] = {}

    def write(self, event: str, **fields: Any) -> None:
        with self.lock:
            self.seq += 1
            row = {
                "seq": self.seq,
                "at": datetime.now(timezone.utc).isoformat(),
                "event": event,
                **{key: plain(value) for key, value in fields.items()},
            }
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

    def on_chain_start(self, serialized: dict[str, Any], inputs: Any, *, run_id: Any,
                       parent_run_id: Any = None, **kwargs: Any) -> None:
        name = kwargs.get("name") or serialized.get("name") or "chain"
        self.names[str(run_id)] = str(name)
        self.write("chain_start", run_id=str(run_id), parent_run_id=str(parent_run_id),
                   name=name, input_keys=list(inputs) if isinstance(inputs, dict) else [])

    def on_chain_end(self, outputs: Any, *, run_id: Any, parent_run_id: Any = None,
                     **kwargs: Any) -> None:
        name = self.names.get(str(run_id), "chain")
        self.write("chain_end", run_id=str(run_id), parent_run_id=str(parent_run_id),
                   name=name, outputs=outputs)

    def on_chat_model_start(self, serialized: dict[str, Any], messages: Any, *, run_id: Any,
                            parent_run_id: Any = None, **kwargs: Any) -> None:
        self.started[str(run_id)] = perf_counter()
        self.write("llm_start", run_id=str(run_id), parent_run_id=str(parent_run_id),
                   messages=messages)

    def on_llm_end(self, response: Any, *, run_id: Any, parent_run_id: Any = None,
                   **kwargs: Any) -> None:
        started = self.started.pop(str(run_id), None)
        self.write("llm_end", run_id=str(run_id), parent_run_id=str(parent_run_id),
                   duration_ms=round((perf_counter() - started) * 1000, 2) if started else None,
                   generations=getattr(response, "generations", None),
                   usage=getattr(response, "llm_output", None))

    def on_llm_error(self, error: BaseException, *, run_id: Any,
                     parent_run_id: Any = None, **kwargs: Any) -> None:
        started = self.started.pop(str(run_id), None)
        self.write("llm_error", run_id=str(run_id), parent_run_id=str(parent_run_id),
                   duration_ms=round((perf_counter() - started) * 1000, 2) if started else None,
                   error=str(error))

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, *, run_id: Any,
                      parent_run_id: Any = None, inputs: Any = None, **kwargs: Any) -> None:
        name = serialized.get("name") or kwargs.get("name") or "tool"
        self.names[str(run_id)] = str(name)
        self.started[str(run_id)] = perf_counter()
        self.write("tool_start", run_id=str(run_id), parent_run_id=str(parent_run_id),
                   name=name, input=inputs if inputs is not None else input_str)

    def on_tool_end(self, output: Any, *, run_id: Any, parent_run_id: Any = None,
                    **kwargs: Any) -> None:
        started = self.started.pop(str(run_id), None)
        self.write("tool_end", run_id=str(run_id), parent_run_id=str(parent_run_id),
                   name=self.names.get(str(run_id), "tool"),
                   duration_ms=round((perf_counter() - started) * 1000, 2) if started else None,
                   output=output)

    def on_tool_error(self, error: BaseException, *, run_id: Any,
                      parent_run_id: Any = None, **kwargs: Any) -> None:
        started = self.started.pop(str(run_id), None)
        self.write("tool_error", run_id=str(run_id), parent_run_id=str(parent_run_id),
                   name=self.names.get(str(run_id), "tool"),
                   duration_ms=round((perf_counter() - started) * 1000, 2) if started else None,
                   error=str(error))


async def main() -> None:
    evidence_root = Path(os.environ.get("RCAGENT_EXPERIMENT_OUTPUT_DIR", "local_testing/evidence"))
    run_name = "mcp-mock-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = evidence_root / run_name
    run_dir.mkdir(parents=True, exist_ok=False)
    recorder = Recorder(run_dir / "events.jsonl")

    original_call = observability_adapter.call_gateway_tool

    async def traced_adapter_call(name: str, arguments: Any) -> dict[str, Any]:
        recorder.write("adapter_tool_start", name=name, input=arguments)
        started = perf_counter()
        try:
            result = await original_call(name, arguments)
        except BaseException as error:
            recorder.write("adapter_tool_error", name=name, error=str(error),
                           duration_ms=round((perf_counter() - started) * 1000, 2))
            raise
        recorder.write("adapter_tool_end", name=name, output=result,
                       duration_ms=round((perf_counter() - started) * 1000, 2))
        return result

    observability_adapter.call_gateway_tool = traced_adapter_call
    input_state = deepcopy(INPUT)
    recorder.write("experiment_start", input=input_state,
                   model=os.environ.get("RCAGENT_LLM_MODEL", "[not set]"))
    started = perf_counter()
    try:
        result = await parent_graph.ainvoke(input_state, config={"callbacks": [recorder]})
        elapsed_ms = round((perf_counter() - started) * 1000, 2)
        status = "passed" if result.get("final_report") else "inconclusive"
        recorder.write("experiment_end", status=status, elapsed_ms=elapsed_ms)
        (run_dir / "final_state.json").write_text(
            json.dumps(plain(result), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(json.dumps({"status": status, "elapsed_ms": elapsed_ms,
                          "evidence_dir": str(run_dir),
                          "final_report": plain(result.get("final_report"))},
                         ensure_ascii=False, indent=2))
    except BaseException as error:
        recorder.write("experiment_error", error=str(error),
                       elapsed_ms=round((perf_counter() - started) * 1000, 2))
        raise
    finally:
        observability_adapter.call_gateway_tool = original_call


if __name__ == "__main__":
    asyncio.run(main())
