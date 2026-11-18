"""Only question-assignment changes, never arbitrary JSON Patch operations."""
from typing import Literal
from pydantic import Field, model_validator
from .stage_proposals import Contract, Nonempty, Positive


class QuestionAssignment(Contract):
    path: Nonempty
    value: Nonempty


class StageRepair(Contract):
    proposal_id: Nonempty
    proposal_version: Positive
    status: Literal['patched', 'cannot_repair']
    changes: list[QuestionAssignment] = Field(default_factory=list)
    reason: str | None = None

    @model_validator(mode='after')
    def result(self):
        if self.status == 'patched' and not self.changes:
            raise ValueError('patched needs changes')
        if self.status == 'cannot_repair' and (self.changes or not self.reason or not self.reason.strip()):
            raise ValueError('cannot_repair needs empty changes and nonempty reason')
        return self
