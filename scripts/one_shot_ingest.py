"""Run one live ingestion cycle from a checkout."""

from __future__ import annotations

import asyncio

from agenticscrum.config import load_settings
from agenticscrum.db import init_db, session_scope
from agenticscrum.ingest import run_ingestion


async def main() -> None:
    settings = load_settings()
    init_db(settings)
    with session_scope(settings) as session:
        run = await run_ingestion(session, settings)
        print(f"Ingestion {run.id}: {run.status.value}, proposals={run.proposals_created}")


if __name__ == "__main__":
    asyncio.run(main())
