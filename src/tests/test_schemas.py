"""Tests for LLM output schema validation."""

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from agenticscrum.schemas import LLMOutputSchema


def valid_payload(confidence: int = 90, state: str = "Active") -> dict:
    return {
        "sourceMeeting": "Standup",
        "sourceMeetingDate": "2026-06-22",
        "sourceLoopUrl": None,
        "processedAt": datetime.now(timezone.utc).isoformat(),
        "proposedChanges": [
            {
                "changeType": "StateTransition",
                "workItemType": "PBI",
                "targetWorkItemId": 123,
                "title": "Move item",
                "confidenceScore": confidence,
                "rationale": "Explicitly discussed",
                "sourceQuote": "This is ready",
                "fieldUpdates": {},
                "newState": state,
            }
        ],
        "unmatchedDiscussion": [],
    }


def test_valid_schema() -> None:
    parsed = LLMOutputSchema.model_validate(valid_payload())
    assert parsed.proposed_changes[0].target_work_item_id == 123


def test_closure_requires_high_confidence() -> None:
    with pytest.raises(ValidationError):
        LLMOutputSchema.model_validate(valid_payload(confidence=70, state="Closed"))


def test_json_validation() -> None:
    parsed = LLMOutputSchema.model_validate_json(json.dumps(valid_payload()))
    assert parsed.source_meeting == "Standup"
