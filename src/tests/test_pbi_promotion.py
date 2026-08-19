from __future__ import annotations

from datetime import date, datetime, timezone

from agenticscrum.ingest import (
    choose_pbi_states,
    infer_pbi_promotion_state,
    persist_llm_output,
)
from agenticscrum.models import (
    ChangeType,
    IngestedMeeting,
    IngestionRun,
    IngestionStatus,
    ProposalStatus,
)
from agenticscrum.schemas import LLMOutputSchema, ProposedChangeSchema


def test_choose_pbi_states_defaults_when_catalog_all_new() -> None:
    catalog = [
        {"id": 1, "type": "Product Backlog Item", "state": "New"},
        {"id": 2, "type": "Product Backlog Item", "state": "New"},
    ]
    ready, active = choose_pbi_states(catalog)
    assert ready == "Approved"
    assert active == "Committed"


def test_choose_pbi_states_uses_allowed_process_states() -> None:
    catalog = [{"id": 1, "type": "Product Backlog Item", "state": "New"}]
    ready, active = choose_pbi_states(
        catalog,
        allowed_states=["New", "Approved", "Committed", "Done"],
    )
    assert ready == "Approved"
    assert active == "Committed"


def test_choose_pbi_states_respects_missing_approved_in_process() -> None:
    catalog = [{"id": 1, "type": "Product Backlog Item", "state": "New"}]
    ready, active = choose_pbi_states(
        catalog,
        allowed_states=["New", "Committed", "Done"],
    )
    assert ready is None
    assert active == "Committed"


def test_infer_promotes_assigned_new_pbi_to_approved() -> None:
    change = ProposedChangeSchema.model_validate(
        {
            "changeType": "Assign",
            "workItemType": "Bug",  # wrong LLM type should not block snapshot PBI
            "targetWorkItemId": 42,
            "title": "Assign owner",
            "confidenceScore": 80,
            "rationale": "Alex will look into this",
            "sourceQuote": "Alex will look into #42",
            "newAssignee": "Alex",
        }
    )
    desired = infer_pbi_promotion_state(
        change,
        snapshot={"type": "Product Backlog Item", "state": "New", "title": "Fix login"},
        ready_state="Approved",
        active_state="Committed",
    )
    assert desired == "Approved"


def test_infer_promotes_active_work_to_committed() -> None:
    change = ProposedChangeSchema.model_validate(
        {
            "changeType": "Comment",
            "workItemType": "PBI",
            "targetWorkItemId": 7,
            "title": "Capture progress",
            "confidenceScore": 70,
            "rationale": "Implementation started",
            "sourceQuote": "I started implementing the export flow",
            "commentText": "Implementation is underway.",
        }
    )
    desired = infer_pbi_promotion_state(
        change,
        snapshot={"type": "Product Backlog Item", "state": "New", "title": "Export"},
        ready_state="Approved",
        active_state="Committed",
    )
    assert desired == "Committed"


def test_persist_creates_companion_state_transition(tmp_path) -> None:
    from sqlalchemy import select

    from agenticscrum.config import Settings
    from agenticscrum.db import init_db, session_scope
    from agenticscrum.models import ProposedChange

    settings = Settings(app_db_path=str(tmp_path / "promo.db"))
    init_db(settings)
    with session_scope(settings) as session:
        run = IngestionRun(
            status=IngestionStatus.RUNNING, meetings_processed=0, proposals_created=0
        )
        session.add(run)
        session.flush()
        ingested = IngestedMeeting(
            ingestion_run_id=run.id,
            source="Test",
            title="Standup",
            meeting_date=date(2026, 8, 12),
            notes="Alex will look into #42",
            content_hash="abc",
        )
        session.add(ingested)
        session.flush()

        output = LLMOutputSchema.model_validate(
            {
                "sourceMeeting": "Standup",
                "sourceMeetingDate": "2026-08-12",
                "processedAt": datetime.now(timezone.utc).isoformat(),
                "proposedChanges": [
                    {
                        "changeType": "Assign",
                        "workItemType": "PBI",
                        "targetWorkItemId": 42,
                        "title": "Assign Alex",
                        "confidenceScore": 80,
                        "rationale": "Alex will look into this item",
                        "sourceQuote": "Alex will look into #42",
                        "newAssignee": "Alex",
                    }
                ],
                "unmatchedDiscussion": [],
            }
        )
        catalog = {
            42: {
                "id": 42,
                "type": "Product Backlog Item",
                "state": "New",
                "title": "Fix login",
            }
        }
        created = persist_llm_output(
            session,
            run,
            ingested,
            output,
            catalog_index=catalog,
            pbi_ready_state="Approved",
            pbi_active_state="Committed",
        )
        session.flush()
        assert created == 2
        rows = list(
            session.scalars(
                select(ProposedChange).where(ProposedChange.ingestion_run_id == run.id)
            )
        )
        types = {row.change_type for row in rows}
        assert ChangeType.ASSIGN in types
        assert ChangeType.STATE_TRANSITION in types
        transition = next(r for r in rows if r.change_type == ChangeType.STATE_TRANSITION)
        assert transition.proposed_payload.get("newState") == "Approved"
        assign = next(r for r in rows if r.change_type == ChangeType.ASSIGN)
        assert "System.State" not in (assign.proposed_payload.get("fieldUpdates") or {})
        assert all(r.status == ProposalStatus.PENDING for r in rows)
