"""Autopilot helpers for more hands-off operation."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from agenticscrum.ado.fields import is_closure_state
from agenticscrum.apply import approve_proposal
from agenticscrum.config import Settings
from agenticscrum.judgements import compute_payload_hash, judgement_for_payload
from agenticscrum.models import ChangeType, ProposalJudgement, ProposalStatus, ProposedChange
from agenticscrum.proposal_judge import judge_and_persist


def _is_empty_field_updates(payload: dict) -> bool:
    value = payload.get("fieldUpdates")
    if value is None:
        return True
    if isinstance(value, dict):
        return len(value) == 0
    return False


def _is_placeholder_comment(text: object) -> bool:
    if not isinstance(text, str):
        return True
    cleaned = " ".join(text.strip().split())
    if not cleaned or len(cleaned) < 20:
        return True
    lowered = cleaned.lower()
    if "no details provided" in lowered:
        return True
    if lowered.startswith("captured meeting discussion"):
        return True
    if "agentic scrum" in lowered:
        return True
    return False


def autopilot_is_eligible(
    proposal: ProposedChange,
    judgement: ProposalJudgement | None,
    settings: Settings,
) -> bool:
    """Return whether a proposal may be auto-applied under smart autopilot rules."""

    if not settings.app_autopilot_enabled:
        return False
    if proposal.status != ProposalStatus.PENDING:
        return False
    if proposal.change_type in {ChangeType.CREATE, ChangeType.UPDATE}:
        return False

    payload = dict(proposal.proposed_payload or {})
    threshold = int(settings.app_autopilot_confidence_threshold)

    if proposal.change_type == ChangeType.COMMENT:
        if not settings.app_autopilot_allow_comments:
            return False
        comment_text = payload.get("commentText")
        if _is_placeholder_comment(comment_text):
            return False
        threshold = max(threshold, int(settings.app_autopilot_comment_min_confidence))
    elif proposal.change_type == ChangeType.ASSIGN:
        if not settings.app_autopilot_allow_assign:
            return False
        if not str(payload.get("newAssignee") or "").strip():
            return False
        if not _is_empty_field_updates(payload):
            return False
    elif proposal.change_type == ChangeType.STATE_TRANSITION:
        if not settings.app_autopilot_allow_state_transitions:
            return False
        if is_closure_state(str(payload.get("newState") or "")):
            return False
        if not _is_empty_field_updates(payload):
            return False
    else:
        return False

    if judgement is None or judgement.error_message:
        return False
    if not judgement.auto_apply_ok:
        return False
    if int(judgement.adjusted_confidence) < threshold:
        return False
    if str(judgement.risk_level).strip().lower() != "low":
        return False
    return True


async def _ensure_judgement(
    session: Session,
    settings: Settings,
    proposal: ProposedChange,
) -> ProposalJudgement | None:
    payload = dict(proposal.proposed_payload or {})
    payload_hash = compute_payload_hash(payload)
    judgement = judgement_for_payload(session, proposal.id, payload_hash)
    if judgement is None or judgement.error_message:
        judgement = await judge_and_persist(session, settings, proposal=proposal)
    return judgement


async def autopilot_apply_pending(
    session: Session,
    settings: Settings,
    *,
    proposal_ids: Iterable[int] | None = None,
) -> list[ProposedChange]:
    """Auto-approve/apply eligible pending proposals.

    Returns the list of proposals that reached APPLIED status.
    """

    if not settings.app_autopilot_enabled:
        return []

    max_apply = max(1, int(settings.app_autopilot_max_apply_per_cycle))
    approver = settings.app_autopilot_approver_name or "Autopilot"
    id_filter = set(int(x) for x in proposal_ids) if proposal_ids is not None else None

    query = (
        select(ProposedChange)
        .where(ProposedChange.status == ProposalStatus.PENDING)
        .order_by(desc(ProposedChange.confidence_score), desc(ProposedChange.ingested_at))
        .limit(max_apply * 5)
    )
    pending = list(session.scalars(query))
    if id_filter is not None:
        pending = [p for p in pending if p.id in id_filter]

    applied: list[ProposedChange] = []
    for proposal in pending:
        if len(applied) >= max_apply:
            break
        judgement = await _ensure_judgement(session, settings, proposal)
        if not autopilot_is_eligible(proposal, judgement, settings):
            continue
        await approve_proposal(session, settings, proposal.id, approver)
        session.refresh(proposal)
        if proposal.status == ProposalStatus.APPLIED:
            applied.append(proposal)

    return applied
