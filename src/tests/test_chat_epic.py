from __future__ import annotations

from agenticscrum.autopilot import autopilot_is_eligible
from agenticscrum.chat_intent import ChatIntent, classify_intent_heuristic
from agenticscrum.chat_markdown import render_chat_markdown
from agenticscrum.config import Settings
from agenticscrum.db import init_db, session_scope
from agenticscrum.models import (
    ChangeType,
    ChatMessage,
    ChatRole,
    ChatSession,
    ProposalJudgement,
    ProposalStatus,
    ProposedChange,
    WorkItemType,
)
from agenticscrum.soft_undo import create_soft_undo_proposal
from datetime import date


def test_classify_intent_propose_and_review() -> None:
    assert classify_intent_heuristic("move #412 to Committed") == ChatIntent.PROPOSE
    assert classify_intent_heuristic("Review the board please") == ChatIntent.REVIEW
    assert classify_intent_heuristic("what's the status of WIP?") == ChatIntent.ANSWER


def test_chat_markdown_links_work_items() -> None:
    html = render_chat_markdown(
        "Please check **#412** and https://example.com/x",
        ado_org="org",
        ado_project="My Project",
    )
    assert "<strong>" in html
    assert "#412" in html
    assert "dev.azure.com/org" in html
    assert "example.com/x" in html


def test_autopilot_allows_hygiene_comment(tmp_path) -> None:
    settings = Settings(
        app_db_path=str(tmp_path / "t.db"),
        app_autopilot_enabled=True,
        app_autopilot_allow_comments=True,
        app_autopilot_comment_min_confidence=90,
        app_autopilot_confidence_threshold=90,
    )
    proposal = ProposedChange(
        source_meeting_title="t",
        source_meeting_date=date.today(),
        change_type=ChangeType.COMMENT,
        work_item_type=WorkItemType.PBI,
        target_work_item_id=1,
        title="Add note",
        confidence_score=95,
        rationale="r",
        source_quote="q",
        proposed_payload={
            "commentText": "Concrete decision: ship the API contract tomorrow and notify QA."
        },
        status=ProposalStatus.PENDING,
    )
    judgement = ProposalJudgement(
        proposal_id=1,
        payload_hash="x",
        auto_apply_ok=True,
        adjusted_confidence=96,
        risk_level="low",
        reasons=[],
        flags=[],
    )
    assert autopilot_is_eligible(proposal, judgement, settings) is True

    settings_off = Settings(
        app_db_path=str(tmp_path / "t2.db"),
        app_autopilot_enabled=True,
        app_autopilot_allow_comments=False,
    )
    assert autopilot_is_eligible(proposal, judgement, settings_off) is False


def test_autopilot_blocks_create_and_closure() -> None:
    settings = Settings(app_autopilot_enabled=True)
    create = ProposedChange(
        source_meeting_title="t",
        source_meeting_date=date.today(),
        change_type=ChangeType.CREATE,
        work_item_type=WorkItemType.PBI,
        title="New",
        confidence_score=99,
        rationale="r",
        source_quote="q",
        proposed_payload={},
        status=ProposalStatus.PENDING,
    )
    judgement = ProposalJudgement(
        proposal_id=1,
        payload_hash="x",
        auto_apply_ok=True,
        adjusted_confidence=99,
        risk_level="low",
        reasons=[],
        flags=[],
    )
    assert autopilot_is_eligible(create, judgement, settings) is False

    closure = ProposedChange(
        source_meeting_title="t",
        source_meeting_date=date.today(),
        change_type=ChangeType.STATE_TRANSITION,
        work_item_type=WorkItemType.PBI,
        target_work_item_id=9,
        title="Close",
        confidence_score=99,
        rationale="r",
        source_quote="q",
        proposed_payload={"newState": "Done", "fieldUpdates": {}},
        status=ProposalStatus.PENDING,
    )
    assert autopilot_is_eligible(closure, judgement, settings) is False


def test_soft_undo_creates_compensating_proposal(tmp_path) -> None:
    settings = Settings(app_db_path=str(tmp_path / "undo.db"))
    init_db(settings)
    with session_scope(settings) as session:
        chat = ChatSession(title="t")
        session.add(chat)
        session.flush()
        proposal = ProposedChange(
            source_meeting_title="t",
            source_meeting_date=date.today(),
            change_type=ChangeType.ASSIGN,
            work_item_type=WorkItemType.PBI,
            target_work_item_id=55,
            applied_work_item_id=55,
            title="Assign Alex",
            confidence_score=90,
            rationale="r",
            source_quote="q",
            proposed_payload={
                "newAssignee": "alex@example.com",
                "fieldUpdates": {},
                "targetSnapshot": {"assignedTo": "sam@example.com", "state": "Active"},
            },
            status=ProposalStatus.APPLIED,
        )
        session.add(proposal)
        session.flush()
        undo = create_soft_undo_proposal(
            session, settings, proposal, chat_session_id=chat.id
        )
        assert undo is not None
        assert undo.change_type == ChangeType.ASSIGN
        assert undo.proposed_payload.get("newAssignee") == "sam@example.com"
        assert undo.status == ProposalStatus.PENDING


def test_chat_message_kind_round_trip(tmp_path) -> None:
    settings = Settings(app_db_path=str(tmp_path / "chatmeta.db"))
    init_db(settings)
    with session_scope(settings) as session:
        chat = ChatSession(title="meta")
        session.add(chat)
        session.flush()
        session.add(
            ChatMessage(
                session_id=chat.id,
                role=ChatRole.ASSISTANT,
                content="hello",
                message_kind="board_review",
                message_meta={"items_scanned": 3},
            )
        )
    with session_scope(settings) as session:
        from sqlalchemy import select

        msg = session.scalar(select(ChatMessage))
        assert msg is not None
        assert msg.message_kind == "board_review"
        assert msg.message_meta["items_scanned"] == 3
