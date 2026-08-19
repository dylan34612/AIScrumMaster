"""Meeting ingestion orchestration."""

from __future__ import annotations

import hashlib
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from agenticscrum.ado.client import AdoClient
from agenticscrum.config import Settings
from agenticscrum.inbox import finalize_processed_file, read_inbox_notes
from agenticscrum.llm.agent import AgentResult, LLMAnalysisError, analyze_meeting
from agenticscrum.models import (
    ChangeType,
    IngestedMeeting,
    IngestionEvent,
    IngestionRun,
    IngestionStatus,
    LLMCallLog,
    ProposalStatus,
    ProposedChange,
    TeamMember,
    ToolCallLog,
    WorkItemType,
    utc_now,
)
from agenticscrum.proposal_judge import judge_and_refine_proposal
from agenticscrum.schemas import LLMOutputSchema, ProposedChangeSchema


def log_ingestion_event(
    session: Session,
    run: IngestionRun,
    message: str,
    level: str = "INFO",
    commit: bool = False,
) -> None:
    """Persist a progress/event message for an ingestion run."""

    session.add(
        IngestionEvent(
            ingestion_run_id=run.id,
            level=level,
            message=message,
        )
    )
    session.flush()
    if commit:
        session.commit()


def _truncate(value: str, limit: int = 200_000) -> str:
    value = value or ""
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n\n[TRUNCATED: {len(value) - limit} chars]"


def persist_llm_call(
    session: Session,
    settings: Settings,
    *,
    run: IngestionRun,
    ingested: IngestedMeeting,
    operation: str,
    request_prompt: str,
    response_text: str,
    normalized_json: dict[str, object] | None,
    error_message: str | None,
    duration_ms: int,
) -> None:
    session.add(
        LLMCallLog(
            ingestion_run_id=run.id,
            ingested_meeting_id=ingested.id,
            operation=operation,
            model_name=settings.llm_model,
            request_prompt=_truncate(request_prompt),
            response_text=_truncate(response_text),
            normalized_json=normalized_json,
            error_message=_truncate(error_message or "", limit=50_000) if error_message else None,
            duration_ms=duration_ms,
        )
    )
    session.flush()


def build_catalog_index(grounding_catalog: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Index the grounding catalog by work item id."""

    index: dict[int, dict[str, Any]] = {}
    for item in grounding_catalog:
        raw_id = item.get("id")
        if raw_id is None:
            continue
        try:
            index[int(raw_id)] = dict(item)
        except Exception:
            continue
    return index


def _collect_pbi_states(
    grounding_catalog: list[dict[str, Any]],
    allowed_states: list[str] | None = None,
) -> dict[str, str]:
    """Map lowercase state name → canonical casing from catalog and/or process states."""

    by_lower: dict[str, str] = {}
    for item in grounding_catalog:
        raw_type = str(item.get("type") or "")
        if raw_type.lower().startswith("product backlog item") or raw_type.strip() == "PBI":
            state = str(item.get("state") or "").strip()
            if state:
                by_lower.setdefault(state.lower(), state)
    for state in allowed_states or []:
        text = str(state or "").strip()
        if text:
            by_lower.setdefault(text.lower(), text)
    return by_lower


def _pick_state(by_lower: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        found = by_lower.get(candidate.lower())
        if found:
            return found
    return None


def choose_pbi_states(
    grounding_catalog: list[dict[str, Any]],
    *,
    allowed_states: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """Choose safe PBI 'ready' and 'active' states.

    Prefers states observed in the catalog or returned by the process API.
    Falls back to standard Scrum names so all-New boards can still promote to Approved.
    """

    by_lower = _collect_pbi_states(grounding_catalog, allowed_states)
    ready_candidates = ("Approved", "Ready", "To Do")
    active_candidates = ("Committed", "Active", "Doing", "In Progress")

    ready_state = _pick_state(by_lower, ready_candidates)
    active_state = _pick_state(by_lower, active_candidates)

    # Catalog may be all-New; if we could not observe process states, use Scrum defaults.
    if ready_state is None and allowed_states is None:
        ready_state = "Approved"
    if active_state is None and allowed_states is None:
        active_state = "Committed"

    return ready_state, active_state


async def resolve_pbi_states(ado: AdoClient, grounding_catalog: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Resolve ready/active PBI states using catalog + ADO work-item-type states."""

    allowed: list[str] = []
    try:
        raw_states = await ado.get_work_item_type_states("PBI")
        for entry in raw_states:
            if isinstance(entry, dict):
                name = entry.get("name") or entry.get("state")
            else:
                name = entry
            if name:
                allowed.append(str(name))
    except Exception:
        allowed = []
    return choose_pbi_states(grounding_catalog, allowed_states=allowed or None)


def choose_in_progress_state_for_type(
    grounding_catalog: list[dict[str, Any]], work_item_type: WorkItemType
) -> str | None:
    """Choose an 'in progress' state for a work item type based on observed catalog states."""

    states: set[str] = set()
    for item in grounding_catalog:
        raw_type = str(item.get("type") or "").strip().lower()
        if raw_type == work_item_type.value.strip().lower():
            state = str(item.get("state") or "").strip()
            if state:
                states.add(state)

    for candidate in ("In Progress", "Active", "Committed", "Doing", "In Development"):
        if candidate in states:
            return candidate
    return None


def normalize_snapshot_work_item_type(value: object) -> WorkItemType | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.lower().replace("_", " ").replace("-", " ")
    if lowered in {"pbi", "product backlog item", "productbacklogitem"}:
        return WorkItemType.PBI
    if lowered in {"feature"}:
        return WorkItemType.FEATURE
    if lowered in {"epic"}:
        return WorkItemType.EPIC
    if lowered in {"bug"}:
        return WorkItemType.BUG
    if lowered in {"task"}:
        return WorkItemType.TASK
    return None


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    haystack = text.lower()
    return any(needle in haystack for needle in needles)


def infer_pbi_promotion_state(
    change: ProposedChangeSchema,
    *,
    snapshot: dict[str, Any] | None,
    ready_state: str | None,
    active_state: str | None,
) -> str | None:
    """Infer whether to promote a PBI from New → Approved/Committed based on the change text."""

    # Only promote existing items where we have a snapshot from ADO.
    if snapshot is None:
        return None
    if change.target_work_item_id is None:
        return None

    raw_type = str(snapshot.get("type") or "").strip().lower()
    is_pbi = raw_type in {"pbi", "product backlog item", "productbacklogitem"}
    # Prefer ADO snapshot type; fall back to LLM type when snapshot type is missing.
    if not is_pbi and change.work_item_type != WorkItemType.PBI:
        return None
    if change.change_type == ChangeType.STATE_TRANSITION:
        return None
    if not ready_state and not active_state:
        return None

    # Only auto-promote existing PBIs that are currently in New.
    raw_state = str(snapshot.get("state") or "").strip().lower()
    if raw_state and raw_state != "new":
        return None

    text = " ".join(
        [
            str(change.title or ""),
            str(change.rationale or ""),
            str(change.source_quote or ""),
        ]
    )

    active_hints = (
        "working on",
        "work on",
        "started",
        "starting",
        "in progress",
        "progress",
        "implement",
        "implementing",
        "building",
        "developing",
        "fixing",
        "debug",
        "debugging",
        "testing",
        "deploy",
        "blocked",
        "unblocked",
        "pull request",
        "pr ",
        " merged",
        "commit",
        "committed",
    )
    looking_hints = (
        "looking at",
        "look into",
        "take a look",
        "investigat",
        "review",
        "triag",
        "spike",
        "research",
        "analyz",
        "diagnos",
        "checking",
        "check on",
        "approv",
        "accepted",
        "accept into",
        "ready for",
        "ready to",
        "groom",
        "priorit",
        "planned",
        "plan to",
        "pull into",
        "bring into",
        "move to approved",
        "mark as approved",
        "owned by",
        "assign",
        "pick up",
        "take ownership",
    )
    future_hints = (
        "future",
        "later",
        "eventually",
        "someday",
        "maybe",
        "consider",
        "would like",
        "not started",
        "on hold",
        "defer",
        "parked",
        "icebox",
    )

    has_active = _contains_any(text, active_hints)
    has_looking = _contains_any(text, looking_hints)

    # Assignment is usually a signal someone will actively look at it.
    if change.change_type == ChangeType.ASSIGN and (change.new_assignee or "").strip():
        has_looking = True

    # If the only signal is "future work", keep it in New.
    if not has_active and not has_looking and _contains_any(text, future_hints):
        return None

    if has_active:
        return active_state or ready_state
    if has_looking:
        return ready_state or active_state
    return None


async def auto_judge_ingested_meeting(
    session: Session,
    settings: Settings,
    ado: AdoClient,
    *,
    ingested: IngestedMeeting,
) -> int:
    """Auto-judge/refine proposals created for a single ingested meeting."""

    if not settings.app_auto_judge_enabled:
        return 0

    proposals = list(
        session.scalars(
            select(ProposedChange)
            .where(
                ProposedChange.ingested_meeting_id == ingested.id,
                ProposedChange.status == ProposalStatus.PENDING,
            )
            .order_by(desc(ProposedChange.ingested_at))
        )
    )
    session.commit()
    handled = 0
    for proposal in proposals:
        await judge_and_refine_proposal(session, settings, proposal=proposal, ado=ado)
        handled += 1
    return handled


async def run_ingestion(session: Session, settings: Settings) -> IngestionRun:
    """Run ingestion for all enabled meeting sources."""

    run = IngestionRun(
        status=IngestionStatus.RUNNING,
        meetings_processed=0,
        proposals_created=0,
    )
    session.add(run)
    session.flush()
    session.commit()
    errors: list[str] = []
    warnings: list[str] = []
    roster = list(session.scalars(select(TeamMember).where(TeamMember.active.is_(True))))
    log_ingestion_event(
        session,
        run,
        f"Started ingestion. inbox_enabled={settings.notes_inbox_enabled}.",
        commit=True,
    )
    try:
        async with AdoClient(settings) as ado:
            log_ingestion_event(session, run, "Building ADO grounding catalog…", commit=True)
            catalog_start = time.perf_counter()
            grounding_catalog = await ado.build_grounding_catalog()
            catalog_index = build_catalog_index(grounding_catalog)
            pbi_ready_state, pbi_active_state = await resolve_pbi_states(ado, grounding_catalog)
            feature_in_progress_state = choose_in_progress_state_for_type(
                grounding_catalog, WorkItemType.FEATURE
            )
            epic_in_progress_state = choose_in_progress_state_for_type(
                grounding_catalog, WorkItemType.EPIC
            )
            log_ingestion_event(
                session,
                run,
                f"ADO grounding catalog built. items={len(grounding_catalog)} "
                f"pbi_ready={pbi_ready_state!r} pbi_active={pbi_active_state!r} "
                f"duration_ms={int((time.perf_counter() - catalog_start) * 1000)}",
                commit=True,
            )
            # Notes inbox ingestion (no Graph required).
            if settings.notes_inbox_enabled:
                inbox_notes = read_inbox_notes(settings)
                log_ingestion_event(
                    session,
                    run,
                    f"Inbox: found {len(inbox_notes)} candidate file(s).",
                    commit=True,
                )
                for note in inbox_notes:
                    try:
                        log_ingestion_event(
                            session,
                            run,
                            f"Inbox: processing {note.path.name} title='{note.title}' date={note.meeting_date.isoformat()} chars={len(note.notes)}…",
                            commit=True,
                        )
                        existing_meeting = session.scalar(
                            select(IngestedMeeting)
                            .where(
                                IngestedMeeting.content_hash == note.content_hash,
                                IngestedMeeting.meeting_date == note.meeting_date,
                                IngestedMeeting.title == note.title,
                            )
                            .order_by(desc(IngestedMeeting.created_at))
                        )
                        if existing_meeting is not None:
                            log_ingestion_event(
                                session,
                                run,
                                "Inbox: duplicate content detected for "
                                f"{note.path.name} (skipped). "
                                f"Previously ingested: meeting_id={existing_meeting.id} "
                                f"run_id={existing_meeting.ingestion_run_id}. "
                                f"To re-run: POST /ingested-meetings/{existing_meeting.id}/rerun",
                                level="INFO",
                                commit=True,
                            )
                            finalize_processed_file(settings, note)
                            continue

                        ingested = persist_ingested_meeting(
                            session,
                            run,
                            source="Inbox",
                            title=note.title,
                            meeting_date=note.meeting_date,
                            notes=note.notes,
                        )
                        start = time.perf_counter()
                        try:
                            result = await analyze_meeting(
                                settings=settings,
                                ado=ado,
                                meeting_title=note.title,
                                meeting_date=note.meeting_date,
                                meeting_notes=note.notes,
                                grounding_catalog=grounding_catalog,
                                roster=roster,
                            )
                        except LLMAnalysisError as exc:
                            persist_llm_call(
                                session,
                                settings,
                                run=run,
                                ingested=ingested,
                                operation="analyze_meeting",
                                request_prompt=exc.request_prompt,
                                response_text=exc.raw_response_text,
                                normalized_json=exc.normalized_json,
                                error_message=str(exc),
                                duration_ms=int((time.perf_counter() - start) * 1000),
                            )
                            persist_agent_result(
                                session,
                                run,
                                ingested,
                                AgentResult(
                                    output=LLMOutputSchema.model_validate(
                                        {
                                            "sourceMeeting": note.title,
                                            "sourceMeetingDate": note.meeting_date.isoformat(),
                                            "sourceLoopUrl": None,
                                            "processedAt": datetime.now(timezone.utc).isoformat(),
                                            "proposedChanges": [],
                                            "unmatchedDiscussion": [],
                                        }
                                    ),
                                    tool_calls=exc.tool_calls,
                                    request_prompt=exc.request_prompt,
                                    raw_response_text=exc.raw_response_text,
                                    normalized_json=exc.normalized_json,
                                ),
                                catalog_index=catalog_index,
                                pbi_ready_state=pbi_ready_state,
                                pbi_active_state=pbi_active_state,
                                feature_in_progress_state=feature_in_progress_state,
                                epic_in_progress_state=epic_in_progress_state,
                            )
                            raise
                        persist_llm_call(
                            session,
                            settings,
                            run=run,
                            ingested=ingested,
                            operation="analyze_meeting",
                            request_prompt=result.request_prompt,
                            response_text=result.raw_response_text,
                            normalized_json=result.normalized_json,
                            error_message=None,
                            duration_ms=int((time.perf_counter() - start) * 1000),
                        )
                        created = persist_agent_result(
                            session,
                            run,
                            ingested,
                            result,
                            catalog_index=catalog_index,
                            pbi_ready_state=pbi_ready_state,
                            pbi_active_state=pbi_active_state,
                            feature_in_progress_state=feature_in_progress_state,
                            epic_in_progress_state=epic_in_progress_state,
                        )
                        run.meetings_processed += 1
                        run.proposals_created += created
                        judged = 0
                        if created and settings.app_auto_judge_enabled:
                            log_ingestion_event(
                                session,
                                run,
                                f"Auto-judge: starting {created} proposal(s) for inbox '{note.title}'…",
                                commit=True,
                            )
                            try:
                                judged = await auto_judge_ingested_meeting(
                                    session, settings, ado, ingested=ingested
                                )
                            except Exception as exc:
                                warnings.append(f"Auto-judge Inbox {note.title}: {exc}")
                                log_ingestion_event(
                                    session,
                                    run,
                                    f"Auto-judge: failed for inbox '{note.title}': {exc}",
                                    level="WARN",
                                    commit=True,
                                )
                        log_ingestion_event(
                            session,
                            run,
                            f"Inbox: finished {note.path.name}. proposals={created} judged={judged} duration_ms={int((time.perf_counter() - start) * 1000)}",
                            commit=True,
                        )
                        finalize_processed_file(settings, note)
                    except Exception as exc:
                        errors.append(f"Inbox {note.path.name}: {exc}")
                        log_ingestion_event(
                            session,
                            run,
                            f"Inbox: error processing {note.path.name}: {exc}",
                            level="ERROR",
                            commit=True,
                        )
    except Exception as exc:
        errors.append(f"Setup/catalog failure: {exc}")
        log_ingestion_event(
            session,
            run,
            f"Setup/catalog failure: {exc}",
            level="ERROR",
            commit=True,
        )
    run.completed_at = datetime.now(timezone.utc)
    run.status = (
        IngestionStatus.SUCCESS
        if not errors
        else IngestionStatus.PARTIAL_FAILURE
        if run.meetings_processed
        else IngestionStatus.FAILURE
    )
    message_lines: list[str] = []
    if errors:
        message_lines.append("Errors:")
        message_lines.extend(errors)
    if warnings:
        if message_lines:
            message_lines.append("")
        message_lines.append("Warnings:")
        message_lines.extend(warnings)
    run.error_message = "\n".join(message_lines) if message_lines else None
    session.flush()
    log_ingestion_event(
        session,
        run,
        f"Completed ingestion. status={run.status.value} meetings_processed={run.meetings_processed} proposals_created={run.proposals_created}",
    )
    session.commit()
    return run


async def ingest_manual_transcript(
    session: Session,
    settings: Settings,
    title: str,
    meeting_date: date,
    notes: str,
    source: str = "Manual",
    *,
    dedupe_against_pending: bool = False,
) -> IngestionRun:
    """Run ingestion for a pasted meeting transcript."""

    run = IngestionRun(status=IngestionStatus.RUNNING, meetings_processed=0, proposals_created=0)
    session.add(run)
    session.flush()
    session.commit()
    roster = list(session.scalars(select(TeamMember).where(TeamMember.active.is_(True))))
    try:
        async with AdoClient(settings) as ado:
            log_ingestion_event(
                session,
                run,
                f"Manual transcript started. title='{title}' date={meeting_date.isoformat()} chars={len(notes)}",
                commit=True,
            )
            log_ingestion_event(session, run, "Building ADO grounding catalog…", commit=True)
            catalog_start = time.perf_counter()
            grounding_catalog = await ado.build_grounding_catalog()
            catalog_index = build_catalog_index(grounding_catalog)
            pbi_ready_state, pbi_active_state = await resolve_pbi_states(ado, grounding_catalog)
            feature_in_progress_state = choose_in_progress_state_for_type(
                grounding_catalog, WorkItemType.FEATURE
            )
            epic_in_progress_state = choose_in_progress_state_for_type(
                grounding_catalog, WorkItemType.EPIC
            )
            log_ingestion_event(
                session,
                run,
                f"ADO grounding catalog built. items={len(grounding_catalog)} "
                f"pbi_ready={pbi_ready_state!r} pbi_active={pbi_active_state!r} "
                f"duration_ms={int((time.perf_counter() - catalog_start) * 1000)}",
                commit=True,
            )
            log_ingestion_event(session, run, "Analyzing transcript with LLM…", commit=True)
            start = time.perf_counter()
            ingested = persist_ingested_meeting(session, run, source, title, meeting_date, notes)
            try:
                result = await analyze_meeting(
                    settings=settings,
                    ado=ado,
                    meeting_title=title,
                    meeting_date=meeting_date,
                    meeting_notes=notes,
                    grounding_catalog=grounding_catalog,
                    roster=roster,
                )
            except LLMAnalysisError as exc:
                persist_llm_call(
                    session,
                    settings,
                    run=run,
                    ingested=ingested,
                    operation="analyze_meeting",
                    request_prompt=exc.request_prompt,
                    response_text=exc.raw_response_text,
                    normalized_json=exc.normalized_json,
                    error_message=str(exc),
                    duration_ms=int((time.perf_counter() - start) * 1000),
                )
                persist_agent_result(
                    session,
                    run,
                    ingested,
                    AgentResult(
                        output=LLMOutputSchema.model_validate(
                            {
                                "sourceMeeting": title,
                                "sourceMeetingDate": meeting_date.isoformat(),
                                "sourceLoopUrl": None,
                                "processedAt": datetime.now(timezone.utc).isoformat(),
                                "proposedChanges": [],
                                "unmatchedDiscussion": [],
                            }
                        ),
                        tool_calls=exc.tool_calls,
                        request_prompt=exc.request_prompt,
                        raw_response_text=exc.raw_response_text,
                        normalized_json=exc.normalized_json,
                    ),
                    catalog_index=catalog_index,
                    pbi_ready_state=pbi_ready_state,
                    pbi_active_state=pbi_active_state,
                    feature_in_progress_state=feature_in_progress_state,
                    epic_in_progress_state=epic_in_progress_state,
                    dedupe_against_pending=dedupe_against_pending,
                )
                raise
            persist_llm_call(
                session,
                settings,
                run=run,
                ingested=ingested,
                operation="analyze_meeting",
                request_prompt=result.request_prompt,
                response_text=result.raw_response_text,
                normalized_json=result.normalized_json,
                error_message=None,
                duration_ms=int((time.perf_counter() - start) * 1000),
            )
        created = persist_agent_result(
            session,
            run,
            ingested,
            result,
            catalog_index=catalog_index,
            pbi_ready_state=pbi_ready_state,
            pbi_active_state=pbi_active_state,
            feature_in_progress_state=feature_in_progress_state,
            epic_in_progress_state=epic_in_progress_state,
            dedupe_against_pending=dedupe_against_pending,
        )
        run.meetings_processed = 1
        run.proposals_created = created
        run.status = IngestionStatus.SUCCESS
        judged = 0
        if created and settings.app_auto_judge_enabled:
            log_ingestion_event(
                session,
                run,
                f"Auto-judge: starting {created} proposal(s) for manual transcript '{title}'…",
                commit=True,
            )
            try:
                async with AdoClient(settings) as judge_ado:
                    judged = await auto_judge_ingested_meeting(
                        session, settings, judge_ado, ingested=ingested
                    )
            except Exception as exc:
                log_ingestion_event(
                    session,
                    run,
                    f"Auto-judge: failed for manual transcript '{title}': {exc}",
                    level="WARN",
                    commit=True,
                )
        log_ingestion_event(
            session,
            run,
            f"Manual transcript finished. proposals={created} judged={judged} duration_ms={int((time.perf_counter() - start) * 1000)}",
            commit=True,
        )
    except Exception as exc:
        run.status = IngestionStatus.FAILURE
        run.error_message = str(exc)
        log_ingestion_event(
            session,
            run,
            f"Manual transcript failed: {exc}",
            level="ERROR",
        )
    run.completed_at = datetime.now(timezone.utc)
    session.flush()
    session.commit()
    return run


def persist_ingested_meeting(
    session: Session,
    run: IngestionRun,
    source: str,
    title: str,
    meeting_date: date,
    notes: str,
) -> IngestedMeeting:
    """Persist captured meeting notes."""

    ingested = IngestedMeeting(
        ingestion_run_id=run.id,
        source=source,
        title=title,
        meeting_date=meeting_date,
        notes=notes,
        content_hash=hashlib.sha256(notes.encode("utf-8")).hexdigest(),
    )
    session.add(ingested)
    session.flush()
    return ingested


def derive_parent_state_rollups(
    session: Session,
    *,
    run: IngestionRun,
    ingested: IngestedMeeting,
    output: LLMOutputSchema,
    proposals: list[ProposedChange],
    catalog_index: dict[int, dict[str, Any]] | None,
    pbi_active_state: str | None,
    feature_in_progress_state: str | None,
    epic_in_progress_state: str | None,
) -> list[ProposedChange]:
    """Derive parent (Feature/Epic) state proposals from active child PBIs."""

    if not catalog_index:
        return []
    if not feature_in_progress_state and not epic_in_progress_state:
        return []

    def normalize_state(value: object | None) -> str:
        return str(value or "").strip()

    def is_done_state(state: str) -> bool:
        return state.strip().lower() in {"done", "closed", "removed"}

    def is_in_progress_state(state: str) -> bool:
        return state.strip().lower() in {
            "in progress",
            "active",
            "committed",
            "doing",
            "in development",
        }

    def is_active_pbi_state(state: str) -> bool:
        if not state.strip():
            return False
        s = state.strip().lower()
        if pbi_active_state and s == pbi_active_state.strip().lower():
            return True
        return s in {"committed", "active", "in progress", "doing"}

    existing_state_targets: set[int] = {
        int(p.target_work_item_id)
        for p in proposals
        if p.target_work_item_id is not None and p.change_type == ChangeType.STATE_TRANSITION
    }
    derived_targets: set[int] = set()
    derived: list[ProposedChange] = []

    def already_has_pending_state_transition(target_id: int) -> bool:
        existing = session.scalar(
            select(ProposedChange.id)
            .where(
                ProposedChange.status == ProposalStatus.PENDING,
                ProposedChange.change_type == ChangeType.STATE_TRANSITION,
                ProposedChange.target_work_item_id == target_id,
            )
            .limit(1)
        )
        return existing is not None

    def build_state_transition_proposal(
        *,
        target_id: int,
        target_type: WorkItemType,
        desired_state: str,
        target_snapshot: dict[str, Any],
        from_child: ProposedChange,
        child_state: str,
    ) -> ProposedChange:
        parent_title = str(target_snapshot.get("title") or "").strip()
        title = (
            f"Move {target_type.value} to {desired_state}"
            + (f": {parent_title}" if parent_title else "")
        )
        child_id = from_child.target_work_item_id
        child_title = None
        child_snapshot = from_child.proposed_payload.get("targetSnapshot")
        if isinstance(child_snapshot, dict):
            child_title = child_snapshot.get("title")
        rationale = (
            f"Child PBI #{child_id} is {child_state or 'in progress'}. "
            f"Promote parent {target_type.value} to {desired_state} to reflect active work."
        )
        if child_title:
            rationale = (
                f"Child PBI #{child_id} ({child_title}) is {child_state or 'in progress'}. "
                f"Promote parent {target_type.value} to {desired_state} to reflect active work."
            )

        payload: dict[str, Any] = {
            "fieldUpdates": {},
            "newState": desired_state,
            "rollupFromWorkItemId": child_id,
            "rollupFromState": child_state,
            "targetSnapshot": target_snapshot,
        }
        return ProposedChange(
            ingestion_run_id=run.id,
            ingested_meeting_id=ingested.id,
            source_meeting_title=output.source_meeting,
            source_meeting_date=output.source_meeting_date,
            change_type=ChangeType.STATE_TRANSITION,
            work_item_type=target_type,
            target_work_item_id=target_id,
            title=title,
            confidence_score=70,
            rationale=rationale,
            source_quote=from_child.source_quote,
            proposed_payload=payload,
            status=ProposalStatus.PENDING,
        )

    def maybe_add_rollup(target_id: int, from_child: ProposedChange, child_state: str) -> None:
        if target_id in existing_state_targets or target_id in derived_targets:
            return
        if already_has_pending_state_transition(target_id):
            return

        snapshot = catalog_index.get(int(target_id))
        if not isinstance(snapshot, dict):
            return
        raw_type = str(snapshot.get("type") or "").strip().lower()
        raw_state = normalize_state(snapshot.get("state"))
        if is_done_state(raw_state):
            return
        if is_in_progress_state(raw_state):
            return

        if raw_type == "feature":
            desired = feature_in_progress_state
            wtype = WorkItemType.FEATURE
        elif raw_type == "epic":
            desired = epic_in_progress_state
            wtype = WorkItemType.EPIC
        else:
            return
        if not desired:
            return

        if normalize_state(desired).strip().lower() == raw_state.strip().lower():
            return

        derived.append(
            build_state_transition_proposal(
                target_id=target_id,
                target_type=wtype,
                desired_state=desired,
                target_snapshot=snapshot,
                from_child=from_child,
                child_state=child_state,
            )
        )
        derived_targets.add(target_id)

    for proposal in proposals:
        if proposal.work_item_type != WorkItemType.PBI:
            continue
        if proposal.target_work_item_id is None:
            continue
        payload = dict(proposal.proposed_payload or {})
        field_updates = payload.get("fieldUpdates")
        field_updates_dict = field_updates if isinstance(field_updates, dict) else {}
        snapshot = payload.get("targetSnapshot")
        snapshot_dict = snapshot if isinstance(snapshot, dict) else None

        child_state = normalize_state(
            payload.get("newState")
            or field_updates_dict.get("System.State")
            or (snapshot_dict.get("state") if snapshot_dict else None)
        )
        if not is_active_pbi_state(child_state):
            continue

        parent_id_raw = payload.get("parentWorkItemId")
        if parent_id_raw is None and snapshot_dict is not None:
            parent_id_raw = snapshot_dict.get("parentId")
        if parent_id_raw is None:
            continue
        try:
            parent_id = int(parent_id_raw)
        except Exception:
            continue

        maybe_add_rollup(parent_id, proposal, child_state)

        parent_snapshot = catalog_index.get(parent_id)
        if isinstance(parent_snapshot, dict):
            grandparent_raw = parent_snapshot.get("parentId")
            if grandparent_raw is not None:
                try:
                    grandparent_id = int(grandparent_raw)
                except Exception:
                    grandparent_id = None
                if grandparent_id:
                    maybe_add_rollup(grandparent_id, proposal, child_state)

    return derived


def _normalize_text(value: object | None) -> str:
    return " ".join(str(value or "").split()).strip().lower()


def proposal_dedupe_fingerprint(
    *,
    change_type: ChangeType,
    target_work_item_id: int | None,
    title: str,
    payload: dict[str, Any] | None,
) -> tuple[Any, ...]:
    """Build a stable fingerprint used to skip repetitive pending proposals."""

    payload = dict(payload or {})
    field_updates = payload.get("fieldUpdates")
    field_updates_dict = field_updates if isinstance(field_updates, dict) else {}

    if change_type == ChangeType.CREATE:
        create_title = _normalize_text(
            field_updates_dict.get("System.Title") or title
        )
        return ("Create", create_title)

    target = int(target_work_item_id) if target_work_item_id is not None else None
    if change_type == ChangeType.STATE_TRANSITION:
        return ("StateTransition", target, _normalize_text(payload.get("newState")))
    if change_type == ChangeType.ASSIGN:
        return ("Assign", target, _normalize_text(payload.get("newAssignee")))
    if change_type == ChangeType.COMMENT:
        # Board hygiene should not stack more comments on the same card while one is pending.
        return ("Comment", target)
    if change_type == ChangeType.UPDATE:
        keys = tuple(sorted(str(k) for k in field_updates_dict.keys()))
        return ("Update", target, keys)
    return (change_type.value, target, _normalize_text(title))


def rejected_comment_dedupe_fingerprint(
    *,
    target_work_item_id: int | None,
    title: str,
    payload: dict[str, Any] | None,
) -> tuple[Any, ...]:
    """Fingerprint for skipping Comment proposals that match a rejected comment body."""

    payload = dict(payload or {})
    target = int(target_work_item_id) if target_work_item_id is not None else None
    comment_text = _normalize_text(payload.get("commentText") or title)[:240]
    return ("RejectedComment", target, comment_text)


def _load_outstanding_dedupe_fingerprints(session: Session) -> set[tuple[Any, ...]]:
    """Fingerprints for outstanding proposals and recently rejected comments."""

    outstanding = session.scalars(
        select(ProposedChange).where(
            ProposedChange.status.in_(
                (
                    ProposalStatus.PENDING,
                    ProposalStatus.AWAITING_ASSIGNEE_APPROVAL,
                )
            )
        )
    )
    fingerprints: set[tuple[Any, ...]] = set()
    for proposal in outstanding:
        fingerprints.add(
            proposal_dedupe_fingerprint(
                change_type=proposal.change_type,
                target_work_item_id=proposal.target_work_item_id,
                title=proposal.title,
                payload=dict(proposal.proposed_payload or {}),
            )
        )

    # Also block re-creating the same rejected comment text on the same work item.
    cutoff = utc_now() - timedelta(days=45)
    rejected_comments = session.scalars(
        select(ProposedChange).where(
            ProposedChange.status == ProposalStatus.REJECTED,
            ProposedChange.change_type == ChangeType.COMMENT,
            ProposedChange.ingested_at >= cutoff,
        )
    )
    for proposal in rejected_comments:
        fingerprints.add(
            rejected_comment_dedupe_fingerprint(
                target_work_item_id=proposal.target_work_item_id,
                title=proposal.title,
                payload=dict(proposal.proposed_payload or {}),
            )
        )
    return fingerprints


def persist_agent_result(
    session: Session,
    run: IngestionRun,
    ingested: IngestedMeeting,
    result: AgentResult,
    *,
    catalog_index: dict[int, dict[str, Any]] | None = None,
    pbi_ready_state: str | None = None,
    pbi_active_state: str | None = None,
    feature_in_progress_state: str | None = None,
    epic_in_progress_state: str | None = None,
    dedupe_against_pending: bool = False,
) -> int:
    """Persist LLM proposals and tool-call logs."""

    for record in result.tool_calls:
        session.add(
            ToolCallLog(
                ingestion_run_id=run.id,
                tool_name=record.tool_name,
                input_payload=record.input_payload,
                output_payload=record.output_payload,
                duration_ms=record.duration_ms,
            )
        )
    return persist_llm_output(
        session,
        run,
        ingested,
        result.output,
        catalog_index=catalog_index,
        pbi_ready_state=pbi_ready_state,
        pbi_active_state=pbi_active_state,
        feature_in_progress_state=feature_in_progress_state,
        epic_in_progress_state=epic_in_progress_state,
        dedupe_against_pending=dedupe_against_pending,
    )


def _build_promotion_state_transition(
    *,
    run: IngestionRun,
    ingested: IngestedMeeting,
    output: LLMOutputSchema,
    from_change: ProposedChangeSchema,
    desired_state: str,
    snapshot: dict[str, Any],
) -> ProposedChange:
    """Build a dedicated StateTransition for New → ready/active promotion."""

    target_id = int(from_change.target_work_item_id)  # type: ignore[arg-type]
    parent_title = str(snapshot.get("title") or "").strip()
    title = f"Move PBI to {desired_state}" + (f": {parent_title}" if parent_title else "")
    rationale = (
        f"Existing PBI #{target_id} is still in New. "
        f"Meeting notes indicate it should move to {desired_state}."
    )
    payload: dict[str, Any] = {
        "fieldUpdates": {},
        "newState": desired_state,
        "promotedFrom": "New",
        "promotedFromChangeType": from_change.change_type.value,
        "targetSnapshot": snapshot,
    }
    return ProposedChange(
        ingestion_run_id=run.id,
        ingested_meeting_id=ingested.id,
        source_meeting_title=output.source_meeting,
        source_meeting_date=output.source_meeting_date,
        change_type=ChangeType.STATE_TRANSITION,
        work_item_type=WorkItemType.PBI,
        target_work_item_id=target_id,
        title=title,
        confidence_score=max(60, min(int(from_change.confidence_score), 85)),
        rationale=rationale,
        source_quote=from_change.source_quote,
        proposed_payload=payload,
        status=ProposalStatus.PENDING,
    )


def persist_llm_output(
    session: Session,
    run: IngestionRun,
    ingested: IngestedMeeting,
    output: LLMOutputSchema,
    *,
    catalog_index: dict[int, dict[str, Any]] | None = None,
    pbi_ready_state: str | None = None,
    pbi_active_state: str | None = None,
    feature_in_progress_state: str | None = None,
    epic_in_progress_state: str | None = None,
    dedupe_against_pending: bool = False,
) -> int:
    """Persist validated LLM output as pending proposals."""

    existing_fingerprints = (
        _load_outstanding_dedupe_fingerprints(session) if dedupe_against_pending else set()
    )
    seen_in_batch: set[tuple[Any, ...]] = set()
    state_targets: set[int] = set()

    count = 0
    skipped = 0
    rows: list[ProposedChange] = []
    for change in output.proposed_changes:
        payload_preview = change.model_dump(by_alias=True, mode="json")
        # Normalize LLM state patches on Update/Assign/Comment into dedicated transitions later.
        if change.change_type != ChangeType.STATE_TRANSITION:
            field_updates = payload_preview.get("fieldUpdates")
            if isinstance(field_updates, dict) and "System.State" in field_updates:
                field_updates = dict(field_updates)
                field_updates.pop("System.State", None)
                payload_preview["fieldUpdates"] = field_updates
        fingerprint = proposal_dedupe_fingerprint(
            change_type=change.change_type,
            target_work_item_id=change.target_work_item_id,
            title=change.title,
            payload=payload_preview,
        )
        rejected_fp = None
        if change.change_type == ChangeType.COMMENT and dedupe_against_pending:
            rejected_fp = rejected_comment_dedupe_fingerprint(
                target_work_item_id=change.target_work_item_id,
                title=change.title,
                payload=payload_preview,
            )
        if (
            fingerprint in existing_fingerprints
            or fingerprint in seen_in_batch
            or (rejected_fp is not None and rejected_fp in existing_fingerprints)
        ):
            skipped += 1
            continue
        seen_in_batch.add(fingerprint)
        row = build_proposed_change(
            run,
            ingested,
            output,
            change,
            catalog_index=catalog_index,
            pbi_ready_state=pbi_ready_state,
            pbi_active_state=pbi_active_state,
        )
        stored_payload = dict(row.proposed_payload or {})
        stored_fingerprint = proposal_dedupe_fingerprint(
            change_type=row.change_type,
            target_work_item_id=row.target_work_item_id,
            title=row.title,
            payload=stored_payload,
        )
        stored_rejected_fp = None
        if row.change_type == ChangeType.COMMENT and dedupe_against_pending:
            stored_rejected_fp = rejected_comment_dedupe_fingerprint(
                target_work_item_id=row.target_work_item_id,
                title=row.title,
                payload=stored_payload,
            )
        if stored_fingerprint in existing_fingerprints or (
            stored_rejected_fp is not None and stored_rejected_fp in existing_fingerprints
        ):
            skipped += 1
            continue
        existing_fingerprints.add(stored_fingerprint)
        seen_in_batch.add(stored_fingerprint)
        if (
            row.change_type == ChangeType.STATE_TRANSITION
            and row.target_work_item_id is not None
        ):
            state_targets.add(int(row.target_work_item_id))
        session.add(row)
        rows.append(row)
        count += 1

        # Companion StateTransition for existing New PBIs (Assign/Update/Comment signals).
        if change.change_type != ChangeType.STATE_TRANSITION and catalog_index:
            snapshot = None
            if change.target_work_item_id is not None:
                snapshot = catalog_index.get(int(change.target_work_item_id))
            desired_state = infer_pbi_promotion_state(
                change,
                snapshot=snapshot if isinstance(snapshot, dict) else None,
                ready_state=pbi_ready_state,
                active_state=pbi_active_state,
            )
            # Also honor an LLM-requested System.State on non-transition changes.
            if not desired_state:
                raw_fields = change.field_updates if isinstance(change.field_updates, dict) else {}
                maybe_state = raw_fields.get("System.State") or change.new_state
                if maybe_state and isinstance(snapshot, dict):
                    current = str(snapshot.get("state") or "").strip().lower()
                    if current in {"", "new"}:
                        desired_state = str(maybe_state).strip() or None
            if (
                desired_state
                and change.target_work_item_id is not None
                and int(change.target_work_item_id) not in state_targets
                and isinstance(snapshot, dict)
            ):
                promo_fp = proposal_dedupe_fingerprint(
                    change_type=ChangeType.STATE_TRANSITION,
                    target_work_item_id=change.target_work_item_id,
                    title=f"Move PBI to {desired_state}",
                    payload={"newState": desired_state},
                )
                if promo_fp not in existing_fingerprints and promo_fp not in seen_in_batch:
                    promo = _build_promotion_state_transition(
                        run=run,
                        ingested=ingested,
                        output=output,
                        from_change=change,
                        desired_state=desired_state,
                        snapshot=snapshot,
                    )
                    session.add(promo)
                    rows.append(promo)
                    state_targets.add(int(change.target_work_item_id))
                    existing_fingerprints.add(promo_fp)
                    seen_in_batch.add(promo_fp)
                    count += 1

    for derived in derive_parent_state_rollups(
        session,
        run=run,
        ingested=ingested,
        output=output,
        proposals=rows,
        catalog_index=catalog_index,
        pbi_active_state=pbi_active_state,
        feature_in_progress_state=feature_in_progress_state,
        epic_in_progress_state=epic_in_progress_state,
    ):
        if dedupe_against_pending:
            fingerprint = proposal_dedupe_fingerprint(
                change_type=derived.change_type,
                target_work_item_id=derived.target_work_item_id,
                title=derived.title,
                payload=dict(derived.proposed_payload or {}),
            )
            if fingerprint in existing_fingerprints:
                skipped += 1
                continue
            existing_fingerprints.add(fingerprint)
        session.add(derived)
        count += 1
    if skipped:
        log_ingestion_event(
            session,
            run,
            f"Skipped {skipped} proposal(s) already covered by pending or rejected changes.",
        )
    session.flush()
    return count


def build_proposed_change(
    run: IngestionRun,
    ingested: IngestedMeeting,
    output: LLMOutputSchema,
    change: ProposedChangeSchema,
    *,
    catalog_index: dict[int, dict[str, Any]] | None = None,
    pbi_ready_state: str | None = None,
    pbi_active_state: str | None = None,
) -> ProposedChange:
    """Convert a pydantic change into an ORM row."""

    payload = change.model_dump(by_alias=True, mode="json")
    effective_type: WorkItemType = change.work_item_type
    # Attach a snapshot of the target work item for UI clarity.
    if change.target_work_item_id is not None and catalog_index:
        snapshot = catalog_index.get(int(change.target_work_item_id))
        if snapshot:
            payload["targetSnapshot"] = snapshot
            inferred = normalize_snapshot_work_item_type(snapshot.get("type"))
            if inferred is not None:
                effective_type = inferred

    # Keep fieldUpdates clean: New→Approved/Committed promotions are separate StateTransitions.
    field_updates = payload.get("fieldUpdates")
    if not isinstance(field_updates, dict):
        field_updates = {}
        payload["fieldUpdates"] = field_updates
    if change.change_type != ChangeType.STATE_TRANSITION:
        field_updates.pop("System.State", None)
    else:
        # Canonicalize newState casing against known ready/active names.
        raw_new_state = str(payload.get("newState") or "").strip()
        for candidate in (pbi_ready_state, pbi_active_state):
            if candidate and raw_new_state.lower() == candidate.lower():
                payload["newState"] = candidate
                break

    if change.change_type == ChangeType.CREATE:
        field_updates["System.Title"] = change.title

    return ProposedChange(
        ingestion_run_id=run.id,
        ingested_meeting_id=ingested.id,
        source_meeting_title=output.source_meeting,
        source_meeting_date=output.source_meeting_date,
        change_type=change.change_type,
        work_item_type=effective_type,
        target_work_item_id=change.target_work_item_id,
        title=change.title,
        confidence_score=change.confidence_score,
        rationale=change.rationale,
        source_quote=change.source_quote,
        proposed_payload=payload,
        status=ProposalStatus.PENDING,
        split_group_id=change.split_group_id,
        split_from_work_item_id=change.split_from_work_item_id,
    )
