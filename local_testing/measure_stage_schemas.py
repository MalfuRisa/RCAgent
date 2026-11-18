"""Compare actual SDK request schemas against this batch's preserved source baseline."""
import argparse
import json
import sys
import types
from pathlib import Path


def module(name, source, path):
    result = types.ModuleType(name)
    sys.modules[name] = result
    exec(compile(source, str(path), 'exec'), result.__dict__)
    return result


def run(baseline):
    from context.request_budget import structured_request, tool_schemas, serialized_chars
    from models import schemas
    from tools.rca_tools import submit_final_diagnosis
    from workspace.stage_tools import commit_investigation_stage
    from workspace.read_tools import READ_TOOLS
    from workspace.report_packets import prepare_report_packet
    model_source = baseline/'models/stage_proposals.py'
    module('_old_stage_contract', model_source.read_text(), model_source)
    schema_source = baseline/'models/schemas.py'
    old = module('models._baseline_schemas', schema_source.read_text().replace(
        'from .stage_proposals import', 'from _old_stage_contract import'), schema_source)
    tool_source = baseline/'workspace/stage_tools.py'
    old_tool = module('workspace._baseline_stage_tools', tool_source.read_text().replace(
        'from models.stage_proposals import', 'from _old_stage_contract import'), tool_source)
    final_source = baseline/'tools/rca_tools.py'
    old_final = module('tools._baseline_rca_tools', final_source.read_text(), final_source)
    before = {'triage': structured_request(old.SymptomList), 'planner': structured_request(old.RCATaskList),
        'worker': {'tools': tool_schemas(READ_TOOLS+[old_tool.commit_investigation_stage, old_final.submit_final_diagnosis])},
        'supervisor': {'tools': tool_schemas(READ_TOOLS+[old_tool.commit_investigation_stage, prepare_report_packet])},
        'supervisor_decision': structured_request(old.SupervisorDecision)}
    after = {'triage': structured_request(schemas.SymptomList), 'planner': structured_request(schemas.RCATaskList),
        'worker': {'tools': tool_schemas(READ_TOOLS+[commit_investigation_stage, submit_final_diagnosis])},
        'supervisor': {'tools': tool_schemas(READ_TOOLS+[commit_investigation_stage, prepare_report_packet])},
        'supervisor_decision': structured_request(schemas.SupervisorDecision)}
    return {role: {'before_request_definition_chars': serialized_chars(before[role]),
                   'after_request_definition_chars': serialized_chars(after[role]),
                   'delta': serialized_chars(after[role])-serialized_chars(before[role]),
                   'before': before[role], 'after': after[role]} for role in before}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(run(args.baseline), ensure_ascii=False, indent=2), encoding='utf-8')
