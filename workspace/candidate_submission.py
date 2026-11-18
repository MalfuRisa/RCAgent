"""Mechanical candidate selection and observation-stage construction.

Domain candidates, prechecks, commit keys, opening capture time and submission
routes remain caller-owned. This service neither invents facts nor refreshes a head.
"""


class CandidateSelectionError(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def select_candidates(offered, ids, *, id_field):
    lookup = {row[id_field]: row for row in offered}
    if not ids or len(set(ids)) != len(ids):
        raise CandidateSelectionError('empty_or_duplicate')
    if any(ref not in lookup for ref in ids):
        raise CandidateSelectionError('unknown')
    return [lookup[ref] for ref in ids]


async def submit_observations(selected, question_ids, opening, *, client_commit_key, stage_goal, commit):
    proposal = {'client_commit_key': client_commit_key, 'stage_goal': stage_goal,
        'completion': 'partial', 'question_ids': question_ids,
        'observation_proposals': selected, 'material_dispositions': [],
        'expected_checkpoint_id': (opening or {}).get('id'),
        'expected_revision': (opening or {}).get('revision')}
    return await commit(proposal)
