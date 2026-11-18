"""Single model-visible contract shared by tools, role outputs and runtime."""
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints, model_validator

Nonempty = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r'\S')]
Positive = Annotated[StrictInt, Field(gt=0)]
QuestionState = Literal['open', 'partially_answered', 'answered', 'blocked', 'out_of_scope']
JudgmentState = Literal['proposed', 'supported', 'refuted', 'unresolved', 'withdrawn']
DispositionState = Literal['covered_by', 'active', 'unanalyzed', 'out_of_scope']
Completion = Literal['complete', 'partial']


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ObservationSourceRef(Contract):
    evidence_id: Nonempty | None = None
    artifact_id: Nonempty | None = None
    json_pointer: str = ''
    source_revision: Positive | None = None
    source_hash: Nonempty | None = None
    range: list[StrictInt] | None = None
    range_unit: Literal['items', 'chars'] | None = None

    @model_validator(mode='after')
    def source_required(self):
        if not self.evidence_id and not self.artifact_id:
            raise ValueError('source_ref needs evidence_id or artifact_id')
        if self.range is not None and (len(self.range) != 2 or not 0 <= self.range[0] <= self.range[1]
                                       or self.range_unit is None):
            raise ValueError('range needs [start,end] and range_unit')
        return self


class TextReference(Contract):
    char_range: list[StrictInt]
    source_ref: ObservationSourceRef | None = None


class Aggregate(Contract):
    operation: Literal['count', 'distinct_count']
    json_pointer: str = ''
    start_index: Annotated[StrictInt, Field(ge=0)] = 0
    end_index: Annotated[StrictInt, Field(ge=0)] | None = None


class ObservationProposal(Contract):
    local_id: Nonempty
    question_ids: list[Nonempty] = Field(default_factory=list)
    source_ref: ObservationSourceRef
    source_document: Literal['artifact', 'evidence'] = 'artifact'
    expected_value: Any = None  # original JSON; fields_set distinguishes missing/null
    text_ref: TextReference | None = None
    fragments: list[Nonempty] = Field(default_factory=list)
    aggregate: Aggregate | None = None
    text: str | None = None
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode='after')
    def branch(self):
        if self.text_ref and self.aggregate:
            raise ValueError('text_ref and aggregate are mutually exclusive')
        if self.text_ref and not self.fragments:
            raise ValueError('text_ref requires exact fragments')
        if not self.text_ref and not self.aggregate and 'expected_value' not in self.model_fields_set:
            raise ValueError('field/range observation requires explicit expected_value (including null)')
        return self


class QuestionProposal(Contract):
    local_id: Nonempty | None = None
    text: Nonempty
    parent_question_id: Nonempty | None = None
    resource_ids: list[Nonempty] = Field(default_factory=list)
    scope: dict = Field(default_factory=dict, description='Descriptive JSON scope, not evidence authority')
    origin_refs: list[Nonempty] = Field(default_factory=list)
    owner_task_ids: list[Nonempty] = Field(default_factory=list)
    status: QuestionState = 'open'


class JudgmentProposal(Contract):
    local_id: Nonempty | None = None
    question_id: Nonempty
    content: Nonempty
    type: Nonempty = 'interpretation'
    status: JudgmentState = 'proposed'
    supporting_observation_ids: list[Nonempty] = Field(default_factory=list)
    opposing_observation_ids: list[Nonempty] = Field(default_factory=list)
    evidence_refs: list[Nonempty] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class MaterialDisposition(Contract):
    source_id: Nonempty
    disposition: DispositionState
    field: str | None = None
    source_version: Positive | None = None
    range: list[StrictInt] | None = None
    range_unit: Literal['items', 'chars'] | None = None
    covered_by: list[Nonempty] = Field(default_factory=list)
    reason: str | None = None


class StageProposal(Contract):
    client_commit_key: Nonempty
    stage_goal: Nonempty
    completion: Completion = 'partial'
    expected_checkpoint_id: Nonempty | None = None
    expected_revision: Annotated[StrictInt, Field(ge=0)] | None = None
    question_ids: list[Nonempty] = Field(min_length=1)
    question_proposals: list[QuestionProposal] = Field(default_factory=list)
    observation_proposals: list[ObservationProposal] = Field(default_factory=list)
    judgments: list[JudgmentProposal] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict, description='Descriptive JSON, not authoritative coverage')
    open_items: list[str] = Field(default_factory=list)
    next_focus: list[str] = Field(default_factory=list)
    material_dispositions: list[MaterialDisposition] = Field(default_factory=list)


def field_errors(error):
    return [{'path': '/' + '/'.join(map(str, r['loc'])), 'code': r['type'], 'message': r['msg'],
             'field': '.'.join(map(str, r['loc'])), 'reason': r['msg']}
            for r in error.errors(include_input=False, include_url=False)]
