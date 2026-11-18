"""Bounded active finalization after a report request times out."""
import json
from pydantic import ValidationError
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from config import settings
from context.request_budget import tool_schemas
from workspace.repository import WorkspaceError
from workspace.runtime import invoke_model
from workspace.read_tools import CURRENT_TOOL_CALL
from workspace.stage_tools import commit_investigation_stage, new_segment_after_commit
from workspace.report_packets import prepare_report_packet
from .packet_submission import submit_packet
from workspace import candidate_submission
from workflow import tool_execution


async def copyable_fields(binding):
    """Mechanical paths/versions come from fields already visible, never guesses."""
    from workspace.observation_visibility import visible_projection_cards, correction_examples
    from workspace.observations import verify_observation, resolve_pointer
    from workspace.queries import read_raw
    if not binding.last_context_id:
        return []
    cards = await visible_projection_cards(binding)
    cards.sort(key=lambda row: row.get('tool_name') != 'get_pod_network_summary')
    examples = await correction_examples(binding, [{'source_ref': {
        'evidence_id': card['evidence_id'], 'json_pointer': '/summary'}}
        for card in cards if card.get('evidence_id')])
    # Read receipts can prove a field even when the default card is a directory.
    # Enumerate bounded, documented telemetry paths; verification below still
    # rejects every value not actually present in this owner's input/receipts.
    fields = {'get_pod_network_summary': ('rst_count', 'establish_fail_ratio', 'retrans_ratio', 'rtt_avg_ms'),
              'get_pod_application_summary': ('error_ratio', 'p99_ms', 'request_count')}
    precise = []
    for evidence in await binding.repository.all(binding.incident_id, 'evidence'):
        if evidence.get('run_id') != binding.run_id or evidence.get('tool_name') not in fields:
            continue
        if evidence.get('transport_status') != 'ok' or evidence.get('source_status') in ('error', 'not_found'):
            continue
        raw = await read_raw(binding, evidence['id'])
        for name in fields[evidence['tool_name']]:
            path = '/summary/' + name
            value, found, _ = resolve_pointer(raw, path)
            if found and not isinstance(value, (dict, list)):
                precise.append({'source_ref': {'evidence_id': evidence['id'], 'json_pointer': path},
                                'expected_value': value})
    examples = precise + examples
    candidates = []
    seen = set()
    for index, example in enumerate(examples):
        key = (example['source_ref']['evidence_id'], example['source_ref']['json_pointer'])
        if key in seen:
            continue
        proposal = {'local_id': 'report_fact_' + str(index + 1), **example}
        result = await verify_observation(binding, proposal, owner=binding.role)
        if result.get('accepted'):
            candidates.append({**proposal, 'source_ref': result['accepted']['source_ref'],
                'limitations': ['Exact visible recorded field; does not establish request-level causation']})
            seen.add(key)
            if len(candidates) == 5:
                break
    return candidates


async def recover_report(binding, model, rules, messages=()):
    """Register exact observations if needed, prepare a packet, then select it.

    No free-form report, weakened source check or program-selected model claim
    is accepted. All additional requests share the original binding/deadline.
    """
    examples = await copyable_fields(binding)
    from workspace.investigations import active_checkpoint, digest
    opening = await active_checkpoint(binding, binding.role)

    @tool
    async def register_report_observations(candidate_ids: list[str], question_ids: list[str]) -> dict:
        """Select visible verified candidate IDs for existing questions. Registers observations, never a cause or material coverage."""
        if not question_ids:
            return {'status': 'rejected', 'reason': 'select nonempty unique candidate IDs and existing question IDs'}
        try:
            selected = candidate_submission.select_candidates(examples, candidate_ids, id_field='local_id')
        except candidate_submission.CandidateSelectionError as exc:
            reason = ('select nonempty unique candidate IDs and existing question IDs'
                      if exc.reason == 'empty_or_duplicate' else 'unknown observation candidate')
            return {'status': 'rejected', 'reason': reason}
        return await candidate_submission.submit_observations(selected, question_ids, opening,
            client_commit_key='report-selection-' + digest([selected, question_ids])[:24],
            stage_goal='Register selected exact visible fields for the report; no causal conclusion',
            commit=commit_investigation_stage.ainvoke)

    tools = [register_report_observations if examples else commit_investigation_stage, prepare_report_packet]
    lookup = {offered.name: offered for offered in tools}
    reader = model.bind_tools(tools, parallel_tool_calls=False)
    visible = list(messages) or [rules]
    visible.append(HumanMessage(content=(
        'The general structured report request timed out. Finish through the active packet protocol. '
        'If no registered observations exist, commit a small stage using exact visible original '
        'JSON paths and values first. Then prepare_report_packet with observation_refs and an explicit '
        'unresolved reason for every question. Include opposing facts and limitations. '
        'Use only one offered tool per response. Never output a free-form FinalReport, '
        'request new telemetry or assert a cause without its mechanism.')))
    packet_contract = {
        'claim_fields': {'claim_id': 'unique local string',
            'type': ['observation', 'hypothesis', 'exclusion', 'causal'],
            'resource_id': 'exact verified resource ID from observation scope',
            'content': 'nonempty text; observation text is replaced by exact registered fields',
            'limitations': ['nonempty explicit limitation']},
        'observation_refs': 'map each claim_id to registered observation IDs, not evidence IDs',
        'question_dispositions': 'map every unresolved question ID to an explicit unresolved reason',
        'instruction': 'Use type=observation for recorded counters. Do not use statement, kind, '
            'supporting, symptom, context or unresolved as alternative claim fields/types. '
            'Evidence IDs are filled by the program from observation_refs. Do not claim causality.'}
    visible.append(HumanMessage(content=json.dumps({'packet_contract': packet_contract})))
    if examples:
        visible.append(HumanMessage(content=json.dumps({
            'copyable_observation_candidates': examples,
            'instructions': 'Call register_report_observations with relevant local_id values as candidate_ids '
                'and existing question_ids. The program supplies source_ref, original document, values and versions; '
                'do not retype metadata or invent material coverage. '
                'The program checked these exact visible fields against originals; they are candidates, '
                'not yet registered observations. A small partial stage with observations alone is sufficient.'},
            ensure_ascii=False)))
    limit = binding.model_steps + settings.SUPERVISOR_FINALIZATION_MAX_STEPS
    previous_limit = binding.local_model_limit
    binding.local_model_limit = min(limit, previous_limit) if previous_limit is not None else limit
    try:
        while binding.model_steps < binding.local_model_limit:
            # Bind the mechanical opening before each new model request, not
            # after receiving a proposal. A concurrent change is still rejected.
            opening = await active_checkpoint(binding, binding.role)
            answer = await invoke_model(reader, visible, phase='report_recovery',
                request={'tools': tool_schemas(tools), 'parallel_tool_calls': False})
            visible.append(answer)
            calls = getattr(answer, 'tool_calls', [])
            if len(calls) != 1 or calls[0]['name'] not in lookup:
                for call in calls:
                    visible.append(ToolMessage(content='Rejected: use exactly one offered finalization tool.',
                        tool_call_id=call['id'], name=call['name'], status='error'))
                visible.append(HumanMessage(content='Use one offered tool; prose and mixed calls do not submit a report.'))
                continue
            call = calls[0]
            try:
                result = await tool_execution.invoke_local(lookup[call['name']], call['args'], call['id'])
            except (WorkspaceError, ValidationError, ValueError) as exc:
                result = {'status': 'rejected', 'reason': str(exc)[:2000]}
            accepted = result.get('status') == 'accepted'
            visible.append(ToolMessage(content=json.dumps(result, ensure_ascii=False),
                tool_call_id=call['id'], name=call['name'], status='success' if accepted else 'error'))
            if not accepted:
                continue
            if call['name'] == 'prepare_report_packet':
                return await submit_packet(binding, model, rules, result['packet_id'])
            visible = await new_segment_after_commit(binding, result['checkpoint_id'], rules,
                messages=visible, extra={'next_action': 'Prepare the final report packet; preserve all unresolved directions.',
                    'packet_contract': packet_contract})
        raise WorkspaceError('report_recovery_budget_exhausted')
    finally:
        binding.local_model_limit = previous_limit
