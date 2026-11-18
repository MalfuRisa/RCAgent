"""Check real structured submissions before starting a full RCA investigation.

    python local_testing/check_llm_compatibility.py --output /path/to/result.json

Uses synthetic data and the configured endpoint; makes no MCP/Kubernetes calls.
Does not change .env, service settings or model protocol after a failure.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import get_chat_model
from config.chat_model import RCAChatOpenAI
from models.schemas import (
    RCAAgentExplaination, RCATaskList, SupervisorDecision, SymptomList,
)

PROBES = (
    ("Triage", SymptomList, {"symptoms": [{
        "potential_symptom": "synthetic DNS failure", "resource_type": "pod",
        "affected_resource": "compatibility-test-pod", "evidence": "synthetic sample",
    }]}),
    ("Planner", RCATaskList, {"rca_tasks": [{
        "priority": 1, "status": "pending", "investigation_goal": "verify synthetic sample",
        "target_resource": "compatibility-test-pod", "resource_type": "pod",
        "suggested_tools": [],
    }]}),
    ("Worker summary", RCAAgentExplaination, {
        "steps": ["read synthetic sample"], "insights": ["no real incident investigated"],
    }),
    ("Supervisor", SupervisorDecision, {"tasks_to_be_executed": [], "final_report": {
        "root_cause": "synthetic compatibility check only", "affected_resources": [],
        "evidence_summary": "synthetic sample", "investigation_summary": "no MCP queries",
        "detection": False, "localization": [],
    }}),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("mini", "advanced", "both"), default="both")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")

    report = {"passed": True, "checks": [], "note": "Protocol check, not diagnosis accuracy"}
    roles = ("mini", "advanced") if args.role == "both" else (args.role,)
    seen = {}
    for role in roles:
        try:
            configured = get_chat_model(role)
            # Build a separate client: copying an existing model would retain
            # its SDK clients and their original timeout/retry configuration.
            model = RCAChatOpenAI(
                model=configured.model_name, api_key=configured.openai_api_key,
                base_url=configured.openai_api_base,
                structured_output_method=configured.structured_output_method,
                timeout=args.timeout, max_retries=0,
            )
        except Exception as exc:
            report["passed"] = False
            report["checks"].append({"role": role, "passed": False, "error_type": type(exc).__name__})
            continue
        identity = (model.model_name, str(model.openai_api_base), model.structured_output_method)
        if identity in seen:
            report["checks"].append({"role": role, "reuses_checks_for": seen[identity]})
            continue
        seen[identity] = role
        for stage, schema, expected in PROBES:
            started = time.perf_counter()
            item = {"role": role, "stage": stage, "model": model.model_name,
                    "method": model.structured_output_method, "passed": False}
            try:
                result = model.with_structured_output(schema).invoke([
                    ("system", "This is a synthetic protocol check. Submit the provided JSON "
                     "as a structured result. Do not investigate or call external tools."),
                    ("human", json.dumps(expected)),
                ])
                # Non-empty samples ensure an empty default-valued object is not
                # mistaken for a successful symptom/task submission.
                if result.model_dump() != schema.model_validate(expected).model_dump():
                    raise ValueError("Structured values differ from the synthetic sample")
                item["passed"] = True
            except Exception as exc:
                report["passed"] = False
                # Do not print SDK exception bodies, headers, credentials or prompts.
                item["error_type"] = type(exc).__name__
                status = getattr(exc, "status_code", None)
                if isinstance(status, int):
                    item["http_status"] = status
            item["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            report["checks"].append(item)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
