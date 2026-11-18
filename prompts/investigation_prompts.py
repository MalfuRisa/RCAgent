"""Stage protocol shared by roles, separate from user-customized role requirements."""
STAGE_PROTOCOL = '''
Active investigation protocol:
Return stage_proposal with nonempty question_ids. Declare new questions using local_id/text/
resource_ids/origin_refs. Give each observation local_id (o1), source_ref.evidence_id,
original json_pointer and exact expected_value. Claim only visible facts.
Original paths: source.rst_count=summary.rst_count -> /summary/rst_count;
summary.anomalies -> /summary/anomalies/0/metric; candidates=pods -> /pods/0/pod_uid.
Display wrappers (observations/items/candidates), shown/total and previews are not raw fields.
Representative rows use source_index. Judgments use content, limitations=[str] and supporting/
opposing_observation_ids=["o1"] or registered observation IDs, never evidence_id:path.
Keep unknowns in open_items/next_focus with completion=partial. Copy ONLY checkpoint_id/revision
into expected_checkpoint_id/expected_revision; first stage uses null/null, never workspace_revision.
Counters and failed lookups do not prove causation.
'''

WORKER_STAGES = '''
Work in small stages. Use commit_investigation_stage ALONE before accumulating many read pages.
List a material_disposition for every source/field read: covered_by (verified observations only),
active, unanalyzed, or out_of_scope with a reason. Do not mark unread material as analyzed.
For a partial page, observations can propose source_ref.range=[start,end], range_unit=items|chars,
json_pointer to the original field and expected_value equal to the exact shown items/text.
Use the same range and unit in material_dispositions. A tail page covers only that range.
Register important counters/contradictions separately as small field observations before handing off pages.
The tool returns official IDs. Commit does not finish the task or reset any budget.
On checkpoint_required, no more queries/reads: submit a valid partial stage or an unresolved final answer.
Use read_investigation to inspect saved facts and read_evidence to recheck an exact original field.
Final submission should carry any new, uncommitted stage_proposal and state remaining acceptance gaps.
'''

SUPERVISOR_STAGES = '''
First review the global direction/task overview, including unassigned evidence and failed tasks.
Review directions in bounded batches using only local reads. New remote information requires follow-up tasks.
Save review stages using commit_investigation_stage ALONE. Preserve opposing observations and limitations.
Before a final report, prepare_report_packet with claims and observation_refs mapping each claim_id
to registered observation IDs. If observations are missing, read their exact evidence fields and commit
them first. Provide a reason in question_dispositions for every direction not reviewed in a saved stage.
Observation wording is generated from verified field values; hypotheses remain qualified interpretations.
When the packet is accepted, select its statement IDs in the final short response. Never invent report text.
'''


def active_protocol(role):
    from config import settings
    if settings.CONTEXT_LIFECYCLE_MODE != 'active':
        return ''
    entry = ('\nIf no question exists, declare question_proposals=[{{"local_id":"q1","text":"What remains unknown?"}}] '
             'and question_ids=["q1"]. Leave material_dispositions=[] for summary cards; they stay active.\n') if role in ('triage', 'planner') else ''
    return STAGE_PROTOCOL + entry + {'worker': WORKER_STAGES, 'supervisor': SUPERVISOR_STAGES,
                            'planner': '\nBind every task to question_ids, known_observation_ids, candidate_judgment_ids '
                                       'and acceptance_checks for support, opposition and missing data.\n'}.get(role, '')


def role_instructions(chosen, default, role):
    """Keep custom requirements; replace obsolete default prose in the new path."""
    from config import settings
    if settings.CONTEXT_LIFECYCLE_MODE != 'active':
        return chosen
    defaults = {
        'worker': 'Investigate only this task, registered identity and time window. Use evidence to test explanations. '
                  'Preserve unknowns, opposition and missing data. A finished task may remain inconclusive.',
        'supervisor': 'Review all incident directions and task outcomes. Compare scope, identity, time and source '
                      'completeness before explanations. Only the existing OOM causal rule can confirm a cause.'}
    base = defaults.get(role, chosen) if chosen == default else chosen
    return base + active_protocol(role)
