"""Soft undo helpers for autopilot / applied proposals."""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from agenticscrum.ado.fields import is_closure_state
from agenticscrum.config import Settings
from agenticscrum.models import (
    ChangeType,
    ChatProposalLink,
    ProposalStatus,
    ProposedChange,
    WorkItemType,
    utc_now,
)


def create_soft_undo_proposal(
    session: Session,
    settings: Settings,
    proposal: ProposedChange,
    *,
    chat_session_id: int | None = None,
) -> ProposedChange | None:
    """Create a compensating pending proposal (comment + optional reverse assign/state).

    True ADO rollback is out of scope; this stages a human-reviewable reverse change.
    """

    if proposal.status != ProposalStatus.APPLIED:
        return None

    payload = dict(proposal.proposed_payload or {})
    snapshot = payload.get("targetSnapshot") if isinstance(payload.get("targetSnapshot"), dict) else {}
    target_id = proposal.applied_work_item_id or proposal.target_work_item_id
    if target_id is None and proposal.change_type != ChangeType.CREATE:
        return None

    change_type = ChangeType.COMMENT
    new_payload: dict = {
        "commentText": (
            f"Soft undo requested for prior change '{proposal.title}'. "
            "Please review and reverse if needed."
        )
    }
    title = f"Soft undo: {proposal.title}"
    rationale = "Compensating proposal created from chat Undo (audit-friendly soft undo)."

    if proposal.change_type == ChangeType.ASSIGN:
        prior = snapshot.get("assignedTo") if snapshot else None
        if isinstance(prior, str) and prior.strip():
            change_type = ChangeType.ASSIGN
            new_payload = {"newAssignee": prior.strip(), "fieldUpdates": {}}
            title = f"Revert assignee on #{target_id}"
            rationale = f"Restore previous assignee ({prior}) after soft undo."
    elif proposal.change_type == ChangeType.STATE_TRANSITION:
        prior_state = snapshot.get("state") if snapshot else None
        new_state = str(payload.get("newState") or "")
        if isinstance(prior_state, str) and prior_state.strip() and not is_closure_state(new_state):
            change_type = ChangeType.STATE_TRANSITION
            new_payload = {"newState": prior_state.strip(), "fieldUpdates": {}}
            title = f"Revert state on #{target_id} to {prior_state}"
            rationale = f"Restore previous state ({prior_state}) after soft undo."

    undo = ProposedChange(
        ingestion_run_id=proposal.ingestion_run_id,
        ingested_meeting_id=proposal.ingested_meeting_id,
        source_meeting_title=f"Soft undo of #{proposal.id}",
        source_meeting_date=date.today(),
        change_type=change_type,
        work_item_type=proposal.work_item_type or WorkItemType.PBI,
        target_work_item_id=target_id,
        title=title[:500],
        confidence_score=70,
        rationale=rationale,
        source_quote=proposal.source_quote or proposal.title,
        proposed_payload=new_payload,
        status=ProposalStatus.PENDING,
        ingested_at=utc_now(),
    )
    session.add(undo)
    session.flush()
    if chat_session_id is not None:
        session.add(ChatProposalLink(session_id=chat_session_id, proposal_id=undo.id))
    return undo
