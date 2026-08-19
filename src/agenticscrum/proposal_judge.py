"""Run and persist proposal judgements."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agenticscrum.ado.client import AdoClient
from agenticscrum.config import Settings
from agenticscrum.judgements import compute_payload_hash, judgement_for_payload
from agenticscrum.llm.agent import refine_payload_with_tools
from agenticscrum.llm.judge import LLMJudgeError, judge_proposal
from agenticscrum.models import ProposalJudgement, ProposalRevision, ProposalStatus, ProposedChange, ToolCallLog


async def judge_and_persist(
    session: Session,
    settings: Settings,
    *,
    proposal: ProposedChange,
    policy_version: str = "v1",
    ado: AdoClient | None = None,
) -> ProposalJudgement:
    """Judge a proposal and persist the result (deduped by payload hash)."""

    payload = dict(proposal.proposed_payload or {})
    payload_hash = compute_payload_hash(payload)

    existing = judgement_for_payload(session, proposal.id, payload_hash)
    if existing is not None and not existing.error_message:
        return existing

    async def _run(ado_client: AdoClient) -> ProposalJudgement:
        try:
            result = await judge_proposal(settings=settings, ado=ado_client, proposal=proposal)
            for record in result.tool_calls:
                session.add(
                    ToolCallLog(
                        ingestion_run_id=proposal.ingestion_run_id,
                        proposal_id=proposal.id,
                        tool_name=record.tool_name,
                        input_payload=record.input_payload,
                        output_payload=record.output_payload,
                        duration_ms=record.duration_ms,
                    )
                )
            row = ProposalJudgement(
                proposal_id=proposal.id,
                payload_hash=payload_hash,
                policy_version=policy_version,
                model_name=settings.llm_model,
                auto_apply_ok=bool(result.output.auto_apply_ok),
                adjusted_confidence=int(result.output.adjusted_confidence),
                risk_level=str(result.output.risk_level),
                reasons=list(result.output.reasons),
                flags=list(result.output.flags),
                request_prompt=result.request_prompt,
                response_text=result.raw_response_text,
                normalized_json=result.normalized_json,
                error_message=None,
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.flush()
            # Avoid holding a SQLite write transaction during network calls elsewhere.
            session.commit()
            return row
        except LLMJudgeError as exc:
            # Persist the failure so we don't hammer the judge repeatedly.
            row = ProposalJudgement(
                proposal_id=proposal.id,
                payload_hash=payload_hash,
                policy_version=policy_version,
                model_name=settings.llm_model,
                auto_apply_ok=False,
                adjusted_confidence=0,
                risk_level="high",
                reasons=[],
                flags=["judge_error"],
                request_prompt=exc.request_prompt,
                response_text=exc.raw_response_text,
                normalized_json=exc.normalized_json,
                error_message=str(exc),
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.flush()
            session.commit()
            return row

    if ado is not None:
        return await _run(ado)
    async with AdoClient(settings) as ado_client:
        return await _run(ado_client)


async def judge_pending_proposals(
    session: Session,
    settings: Settings,
    *,
    limit: int = 25,
    min_confidence: int = 0,
) -> int:
    """Judge pending proposals that do not yet have a judgement for current payload."""

    pending = list(
        session.scalars(
            select(ProposedChange)
            .where(
                ProposedChange.status == ProposalStatus.PENDING,
                ProposedChange.confidence_score >= int(min_confidence),
            )
            .order_by(ProposedChange.ingested_at.desc())
            .limit(limit)
        )
    )
    session.commit()
    judged = 0
    for proposal in pending:
        payload_hash = compute_payload_hash(dict(proposal.proposed_payload or {}))
        existing = judgement_for_payload(session, proposal.id, payload_hash)
        if existing is not None and not existing.error_message:
            continue
        await judge_and_persist(session, settings, proposal=proposal)
        judged += 1
    return judged


async def judge_and_refine_proposal(
    session: Session,
    settings: Settings,
    *,
    proposal: ProposedChange,
    policy_version: str = "v1",
    ado: AdoClient | None = None,
) -> ProposalJudgement:
    """Judge a proposal; if needed, refine its payload and re-judge a few times."""

    loops = max(0, int(settings.app_auto_refine_max_loops))
    latest = await judge_and_persist(
        session, settings, proposal=proposal, policy_version=policy_version, ado=ado
    )

    if not settings.app_auto_refine_enabled or loops <= 0:
        return latest

    # Only refine pending proposals (never touch already approved/applied).
    if proposal.status != ProposalStatus.PENDING:
        return latest

    def should_refine(j: ProposalJudgement) -> bool:
        if j.error_message:
            return True
        if j.risk_level and str(j.risk_level).lower() == "high":
            return True
        # If judge explicitly flags clarity/inaccuracy, refine.
        flags = set(str(f).lower() for f in (j.flags or []))
        if "unclear_comment" in flags or "inaccurate" in flags or "needs_clarification" in flags:
            return True
        # For comment proposals, require at least medium confidence.
        if proposal.change_type.value == "Comment" and int(j.adjusted_confidence) < 70:
            return True
        return False

    for _i in range(loops):
        if not should_refine(latest):
            break

        payload = dict(proposal.proposed_payload or {})
        proposal_meta = {
            "proposalId": proposal.id,
            "changeType": proposal.change_type.value,
            "workItemType": proposal.work_item_type.value,
            "targetWorkItemId": proposal.target_work_item_id,
            "title": proposal.title,
            "rationale": proposal.rationale,
        }
        judge_meta = {
            "autoApplyOk": bool(latest.auto_apply_ok),
            "adjustedConfidence": int(latest.adjusted_confidence),
            "riskLevel": str(latest.risk_level),
            "reasons": list(latest.reasons or []),
            "flags": list(latest.flags or []),
            "errorMessage": latest.error_message,
        }

        try:
            if ado is not None:
                refined = await refine_payload_with_tools(
                    settings,
                    ado,
                    proposal=proposal_meta,
                    judge=judge_meta,
                    original_payload=payload,
                )
            else:
                async with AdoClient(settings) as ado_client:
                    refined = await refine_payload_with_tools(
                        settings,
                        ado_client,
                        proposal=proposal_meta,
                        judge=judge_meta,
                        original_payload=payload,
                    )
        except Exception:
            break

        refined = dict(refined)
        if refined == payload:
            break

        # Persist revision for audit.
        previous = dict(proposal.proposed_payload)
        proposal.proposed_payload = refined
        session.add(
            ProposalRevision(
                proposal_id=proposal.id,
                request_text="Auto-refine: judge requested clarity/accuracy improvements.",
                previous_payload=previous,
                revised_payload=refined,
            )
        )
        session.flush()
        session.commit()

        # Re-judge the updated payload.
        latest = await judge_and_persist(
            session, settings, proposal=proposal, policy_version=policy_version, ado=ado
        )

    return latest


async def auto_judge_and_refine_pending(
    session: Session,
    settings: Settings,
    *,
    limit: int = 25,
) -> int:
    """Automatically judge (and optionally refine) pending proposals."""

    if not settings.app_auto_judge_enabled:
        return 0

    pending = list(
        session.scalars(
            select(ProposedChange)
            .where(ProposedChange.status == ProposalStatus.PENDING)
            .order_by(ProposedChange.ingested_at.desc())
            .limit(limit)
        )
    )
    session.commit()
    handled = 0
    for proposal in pending:
        await judge_and_refine_proposal(session, settings, proposal=proposal)
        handled += 1
    return handled

