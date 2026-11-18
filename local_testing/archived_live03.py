"""Actual archived tool transport feeding the unchanged complete SRE product graph."""

import asyncio
import copy
import json
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from config import settings
from graph import build_parent_graph
from platforms.insights_v5.diagnostics import make_catalog
from workspace.repository import WorkspaceError
from workspace.model_requests import safe_message
from .investigation_export import export_run
from .preflight_v5_cpu import save


def signature(value):
    if isinstance(value, dict):
        return {k: signature(v) for k, v in value.items()}
    if isinstance(value, list):
        return [signature(v) for v in value]
    if isinstance(value, str) and "T" in value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
    return value


class ArchivedClient:
    """Exact recorded parameters only; unrecorded queries cannot fetch remotely."""

    def __init__(self, source):
        self.source = Path(source)
        self.schemas = json.loads((self.source / "tool-schemas.json").read_text())
        self.saved = json.loads((self.source / "mcp-calls.json").read_text())
        self.calls = []
        self.url = "archive://r2-live-03"
        self.identity = {"transport": "archived_fixture", "fresh_remote_access": False}

    async def call(self, role, name, arguments):
        self.calls.append(
            {
                "role": role,
                "tool": name,
                "arguments": copy.deepcopy(arguments),
                "origin": "archive",
                "fresh_remote_access": False,
            }
        )
        for row in self.saved:
            if row["tool"] == name and signature(row["arguments"]) == signature(
                arguments
            ):
                response = row["raw_mcp_result"]
                if isinstance(response.get("structuredContent"), dict):
                    return copy.deepcopy(response["structuredContent"])
                for block in response.get("content", []):
                    if block.get("type") == "text":
                        return json.loads(block["text"])
        self.calls[-1]["status"] = "archive_unavailable"
        raise WorkspaceError(
            "archive_unavailable: no saved response for these parameters; no remote fetch permitted"
        )


async def run_archive(output, *, source=None, model=None):
    """One model-selected Triage/Planner/Worker/Supervisor/gate path, no prefilled facts."""
    output = Path(output)
    if output.exists():
        raise FileExistsError("preserve earlier attempts; use a fresh archival output")
    output.mkdir(parents=True)
    source = Path(
        source
        or (
            settings.INVESTIGATION_HISTORY_ROOT
            / "观测提交与调查收尾Skill-2026-10-29/R2-2026-10-30/live-03"
        )
    )
    records = json.loads((source / "records.json").read_text())
    original_input = json.loads((source / "input.json").read_text())
    target = copy.deepcopy(records["resource"][0])
    for field in ("incident_id", "run_id", "revision"):
        target.pop(field, None)
    client = ArchivedClient(source)
    catalog = make_catalog(
        client,
        service_identity=client.identity,
        initial_resources=[target],
        identity_source={
            "origin": "archive",
            "source_run": str(source),
            "note": "Historical identity fixture; no current identity preflight",
        },
    )
    for capability in catalog.capabilities.values():
        original = capability.invoke

        async def archived(binding, arguments, fn=original):
            result = await fn(binding, arguments)
            result.freshness = "archived_fixture"
            result.warnings = list(result.warnings) + [
                "Offline R2 live-03 original; not fresh remote evidence"
            ]
            return result

        capability.invoke = archived
        capability.availability = (
            "Archived live-03 responses only; unmatched arguments unavailable locally"
        )
    # The original focused task is the task input, not a supplied answer or formal observation.
    question = next(
        q
        for q in records["question"]
        if q["id"] == records["task"][0]["question_ids"][0]
    )
    state = {
        "target_cluster": "rd2",
        "target_resource": target["name"],
        "query_window": original_input["query_window"],
        "incident_text": "仅对已归档的 live-03 真实材料进行离线调查，不访问当前现场。请回答："
        + question["text"]
        + " 可用工具只读取存档。自行选择相关原值、登记、保留支持/反证/局限并形成报告；没有的材料保持未决。",
        "app_name": "R3 archived investigation",
        "prompts_config": {},
    }
    save(
        output / "input.json",
        {
            **state,
            "origin": "archived_real_tool_returns",
            "source": str(source),
            "fresh_remote_access": False,
            "prefilled_observations": 0,
        },
    )
    latest, failure = None, None
    begun = time.monotonic()
    with patch.object(settings, "WORKSPACE_ROOT", output / "workspace"):
        try:
            graph = build_parent_graph(catalog=catalog, model=model)
            async for update in graph.astream(
                state, stream_mode="values", config={"recursion_limit": 30}
            ):
                latest = update
                save(output / "latest-state.json", latest)
            save(output / "final-state.json", latest)
        except BaseException as error:
            failure = {"type": type(error).__name__, "message": safe_message(error)}
            save(output / "failure.json", failure)
            if isinstance(
                error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
        finally:
            save(output / "archived-transport-calls.json", client.calls)
            summary = await asyncio.shield(
                export_run(
                    output,
                    output / "workspace",
                    latest,
                    failure,
                    elapsed_seconds=time.monotonic() - begun,
                    mcp_calls=[],
                    environment_verified=False,
                )
            )
    result = json.loads((output / "records.json").read_text())
    worker_ids = {
        o["id"]
        for o in result.get("observation", [])
        if o.get("producer_role") == "worker"
    }
    packet_id = (latest or {}).get("report_packet_id")
    accepted = [
        p
        for p in result.get("report_proposal", [])
        if not p.get("errors") and p.get("packet_id") == packet_id
    ]
    choice = (accepted[-1].get("selection") or {}) if accepted else {}
    statement_ids = set(choice.get("selected_statement_ids", []))
    selected = {
        oid
        for packet in result.get("report_packet", [])
        if packet["id"] == packet_id
        for statement in packet.get("statements", [])
        if statement["statement_id"] in statement_ids
        for oid in statement.get("observation_ids", [])
    }
    accepted_worker = any(
        a.get("operation") == "complete_task"
        and a.get("outcome", {}).get("status") == "accepted"
        for a in result.get("model_action", [])
    )
    outcome = {
        "failure": failure,
        "actual_model": model is None,
        "worker_completed": accepted_worker,
        "worker_observation_ids": sorted(worker_ids),
        "selected_worker_observation_ids": sorted(worker_ids & selected),
        "model_report_completed": summary["software_flow_completed"],
        "gate_passed": summary["software_flow_completed"],
        "remote_mcp_requests": 0,
        "actual_requests": len(result.get("model_attempt", [])),
        "cost": "unknown",
    }
    outcome["goal_met"] = bool(
        accepted_worker and worker_ids & selected and summary["software_flow_completed"]
    )
    save(output / "outcome.json", outcome)
    print(json.dumps(outcome, ensure_ascii=False), flush=True)
    return outcome
