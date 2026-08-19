"""Helpers for posting structured events into chat sessions."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from agenticscrum.models import ChatMessage, ChatRole


def post_chat_system_event(
    session: Session,
    session_id: int,
    text: str,
    *,
    kind: str = "system",
    meta: dict[str, Any] | None = None,
    role: ChatRole = ChatRole.ASSISTANT,
) -> ChatMessage:
    """Persist a chat message used for apply/review/retry progress updates."""

    message = ChatMessage(
        session_id=session_id,
        role=role,
        content=text.strip() or "…",
        message_kind=kind,
        message_meta=meta or {},
    )
    session.add(message)
    session.flush()
    # Touch session updated_at via relationship if loaded elsewhere.
    return message


def format_applied_event(*, title: str, work_item_id: int | None, url: str | None, via: str) -> str:
    """Format a short applied-change chat notice."""

    parts = [f"**Applied** ({via}): {title}"]
    if work_item_id is not None:
        link = f"[#{work_item_id}]({url})" if url else f"#{work_item_id}"
        parts.append(f"Work item: {link}")
    return "\n".join(parts)


def format_failed_event(*, title: str, error: str | None) -> str:
    """Format a failed-apply chat notice."""

    detail = (error or "Unknown error").strip()
    return f"**Failed applying:** {title}\n\n```\n{detail}\n```"


def format_autopilot_summary(
    *,
    applied: list[str],
    needs_approval: list[str],
) -> str:
    """Summarize autopilot results after chat proposal generation."""

    lines = ["**Board update**"]
    if applied:
        lines.append("")
        lines.append("Auto-applied:")
        lines.extend(f"- {item}" for item in applied)
    if needs_approval:
        lines.append("")
        lines.append("Needs your approval:")
        lines.extend(f"- {item}" for item in needs_approval)
    if not applied and not needs_approval:
        lines.append("")
        lines.append("No proposals were created.")
    return "\n".join(lines)
