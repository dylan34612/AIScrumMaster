"""SQLAlchemy ORM models for Agentic Scrum state."""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy import JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class ChangeType(str, Enum):
    """Supported proposal change types."""

    CREATE = "Create"
    UPDATE = "Update"
    STATE_TRANSITION = "StateTransition"
    ASSIGN = "Assign"
    COMMENT = "Comment"


class WorkItemType(str, Enum):
    """Supported ADO work item types."""

    PBI = "PBI"
    FEATURE = "Feature"
    EPIC = "Epic"
    BUG = "Bug"
    TASK = "Task"


class ProposalStatus(str, Enum):
    """Lifecycle status for proposed changes."""

    PENDING = "Pending"
    APPROVED = "Approved"
    REJECTED = "Rejected"
    APPLIED = "Applied"
    FAILED = "Failed"
    AWAITING_ASSIGNEE_APPROVAL = "AwaitingAssigneeApproval"
    ROLLED_BACK = "RolledBack"


class IngestionStatus(str, Enum):
    """Lifecycle status for ingestion runs."""

    RUNNING = "Running"
    SUCCESS = "Success"
    PARTIAL_FAILURE = "PartialFailure"
    FAILURE = "Failure"


class MeetingSource(Base):
    """Legacy Teams meeting or chat source (unused)."""

    __tablename__ = "meeting_sources"
    __table_args__ = (UniqueConstraint("chat_id", name="uq_meeting_sources_chat_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(512), nullable=False)
    facilitator_sender_filter: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class TeamMember(Base):
    """A team member used for assignee resolution and approval routing."""

    __tablename__ = "team_members"
    __table_args__ = (UniqueConstraint("ado_unique_name", name="uq_team_members_ado_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    ado_unique_name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class IngestionRun(Base):
    """A single ingestion attempt across one or more meeting sources."""

    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[IngestionStatus] = mapped_column(SAEnum(IngestionStatus), nullable=False)
    meetings_processed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    proposals_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class IngestionEvent(Base):
    """A human-readable progress/event log entry for an ingestion run."""

    __tablename__ = "ingestion_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False, default="INFO")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class IngestedMeeting(Base):
    """Meeting notes captured from inbox files or manual transcript paste."""

    __tablename__ = "ingested_meetings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.id"))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    meeting_date: Mapped[date] = mapped_column(Date, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProposedChange(Base):
    """A staged ADO work item change awaiting approval or application."""

    __tablename__ = "proposed_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.id"))
    ingested_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("ingested_meetings.id"))
    source_meeting_title: Mapped[str] = mapped_column(String(255), nullable=False)
    source_meeting_date: Mapped[date] = mapped_column(Date, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    change_type: Mapped[ChangeType] = mapped_column(SAEnum(ChangeType), nullable=False)
    work_item_type: Mapped[WorkItemType] = mapped_column(SAEnum(WorkItemType), nullable=False)
    target_work_item_id: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    confidence_score: Mapped[int] = mapped_column(Integer, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    source_quote: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[ProposalStatus] = mapped_column(
        SAEnum(ProposalStatus), default=ProposalStatus.PENDING, nullable=False
    )
    approved_by: Mapped[str | None] = mapped_column(String(255))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    applied_work_item_id: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    assignee_approval_token: Mapped[str | None] = mapped_column(String(64), index=True)
    ado_approval_comment_id: Mapped[str | None] = mapped_column(String(255))
    teams_approval_message_id: Mapped[str | None] = mapped_column(String(255))
    split_group_id: Mapped[str | None] = mapped_column(String(128), index=True)
    split_from_work_item_id: Mapped[int | None] = mapped_column(Integer)

    revisions: Mapped[list[ProposalRevision]] = relationship(back_populates="proposal")
    judgements: Mapped[list[ProposalJudgement]] = relationship(back_populates="proposal")


class ProposalRevision(Base):
    """A user-requested or LLM-generated revision to a staged proposal."""

    __tablename__ = "proposal_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposed_changes.id"), nullable=False)
    request_text: Mapped[str] = mapped_column(Text, nullable=False)
    previous_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    revised_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    proposal: Mapped[ProposedChange] = relationship(back_populates="revisions")


class ProposalJudgement(Base):
    """A second-pass safety/confidence judgement for a proposal."""

    __tablename__ = "proposal_judgements"
    __table_args__ = (
        UniqueConstraint(
            "proposal_id",
            "payload_hash",
            name="uq_proposal_judgements_proposal_payload",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(
        ForeignKey("proposed_changes.id"), nullable=False, index=True
    )
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    policy_version: Mapped[str] = mapped_column(String(50), nullable=False, default="v1")
    model_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    auto_apply_ok: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    adjusted_confidence: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    reasons: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    request_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    normalized_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    proposal: Mapped[ProposedChange] = relationship(back_populates="judgements")


class ToolCallLog(Base):
    """Audit log for LangChain tool calls made during an ingestion or revision."""

    __tablename__ = "tool_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.id"))
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposed_changes.id"))
    chat_session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id"))
    chat_message_id: Mapped[int | None] = mapped_column(ForeignKey("chat_messages.id"))
    tool_name: Mapped[str] = mapped_column(String(255), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LLMCallLog(Base):
    """Captured LLM request/response for debugging."""

    __tablename__ = "llm_call_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.id"))
    ingested_meeting_id: Mapped[int | None] = mapped_column(ForeignKey("ingested_meetings.id"))
    chat_session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id"))

    operation: Mapped[str] = mapped_column(String(100), nullable=False, default="analyze_meeting")
    model_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    request_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    normalized_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ApprovalResponse(Base):
    """A parsed assignee approval/rejection response from ADO comments."""

    __tablename__ = "approval_responses"
    __table_args__ = (UniqueConstraint("source", "source_message_id", name="uq_approval_source"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposed_changes.id"), nullable=False)
    token: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    responder: Mapped[str | None] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ChatRole(str, Enum):
    """Chat message role."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class ChatSession(Base):
    """A persisted chat session for the UI."""

    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="Scrum Master Chat")
    summary: Mapped[str | None] = mapped_column(Text)
    archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )
    proposal_links: Mapped[list[ChatProposalLink]] = relationship(
        back_populates="session", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    """One user/assistant message in a chat session."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), nullable=False)
    role: Mapped[ChatRole] = mapped_column(SAEnum(ChatRole), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_kind: Mapped[str] = mapped_column(String(50), nullable=False, default="message")
    message_meta: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped[ChatSession] = relationship(back_populates="messages")


class ChatProposalLink(Base):
    """Link a chat session to proposals created from it."""

    __tablename__ = "chat_proposal_links"
    __table_args__ = (
        UniqueConstraint("session_id", "proposal_id", name="uq_chat_proposal_links"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("chat_sessions.id"), nullable=False)
    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposed_changes.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    session: Mapped[ChatSession] = relationship(back_populates="proposal_links")
    proposal: Mapped[ProposedChange] = relationship()


class BoardReviewStatus(str, Enum):
    """Lifecycle status for a daily board review run."""

    RUNNING = "Running"
    SUCCESS = "Success"
    FAILURE = "Failure"


class BoardReviewRun(Base):
    """Audit record for a scheduled or on-demand board hygiene review."""

    __tablename__ = "board_review_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[BoardReviewStatus] = mapped_column(SAEnum(BoardReviewStatus), nullable=False)
    trigger: Mapped[str] = mapped_column(String(50), nullable=False, default="scheduled")
    items_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    comments_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    proposals_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    briefing_text: Mapped[str | None] = mapped_column(Text)
    scanned_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    chat_session_id: Mapped[int | None] = mapped_column(ForeignKey("chat_sessions.id"))
    chat_message_id: Mapped[int | None] = mapped_column(ForeignKey("chat_messages.id"))
