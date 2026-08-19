from __future__ import annotations

from agenticscrum.board_review import (
    _format_pending_for_notes,
    _format_rejected_comments_for_notes,
)
from agenticscrum.ingest import (
    proposal_dedupe_fingerprint,
    rejected_comment_dedupe_fingerprint,
)
from agenticscrum.models import ChangeType, ProposalStatus, ProposedChange, WorkItemType


def _proposal(**kwargs: object) -> ProposedChange:
    defaults: dict[str, object] = {
        "change_type": ChangeType.ASSIGN,
        "work_item_type": WorkItemType.PBI,
        "target_work_item_id": 123,
        "title": "Assign owner",
        "confidence_score": 80,
        "rationale": "needed",
        "source_quote": "assign me",
        "proposed_payload": {"newAssignee": "Ada Lovelace"},
        "status": ProposalStatus.PENDING,
    }
    defaults.update(kwargs)
    return ProposedChange(**defaults)  # type: ignore[arg-type]


def test_format_pending_for_notes_includes_target_and_details() -> None:
    text = _format_pending_for_notes(
        [
            _proposal(
                change_type=ChangeType.STATE_TRANSITION,
                title="Move to Committed",
                proposed_payload={"newState": "Committed"},
            )
        ]
    )
    assert "[StateTransition] #123" in text
    assert "state→Committed" in text
    assert "Move to Committed" in text


def test_format_pending_for_notes_empty() -> None:
    assert _format_pending_for_notes([]) == "(none)"


def test_assign_fingerprint_normalizes_assignee() -> None:
    left = proposal_dedupe_fingerprint(
        change_type=ChangeType.ASSIGN,
        target_work_item_id=10,
        title="Assign",
        payload={"newAssignee": "  Ada   Lovelace "},
    )
    right = proposal_dedupe_fingerprint(
        change_type=ChangeType.ASSIGN,
        target_work_item_id=10,
        title="Different title",
        payload={"newAssignee": "ada lovelace"},
    )
    assert left == right


def test_comment_fingerprint_collapses_same_target() -> None:
    left = proposal_dedupe_fingerprint(
        change_type=ChangeType.COMMENT,
        target_work_item_id=22,
        title="Comment A",
        payload={"commentText": "first"},
    )
    right = proposal_dedupe_fingerprint(
        change_type=ChangeType.COMMENT,
        target_work_item_id=22,
        title="Comment B",
        payload={"commentText": "second"},
    )
    assert left == right


def test_update_fingerprint_uses_field_keys() -> None:
    left = proposal_dedupe_fingerprint(
        change_type=ChangeType.UPDATE,
        target_work_item_id=5,
        title="Estimate",
        payload={"fieldUpdates": {"Microsoft.VSTS.Scheduling.StoryPoints": 3}},
    )
    right = proposal_dedupe_fingerprint(
        change_type=ChangeType.UPDATE,
        target_work_item_id=5,
        title="Estimate again",
        payload={"fieldUpdates": {"Microsoft.VSTS.Scheduling.StoryPoints": 8}},
    )
    assert left == right


def test_format_rejected_comments_includes_reason() -> None:
    text = _format_rejected_comments_for_notes(
        [
            _proposal(
                change_type=ChangeType.COMMENT,
                title="Noisy comment",
                status=ProposalStatus.REJECTED,
                proposed_payload={"commentText": "Please update the card"},
                rejection_reason="Too vague; do not post hygiene nags",
            )
        ]
    )
    assert "#123" in text
    assert "Please update the card" in text
    assert "Too vague; do not post hygiene nags" in text


def test_format_rejected_comments_empty() -> None:
    assert _format_rejected_comments_for_notes([]) == "(none)"


def test_rejected_comment_fingerprint_normalizes_text() -> None:
    left = rejected_comment_dedupe_fingerprint(
        target_work_item_id=9,
        title="Comment",
        payload={"commentText": "  Please   assign an owner "},
    )
    right = rejected_comment_dedupe_fingerprint(
        target_work_item_id=9,
        title="Comment",
        payload={"commentText": "please assign an owner"},
    )
    assert left == right
    assert left[0] == "RejectedComment"
