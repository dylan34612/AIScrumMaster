"""Agentic diagnose → plan → patch → verify repair for failed proposals."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from agenticscrum.ado.client import AdoClient
from agenticscrum.ado.fields import ASSIGNED_TO, STATE, is_closure_state
from agenticscrum.apply import approve_proposal
from agenticscrum.config import Settings
from agenticscrum.llm.agent import revise_payload_with_tools
from agenticscrum.models import (
    ChangeType,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
)


UNRETRIABLE_MARKERS = (
    "HTTP 401",
    "HTTP 403",
    "unauthorized",
    "access denied",
    "TF401232",  # work item deleted / not found style
)


@dataclass
class AgenticRetryResult:
    """Outcome of an agentic repair attempt."""

    proposal: ProposedChange
    success: bool
    unretriable: bool
    diagnosis: str
    loops_used: int
    guidance: str | None = None


def _is_unretriable(error_message: str | None) -> bool:
    text = (error_message or "").lower()
    if not text:
        return False
    return any(marker.lower() in text for marker in UNRETRIABLE_MARKERS)


def _reset_auto_fix_attempts(payload: dict[str, object]) -> dict[str, object]:
    revised = dict(payload)
    meta = revised.get("_meta")
    meta_dict = dict(meta) if isinstance(meta, dict) else {}
    meta_dict["autoFixAttempts"] = 0
    meta_dict["agenticRetryLoops"] = int(meta_dict.get("agenticRetryLoops") or 0)
    revised["_meta"] = meta_dict
    return revised


def _bump_agentic_loops(payload: dict[str, object]) -> dict[str, object]:
    revised = dict(payload)
    meta = revised.get("_meta")
    meta_dict = dict(meta) if isinstance(meta, dict) else {}
    meta_dict["agenticRetryLoops"] = int(meta_dict.get("agenticRetryLoops") or 0) + 1
    meta_dict["autoFixAttempts"] = 0
    revised["_meta"] = meta_dict
    return revised


def _agentic_loops(payload: dict[str, object]) -> int:
    meta = payload.get("_meta")
    if not isinstance(meta, dict):
        return 0
    try:
        return int(meta.get("agenticRetryLoops") or 0)
    except Exception:
        return 0


async def verify_applied_proposal(
    ado: AdoClient,
    proposal: ProposedChange,
) -> tuple[bool, str]:
    """Re-fetch the work item and confirm expected fields/state when possible."""

    work_item_id = proposal.applied_work_item_id or proposal.target_work_item_id
    if work_item_id is None:
        return True, "No work item id to verify."

    try:
        item = await ado.get_work_item(int(work_item_id))
    except Exception as exc:
        return False, f"Verification fetch failed: {exc}"

    fields = item.get("fields") if isinstance(item, dict) else None
    if not isinstance(fields, dict):
        return False, "Verification response missing fields."

    payload = dict(proposal.proposed_payload or {})
    mismatches: list[str] = []

    if proposal.change_type == ChangeType.STATE_TRANSITION:
        expected = str(payload.get("newState") or "").strip()
        actual = str(fields.get(STATE) or "").strip()
        if expected and actual.lower() != expected.lower():
            mismatches.append(f"state expected '{expected}' got '{actual}'")

    if proposal.change_type == ChangeType.ASSIGN:
        expected = str(payload.get("newAssignee") or "").strip().lower()
        assigned = fields.get(ASSIGNED_TO)
        if isinstance(assigned, dict):
            actual = str(
                assigned.get("uniqueName") or assigned.get("displayName") or ""
            ).strip().lower()
        else:
            actual = str(assigned or "").strip().lower()
        if expected and expected not in actual and actual not in expected:
            mismatches.append(f"assignee expected '{expected}' got '{actual}'")

    if mismatches:
        return False, "; ".join(mismatches)
    return True, "Verified against live work item."


async def agentic_retry_proposal(
    session: Session,
    settings: Settings,
    proposal_id: int,
    *,
    approver: str | None = None,
) -> AgenticRetryResult:
    """Diagnose a failed proposal, revise the payload with ADO tools, and re-apply.

    Never blindly re-runs an unchanged failed payload.
    """

    proposal = session.get(ProposedChange, proposal_id)
    if proposal is None:
        raise ValueError(f"Proposal {proposal_id} not found")

    who = approver or settings.app_approver_name
    max_loops = max(1, int(settings.app_agentic_retry_max_loops))
    original_error = proposal.error_message or "No error message recorded."

    if _is_unretriable(original_error):
        guidance = (
            "This failure looks like an auth/permissions or missing-work-item issue. "
            "Check ADO credentials and that the target work item still exists."
        )
        return AgenticRetryResult(
            proposal=proposal,
            success=False,
            unretriable=True,
            diagnosis=original_error,
            loops_used=0,
            guidance=guidance,
        )

    if proposal.status not in {
        ProposalStatus.FAILED,
        ProposalStatus.APPROVED,
        ProposalStatus.PENDING,
    }:
        return AgenticRetryResult(
            proposal=proposal,
            success=proposal.status == ProposalStatus.APPLIED,
            unretriable=False,
            diagnosis=f"Proposal is {proposal.status.value}; nothing to repair.",
            loops_used=0,
        )

    loops_used = 0
    last_diagnosis = original_error

    while loops_used < max_loops:
        payload = _reset_auto_fix_attempts(dict(proposal.proposed_payload or {}))
        previous = dict(payload)

        diagnosis_bits = [
            "Agentic repair: diagnose the ADO apply failure, fetch live work item state,",
            "choose a minimal payload fix, and return ONLY revised payload JSON.",
            "If the failure cannot be fixed safely, keep the payload identical and we will stop.",
            "",
            f"Proposal id: {proposal.id}",
            f"changeType: {proposal.change_type.value}",
            f"workItemType: {proposal.work_item_type.value}",
            f"targetWorkItemId: {proposal.target_work_item_id}",
            f"title: {proposal.title}",
            "",
            f"ADO error:\n{proposal.error_message or original_error}",
        ]
        if proposal.change_type == ChangeType.STATE_TRANSITION and is_closure_state(
            str(payload.get("newState") or "")
        ):
            diagnosis_bits.append(
                "Note: this is a closure transition; only change newState to a valid allowed state."
            )

        request_text = "\n".join(diagnosis_bits)
        try:
            async with AdoClient(settings) as ado:
                revised_payload = await revise_payload_with_tools(
                    settings,
                    ado,
                    request_text=request_text,
                    original_payload=payload,
                )
        except Exception as exc:
            return AgenticRetryResult(
                proposal=proposal,
                success=False,
                unretriable=False,
                diagnosis=f"Repair LLM failed: {exc}",
                loops_used=loops_used,
                guidance="Try again later or edit the payload manually.",
            )

        if not isinstance(revised_payload, dict):
            return AgenticRetryResult(
                proposal=proposal,
                success=False,
                unretriable=False,
                diagnosis="Repair agent did not return a JSON object.",
                loops_used=loops_used,
            )

        # Strip identity-equal payloads (no actual fix).
        comparable_prev = {k: v for k, v in previous.items() if k != "_meta"}
        comparable_new = {k: v for k, v in revised_payload.items() if k != "_meta"}
        if comparable_new == comparable_prev and loops_used == 0:
            # First loop with no change: still attempt apply once after resetting status,
            # but only if payload previously failed for transient reasons. Prefer stop.
            last_diagnosis = (
                "Repair agent could not find a safe payload change. "
                f"Original error: {original_error}"
            )
            return AgenticRetryResult(
                proposal=proposal,
                success=False,
                unretriable=False,
                diagnosis=last_diagnosis,
                loops_used=loops_used,
                guidance="Edit the proposal payload or reject it.",
            )

        revised_payload = _bump_agentic_loops(dict(revised_payload))
        loops_used = _agentic_loops(revised_payload)
        last_diagnosis = request_text

        proposal.proposed_payload = revised_payload
        proposal.status = ProposalStatus.PENDING
        proposal.error_message = None
        session.add(
            ProposalRevision(
                proposal_id=proposal.id,
                request_text=request_text[:8000],
                previous_payload=previous,
                revised_payload=dict(revised_payload),
            )
        )
        session.flush()
        session.commit()

        await approve_proposal(session, settings, proposal.id, who)
        session.refresh(proposal)

        if proposal.status == ProposalStatus.APPLIED:
            try:
                async with AdoClient(settings) as ado:
                    ok, verify_msg = await verify_applied_proposal(ado, proposal)
            except Exception as exc:
                ok, verify_msg = False, str(exc)
            if not ok:
                proposal.status = ProposalStatus.FAILED
                proposal.error_message = f"Applied but verification failed: {verify_msg}"
                session.flush()
                session.commit()
                last_diagnosis = proposal.error_message
                continue
            return AgenticRetryResult(
                proposal=proposal,
                success=True,
                unretriable=False,
                diagnosis=f"Repaired and verified. {verify_msg}",
                loops_used=loops_used,
            )

        if _is_unretriable(proposal.error_message):
            return AgenticRetryResult(
                proposal=proposal,
                success=False,
                unretriable=True,
                diagnosis=proposal.error_message or last_diagnosis,
                loops_used=loops_used,
                guidance="Auth/permissions issue — fix credentials or access, then retry.",
            )

        last_diagnosis = proposal.error_message or last_diagnosis

    return AgenticRetryResult(
        proposal=proposal,
        success=False,
        unretriable=False,
        diagnosis=last_diagnosis,
        loops_used=loops_used,
        guidance=f"Exhausted {max_loops} repair loops. Edit manually or reject.",
    )


# Re-export for callers that expect apply helpers nearby.
__all__ = [
    "AgenticRetryResult",
    "agentic_retry_proposal",
    "verify_applied_proposal",
]
