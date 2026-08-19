"""FastAPI routes for the local approval UI."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import delete, desc, func, select
from sqlalchemy.orm import Session

from agenticscrum.ado.client import AdoClient
from agenticscrum.ado.client import (
    professional_comment_summary,
    sanitize_ado_text,
    sanitize_fields_for_ado,
)
from agenticscrum.ado.fields import (
    ACCEPTANCE_CRITERIA,
    AREA_PATH,
    ASSIGNED_TO,
    DESCRIPTION,
    STATE,
    TITLE,
    extract_append_value,
)
from agenticscrum.apply import approve_proposal, reject_proposal
from agenticscrum.autopilot import autopilot_apply_pending
from agenticscrum.board_review import run_board_review
from agenticscrum.chat_events import (
    format_applied_event,
    format_autopilot_summary,
    format_failed_event,
    post_chat_system_event,
)
from agenticscrum.chat_intent import ChatIntent, classify_chat_intent
from agenticscrum.config import Settings, load_settings
from agenticscrum.db import create_session_factory, session_scope
from agenticscrum.inbox import (
    list_inbox_files,
    resolve_archive_dir,
    resolve_inbox_dir,
    write_processed_transcript_copy,
)
from agenticscrum.ingest import ingest_manual_transcript, run_ingestion
from agenticscrum.judgements import latest_judgements_by_proposal_id
from agenticscrum.llm.agent import revise_payload
from agenticscrum.llm.scrum_chat import (
    maybe_summarize_session,
    persist_chat_llm_call,
    persist_chat_tool_records,
    scrum_master_reply,
)
from agenticscrum.proposal_judge import (
    auto_judge_and_refine_pending,
    judge_and_persist,
    judge_pending_proposals,
)
from agenticscrum.repair import agentic_retry_proposal
from agenticscrum.soft_undo import create_soft_undo_proposal
from agenticscrum.models import (
    ChatMessage,
    ChatProposalLink,
    ChatRole,
    ChatSession,
    ChangeType,
    IngestedMeeting,
    IngestionEvent,
    IngestionRun,
    IngestionStatus,
    LLMCallLog,
    ProposalRevision,
    ProposalStatus,
    ProposedChange,
    ProposalJudgement,
    TeamMember,
    ToolCallLog,
)

router = APIRouter()


def as_utc(value: datetime) -> datetime:
    """Normalize DB datetimes (SQLite) to UTC-aware datetimes."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_settings(request: Request) -> Settings:
    """Return fresh app settings so .env edits are picked up without restart."""

    settings = load_settings()
    request.app.state.settings = settings
    return settings


def get_session(settings: Settings = Depends(get_settings)) -> Iterator[Session]:
    """Provide a database session dependency."""

    factory = create_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Render the main approval dashboard."""

    starting = request.query_params.get("starting") == "1"
    pending_proposals = list(
        session.scalars(
            select(ProposedChange)
            .where(ProposedChange.status == ProposalStatus.PENDING)
            .order_by(ProposedChange.confidence_score.asc())
        )
    )
    pending_judgements = latest_judgements_by_proposal_id(
        session, [proposal.id for proposal in pending_proposals]
    )
    applied_since = datetime.now(timezone.utc) - timedelta(days=7)
    applied = list(
        session.scalars(
            select(ProposedChange)
            .where(
                ProposedChange.status == ProposalStatus.APPLIED,
                ProposedChange.approved_at >= applied_since,
            )
            .order_by(desc(ProposedChange.approved_at))
        )
    )
    failures = list(
        session.scalars(
            select(ProposedChange)
            .where(ProposedChange.status == ProposalStatus.FAILED)
            .order_by(desc(ProposedChange.ingested_at))
        )
    )
    roster = list(session.scalars(select(TeamMember).order_by(TeamMember.display_name)))
    last_run = session.scalar(select(IngestionRun).order_by(desc(IngestionRun.started_at)))
    running_runs = list(
        session.scalars(
            select(IngestionRun)
            .where(IngestionRun.status == IngestionStatus.RUNNING)
            .order_by(desc(IngestionRun.started_at))
        )
    )
    running_details: list[dict[str, object]] = []
    if running_runs:
        now = datetime.now(timezone.utc)
        for run in running_runs:
            last_event = session.scalar(
                select(IngestionEvent)
                .where(IngestionEvent.ingestion_run_id == run.id)
                .order_by(desc(IngestionEvent.created_at))
            )
            age_seconds = int((now - as_utc(run.started_at)).total_seconds())
            running_details.append(
                {
                    "run": run,
                    "age_seconds": age_seconds,
                    "last_event": last_event,
                }
            )
    inbox_status: dict[str, object] = {
        "enabled": settings.notes_inbox_enabled,
        "inbox_path": str(resolve_inbox_dir(settings)),
        "archive_path": str(resolve_archive_dir(settings)),
        "archive_mode": settings.notes_inbox_archive_mode,
        "extensions": ", ".join(settings.notes_inbox_extensions),
        "min_age_seconds": settings.notes_inbox_min_age_seconds,
        "pending_count": 0,
        "pending_files": [],
        "error": None,
    }
    if settings.notes_inbox_enabled:
        try:
            inbox_pending_files = list_inbox_files(settings)
            inbox_status["pending_count"] = len(inbox_pending_files)
            inbox_status["pending_files"] = [p.name for p in inbox_pending_files[:10]]
        except Exception as exc:
            inbox_status["error"] = str(exc)
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "pending": pending_proposals,
            "proposal_previews": build_proposal_previews(settings, pending_proposals),
            "proposal_judgements": pending_judgements,
            "applied": applied,
            "failures": failures,
            "roster": roster,
            "last_run": last_run,
            "running_runs": running_runs,
            "running_details": running_details,
            "settings": settings,
            "starting": starting,
            "inbox_status": inbox_status,
            "config_status": {
                "ado_pat_configured": bool(settings.ado_pat),
                "llm_configured": bool(settings.llm_api_key)
                or bool(settings.llm_azure_client_id and settings.llm_azure_client_secret),
            },
        },
    )


@router.post("/ingest/run")
async def trigger_ingestion(
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Run a live ingestion cycle from the UI."""

    if not has_running_ingestion(session):
        background_tasks.add_task(run_ingestion_background, settings)
        return RedirectResponse("/?starting=1", status_code=303)
    return redirect_home()


@router.post("/transcripts")
async def ingest_transcript(
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    meeting_date: date = Form(...),
    notes: str = Form(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Ingest a manually pasted transcript."""

    if not has_running_ingestion(session):
        background_tasks.add_task(
            ingest_manual_transcript_background,
            settings,
            title,
            meeting_date,
            notes,
        )
    return redirect_home()


@router.post("/ingestion/clear-errors")
async def clear_ingestion_errors(session: Session = Depends(get_session)) -> RedirectResponse:
    """Clear stored ingestion error messages from previous runs."""

    runs = list(
        session.scalars(
            select(IngestionRun).where(IngestionRun.error_message.is_not(None))
        )
    )
    for run in runs:
        run.error_message = None
    return redirect_home()


@router.post("/ingestion/runs/{run_id}/mark-failed")
async def mark_ingestion_run_failed(
    run_id: int,
    request: Request,
    reason: str = Form("Manually marked failed from UI."),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Mark a RUNNING ingestion as failed (unstick the UI)."""

    run = session.get(IngestionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Ingestion run not found.")
    if run.status != IngestionStatus.RUNNING:
        return RedirectResponse(str(request.headers.get("referer", "/")), status_code=303)

    run.status = IngestionStatus.FAILURE
    run.completed_at = datetime.now(timezone.utc)
    existing = run.error_message or ""
    stamp = datetime.now(timezone.utc).isoformat()
    extra = f"[{stamp}] {reason}"
    run.error_message = f"{existing}\n{extra}".strip() if existing else extra
    session.add(
        IngestionEvent(
            ingestion_run_id=run.id,
            level="WARN",
            message=f"Run marked failed from UI. reason={reason}",
        )
    )
    session.flush()
    return RedirectResponse(str(request.headers.get("referer", "/")), status_code=303)


@router.get("/ingestion/runs/{run_id}", response_class=HTMLResponse)
async def ingestion_run_detail(
    run_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """Show details for a single ingestion run."""

    run = session.get(IngestionRun, run_id)
    if run is None:
        return request.app.state.templates.TemplateResponse(
            request,
            "error.html",
            {
                "error_type": "NotFound",
                "error_message": f"Ingestion run {run_id} not found",
                "traceback": "",
                "when": datetime.now(timezone.utc).isoformat(),
                "path": request.url.path,
                "method": request.method,
            },
            status_code=404,
        )

    events = list(
        session.scalars(
            select(IngestionEvent)
            .where(IngestionEvent.ingestion_run_id == run_id)
            .order_by(desc(IngestionEvent.created_at))
            .limit(250)
        )
    )
    events.reverse()
    meetings = list(
        session.scalars(
            select(IngestedMeeting)
            .where(IngestedMeeting.ingestion_run_id == run_id)
            .order_by(desc(IngestedMeeting.created_at))
            .limit(50)
        )
    )
    tool_calls = list(
        session.scalars(
            select(ToolCallLog)
            .where(ToolCallLog.ingestion_run_id == run_id)
            .order_by(desc(ToolCallLog.created_at))
            .limit(100)
        )
    )
    llm_calls = list(
        session.scalars(
            select(LLMCallLog)
            .where(LLMCallLog.ingestion_run_id == run_id)
            .order_by(desc(LLMCallLog.created_at))
            .limit(50)
        )
    )
    llm_calls.reverse()
    age_seconds = int((datetime.now(timezone.utc) - as_utc(run.started_at)).total_seconds())
    return request.app.state.templates.TemplateResponse(
        request,
        "ingestion_run.html",
        {
            "run": run,
            "age_seconds": age_seconds,
            "events": events,
            "meetings": meetings,
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
        },
    )


@router.get("/ingestion/runs", response_class=HTMLResponse)
async def ingestion_runs(
    request: Request,
    session: Session = Depends(get_session),
) -> HTMLResponse:
    """List recent ingestion runs."""

    runs = list(
        session.scalars(select(IngestionRun).order_by(desc(IngestionRun.started_at)).limit(100))
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "ingestion_runs.html",
        {"runs": runs},
    )


@router.post("/ingested-meetings/{meeting_id}/rerun")
async def rerun_ingested_meeting(
    meeting_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Re-run an already ingested transcript with current prompts."""

    meeting = session.get(IngestedMeeting, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=404, detail="Ingested meeting not found.")
    if not has_running_ingestion(session):
        background_tasks.add_task(rerun_ingested_meeting_background, settings, meeting_id)
    return redirect_home()


@router.post("/proposals/{proposal_id}/approve")
async def approve(
    proposal_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Approve a proposal."""

    await approve_proposal(session, settings, proposal_id, settings.app_approver_name)
    return redirect_home()


@router.post("/proposals/judge")
async def judge_pending(
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Judge recent pending proposals (second-pass safety/confidence)."""

    background_tasks.add_task(judge_pending_background, settings)
    return redirect_home()


@router.post("/proposals/{proposal_id}/judge")
async def judge_one(
    proposal_id: int,
    background_tasks: BackgroundTasks,
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Judge one proposal now."""

    background_tasks.add_task(judge_one_background, settings, proposal_id)
    return redirect_home()


@router.post("/proposals/{proposal_id}/retry")
async def retry_failed_proposal(
    proposal_id: int,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Agentically diagnose, repair, and re-apply a failed proposal."""

    await agentic_retry_proposal(
        session,
        settings,
        proposal_id,
        approver=settings.app_approver_name,
    )
    return redirect_home()


@router.post("/proposals/{proposal_id}/reject")
async def reject(
    proposal_id: int,
    reason: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Reject a proposal."""

    reject_proposal(session, proposal_id, reason or None)
    return redirect_home()


@router.post("/proposals/{proposal_id}/edit")
async def edit_payload(
    proposal_id: int,
    payload_json: str = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Save a manually edited proposal payload."""

    proposal = session.get(ProposedChange, proposal_id)
    if proposal is None:
        raise ValueError("Proposal not found")
    previous = dict(proposal.proposed_payload)
    revised = json.loads(payload_json)
    proposal.proposed_payload = revised
    session.add(
        ProposalRevision(
            proposal_id=proposal.id,
            request_text="Manual JSON edit",
            previous_payload=previous,
            revised_payload=revised,
        )
    )
    return redirect_home()


@router.post("/proposals/{proposal_id}/change-request")
async def change_request(
    proposal_id: int,
    request_text: str = Form(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Revise a proposal through a free-form LLM change request."""

    proposal = session.get(ProposedChange, proposal_id)
    if proposal is None:
        raise ValueError("Proposal not found")
    previous = dict(proposal.proposed_payload)
    revised = await revise_payload(settings, request_text, previous)
    proposal.proposed_payload = revised
    session.add(
        ProposalRevision(
            proposal_id=proposal.id,
            request_text=request_text,
            previous_payload=previous,
            revised_payload=revised,
        )
    )
    return redirect_home()


@router.post("/roster")
async def save_team_member(
    display_name: str = Form(...),
    email: str = Form(...),
    ado_unique_name: str = Form(...),
    active: bool = Form(False),
    member_id: int | None = Form(None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Create or update a roster member."""

    member = session.get(TeamMember, member_id) if member_id else TeamMember()
    member.display_name = display_name
    member.email = email
    member.ado_unique_name = ado_unique_name
    member.active = active
    session.add(member)
    return redirect_home()


@router.post("/roster/{member_id}/delete")
async def delete_team_member(member_id: int, session: Session = Depends(get_session)) -> RedirectResponse:
    """Deactivate a roster member."""

    member = session.get(TeamMember, member_id)
    if member is not None:
        member.active = False
    return redirect_home()


def get_or_create_chat_session(
    session: Session,
    session_id: int | None = None,
) -> ChatSession:
    """Return a chat session by id, or the latest non-archived session."""

    if session_id is not None:
        existing = session.get(ChatSession, session_id)
        if existing is not None and not existing.archived:
            return existing
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


def list_chat_sessions(session: Session, limit: int = 20) -> list[ChatSession]:
    return list(
        session.scalars(
            select(ChatSession)
            .where(ChatSession.archived.is_(False))
            .order_by(desc(ChatSession.updated_at))
            .limit(limit)
        )
    )


def chat_counts(session: Session) -> tuple[int, int, int]:
    """Return (pending, failed, awaiting_assignee_approval) counts."""

    pending = (
        session.scalar(
            select(func.count(ProposedChange.id)).where(
                ProposedChange.status == ProposalStatus.PENDING
            )
        )
        or 0
    )
    failed = (
        session.scalar(
            select(func.count(ProposedChange.id)).where(
                ProposedChange.status == ProposalStatus.FAILED
            )
        )
        or 0
    )
    awaiting = (
        session.scalar(
            select(func.count(ProposedChange.id)).where(
                ProposedChange.status == ProposalStatus.AWAITING_ASSIGNEE_APPROVAL
            )
        )
        or 0
    )
    return int(pending), int(failed), int(awaiting)


def autopilot_applied_today_count(session: Session, settings: Settings) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    approver = settings.app_autopilot_approver_name or "Autopilot"
    return int(
        session.scalar(
            select(func.count(ProposedChange.id)).where(
                ProposedChange.status == ProposalStatus.APPLIED,
                ProposedChange.approved_by == approver,
                ProposedChange.approved_at >= start,
            )
        )
        or 0
    )


def chat_history(session: Session, chat_session: ChatSession, limit: int = 50) -> list[ChatMessage]:
    """Return chat message history."""

    return list(
        session.scalars(
            select(ChatMessage)
            .where(ChatMessage.session_id == chat_session.id)
            .order_by(ChatMessage.created_at)
            .limit(limit)
        )
    )


def chat_proposals(session: Session, chat_session: ChatSession, limit: int = 25) -> list[ProposedChange]:
    """Return proposals linked to a chat session."""

    return list(
        session.scalars(
            select(ProposedChange)
            .join(ChatProposalLink, ChatProposalLink.proposal_id == ProposedChange.id)
            .where(ChatProposalLink.session_id == chat_session.id)
            .order_by(desc(ProposedChange.ingested_at))
            .limit(limit)
        )
    )


def _tool_summary(records: list) -> str:
    if not records:
        return ""
    parts: list[str] = []
    for record in records:
        name = getattr(record, "tool_name", "tool")
        args = getattr(record, "input_payload", {}) or {}
        if "work_item_id" in args:
            parts.append(f"{name}(#{args['work_item_id']})")
        elif "ids" in args:
            ids = args.get("ids") or []
            parts.append(f"{name}({len(ids)} ids)")
        else:
            parts.append(name)
    return ", ".join(parts[:8])


def _chat_template_context(
    session: Session,
    settings: Settings,
    chat_session: ChatSession,
    *,
    ado_unavailable_banner: bool = False,
) -> dict:
    messages = chat_history(session, chat_session)
    proposals = chat_proposals(session, chat_session)
    pending_count, failed_count, awaiting_count = chat_counts(session)
    roster = list(session.scalars(select(TeamMember).where(TeamMember.active.is_(True))))
    recent_ids = [
        int(x)
        for x in session.scalars(
            select(ProposedChange.target_work_item_id)
            .where(ProposedChange.target_work_item_id.is_not(None))
            .order_by(desc(ProposedChange.ingested_at))
            .limit(40)
        )
        if x is not None
    ]
    running_runs = list(
        session.scalars(
            select(IngestionRun)
            .where(IngestionRun.status == IngestionStatus.RUNNING)
            .order_by(desc(IngestionRun.started_at))
        )
    )
    return {
        "chat_session": chat_session,
        "chat_sessions": list_chat_sessions(session),
        "messages": messages,
        "proposals": proposals,
        "proposal_previews": build_proposal_previews(settings, proposals),
        "proposal_judgements": latest_judgements_by_proposal_id(
            session, [proposal.id for proposal in proposals]
        ),
        "pending_count": pending_count,
        "failed_count": failed_count,
        "awaiting_count": awaiting_count,
        "running_runs": running_runs,
        "settings": settings,
        "autopilot_applied_today": autopilot_applied_today_count(session, settings),
        "ado_unavailable_banner": ado_unavailable_banner,
        "roster_json": json.dumps(
            [
                {
                    "display_name": m.display_name,
                    "ado_unique_name": m.ado_unique_name,
                }
                for m in roster
            ]
        ),
        "recent_wi_ids_json": json.dumps(recent_ids),
    }


async def _run_chat_propose(
    session: Session,
    settings: Settings,
    chat_session: ChatSession,
    content: str,
) -> str:
    if has_running_ingestion(session):
        return "An ingestion is already running. Please wait and try again."

    run = await ingest_manual_transcript(
        session,
        settings,
        title=f"Chat: {chat_session.title}",
        meeting_date=date.today(),
        notes=content,
        source="Chat",
    )
    try:
        write_processed_transcript_copy(
            settings,
            title=f"Chat: {chat_session.title}",
            meeting_date=date.today(),
            notes=content,
            source="Chat Propose",
            run_id=run.id,
            run_status=run.status.value,
        )
    except Exception:
        pass

    created = list(
        session.scalars(select(ProposedChange).where(ProposedChange.ingestion_run_id == run.id))
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

    if run.status != IngestionStatus.SUCCESS:
        details = f"\n\n{run.error_message}" if run.error_message else ""
        return f"Proposal generation finished as {run.status.value}.{details}"

    summary = format_autopilot_summary(applied=applied_lines, needs_approval=pending_lines)
    return f"Created {len(created)} proposal(s).\n\n{summary}"


async def _run_chat_answer(
    session: Session,
    settings: Settings,
    chat_session: ChatSession,
    *,
    pending_count: int,
    failed_count: int,
    awaiting_count: int,
) -> tuple[str, list, bool]:
    """Return (assistant_text, tool_records, ado_unavailable)."""

    history = chat_history(session, chat_session)
    await maybe_summarize_session(session, settings, chat_session, history)
    ado_unavailable = False
    records: list = []
    try:
        try:
            async with AdoClient(settings) as ado:
                text, records = await scrum_master_reply(
                    settings=settings,
                    ado=ado,
                    history=history,
                    pending_count=pending_count,
                    failed_count=failed_count,
                    awaiting_count=awaiting_count,
                    session_summary=chat_session.summary,
                )
        except Exception:
            ado_unavailable = True
            text, records = await scrum_master_reply(
                settings=settings,
                ado=None,
                history=history,
                pending_count=pending_count,
                failed_count=failed_count,
                awaiting_count=awaiting_count,
                session_summary=chat_session.summary,
            )
            if text:
                text = f"{text}\n\n_(Live ADO tools were unavailable.)_"
    except Exception as exc:
        text = f"Chat failed: {exc}"
    return text, records, ado_unavailable



def ado_work_item_url(settings: Settings, work_item_id: int) -> str:
    """Return a clickable ADO work item URL."""

    return (
        f"https://dev.azure.com/{settings.ado_org}/"
        f"{settings.ado_project_url_segment}/_workitems/edit/{work_item_id}"
    )


def build_applied_fields(
    settings: Settings,
    proposal: ProposedChange,
    payload: dict[str, object],
    safe_fields: dict[str, object],
) -> dict[str, object]:
    """Return the full field payload that will be applied to ADO."""

    fields: dict[str, object] = dict(safe_fields)
    if proposal.change_type == ChangeType.CREATE:
        fields.setdefault(TITLE, sanitize_ado_text(proposal.title, proposal.source_quote))
        fields.setdefault(AREA_PATH, settings.ado_area_path)

        desc = fields.get(DESCRIPTION)
        if isinstance(desc, str):
            unwrapped = extract_append_value(desc)
            if unwrapped is not None:
                desc = unwrapped
                fields[DESCRIPTION] = unwrapped
        if not (isinstance(desc, str) and desc.strip()):
            derived = sanitize_ado_text(proposal.rationale, proposal.source_quote).strip()
            fields[DESCRIPTION] = derived or sanitize_ado_text(
                proposal.title, proposal.source_quote
            ).strip() or "TBD"

        ac = fields.get(ACCEPTANCE_CRITERIA)
        if isinstance(ac, str):
            unwrapped = extract_append_value(ac)
            if unwrapped is not None:
                ac = unwrapped
                fields[ACCEPTANCE_CRITERIA] = unwrapped
        if not (isinstance(ac, str) and ac.strip()):
            fields[ACCEPTANCE_CRITERIA] = "TBD"

        new_assignee = payload.get("newAssignee")
        if isinstance(new_assignee, str) and new_assignee.strip():
            fields[ASSIGNED_TO] = new_assignee
        return fields

    if proposal.change_type == ChangeType.STATE_TRANSITION:
        new_state = payload.get("newState")
        if isinstance(new_state, str) and new_state.strip():
            fields[STATE] = new_state
    if proposal.change_type == ChangeType.ASSIGN:
        new_assignee = payload.get("newAssignee")
        if isinstance(new_assignee, str) and new_assignee.strip():
            fields[ASSIGNED_TO] = new_assignee
    return fields


def build_proposal_previews(
    settings: Settings,
    proposals: list[ProposedChange],
    approver: str | None = None,
) -> dict[int, dict[str, object]]:
    """Build UI-friendly previews of proposal payload details."""

    who = approver or settings.app_approver_name
    previews: dict[int, dict[str, object]] = {}
    for proposal in proposals:
        payload = dict(proposal.proposed_payload or {})
        snapshot = payload.get("targetSnapshot")
        if not isinstance(snapshot, dict):
            snapshot = None

        target_url = (
            ado_work_item_url(settings, int(proposal.target_work_item_id))
            if proposal.target_work_item_id is not None
            else None
        )
        fields = dict(payload.get("fieldUpdates") or {})
        safe_fields = sanitize_fields_for_ado(fields, proposal.source_quote)
        applied_fields = build_applied_fields(settings, proposal, payload, safe_fields)

        comment_preview: str | None = None
        if proposal.change_type == ChangeType.COMMENT:
            comment_preview = professional_comment_summary(
                proposal,
                who,
                comment_text=str(payload.get("commentText") or ""),
            )

        previews[proposal.id] = {
            "target_title": snapshot.get("title") if snapshot else None,
            "target_type": snapshot.get("type") if snapshot else None,
            "target_state": snapshot.get("state") if snapshot else None,
            "target_assigned_to": snapshot.get("assignedTo") if snapshot else None,
            "target_url": target_url,
            "safe_title": sanitize_ado_text(proposal.title, proposal.source_quote),
            "safe_fields": safe_fields,
            "applied_fields": applied_fields,
            "comment_preview": comment_preview,
            "new_state": payload.get("newState"),
            "new_assignee": payload.get("newAssignee"),
            "parent_work_item_id": payload.get("parentWorkItemId"),
        }
    return previews


def _is_placeholder_comment_text(value: object) -> bool:
    if not isinstance(value, str):
        return True
    text = " ".join(value.strip().split())
    if not text:
        return True
    lowered = text.lower()
    if "no details provided" in lowered:
        return True
    if lowered.startswith("captured meeting discussion"):
        return True
    if lowered in {
        "captured meeting discussion.",
        "captured from meeting notes for review and tracking.",
        "captured from meeting notes.",
    }:
        return True
    return False


@router.post("/proposals/bulk-reject-placeholder-comments")
async def bulk_reject_placeholder_comments(
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Reject pending comment proposals that contain placeholder/no-op text."""

    pending_comments = list(
        session.scalars(
            select(ProposedChange).where(
                ProposedChange.status == ProposalStatus.PENDING,
                ProposedChange.change_type == ChangeType.COMMENT,
            )
        )
    )
    for proposal in pending_comments:
        payload = dict(proposal.proposed_payload or {})
        comment_text = payload.get("commentText")
        if _is_placeholder_comment_text(comment_text):
            proposal.status = ProposalStatus.REJECTED
            proposal.rejection_reason = "Auto-rejected: non-informational comment proposal."
    return redirect_home()


@router.get("/chat", response_class=HTMLResponse)
async def chat(
    request: Request,
    session_id: int | None = None,
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Render the scrum-master chat UI."""

    chat_session = get_or_create_chat_session(session, session_id)
    ctx = _chat_template_context(session, settings, chat_session)
    return request.app.state.templates.TemplateResponse(request, "chat.html", ctx)


@router.post("/chat/sessions/new")
async def chat_new_session(
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Create a fresh chat session."""

    chat_session = ChatSession(title=f"Scrum Master Chat {datetime.now(timezone.utc):%Y-%m-%d %H:%M}")
    session.add(chat_session)
    session.flush()
    return RedirectResponse(f"/chat?session_id={chat_session.id}", status_code=303)


@router.post("/chat/clear")
async def chat_clear(
    session_id: int = Form(...),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    """Clear chat messages and chat-linked proposals, then start a fresh session."""

    chat_session = session.get(ChatSession, session_id)
    if chat_session is not None:
        linked_proposal_ids = list(
            session.scalars(
                select(ChatProposalLink.proposal_id).where(
                    ChatProposalLink.session_id == chat_session.id
                )
            )
        )
        for proposal_id in linked_proposal_ids:
            proposal = session.get(ProposedChange, proposal_id)
            if proposal is None:
                continue
            if proposal.status in {
                ProposalStatus.PENDING,
                ProposalStatus.AWAITING_ASSIGNEE_APPROVAL,
                ProposalStatus.FAILED,
            }:
                proposal.status = ProposalStatus.REJECTED
                proposal.rejection_reason = proposal.rejection_reason or "Cleared with chat session"

        session.execute(
            delete(ChatProposalLink).where(ChatProposalLink.session_id == chat_session.id)
        )
        session.execute(delete(ChatMessage).where(ChatMessage.session_id == chat_session.id))
        chat_session.archived = True
        chat_session.title = f"Archived · {chat_session.title}"[:255]
        chat_session.updated_at = datetime.now(timezone.utc)

    fresh = ChatSession(title="Scrum Master Chat")
    session.add(fresh)
    session.flush()
    return RedirectResponse(f"/chat?session_id={fresh.id}", status_code=303)


@router.post("/chat/messages", response_class=HTMLResponse)
async def chat_send(
    request: Request,
    session_id: int = Form(...),
    content: str = Form(...),
    mode: str = Form("auto"),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Accept one chat message and return updated message markup."""

    chat_session = session.get(ChatSession, session_id) or get_or_create_chat_session(session)
    user_text = content.strip()
    session.add(
        ChatMessage(
            session_id=chat_session.id,
            role=ChatRole.USER,
            content=user_text,
            message_kind="message",
        )
    )
    session.flush()

    pending_count, failed_count, awaiting_count = chat_counts(session)
    mode_normalized = (mode or "auto").strip().lower()
    if mode_normalized == "propose":
        intent = ChatIntent.PROPOSE
    elif mode_normalized == "chat":
        intent = ChatIntent.ANSWER
    else:
        intent = await classify_chat_intent(settings, user_text)

    # Shortcut: user asked to fix failures.
    if "fix failed" in user_text.lower() or "fix with ai" in user_text.lower():
        failures = list(
            session.scalars(
                select(ProposedChange)
                .where(ProposedChange.status == ProposalStatus.FAILED)
                .order_by(desc(ProposedChange.ingested_at))
                .limit(5)
            )
        )
        lines: list[str] = []
        for proposal in failures:
            post_chat_system_event(
                session,
                chat_session.id,
                f"Diagnosing failure for **{proposal.title}**…",
                kind="retry_progress",
            )
            result = await agentic_retry_proposal(
                session, settings, proposal.id, approver=settings.app_approver_name
            )
            if result.success:
                lines.append(f"- Fixed: {proposal.title}")
            else:
                lines.append(
                    f"- Still failed: {proposal.title} — {result.guidance or result.diagnosis}"
                )
        assistant_text = (
            "**Fix with AI results**\n\n" + "\n".join(lines)
            if lines
            else "No failed proposals to repair."
        )
        session.add(
            ChatMessage(
                session_id=chat_session.id,
                role=ChatRole.ASSISTANT,
                content=assistant_text,
                message_kind="retry_summary",
            )
        )
        session.flush()
        ctx = _chat_template_context(session, settings, chat_session)
        return request.app.state.templates.TemplateResponse(request, "chat_update.html", ctx)

    ado_unavailable = False
    tool_records: list = []
    if intent == ChatIntent.REVIEW:
        post_chat_system_event(
            session,
            chat_session.id,
            "Running board review…",
            kind="review_progress",
        )
        await run_board_review(
            session,
            settings,
            trigger="chat",
            focus_notes=user_text,
        )
        # Board review posts its own briefing message.
    elif intent == ChatIntent.PROPOSE:
        post_chat_system_event(
            session,
            chat_session.id,
            "Analyzing your request against the board…",
            kind="propose_progress",
        )
        assistant_text = await _run_chat_propose(session, settings, chat_session, user_text)
        session.add(
            ChatMessage(
                session_id=chat_session.id,
                role=ChatRole.ASSISTANT,
                content=assistant_text,
                message_kind="propose_result",
            )
        )
    elif intent == ChatIntent.CLARIFY:
        assistant_text = (
            "I want to get this right — can you clarify:\n"
            "1. Which work item (ID or title)?\n"
            "2. What change should I make (state, assignee, fields, or comment)?"
        )
        session.add(
            ChatMessage(
                session_id=chat_session.id,
                role=ChatRole.ASSISTANT,
                content=assistant_text,
                message_kind="clarify",
            )
        )
    else:
        assistant_text, tool_records, ado_unavailable = await _run_chat_answer(
            session,
            settings,
            chat_session,
            pending_count=pending_count,
            failed_count=failed_count,
            awaiting_count=awaiting_count,
        )
        msg = ChatMessage(
            session_id=chat_session.id,
            role=ChatRole.ASSISTANT,
            content=(assistant_text or "…"),
            message_kind="message",
            message_meta={
                "tool_summary": _tool_summary(tool_records),
                "ado_unavailable": ado_unavailable,
            },
        )
        session.add(msg)
        session.flush()
        persist_chat_tool_records(
            session,
            chat_session_id=chat_session.id,
            chat_message_id=msg.id,
            records=tool_records,
        )
        persist_chat_llm_call(
            session,
            settings,
            chat_session_id=chat_session.id,
            response_text=assistant_text or "",
        )

    session.flush()
    ctx = _chat_template_context(
        session,
        settings,
        chat_session,
        ado_unavailable_banner=ado_unavailable,
    )
    return request.app.state.templates.TemplateResponse(request, "chat_update.html", ctx)


@router.post("/chat/review", response_class=HTMLResponse)
async def chat_review_board(
    request: Request,
    session_id: int = Form(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Run an on-demand board review into the chat session."""

    chat_session = session.get(ChatSession, session_id) or get_or_create_chat_session(session)
    post_chat_system_event(
        session,
        chat_session.id,
        "Running board review…",
        kind="review_progress",
    )
    await run_board_review(session, settings, trigger="chat-button")
    ctx = _chat_template_context(session, settings, chat_session)
    return request.app.state.templates.TemplateResponse(request, "chat_update.html", ctx)


@router.post("/chat/proposals/{proposal_id}/approve", response_class=HTMLResponse)
async def chat_approve_proposal(
    request: Request,
    proposal_id: int,
    session_id: int = Form(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Approve and apply a proposal from within chat."""

    chat_session = session.get(ChatSession, session_id) or get_or_create_chat_session(session)
    proposal = session.get(ProposedChange, proposal_id)
    if proposal is None:
        post_chat_system_event(
            session,
            chat_session.id,
            f"Proposal {proposal_id} not found.",
            kind="error",
        )
    else:
        updated = await approve_proposal(session, settings, proposal_id, settings.app_approver_name)
        if updated.status == ProposalStatus.APPLIED:
            url = (
                ado_work_item_url(settings, updated.applied_work_item_id)
                if updated.applied_work_item_id
                else None
            )
            post_chat_system_event(
                session,
                chat_session.id,
                format_applied_event(
                    title=updated.title,
                    work_item_id=updated.applied_work_item_id,
                    url=url,
                    via="manual approve",
                ),
                kind="applied",
            )
        elif updated.status == ProposalStatus.FAILED:
            post_chat_system_event(
                session,
                chat_session.id,
                format_failed_event(title=updated.title, error=updated.error_message),
                kind="failed",
            )
        else:
            post_chat_system_event(
                session,
                chat_session.id,
                f"Approved: {updated.title} ({updated.status.value})",
                kind="approved",
            )
    session.flush()
    ctx = _chat_template_context(session, settings, chat_session)
    return request.app.state.templates.TemplateResponse(
        request, "chat_proposals_update.html", ctx
    )


@router.post("/chat/proposals/{proposal_id}/retry", response_class=HTMLResponse)
async def chat_retry_proposal(
    request: Request,
    proposal_id: int,
    session_id: int = Form(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Agentically repair and re-apply a failed proposal from chat."""

    chat_session = session.get(ChatSession, session_id) or get_or_create_chat_session(session)
    proposal = session.get(ProposedChange, proposal_id)
    if proposal is None:
        post_chat_system_event(
            session, chat_session.id, f"Proposal {proposal_id} not found.", kind="error"
        )
    else:
        post_chat_system_event(
            session,
            chat_session.id,
            f"Diagnosing ADO error for **{proposal.title}**…",
            kind="retry_progress",
        )
        result = await agentic_retry_proposal(
            session, settings, proposal_id, approver=settings.app_approver_name
        )
        if result.success:
            url = (
                ado_work_item_url(settings, result.proposal.applied_work_item_id)
                if result.proposal.applied_work_item_id
                else None
            )
            post_chat_system_event(
                session,
                chat_session.id,
                format_applied_event(
                    title=result.proposal.title,
                    work_item_id=result.proposal.applied_work_item_id,
                    url=url,
                    via=f"AI repair · {result.loops_used} loop(s)",
                )
                + f"\n\n{result.diagnosis}",
                kind="retry_success",
            )
        else:
            guidance = result.guidance or "Edit the payload manually or reject."
            post_chat_system_event(
                session,
                chat_session.id,
                f"**Could not auto-fix:** {result.proposal.title}\n\n"
                f"{result.diagnosis}\n\n_{guidance}_",
                kind="retry_failed",
            )
    session.flush()
    ctx = _chat_template_context(session, settings, chat_session)
    return request.app.state.templates.TemplateResponse(
        request, "chat_proposals_update.html", ctx
    )


@router.post("/chat/proposals/{proposal_id}/undo", response_class=HTMLResponse)
async def chat_undo_proposal(
    request: Request,
    proposal_id: int,
    session_id: int = Form(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Create a soft-undo compensating proposal."""

    chat_session = session.get(ChatSession, session_id) or get_or_create_chat_session(session)
    proposal = session.get(ProposedChange, proposal_id)
    if proposal is None:
        post_chat_system_event(
            session, chat_session.id, f"Proposal {proposal_id} not found.", kind="error"
        )
    else:
        undo = create_soft_undo_proposal(
            session, settings, proposal, chat_session_id=chat_session.id
        )
        if undo is None:
            post_chat_system_event(
                session,
                chat_session.id,
                f"Cannot soft-undo proposal {proposal_id} (not applied or missing target).",
                kind="error",
            )
        else:
            post_chat_system_event(
                session,
                chat_session.id,
                f"Created soft-undo proposal: **{undo.title}**. Review and approve on the right.",
                kind="undo",
            )
    session.flush()
    ctx = _chat_template_context(session, settings, chat_session)
    return request.app.state.templates.TemplateResponse(
        request, "chat_proposals_update.html", ctx
    )


@router.post("/chat/proposals/{proposal_id}/judge", response_class=HTMLResponse)
async def chat_judge_proposal(
    request: Request,
    proposal_id: int,
    session_id: int = Form(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Judge a proposal from within chat (second-pass safety/confidence)."""

    chat_session = session.get(ChatSession, session_id) or get_or_create_chat_session(session)
    proposal = session.get(ProposedChange, proposal_id)
    if proposal is not None:
        await judge_and_persist(session, settings, proposal=proposal)
    session.flush()
    ctx = _chat_template_context(session, settings, chat_session)
    return request.app.state.templates.TemplateResponse(
        request, "chat_proposals_update.html", ctx
    )


@router.post("/chat/proposals/{proposal_id}/reject", response_class=HTMLResponse)
async def chat_reject_proposal(
    request: Request,
    proposal_id: int,
    session_id: int = Form(...),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    """Reject a proposal from within chat."""

    chat_session = session.get(ChatSession, session_id) or get_or_create_chat_session(session)
    try:
        reject_proposal(session, proposal_id, None)
        post_chat_system_event(
            session,
            chat_session.id,
            f"Rejected proposal {proposal_id}.",
            kind="rejected",
        )
    except Exception as exc:
        post_chat_system_event(
            session,
            chat_session.id,
            f"Reject failed: {exc}",
            kind="error",
        )
    session.flush()
    ctx = _chat_template_context(session, settings, chat_session)
    return request.app.state.templates.TemplateResponse(
        request, "chat_proposals_update.html", ctx
    )



@router.get("/chat/stream/demo")
async def chat_stream_demo() -> StreamingResponse:
    """Demo SSE status stream used by progressive chat UX experiments."""

    from agenticscrum.chat_stream import chat_status_event_stream

    return StreamingResponse(
        chat_status_event_stream(
            messages=[
                "Checking the board…",
                "Reading recent comments…",
                "Drafting next steps…",
            ]
        ),
        media_type="text/event-stream",
    )


def redirect_home() -> RedirectResponse:
    """Return a redirect to the dashboard."""

    return RedirectResponse("/", status_code=303)


def has_running_ingestion(session: Session) -> bool:
    """Return whether an ingestion is already running."""

    return (
        session.scalar(
            select(IngestionRun.id).where(IngestionRun.status == IngestionStatus.RUNNING)
        )
        is not None
    )


async def run_ingestion_background(settings: Settings) -> None:
    """Run scheduled-style ingestion with an isolated DB session."""

    with session_scope(settings) as session:
        await run_ingestion(session, settings)


async def judge_pending_background(settings: Settings) -> None:
    """Judge recent pending proposals using an isolated DB session."""

    with session_scope(settings) as session:
        await auto_judge_and_refine_pending(session, settings, limit=25)


async def judge_one_background(settings: Settings, proposal_id: int) -> None:
    """Judge one proposal using an isolated DB session."""

    with session_scope(settings) as session:
        proposal = session.get(ProposedChange, proposal_id)
        if proposal is None:
            return
        await judge_and_persist(session, settings, proposal=proposal)


async def ingest_manual_transcript_background(
    settings: Settings,
    title: str,
    meeting_date: date,
    notes: str,
) -> None:
    """Run manual transcript ingestion with an isolated DB session."""

    with session_scope(settings) as session:
        run = await ingest_manual_transcript(session, settings, title, meeting_date, notes, source="Manual")
        try:
            write_processed_transcript_copy(
                settings,
                title=title,
                meeting_date=meeting_date,
                notes=notes,
                source="Manual UI",
                run_id=run.id,
                run_status=run.status.value,
            )
        except Exception as exc:
            existing = run.error_message
            warning = f"Failed to save transcript copy to Processed folder: {exc}"
            run.error_message = f"{existing}\n{warning}" if existing else warning
            session.flush()


async def rerun_ingested_meeting_background(settings: Settings, meeting_id: int) -> None:
    """Re-run analysis for an existing ingested meeting."""

    with session_scope(settings) as session:
        meeting = session.get(IngestedMeeting, meeting_id)
        if meeting is None:
            return
        run = await ingest_manual_transcript(
            session,
            settings,
            title=meeting.title,
            meeting_date=meeting.meeting_date,
            notes=meeting.notes,
            source="Rerun",
        )
        try:
            write_processed_transcript_copy(
                settings,
                title=meeting.title,
                meeting_date=meeting.meeting_date,
                notes=meeting.notes,
                source="Rerun",
                run_id=run.id,
                run_status=run.status.value,
            )
        except Exception:
            pass
