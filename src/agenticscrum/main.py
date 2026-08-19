"""CLI entry point for Agentic Scrum."""

from __future__ import annotations

import argparse
import asyncio
from typing import NoReturn

import uvicorn
from sqlalchemy import func, select

from agenticscrum.ado.client import AdoClient
from agenticscrum.apply import apply_approved, poll_approval_responses
from agenticscrum.config import load_settings
from agenticscrum.db import init_db, session_scope
from agenticscrum.ingest import run_ingestion
from agenticscrum.llm.auth import auth_record_exists, ensure_llm_login, probe_llm_browser_token
from agenticscrum.logging import configure_logging
from agenticscrum.models import IngestionRun, ProposalStatus, ProposedChange


def main() -> None:
    """Run the Agentic Scrum CLI."""

    parser = argparse.ArgumentParser(prog="agenticscrum")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init")
    subparsers.add_parser("login")
    subparsers.add_parser("serve")
    subparsers.add_parser("ingest")
    subparsers.add_parser("apply")
    subparsers.add_parser("status")
    subparsers.add_parser("doctor")
    rollback_parser = subparsers.add_parser("rollback")
    rollback_parser.add_argument("id", type=int)
    args = parser.parse_args()
    settings = load_settings()
    configure_logging(console=args.command == "serve")

    if args.command == "init":
        init_db(settings)
        print("Initialized Agentic Scrum database.")
    elif args.command == "login":
        ensure_llm_login(settings)
        print("LLM browser login succeeded. Token cache saved under data/.")
    elif args.command == "serve":
        init_db(settings)
        uvicorn.run(
            "agenticscrum.web.app:app",
            host=settings.app_host,
            port=settings.app_port,
            reload=False,
        )
    elif args.command == "ingest":
        init_db(settings)
        asyncio.run(_ingest(settings))
    elif args.command == "apply":
        init_db(settings)
        asyncio.run(_apply(settings))
    elif args.command == "status":
        init_db(settings)
        print_status(settings)
    elif args.command == "doctor":
        init_db(settings)
        asyncio.run(_doctor(settings))
    elif args.command == "rollback":
        init_db(settings)
        rollback(settings, args.id)
    else:
        unreachable()


async def _ingest(settings) -> None:
    with session_scope(settings) as session:
        run = await run_ingestion(session, settings)
        print(f"Ingestion {run.id}: {run.status.value}, proposals={run.proposals_created}")


async def _apply(settings) -> None:
    with session_scope(settings) as session:
        applied = await apply_approved(session, settings)
        handled = await poll_approval_responses(session, settings)
        print(f"Applied {applied}; handled approval responses {handled}.")


async def _doctor(settings) -> None:
    """Print masked config status and test basic ADO connectivity."""

    print(f"ADO org/project: {settings.ado_org}/{settings.ado_project}")
    print(f"ADO area path: {settings.ado_area_path}")
    print(f"ADO PAT configured: {bool(settings.ado_pat)}")
    print(f"LLM auth mode: {settings.llm_auth_mode}")
    if settings.llm_auth_mode == "browser":
        print(f"LLM browser auth record present: {auth_record_exists()}")
        print(probe_llm_browser_token(settings))
    try:
        async with AdoClient(settings) as ado:
            ids = await ado.query_active_ids()
        print(f"ADO WIQL query succeeded. Active IDs returned: {len(ids)}")
    except Exception as exc:
        print(f"ADO WIQL query failed: {exc}")


def print_status(settings) -> None:
    """Print pending counts, last run, and failed count."""

    with session_scope(settings) as session:
        pending = session.scalar(
            select(func.count(ProposedChange.id)).where(
                ProposedChange.status == ProposalStatus.PENDING
            )
        )
        failed = session.scalar(
            select(func.count(ProposedChange.id)).where(
                ProposedChange.status == ProposalStatus.FAILED
            )
        )
        awaiting = session.scalar(
            select(func.count(ProposedChange.id)).where(
                ProposedChange.status == ProposalStatus.AWAITING_ASSIGNEE_APPROVAL
            )
        )
        last_run = session.scalar(select(IngestionRun).order_by(IngestionRun.started_at.desc()))
        print(f"Pending: {pending or 0}")
        print(f"Awaiting assignee approval: {awaiting or 0}")
        print(f"Failed: {failed or 0}")
        if last_run:
            print(f"Last run: {last_run.started_at} {last_run.status.value}")
            if last_run.error_message:
                print(last_run.error_message)


def rollback(settings, proposal_id: int) -> None:
    """Mark an applied proposal as rolled back for audit purposes."""

    with session_scope(settings) as session:
        proposal = session.get(ProposedChange, proposal_id)
        if proposal is None:
            raise SystemExit(f"Proposal {proposal_id} not found")
        proposal.status = ProposalStatus.ROLLED_BACK
        print(f"Marked proposal {proposal_id} as rolled back.")


def unreachable() -> NoReturn:
    """Defensive helper for argparse exhaustiveness."""

    raise RuntimeError("Unreachable command")


if __name__ == "__main__":
    main()
