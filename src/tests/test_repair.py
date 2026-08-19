from __future__ import annotations

import pytest

from agenticscrum.config import Settings
from agenticscrum.db import init_db, session_scope
from agenticscrum.models import (
    ChangeType,
    ProposalStatus,
    ProposedChange,
    WorkItemType,
)
from agenticscrum.repair import AgenticRetryResult, agentic_retry_proposal
from datetime import date


@pytest.mark.asyncio
async def test_agentic_retry_marks_auth_errors_unretriable(tmp_path) -> None:
    settings = Settings(app_db_path=str(tmp_path / "repair.db"), app_agentic_retry_max_loops=2)
    init_db(settings)
    with session_scope(settings) as session:
        proposal = ProposedChange(
            source_meeting_title="t",
            source_meeting_date=date.today(),
            change_type=ChangeType.ASSIGN,
            work_item_type=WorkItemType.PBI,
            target_work_item_id=1,
            title="Assign",
            confidence_score=80,
            rationale="r",
            source_quote="q",
            proposed_payload={"newAssignee": "a@b.com", "fieldUpdates": {}},
            status=ProposalStatus.FAILED,
            error_message="ADO apply failed HTTP 401 Unauthorized",
        )
        session.add(proposal)
        session.flush()
        proposal_id = proposal.id

    with session_scope(settings) as session:
        result = await agentic_retry_proposal(session, settings, proposal_id)
        assert isinstance(result, AgenticRetryResult)
        assert result.success is False
        assert result.unretriable is True
        assert result.loops_used == 0
        assert result.guidance is not None
