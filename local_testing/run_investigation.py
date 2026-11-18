"""Run the normal RCAgent product graph from a natural-language JSON input."""
import argparse
import asyncio
import json
import time
from pathlib import Path

from agents.investigation_budget import ENTRY_BUDGET, RunBudget
from graph import build_parent_graph


async def _run(output, input_path):
    received = time.monotonic()
    output = Path(output)
    if output.exists():
        raise FileExistsError("Use a new output directory; earlier runs are retained")
    state = json.loads(Path(input_path).read_text(encoding="utf-8-sig"))
    token = ENTRY_BUDGET.set(RunBudget(state.get("deadline")))
    state["output_directory"] = str(output.resolve())
    result = None
    try:
        async for mode, update in build_parent_graph().astream(state, stream_mode=["values", "custom"]):
            if mode == "custom":
                print(json.dumps(update, ensure_ascii=False), flush=True)
            else:
                result = update
    finally:
        ENTRY_BUDGET.reset(token)
    report = (result or {}).get("final_report", {})
    print(json.dumps({k: report.get(k) for k in ("run_id", "conversation_id", "execution_status",
        "answer_status", "evidence_status", "elapsed_seconds")}, ensure_ascii=False), flush=True)
    print(report.get("body", ""), flush=True)
    elapsed = time.monotonic()-received
    try:
        (output / "response-completed.json").write_text(json.dumps({"elapsed_seconds": elapsed, "within_300_seconds": elapsed <= 300, "meaning": "CLI input loaded through complete stdout output"}), encoding="utf-8")
    except OSError as exc:
        print(json.dumps({"archive_warning": str(exc), "answer_already_printed": True}, ensure_ascii=False), flush=True)
    return bool(report.get("body") and report.get("answer_source") == "model")


async def run(output, input_path):
    try:
        return await _run(output, input_path)
    except (OSError, ValueError) as exc:
        print(json.dumps({"execution_status": "execution_error", "answer_status": "none", "error": str(exc)}, ensure_ascii=False), flush=True)
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("input", type=Path, help="JSON: incident_text, target_*, query_window; deadline is optional")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.output, args.input)) else 1)
