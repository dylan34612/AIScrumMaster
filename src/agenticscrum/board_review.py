"""Daily / on-demand board hygiene review."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from agenticscrum.ado.client import AdoClient
from agenticscrum.autopilot import autopilot_apply_pending
from agenticscrum.chat_events import format_autopilot_summary, post_chat_system_event
from agenticscrum.config import Settings
from agenticscrum.ingest import ingest_manual_transcript
from agenticscrum.llm.client import build_chat_model
from agenticscrum.models import (
    BoardReviewRun,
    BoardReviewStatus,
    ChangeType,
    ChatProposalLink,
    ChatRole,
    ChatSession,
    ProposalStatus,
    ProposedChange,
    utc_now,
)

BOARD_REVIEW_BRIEF_PROMPT = """You are an AI scrum master performing a daily board hygiene review.

Given a JSON snapshot of active work items (and selected comments), write a concise markdown briefing for the team chat:
- Start with a 2-4 sentence executive summary
- Then bullets for: Risks / Blockers, Stale or dirty cards, Comment-driven cleanup opportunities, Wins / healthy signals
- Reference work item IDs as #12345
- Be practical and specific; no fluff
- Do NOT invent work items that are not in the snapshot

Snapshot JSON:
{snapshot_json}
"""

BOARD_REVIEW_NOTES_TEMPLATE = """Daily board hygiene review ({when}).

Focus areas:
- PBIs stuck in New too long: propose changeType=StateTransition with newState=Approved
  (use Committed only when comments clearly show active implementation). Prefer a
  dedicated StateTransition on the EXISTING work item id — do not recreate the item.
- Missing assignee or estimate on active work
- Parent/child state mismatches
- Action items in comments not reflected on the card
- Items that comments suggest are Done but still Active (propose closure; do not auto-close)
- Duplicate / noisy comments that should not be re-posted

CRITICAL — already-pending proposals (do NOT re-propose these or substantially equivalent changes;
only propose NEW hygiene issues not already covered below):
{pending_text}

CRITICAL — recently rejected Comment proposals (do NOT re-propose the same or similar comments;
respect the rejection reason — if the reason says not to comment, leave that item alone):
{rejected_comments_text}

Active board snapshot (truncated):
{snapshot_text}

Recent comments (truncated):
{comments_text}
"""


def _truncate(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _load_outstanding_proposals(session: Session, *, limit: int = 100) -> list[ProposedChange]:
    """Return proposals still awaiting action (pending or assignee approval)."""

    return list(
        session.scalars(
            select(ProposedChange)
            .where(
                ProposedChange.status.in_(
                    (
                        ProposalStatus.PENDING,
                        ProposalStatus.AWAITING_ASSIGNEE_APPROVAL,
                    )
                )
            )
            .order_by(desc(ProposedChange.ingested_at))
            .limit(limit)
        )
    )


def _format_pending_for_notes(pending: list[ProposedChange]) -> str:
    """Format outstanding proposals so the analyzer can avoid repeats."""

    if not pending:
        return "(none)"

    lines: list[str] = []
    for proposal in pending:
        target = (
            f"#{proposal.target_work_item_id}"
            if proposal.target_work_item_id is not None
            else "(create)"
        )
        payload = dict(proposal.proposed_payload or {})
        details: list[str] = []
        new_state = payload.get("newState")
        if new_state:
            details.append(f"state→{new_state}")
        new_assignee = payload.get("newAssignee")
        if new_assignee:
            details.append(f"assignee={new_assignee}")
        field_updates = payload.get("fieldUpdates")
        if isinstance(field_updates, dict) and field_updates:
            keys = ", ".join(sorted(str(k) for k in field_updates.keys())[:6])
            details.append(f"fields={keys}")
        comment_text = payload.get("commentText")
        if comment_text:
            details.append(f"comment={_truncate(str(comment_text), 80)}")
        detail_suffix = f" ({'; '.join(details)})" if details else ""
        lines.append(
            f"- [{proposal.change_type.value}] {target}: "
            f"{_truncate(proposal.title, 120)}{detail_suffix}"
        )
    return "\n".join(lines)


def _load_rejected_comments(
    session: Session,
    *,
    lookback_days: int = 45,
    limit: int = 75,
) -> list[ProposedChange]:
    """Return recent rejected Comment proposals (with rejection reasons when available)."""

    cutoff = utc_now() - timedelta(days=max(1, lookback_days))
    return list(
        session.scalars(
            select(ProposedChange)
            .where(
                ProposedChange.status == ProposalStatus.REJECTED,
                ProposedChange.change_type == ChangeType.COMMENT,
                ProposedChange.ingested_at >= cutoff,
            )
            .order_by(desc(ProposedChange.ingested_at))
            .limit(limit)
        )
    )


def _format_rejected_comments_for_notes(rejected: list[ProposedChange]) -> str:
    """Format rejected comments + reasons so the analyzer respects prior decisions."""

    if not rejected:
        return "(none)"

    lines: list[str] = []
    for proposal in rejected:
        target = (
            f"#{proposal.target_work_item_id}"
            if proposal.target_work_item_id is not None
            else "(unknown)"
        )
        payload = dict(proposal.proposed_payload or {})
        comment_text = _truncate(str(payload.get("commentText") or proposal.title or ""), 160)
        reason = _truncate(str(proposal.rejection_reason or "").strip() or "(no reason given)", 200)
        lines.append(f"- {target}: rejected comment={comment_text} | reason={reason}")
    return "\n".join(lines)


def _get_or_create_chat_session(session: Session) -> ChatSession:
    existing = session.scalar(
        select(ChatSession)
        .where(ChatSession.archived.is_(False))
        .order_by(desc(ChatSession.updated_at))
    )
    if existing is not None:
        return existing
    chat_session = ChatSession(title="Scrum Master Chat")
    session.add(chat_session)
    session.flush()
    return chat_session


async def _build_board_snapshot(
    ado: AdoClient,
    settings: Settings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int]]:
    ids = await ado.query_active_ids()
    max_items = max(10, int(settings.app_daily_review_max_items))
    ids = list(ids)[:max_items]
    if not ids:
        return [], [], []

    items = await ado.batch_get(ids)
    snapshot: list[dict[str, Any]] = []
    for item in items:
        fields = item.get("fields") or {}
        assigned = fields.get("System.AssignedTo")
        if isinstance(assigned, dict):
            assigned_name = assigned.get("displayName")
        else:
            assigned_name = assigned
        snapshot.append(
            {
                "id": item.get("id"),
                "title": fields.get("System.Title"),
                "type": fields.get("System.WorkItemType"),
                "state": fields.get("System.State"),
                "assignedTo": assigned_name,
                "changedDate": fields.get("System.ChangedDate"),
            }
        )

    stale_days = max(1, int(settings.app_daily_review_stale_days))
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    max_comment_items = max(5, int(settings.app_daily_review_max_comment_items))
    comment_targets: list[int] = []
    for row in snapshot:
        wi_id = row.get("id")
        if not isinstance(wi_id, int):
            continue
        changed = row.get("changedDate")
        is_stale = False
        if isinstance(changed, str):
            try:
                changed_dt = datetime.fromisoformat(changed.replace("Z", "+00:00"))
                is_stale = changed_dt < cutoff
            except Exception:
                is_stale = False
        state = str(row.get("state") or "").lower()
        if is_stale or state in {"new", "to do", "proposed"} or not row.get("assignedTo"):
            comment_targets.append(wi_id)
        if len(comment_targets) >= max_comment_items:
            break

    comments_out: list[dict[str, Any]] = []
    for wi_id in comment_targets:
        try:
            entries = await ado.get_comments(wi_id)
        except Exception:
            continue
        for entry in entries[-3:]:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text") or entry.get("body") or ""
            comments_out.append(
                {
                    "workItemId": wi_id,
                    "text": _truncate(str(text), 500),
                    "createdDate": entry.get("createdDate") or entry.get("revisedDate"),
                }
            )

    return snapshot, comments_out, ids


async def _write_briefing(
    settings: Settings,
    snapshot: list[dict[str, Any]],
    comments: list[dict[str, Any]],
) -> str:
    model = build_chat_model(settings)
    payload = {"workItems": snapshot[:60], "comments": comments[:40]}
    response = await model.ainvoke(
        [
            SystemMessage(
                content=BOARD_REVIEW_BRIEF_PROMPT.format(
                    snapshot_json=json.dumps(payload, indent=2, default=str)[:20000]
                )
            ),
            HumanMessage(content="Write the daily briefing now."),
        ]
    )
    return str(response.content or "").strip() or "Board review completed."


async def run_board_review(
    session: Session,
    settings: Settings,
    *,
    trigger: str = "scheduled",
    focus_notes: str | None = None,
) -> BoardReviewRun:
    """Scan the board, post a chat briefing, and create hygiene proposals."""

    run = BoardReviewRun(
        status=BoardReviewStatus.RUNNING,
        trigger=trigger,
        scanned_ids=[],
    )
    session.add(run)
    session.flush()

    chat_session = _get_or_create_chat_session(session)
    run.chat_session_id = chat_session.id

    try:
        async with AdoClient(settings) as ado:
            snapshot, comments, ids = await _build_board_snapshot(ado, settings)
        run.items_scanned = len(snapshot)
        run.comments_scanned = len(comments)
        run.scanned_ids = ids

        briefing = await _write_briefing(settings, snapshot, comments)
        if focus_notes:
            briefing = f"**Focused review**\n\n{focus_notes.strip()}\n\n---\n\n{briefing}"

        when = utc_now().strftime("%Y-%m-%d %H:%M UTC")
        snapshot_text = "\n".join(
            f"- #{row.get('id')} [{row.get('type')}] {row.get('state')} · "
            f"{row.get('assignedTo') or 'Unassigned'} · {_truncate(str(row.get('title') or ''), 120)}"
            for row in snapshot[:50]
        ) or "(no active items)"
        comments_text = "\n".join(
            f"- #{c.get('workItemId')}: {_truncate(str(c.get('text') or ''), 200)}"
            for c in comments[:30]
        ) or "(no recent comments sampled)"
        outstanding = _load_outstanding_proposals(session)
        pending_text = _format_pending_for_notes(outstanding)
        rejected_comments = _load_rejected_comments(session)
        rejected_comments_text = _format_rejected_comments_for_notes(rejected_comments)
        notes = BOARD_REVIEW_NOTES_TEMPLATE.format(
            when=when,
            pending_text=pending_text,
            rejected_comments_text=rejected_comments_text,
            snapshot_text=snapshot_text,
            comments_text=comments_text,
        )
        if focus_notes:
            notes = f"{focus_notes.strip()}\n\n{notes}"

        ingestion = await ingest_manual_transcript(
            session,
            settings,
            title=f"Board review ({trigger})",
            meeting_date=utc_now().date(),
            notes=notes,
            source="BoardReview",
            dedupe_against_pending=True,
        )
        created = list(
            session.scalars(
                select(ProposedChange).where(ProposedChange.ingestion_run_id == ingestion.id)
            )
        )
        for proposal in created:
            session.add(ChatProposalLink(session_id=chat_session.id, proposal_id=proposal.id))

        applied = await autopilot_apply_pending(
            session,
            settings,
            proposal_ids=[p.id for p in created],
        )
        applied_ids = {a.id for a in applied}
        applied_lines = [
            f"#{p.applied_work_item_id or p.target_work_item_id} · {p.title}" for p in applied
        ]
        pending_lines = [
            f"{p.change_type.value} · {p.title}" for p in created if p.id not in applied_ids
        ]
        auto_summary = format_autopilot_summary(
            applied=applied_lines,
            needs_approval=pending_lines,
        )

        full_message = f"## Daily board review\n\n{briefing}\n\n---\n\n{auto_summary}"
        msg = post_chat_system_event(
            session,
            chat_session.id,
            full_message,
            kind="board_review",
            meta={
                "board_review_run_id": run.id,
                "items_scanned": run.items_scanned,
                "proposals_created": len(created),
                "auto_applied": len(applied),
            },
            role=ChatRole.ASSISTANT,
        )
        run.briefing_text = briefing
        run.proposals_created = len(created)
        run.chat_message_id = msg.id
        run.status = BoardReviewStatus.SUCCESS
        run.completed_at = utc_now()
        return run
    except Exception as exc:
        run.status = BoardReviewStatus.FAILURE
        run.error_message = str(exc)
        run.completed_at = utc_now()
        post_chat_system_event(
            session,
            chat_session.id,
            f"**Board review failed:** {exc}",
            kind="board_review_error",
            role=ChatRole.ASSISTANT,
        )
        return run
