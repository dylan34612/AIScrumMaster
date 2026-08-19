"""APScheduler setup for ingestion, approval polling, autopilot, and board review."""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from agenticscrum.apply import poll_approval_responses
from agenticscrum.autopilot import autopilot_apply_pending
from agenticscrum.board_review import run_board_review
from agenticscrum.config import Settings, load_settings
from agenticscrum.db import session_scope
from agenticscrum.inbox import list_inbox_files
from agenticscrum.ingest import run_ingestion
from agenticscrum.models import IngestionRun, IngestionStatus


def build_scheduler(settings: Settings) -> AsyncIOScheduler:
    """Create and configure the app scheduler."""

    timezone = ZoneInfo(settings.app_timezone)
    scheduler = AsyncIOScheduler(timezone=timezone)
    daily_time = parse_time(settings.app_daily_run_time)
    scheduler.add_job(
        _run_ingestion_job,
        CronTrigger(hour=daily_time.hour, minute=daily_time.minute, timezone=timezone),
        args=[settings],
        id="daily-ingestion",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    if settings.app_daily_review_enabled:
        review_time = parse_time(settings.app_daily_review_time)
        scheduler.add_job(
            _daily_board_review_job,
            CronTrigger(
                hour=review_time.hour,
                minute=review_time.minute,
                timezone=timezone,
            ),
            args=[settings],
            id="daily-board-review",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
    scheduler.add_job(
        _approval_poll_job,
        IntervalTrigger(minutes=settings.app_approval_poll_minutes, timezone=timezone),
        args=[settings],
        id="approval-poll",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
    )
    if settings.app_autopilot_enabled:
        scheduler.add_job(
            _autopilot_job,
            IntervalTrigger(minutes=settings.app_autopilot_poll_minutes, timezone=timezone),
            args=[settings],
            id="autopilot",
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
    return scheduler


async def _run_ingestion_job(settings: Settings) -> None:
    current_settings = load_settings()
    with session_scope(current_settings) as session:
        already_running = (
            session.scalar(
                select(IngestionRun.id).where(IngestionRun.status == IngestionStatus.RUNNING)
            )
            is not None
        )
        if already_running:
            return
        await run_ingestion(session, current_settings)


async def _daily_board_review_job(settings: Settings) -> None:
    current_settings = load_settings()
    if not current_settings.app_daily_review_enabled:
        return
    with session_scope(current_settings) as session:
        await run_board_review(session, current_settings, trigger="scheduled")


async def _approval_poll_job(settings: Settings) -> None:
    current_settings = load_settings()
    with session_scope(current_settings) as session:
        await poll_approval_responses(session, current_settings)


async def _autopilot_job(settings: Settings) -> None:
    current_settings = load_settings()
    if not current_settings.app_autopilot_enabled:
        return
    with session_scope(current_settings) as session:
        already_running = (
            session.scalar(
                select(IngestionRun.id).where(IngestionRun.status == IngestionStatus.RUNNING)
            )
            is not None
        )
        if (
            not already_running
            and current_settings.notes_inbox_enabled
            and current_settings.app_autopilot_ingest_inbox
        ):
            try:
                pending_files = list_inbox_files(current_settings)
            except Exception:
                pending_files = []
            if pending_files:
                await run_ingestion(session, current_settings)

        already_running = (
            session.scalar(
                select(IngestionRun.id).where(IngestionRun.status == IngestionStatus.RUNNING)
            )
            is not None
        )
        if not already_running:
            await autopilot_apply_pending(session, current_settings)


def parse_time(value: str) -> time:
    """Parse an HH:MM time value."""

    hour, minute = value.split(":", maxsplit=1)
    return time(hour=int(hour), minute=int(minute))
