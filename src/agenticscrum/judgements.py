"""Proposal judgement persistence helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from agenticscrum.models import ProposalJudgement


def compute_payload_hash(payload: dict[str, Any]) -> str:
    """Compute a stable hash for a proposal payload."""

    normalized = dict(payload)
    # Ignore internal metadata when deduping judge results.
    normalized.pop("_meta", None)
    blob = json.dumps(
        normalized, sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def latest_judgements_by_proposal_id(
    session: Session, proposal_ids: list[int]
) -> dict[int, ProposalJudgement]:
    """Return latest judgement row per proposal id (if any)."""

    if not proposal_ids:
        return {}

    rows = list(
        session.scalars(
            select(ProposalJudgement)
            .where(ProposalJudgement.proposal_id.in_(proposal_ids))
            .order_by(desc(ProposalJudgement.created_at))
        )
    )
    latest: dict[int, ProposalJudgement] = {}
    for row in rows:
        if row.proposal_id not in latest:
            latest[row.proposal_id] = row
    return latest


def judgement_for_payload(
    session: Session, proposal_id: int, payload_hash: str
) -> ProposalJudgement | None:
    """Return an existing judgement for the given payload hash."""

    return session.scalar(
        select(ProposalJudgement)
        .where(
            ProposalJudgement.proposal_id == proposal_id,
            ProposalJudgement.payload_hash == payload_hash,
        )
        .order_by(desc(ProposalJudgement.created_at))
        .limit(1)
    )

