"""Tests for LLM output parsing helpers."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agenticscrum.llm.agent import (
    parse_llm_output,
    parse_llm_output_with_repair,
    repair_instruction,
)
from agenticscrum.llm.prompt import SYSTEM_PROMPT


def test_parse_noop_output_with_missing_metadata() -> None:
    raw = json.dumps(
        {
            "proposedChanges": [],
            "unmatchedDiscussion": [
                {"topic": "No actionable work item updates were discussed."}
            ],
        }
    )

    output = parse_llm_output(
        raw,
        meeting_title="Validation Transcript",
        meeting_date=date(2026, 6, 22),
    )

    assert output.source_meeting == "Validation Transcript"
    assert output.source_meeting_date == date(2026, 6, 22)
    assert output.proposed_changes == []
    assert output.unmatched_discussion[0].rationale


def test_parse_output_accepts_changes_alias() -> None:
    raw = json.dumps(
        {
            "sourceMeeting": "Standup",
            "sourceMeetingDate": "2026-06-23",
            "changes": [],
            "unmatchedDiscussion": [],
            "sourceLoopUrl": None,
        }
    )

    output = parse_llm_output(raw)
    assert output.source_meeting == "Standup"
    assert output.proposed_changes == []


def test_parse_output_normalizes_common_change_shape() -> None:
    raw = json.dumps(
        {
            "sourceMeeting": "Standup",
            "sourceMeetingDate": "2026-06-23",
            "changes": [
                {
                    "changeType": "Comment",
                    "workItemId": 156294,
                    "confidenceScore": 80,
                    "comment": "Investigated error in pipeline; next step is to update config and rerun nightly job.",
                    "sourceQuote": "Quote",
                },
                {
                    "changeType": "Create",
                    "confidenceScore": 70,
                    "newWorkItem": {
                        "type": "Product Backlog Item",
                        "title": "New PBI title",
                        "areaPath": "Demo Project",
                        "description": "Desc",
                        "acceptanceCriteria": "<APPEND>AC</APPEND>",
                        "effort": 5,
                    },
                    "sourceQuote": "Quote2",
                },
            ],
            "unmatchedDiscussion": [],
        }
    )

    output = parse_llm_output(raw)
    assert len(output.proposed_changes) == 2
    first = output.proposed_changes[0]
    assert first.target_work_item_id == 156294
    assert (
        first.comment_text
        == "Investigated error in pipeline; next step is to update config and rerun nightly job."
    )
    assert first.title
    assert first.rationale

    second = output.proposed_changes[1]
    assert second.work_item_type.value == "PBI"
    assert second.target_work_item_id is None
    assert second.title == "New PBI title"
    assert second.field_updates.get("System.AreaPath") == "Demo Project"
    assert second.field_updates.get("System.Title") == "New PBI title"


def test_parse_create_replaces_generic_title_from_source_quote() -> None:
    raw = json.dumps(
        {
            "sourceMeeting": "Weekly working Session",
            "sourceMeetingDate": "2026-07-06",
            "proposedChanges": [
                {
                    "changeType": "Create",
                    "workItemType": "PBI",
                    "confidenceScore": 76,
                    "fieldUpdates": {
                        "System.Title": "Create new work item",
                        "System.AreaPath": "Demo Project",
                    },
                    "sourceQuote": (
                        "We need a dashboard widget that shows ingestion run status "
                        "and proposal counts for the team."
                    ),
                }
            ],
            "unmatchedDiscussion": [],
        }
    )

    output = parse_llm_output(raw)
    create = output.proposed_changes[0]
    assert create.title != "Create new work item"
    assert "dashboard widget" in create.title.lower()
    assert create.field_updates.get("System.Title") == create.title


def test_parse_create_uses_system_title_when_descriptive() -> None:
    raw = json.dumps(
        {
            "sourceMeeting": "Standup",
            "sourceMeetingDate": "2026-07-06",
            "proposedChanges": [
                {
                    "changeType": "Create",
                    "workItemType": "PBI",
                    "confidenceScore": 80,
                    "fieldUpdates": {"System.Title": "Add ingestion health widget"},
                    "sourceQuote": "Quote",
                }
            ],
            "unmatchedDiscussion": [],
        }
    )

    output = parse_llm_output(raw)
    create = output.proposed_changes[0]
    assert create.title == "Add ingestion health widget"
    assert create.field_updates.get("System.Title") == "Add ingestion health widget"


def test_system_prompt_formatting_does_not_raise() -> None:
    rendered = SYSTEM_PROMPT.format(
        meeting_title="Inbox Transcript",
        meeting_date="2026-06-26",
        meeting_notes="Some notes",
        grounding_catalog_json="[]",
        team_roster_json="[]",
        effort_scale="1, 2, 3, 5, 8, 13",
        area_path="Demo Project",
    )
    assert "sourceMeeting" in rendered


def test_parse_output_normalizes_top_level_product_backlog_item() -> None:
    raw = json.dumps(
        {
            "sourceMeeting": "Standup",
            "sourceMeetingDate": "2026-06-23",
            "proposedChanges": [
                {
                    "changeType": "Create",
                    "workItemType": "Product Backlog Item",
                    "confidenceScore": 70,
                    "title": "Normalize type aliases",
                    "rationale": "LLM used ADO native type name",
                    "sourceQuote": "Create a PBI for type alias normalization",
                    "fieldUpdates": {"System.AreaPath": "Demo Project"},
                }
            ],
            "unmatchedDiscussion": [],
        }
    )

    output = parse_llm_output(raw)
    assert output.proposed_changes[0].work_item_type.value == "PBI"


def test_repair_instruction_includes_error_and_previous_response() -> None:
    message = repair_instruction(
        error="Input should be 'PBI' [type=enum, input_value='Product Backlog Item']",
        previous_response='{"proposedChanges":[]}',
    )
    assert isinstance(message, HumanMessage)
    assert "Product Backlog Item" in str(message.content)
    assert "[type=enum" in str(message.content)
    assert '{"proposedChanges":[]}' in str(message.content)


@pytest.mark.asyncio
async def test_parse_llm_output_with_repair_retries_until_valid() -> None:
    invalid = json.dumps(
        {
            "sourceMeeting": "Standup",
            "sourceMeetingDate": "2026-06-23",
            "proposedChanges": [
                {
                    "changeType": "Update",
                    "workItemType": "NotARealType",
                    "targetWorkItemId": 42,
                    "confidenceScore": 80,
                    "title": "Broken type",
                    "rationale": "Should be repaired",
                    "sourceQuote": "Fix the type",
                    "fieldUpdates": {},
                }
            ],
            "unmatchedDiscussion": [],
        }
    )
    valid = json.dumps(
        {
            "sourceMeeting": "Standup",
            "sourceMeetingDate": "2026-06-23",
            "proposedChanges": [
                {
                    "changeType": "Update",
                    "workItemType": "PBI",
                    "targetWorkItemId": 42,
                    "confidenceScore": 80,
                    "title": "Broken type",
                    "rationale": "Should be repaired",
                    "sourceQuote": "Fix the type",
                    "fieldUpdates": {},
                }
            ],
            "unmatchedDiscussion": [],
        }
    )

    class FakeModel:
        def __init__(self) -> None:
            self.ainvoke = AsyncMock(return_value=AIMessage(content=valid))

    model = FakeModel()
    messages: list[Any] = [HumanMessage(content="Analyze now.")]

    text, normalized, parsed = await parse_llm_output_with_repair(
        model,
        messages,
        invalid,
        meeting_title="Standup",
        meeting_date=date(2026, 6, 23),
        max_attempts=2,
    )

    assert text == valid
    assert parsed.proposed_changes[0].work_item_type.value == "PBI"
    assert normalized["proposedChanges"][0]["workItemType"] == "PBI"
    assert model.ainvoke.await_count == 1
    assert any(isinstance(item, HumanMessage) for item in messages[1:])


@pytest.mark.asyncio
async def test_parse_llm_output_with_repair_gives_up_after_max_attempts() -> None:
    invalid = "{not-json"

    class FakeModel:
        def __init__(self) -> None:
            self.ainvoke = AsyncMock(return_value=AIMessage(content="{still-not-json"))

    model = FakeModel()
    messages: list[Any] = [HumanMessage(content="Analyze now.")]

    with pytest.raises(json.JSONDecodeError):
        await parse_llm_output_with_repair(
            model,
            messages,
            invalid,
            meeting_title="Standup",
            meeting_date=date(2026, 6, 23),
            max_attempts=2,
        )

    assert model.ainvoke.await_count == 2
