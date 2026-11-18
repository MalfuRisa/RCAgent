"""Small paid archived-material evaluation; no MCP client and no alternate graph."""

import argparse
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch
from config import settings
from agents.investigation_worker import worker
from agents.investigation_supervisor import supervisor
from local_testing.skill_fixture import seed
from local_testing.investigation_export import export_run
from workspace.runtime import bind
from workspace.model_requests import safe_message
from workspace.model_audit import public_value
from models.investigation import TaskSubmission


async def run_case(root, case, skills, *, model=None):
    if case == "R3":
        from .archived_live03 import run_archive

        outcome = await run_archive(Path(root) / "R3-skill", model=model)
        return outcome.get("failure") or (
            None
            if outcome["goal_met"]
            else {
                "type": "AcceptanceNotMet",
                "message": "Worker formal facts did not reach a gated model report",
            }
        )
    output = Path(root) / (case + ("-skill" if skills else "-control"))
    if output.exists():
        raise FileExistsError("preserve initial attempts; choose a fresh output root")
    output.mkdir(parents=True)
    start = time.monotonic()
    latest = None
    failure = None
    with patch.object(settings, "INVESTIGATION_SKILLS_ENABLED", skills):
        state, b, catalog, data = await seed(
            output / "workspace", case, start_attempt=case == "E4"
        )
        latest = state
        instructions = {
            "E1": "修正先前被拒的观测稿，登记至少一个当前可见合法原值；分位数未知放入解释或局限，完成任务。",
            "E3": "当前已经进入仅提交阶段，不能新增业务取证。根据现有材料完成任务，保留未覆盖范围和局限。",
            "E4": "Worker局部失败且零正式观测。不能确认根因或业务影响；逐问题提交明确未决报告并完成最终选择。",
        }
        if case == "E1":
            try:
                TaskSubmission.model_validate(data["rejected_task_submission"])
            except ValueError as error:
                rejection = str(error)
            else:
                raise AssertionError("original rejected draft unexpectedly valid")
            task = await b.repository.read(b.incident_id, b.task_id, "task")
            task["investigation_goal"] = (
                instructions[case] + "\n原稿拒绝：" + rejection[:1800]
            )
            task["archived_rejected_fragment"] = {
                **data["rejected_task_submission"]["observations"][5],
                "source_ref": {
                    "evidence_id": data["evidence_id"],
                    "json_pointer": "/data/list",
                    "range": [0, 6],
                    "range_unit": "items",
                    "source_revision": 1,
                },
            }
            await b.repository.save(b.incident_id, "task", task)
        if case == "E3":
            b.model_steps = settings.WORKER_MAX_MODEL_STEPS - 2
            b.checkpoint_required = True
            task = await b.repository.read(b.incident_id, b.task_id, "task")
            task["investigation_goal"] = instructions[case]
            await b.repository.save(b.incident_id, "task", task)
        if case == "E4":
            q = (await b.repository.all(b.incident_id, "question"))[0]
            q["text"] += " " + instructions[case]
            await b.repository.save(b.incident_id, "question", q)
            b.local_model_limit = (
                settings.SUPERVISOR_MAX_READS
                + settings.SUPERVISOR_MAX_CORRECTIONS
                + 5
                + (settings.SKILL_READ_EXTRA_ROUNDS if skills else 0)
            )
        else:
            from workspace.tasks import start as start_task

            attempt = await start_task(b, b.task_id)
            b.attempt_id = attempt["id"]
        (output / "conditions.json").write_text(
            json.dumps(
                {
                    "case": case,
                    "skills": skills,
                    "model": settings.LLM_MODEL,
                    "instruction": instructions[case],
                    "source": data["provenance"],
                    "initial_model_steps": b.model_steps,
                    "worker_business_limit": settings.WORKER_MAX_MODEL_STEPS,
                    "knowledge_extra_max": settings.SKILL_READ_EXTRA_ROUNDS
                    if skills
                    else 0,
                    "deadline": b.deadline,
                    "mcp_available": False,
                    "cost": "unknown",
                    "comparison": "same actual model, source, tools and business allowance; skill increment explicitly separate",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        print(f"START {output.name}", flush=True)
        try:
            with bind(b):
                action = supervisor if case == "E4" else worker
                result = await asyncio.wait_for(
                    action(b, settings.get_chat_model(), state, catalog), b.remaining()
                )
                latest = {**state, **result}
                if case == "E4":
                    from workflow.gate import evidence_gate

                    latest.update(await evidence_gate(latest))
                (output / "role-result.json").write_text(
                    json.dumps(public_value(result), ensure_ascii=False, indent=2)
                )
        except BaseException as error:
            failure = {"type": type(error).__name__, "message": safe_message(error)}
            if isinstance(
                error, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
        finally:
            await asyncio.shield(
                export_run(
                    output,
                    output / "workspace",
                    latest,
                    failure,
                    elapsed_seconds=time.monotonic() - start,
                    mcp_calls=[],
                )
            )
            records = json.loads((output / "records.json").read_text())
            accepted = [
                r
                for r in records.get("model_action", [])
                if r["outcome"].get("status") == "accepted"
            ]
            outcome = {
                "case": case,
                "skills": skills,
                "failure": failure,
                "elapsed_seconds": time.monotonic() - start,
                "actual_requests": len(records.get("model_attempt", [])),
                "model_responses": len(records.get("model_response", [])),
                "knowledge_reads": len(records.get("skill_read", [])),
                "knowledge_rounds": len(records.get("skill_round", [])),
                "observation_ids": [r["id"] for r in records.get("observation", [])],
                "task_completed": any(
                    r["operation"] == "complete_task" for r in accepted
                ),
                "packet_selection_accepted": any(
                    r["operation"] == "expand_report_selection" for r in accepted
                ),
                "gate_passed": bool((latest or {}).get("final_report"))
                and not (latest or {}).get("final_report", {}).get("fallback", True),
                "token_usage_per_attempt": [
                    r.get("token_usage") for r in records.get("model_attempt", [])
                ],
                "root_cause_confirmed": False,
                "cost": "unknown",
                "remote_mcp_requests": 0,
            }
            outcome["goal_met"] = bool(
                outcome["packet_selection_accepted"] and outcome["gate_passed"]
                if case == "E4"
                else outcome["task_completed"]
                and (outcome["observation_ids"] if case == "E1" else True)
            )
            (output / "outcome.json").write_text(
                json.dumps(outcome, ensure_ascii=False, indent=2)
            )
            print(
                json.dumps(
                    {
                        k: v
                        for k, v in outcome.items()
                        if k != "token_usage_per_attempt"
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return failure


async def main(root, cases=("E1", "E3", "E4")):
    for case in cases:
        for skills in (True,) if case == "R3" else (False, True):
            failure = await run_case(root, case, skills)
            if failure and any(
                marker in failure["message"]
                for marker in (
                    "model_rate_limited",
                    "model_authentication_failed",
                    "investigation_deadline",
                )
            ):
                print(
                    "STOP further paid requests after provider/global stop", flush=True
                )
                return


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--cases",
        nargs="+",
        choices=("E1", "E3", "E4", "R3"),
        default=("E1", "E3", "E4"),
    )
    args = parser.parse_args()
    asyncio.run(main(args.output, args.cases))
