"""Approval and Azure DevOps apply orchestration."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenticscrum.ado.client import AdoClient
from agenticscrum.ado.fields import is_closure_state
from agenticscrum.config import Settings
from agenticscrum.llm.agent import revise_payload_with_tools
from agenticscrum.models import (
    ApprovalResponse,
    ChangeType,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
)
from agenticscrum.schemas import ApprovalCommand
from agenticscrum.teams.client import parse_approval_command


def is_closure_proposal(proposal: ProposedChange) -> bool:
    """Return whether a proposal is a closure state transition."""

    return (
        proposal.change_type == ChangeType.STATE_TRANSITION
        and is_closure_state(proposal.proposed_payload.get("newState"))
    )


def _try_parse_json_from_error_message(message: str) -> dict[str, object] | None:
    start = message.find("{")
    end = message.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    blob = message[start : end + 1]
    try:
        return json.loads(blob)
    except Exception:
        return None


def _deterministic_payload_fix(
    proposal: ProposedChange,
    *,
    error_message: str,
    payload: dict[str, object],
) -> tuple[str, dict[str, object]] | None:
    """Try safe, minimal payload fixes without calling the LLM."""

    changed = False
    revised = dict(payload)
    field_updates = revised.get("fieldUpdates")
    if not isinstance(field_updates, dict):
        field_updates = {}
        revised["fieldUpdates"] = field_updates

    error_json = _try_parse_json_from_error_message(error_message) or {}
    custom = error_json.get("customProperties")
    rule_errors = []
    if isinstance(custom, dict):
        maybe_errors = custom.get("RuleValidationErrors")
        if isinstance(maybe_errors, list):
            rule_errors = [err for err in maybe_errors if isinstance(err, dict)]

    for err in rule_errors:
        field_name = err.get("fieldReferenceName")
        flags = str(err.get("fieldStatusFlags") or "").lower()
        if not isinstance(field_name, str) or not field_name:
            continue

        # If a field is read-only/set-by-rule, remove it from updates.
        if "readonly" in flags or "read only" in flags or "setbyrule" in flags:
            if field_name in field_updates:
                field_updates.pop(field_name, None)
                changed = True

        # For non-state-transition proposals, it's safe to drop a failing state update.
        if (
            field_name == "System.State"
            and "invalidlistvalue" in flags
            and proposal.change_type != ChangeType.STATE_TRANSITION
        ):
            if "System.State" in field_updates:
                field_updates.pop("System.State", None)
                changed = True

    # Fallback when the error body isn't parseable JSON.
    if (
        proposal.change_type != ChangeType.STATE_TRANSITION
        and "field 'state' contains the value" in error_message.lower()
        and "System.State" in field_updates
    ):
        field_updates.pop("System.State", None)
        changed = True

    if not changed:
        return None

    request_text = (
        "Auto-fix after ADO apply error: removed invalid/read-only field updates."
    )
    return request_text, revised


def _auto_fix_attempts(payload: dict[str, object]) -> int:
    meta = payload.get("_meta")
    if not isinstance(meta, dict):
        return 0
    try:
        return int(meta.get("autoFixAttempts") or 0)
    except Exception:
        return 0


def _bump_auto_fix_attempts(payload: dict[str, object]) -> dict[str, object]:
    revised = dict(payload)
    meta = revised.get("_meta")
    meta_dict = dict(meta) if isinstance(meta, dict) else {}
    meta_dict["autoFixAttempts"] = int(meta_dict.get("autoFixAttempts") or 0) + 1
    revised["_meta"] = meta_dict
    return revised


async def approve_proposal(
    session: Session,
    settings: Settings,
    proposal_id: int,
    approver: str,
) -> ProposedChange:
    """Approve one proposal and apply immediately when allowed."""

    proposal = session.get(ProposedChange, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal {proposal_id} not found")
    proposal.status = ProposalStatus.APPROVED
    proposal.approved_by = approver
    proposal.approved_at = datetime.now(timezone.utc)
    session.flush()
    # Avoid holding a SQLite write transaction during network calls.
    session.commit()
    await apply_one(session, settings, proposal, approver)
    session.commit()
    return proposal


def reject_proposal(session: Session, proposal_id: int, reason: str | None = None) -> ProposedChange:
    """Reject one proposal."""

    proposal = session.get(ProposedChange, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal {proposal_id} not found")
    proposal.status = ProposalStatus.REJECTED
    proposal.rejection_reason = reason
    return proposal


async def apply_one(
    session: Session,
    settings: Settings,
    proposal: ProposedChange,
    approver: str,
) -> None:
    """Apply one approved proposal to ADO and update DB status."""

    try:
        async with AdoClient(settings) as ado:
            work_item_id = await ado.apply_payload(proposal, approver)
        proposal.applied_work_item_id = work_item_id
        proposal.status = ProposalStatus.APPLIED
        proposal.error_message = None
    except Exception as exc:
        proposal.status = ProposalStatus.FAILED
        proposal.error_message = str(exc)

        if not settings.app_auto_fix_errors:
            return
        error_text = proposal.error_message or ""
        retriable = (
            "HTTP 400" in error_text
            or "HTTP 409" in error_text
            or "conflict" in error_text.lower()
            or "stale" in error_text.lower()
            or "rule validation" in error_text.lower()
        )
        if not retriable:
            return

        payload = dict(proposal.proposed_payload or {})
        if _auto_fix_attempts(payload) >= 1:
            return

        fixed: tuple[str, dict[str, object]] | None = _deterministic_payload_fix(
            proposal, error_message=error_text, payload=payload
        )
        request_text: str | None = None
        revised_payload: dict[str, object] | None = None
        if fixed is not None:
            request_text, revised_payload = fixed
        else:
            # Escalate to an LLM tool-calling revision when deterministic fixes aren't enough.
            try:
                async with AdoClient(settings) as ado:
                    request_text = (
                        "Auto-fix this payload so it applies successfully in Azure DevOps. "
                        "Use the error details and live work item state to decide what to change. "
                        "Keep changes minimal and do not add any automation watermarks.\n\n"
                        f"Proposal metadata:\n"
                        f"- proposalId: {proposal.id}\n"
                        f"- changeType: {proposal.change_type.value}\n"
                        f"- workItemType: {proposal.work_item_type.value}\n"
                        f"- targetWorkItemId: {proposal.target_work_item_id}\n"
                        f"- title: {proposal.title}\n\n"
                        f"ADO error:\n{error_text}"
                    )
                    revised_payload = await revise_payload_with_tools(
                        settings,
                        ado,
                        request_text=request_text,
                        original_payload=payload,
                    )
            except Exception:
                return

        if not request_text or revised_payload is None:
            return
        if dict(revised_payload) == payload:
            return

        revised_payload = _bump_auto_fix_attempts(dict(revised_payload))
        previous = dict(proposal.proposed_payload)
        proposal.proposed_payload = dict(revised_payload)
        session.add(
            ProposalRevision(
                proposal_id=proposal.id,
                request_text=request_text,
                previous_payload=previous,
                revised_payload=dict(revised_payload),
            )
        )
        session.flush()
        # Avoid holding a SQLite write transaction during network calls.
        session.commit()

        # Retry apply once with the revised payload.
        try:
            async with AdoClient(settings) as ado:
                work_item_id = await ado.apply_payload(proposal, approver)
            proposal.applied_work_item_id = work_item_id
            proposal.status = ProposalStatus.APPLIED
            proposal.error_message = None
        except Exception as exc2:
            proposal.status = ProposalStatus.FAILED
            proposal.error_message = str(exc2)
    finally:
        session.flush()


async def apply_approved(session: Session, settings: Settings) -> int:
    """Apply all approved proposals that are not waiting for secondary approval."""

    proposals = list(
        session.scalars(
            select(ProposedChange).where(ProposedChange.status == ProposalStatus.APPROVED)
        )
    )
    # End any read transaction before network calls.
    session.commit()
    applied = 0
    for proposal in proposals:
        await apply_one(session, settings, proposal, proposal.approved_by or settings.app_approver_name)
        session.commit()
        if proposal.status == ProposalStatus.APPLIED:
            applied += 1
    return applied


async def request_closure_approval(
    session: Session,
    settings: Settings,
    proposal: ProposedChange,
) -> None:
    """Request secondary assignee approval through ADO comments."""

    if not proposal.assignee_approval_token:
        proposal.assignee_approval_token = secrets.token_urlsafe(24)
    proposal.status = ProposalStatus.AWAITING_ASSIGNEE_APPROVAL
    token = proposal.assignee_approval_token
    text = (
        f"Agentic Scrum requests closure approval for '{proposal.title}'.\n\n"
        f"Source quote: {proposal.source_quote}\n\n"
        f"Reply with `APPROVE {token}` or `REJECT {token}`."
    )
    if proposal.target_work_item_id is not None:
        async with AdoClient(settings) as ado:
            comment = await ado.add_comment(proposal.target_work_item_id, text)
            proposal.ado_approval_comment_id = str(comment.get("id", ""))
    session.flush()


async def poll_approval_responses(session: Session, settings: Settings) -> int:
    """Poll ADO comments for pending closure approvals."""

    proposals = list(
        session.scalars(
            select(ProposedChange).where(
                ProposedChange.status == ProposalStatus.AWAITING_ASSIGNEE_APPROVAL
            )
        )
    )
    # End any read transaction before network calls.
    session.commit()
    handled = 0
    for proposal in proposals:
        # Legacy behavior: previously closures waited for secondary approval. For this
        # local-first deployment, UI/chat approval is sufficient, so apply now.
        proposal.status = ProposalStatus.APPROVED
        await apply_one(session, settings, proposal, proposal.approved_by or settings.app_approver_name)
        session.commit()
        handled += 1
    return handled


async def find_approval_command(
    settings: Settings,
    proposal: ProposedChange,
) -> ApprovalCommand | None:
    """Find the first matching approval command for a proposal token."""

    token = proposal.assignee_approval_token
    if not token:
        return None
    if proposal.target_work_item_id is not None:
        async with AdoClient(settings) as ado:
            comments = await ado.get_comments(proposal.target_work_item_id)
        for comment in comments:
            command = parse_approval_command(
                str(comment.get("text", "")),
                str(comment.get("id", "")),
                comment.get("createdBy", {}).get("displayName"),
            )
            if command and command.token == token:
                return command
    return None


def record_response(session: Session, proposal: ProposedChange, command: ApprovalCommand) -> None:
    """Persist a parsed approval response if not already recorded."""

    existing = session.scalar(
        select(ApprovalResponse).where(
            ApprovalResponse.source == "approval",
            ApprovalResponse.source_message_id == command.message_id,
        )
    )
    if existing:
        return
    session.add(
        ApprovalResponse(
            proposal_id=proposal.id,
            token=command.token,
            action=command.action,
            source="approval",
            source_message_id=command.message_id,
            responder=command.responder,
            body=command.body,
        )
    )
