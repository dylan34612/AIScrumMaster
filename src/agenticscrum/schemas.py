"""Pydantic schemas for LLM I/O and form payloads."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agenticscrum.models import ChangeType, WorkItemType


class UnmatchedDiscussion(BaseModel):
    """A meeting topic that was discussed but not actionable."""

    topic: str
    rationale: str


class ProposedChangeSchema(BaseModel):
    """A proposed Azure DevOps work item change produced by the LLM."""

    model_config = ConfigDict(populate_by_name=True)

    change_type: ChangeType = Field(alias="changeType")
    work_item_type: WorkItemType = Field(alias="workItemType")
    target_work_item_id: int | None = Field(default=None, alias="targetWorkItemId")
    title: str
    confidence_score: int = Field(alias="confidenceScore", ge=0, le=100)
    rationale: str
    source_quote: str = Field(alias="sourceQuote")
    field_updates: dict[str, Any] = Field(default_factory=dict, alias="fieldUpdates")
    new_state: str | None = Field(default=None, alias="newState")
    new_assignee: str | None = Field(default=None, alias="newAssignee")
    comment_text: str | None = Field(default=None, alias="commentText")
    parent_work_item_id: int | None = Field(default=None, alias="parentWorkItemId")
    parent_rationale: str | None = Field(default=None, alias="parentRationale")
    split_group_id: str | None = Field(default=None, alias="splitGroupId")
    split_from_work_item_id: int | None = Field(default=None, alias="splitFromWorkItemId")

    @field_validator("source_quote")
    @classmethod
    def source_quote_required(cls, value: str) -> str:
        """Require a source quote for every proposed change."""

        if not value.strip():
            raise ValueError("sourceQuote is required")
        return value

    @model_validator(mode="after")
    def validate_change(self) -> ProposedChangeSchema:
        """Validate change-type-specific invariants."""

        if self.change_type != ChangeType.CREATE and self.target_work_item_id is None:
            raise ValueError("targetWorkItemId is required unless changeType is Create")
        if self.change_type == ChangeType.STATE_TRANSITION and not self.new_state:
            raise ValueError("newState is required for StateTransition")
        if self.new_state and self.new_state.lower() in {"closed", "done"}:
            if self.confidence_score < 80:
                raise ValueError("Closure proposals require confidenceScore >= 80")
        if self.change_type == ChangeType.COMMENT and not self.comment_text:
            raise ValueError("commentText is required for Comment")
        return self

    def contains_estimate(self, fields: list[str]) -> bool:
        """Return whether the change updates configured estimation fields."""

        return any(field in self.field_updates for field in fields)


class LLMOutputSchema(BaseModel):
    """The top-level JSON object returned by the LLM."""

    model_config = ConfigDict(populate_by_name=True)

    source_meeting: str = Field(alias="sourceMeeting")
    source_meeting_date: date = Field(alias="sourceMeetingDate")
    source_loop_url: str | None = Field(default=None, alias="sourceLoopUrl")
    processed_at: datetime = Field(alias="processedAt")
    proposed_changes: list[ProposedChangeSchema] = Field(alias="proposedChanges")
    unmatched_discussion: list[UnmatchedDiscussion] = Field(
        default_factory=list, alias="unmatchedDiscussion"
    )


class ManualTranscriptRequest(BaseModel):
    """User-submitted manual transcript ingestion request."""

    title: str
    meeting_date: date
    notes: str


class ChangeRequestSchema(BaseModel):
    """Free-form request to revise an existing proposal."""

    proposal_id: int
    request_text: str


class ApprovalCommand(BaseModel):
    """Parsed command from ADO approval comments."""

    action: str
    token: str
    responder: str | None = None
    message_id: str
    body: str
