"""Export a fresh investigation even when its current node never yielded state."""

import json
from pathlib import Path
from workspace.repository import Repository
from workspace.model_requests import safe_message
from .preflight_v5_cpu import save


async def read_workspace(root, latest):
    repo = Repository(root)
    if not repo.database.exists():
        return (
            {},
            [],
            {
                "status": "workspace_not_created",
                "reason": "failure before incident initialization",
            },
        )

    def snapshot(db):
        incidents = [r[0] for r in db.execute("SELECT id FROM incidents")]
        incident = (latest or {}).get("incident_id")
        if incident is None and len(incidents) == 1:
            incident = incidents[0]
        if incident not in incidents:
            raise ValueError(
                "cannot uniquely locate the current incident in this fresh workspace"
            )
        records = {}
        for row in db.execute(
            "SELECT kind,body FROM records WHERE incident=? ORDER BY rowid", (incident,)
        ):
            records.setdefault(row["kind"], []).append(json.loads(row["body"]))
        runs = records.get("run", [])
        run_id = (latest or {}).get("run_id")
        if run_id is None and len(runs) == 1:
            run_id = runs[0]["id"]
        if run_id not in {r["id"] for r in runs}:
            raise ValueError(
                "cannot uniquely locate the current run; records retained without inventing a run"
            )
        events = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM events WHERE incident=? ORDER BY seq", (incident,)
            )
        ]
        return (
            records,
            events,
            {
                "status": "exported",
                "incident_id": incident,
                "run_id": run_id,
                "located_from": "latest_state"
                if (latest or {}).get("incident_id")
                else "persistent_workspace",
                "record_counts": {k: len(v) for k, v in records.items()},
            },
        )

    return await repo.transact(snapshot)


async def export_run(
    output,
    workspace,
    latest,
    failure,
    *,
    elapsed_seconds,
    mcp_calls,
    environment_verified=False,
):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    try:
        records, events, status = await read_workspace(workspace, latest)
    except Exception as error:
        records, events = {}, []
        status = {
            "status": "export_failed",
            "type": type(error).__name__,
            "reason": safe_message(error),
            "workspace": str(workspace),
            "note": "Empty export is not proof that the persistent workspace contains no evidence",
        }
    save(output / "export-status.json", status)
    save(output / "records.json", records)
    save(output / "events.json", events)
    save(output / "mcp-calls.json", mcp_calls)
    final = (latest or {}).get("final_report") or {}
    model_report = bool(
        final
        and final.get("audit_id")
        and records.get("report_packet")
        and records.get("report_proposal")
        and not final.get("fallback")
        and not final.get("report_errors")
    )
    attempts = records.get("model_attempt", [])
    contexts = records.get("context", [])
    skill_rows = records.get("skill_read", [])
    delivered = records.get("skill_delivery", [])
    delivered_ids = {
        r.get("read_id") for r in delivered if r.get("event") == "runtime_activated"
    }
    registered = [
        r
        for r in records.get("model_action", [])
        if r.get("operation") == "record_observations"
        and r.get("outcome", {}).get("status") == "accepted"
    ]
    first_registration = next(
        (
            i + 1
            for i, c in enumerate(contexts)
            if any(a.get("context_id") == c["id"] for a in registered)
        ),
        None,
    )
    summary = {
        "fresh_workspace": True,
        "failure": failure,
        "elapsed_seconds": elapsed_seconds,
        "export": status,
        "software_flow_completed": model_report,
        "requested_real_environment_verified": environment_verified,
        "evidence_backed_report": bool(model_report and final.get("statements")),
        "root_cause_confirmed": bool(
            model_report and final.get("answer_status") == "supported"
        ),
        "result_source": "model_report" if model_report else "program_partial_result",
        "model_attempts": len(attempts),
        "logical_steps": sum(
            r.get("logical_steps", 0) for r in records.get("role_budget", [])
        ),
        "role_budgets": records.get("role_budget", []),
        "first_registration_request_position": first_registration,
        "registration_accepted_calls": len(registered),
        "runtime_skill_prepared": len(
            [r for r in skill_rows if r.get("activation_source") == "runtime"]
        ),
        "runtime_skill_versions_sent": len(delivered_ids - {None}),
        "model_skill_reads": len(
            [
                r
                for r in skill_rows
                if r.get("activation_source", "model_read") == "model_read"
            ]
        ),
        "model_reference_reads": len(
            [r for r in skill_rows if r.get("activation_source") == "reference_read"]
        ),
        "mcp_calls": len(mcp_calls),
        "model_request_ms": sum(r.get("request_ms") or 0 for r in attempts),
        "model_queue_ms": sum(r.get("queue_ms") or 0 for r in attempts),
        "token_usage": [
            r.get("token_usage") if r.get("token_usage") is not None else "unknown"
            for r in attempts
        ],
        "cost": "unknown",
        "last_role": contexts[-1].get("role") if contexts else None,
        "last_phase": contexts[-1].get("phase") if contexts else None,
        "record_counts": {k: len(v) for k, v in records.items()},
        "timing_note": "request_ms includes unknown first-response/generation split; not inference-only time",
    }
    save(output / "run-analysis.json", summary)
    save(
        output / "context-composition.json",
        [
            {
                k: c.get(k)
                for k in (
                    "id",
                    "role",
                    "task_id",
                    "attempt_id",
                    "phase",
                    "request_chars_v2",
                    "component_chars",
                    "budget_steps",
                    "soft_target_expanded",
                    "history_replacements",
                    "exact_representation",
                    "recovery_view",
                    "read_retention_checks",
                    "coverage_three_ranges",
                    "rejection_reason",
                )
            }
            for c in contexts
        ],
    )
    lines = [
        "# 统一调查结果",
        "",
        "来源：" + summary["result_source"],
        "软件模型报告闭环：" + str(model_report),
        "工作区导出：" + status["status"],
        "最后阶段：" + str(summary["last_role"]) + "/" + str(summary["last_phase"]),
        "",
    ]
    if model_report:
        lines.extend(
            str(final.get(k) or "")
            for k in ("root_cause", "evidence_summary", "investigation_summary")
        )
    else:
        execution_error = (latest or {}).get("execution_error") or next(
            (
                row.get("execution_error")
                for row in records.get("run", [])
                if row.get("execution_error")
            ),
            None,
        )
        lines.extend(
            [
                "没有已接受的模型报告；以下仅为程序保留的部分结果，不能视为模型结论。",
                "错误：" + json.dumps(failure, ensure_ascii=False),
                "调查终止原因：" + json.dumps(execution_error, ensure_ascii=False),
                "\n## 已保留的状态\n",
                "| 记录类型 | 数量 |",
                "| --- | ---: |",
            ]
        )
        lines.extend(
            f"| {kind} | {count} |" for kind, count in summary["record_counts"].items()
        )
        lines.extend(
            [
                "\n## 工具原文目录\n",
                "这是已取回的原始返回目录，不能代替正式观测。完整原文保留在records.json及workspace/artifacts中。",
                "| evidence_id | 工具 | 传输/来源状态 |",
                "| --- | --- | --- |",
            ]
        )
        lines.extend(
            "| %s | %s | %s / %s |"
            % (
                row["id"],
                row.get("tool_name"),
                row.get("transport_status"),
                row.get("source_status"),
            )
            for row in records.get("evidence", [])
        )
        for kind, label in [
            ("question", "未决问题"),
            ("observation", "正式观测"),
            ("recovery_issue", "恢复/缺口"),
        ]:
            lines.append("\n## " + label + "\n")
            if not records.get(kind):
                lines.append("无。")
            for row in records.get(kind, []):
                description = (
                    row.get("text") or row.get("field") or row.get("code") or ""
                )
                lines.append("- " + row["id"] + "：" + str(description))
                if row.get("action"):
                    lines.append("  - 处置：" + str(row["action"]))
                if row.get("detail"):
                    lines.append("  - 说明：" + str(row["detail"]))
        lines.extend(
            [
                "\n## 结果边界\n",
                "未接受的模型稿、读取回执、正式观测和原始返回分别保留；没有正式观测或报告时不确认根因。",
                "逐次输入组成见context-composition.json，完整状态/事件见records.json、events.json和final-state.json（若存在）。",
            ]
        )
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
