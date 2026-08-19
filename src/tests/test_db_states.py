"""Tests for proposal status transitions."""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from agenticscrum.models import (
    Base,
    ChangeType,
    ProposalStatus,
    ProposedChange,
    WorkItemType,
)


def test_proposal_status_transition() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        proposal = ProposedChange(
            source_meeting_title="Standup",
            source_meeting_date=date(2026, 6, 22),
            change_type=ChangeType.UPDATE,
            work_item_type=WorkItemType.PBI,
            target_work_item_id=1,
            title="Update title",
            confidence_score=80,
            rationale="Discussed",
            source_quote="Update it",
            proposed_payload={"fieldUpdates": {"System.Title": "Update title"}},
            status=ProposalStatus.PENDING,
        )
        session.add(proposal)
        session.commit()
        proposal.status = ProposalStatus.APPROVED
        session.commit()
        assert proposal.status == ProposalStatus.APPROVED
