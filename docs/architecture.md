# Architecture

## Overview

Agentic Scrum is a **local modular monolith**: a Python package with a CLI, a FastAPI + Jinja2 web UI, SQLite persistence, an in-process scheduler, and integrations for Azure DevOps and an OpenAI-compatible LLM.

```text
Notes inbox / Manual UI / Chat
            │
            ▼
      ingest.py ──► llm/agent.py (tool loop) ──► ProposedChange rows
            │                │
            │                ▼
            │         proposal_judge.py (optional)
            ▼
   Web UI / Autopilot / CLI apply
            │
            ▼
       apply.py ──► ado/client.py ──► Azure DevOps
            │
            └── repair.py (diagnose → patch → retry)
```

## Package layout

```text
src/agenticscrum/
  main.py              CLI (init, serve, ingest, apply, status, doctor, rollback)
  config.py            pydantic-settings + YAML seed loaders
  db.py                SQLAlchemy engine, sessions, init, light migrations
  models.py            ORM + enums
  schemas.py           Pydantic schemas for LLM/API payloads
  ingest.py            Orchestrates inbox → analysis → proposals
  inbox.py             Local file inbox I/O
  transcript_formats.py  .md/.txt/.vtt/.docx → text
  apply.py             Approve/reject/apply + approval poll
  repair.py            Agentic failure recovery
  soft_undo.py         Compensating undo proposals
  autopilot.py         Safe auto-apply rules
  proposal_judge.py    Persist/judge/refine pipeline
  board_review.py      Board hygiene review
  estimation.py        Story points / effort normalization
  scheduler.py         APScheduler jobs
  chat_*.py            Chat intent, events, markdown, SSE helpers
  ado/                 ADO REST client + field helpers
  llm/                 Model client, agent, tools, prompts, judge, scrum chat
  teams/               Legacy Graph/Teams (not on active inbox path)
  web/                 FastAPI app, routes, Jinja templates
src/tests/             pytest suite
config/                roster.yaml, meetings.yaml (legacy)
data/                  SQLite DB, inbox, archives (runtime)
scripts/               setup / start / debug helpers
```

## Runtime processes

| Mode | What runs |
|------|-----------|
| `serve` | Uvicorn + FastAPI, DB init, APScheduler (ingest, review, poll, optional autopilot) |
| `ingest` / `apply` / etc. | One-shot CLI; no long-lived scheduler |
| Portable `.cmd` | Primary operator entry: setup/repair venv → browser LLM login if needed → `serve` + open UI |

## Data flow: ingestion

1. Discover eligible inbox files (extension, min age, content-hash dedupe).
2. Parse transcript text (`transcript_formats.py`).
3. Build / refresh an ADO work-item catalog (lookback window).
4. Run the LLM agent with ADO read tools (`llm/tools.py`).
5. Validate / repair structured proposals (`schemas.py`, schema repair loop).
6. Persist `IngestionRun`, meeting record, `ProposedChange` rows, tool/LLM logs.
7. Optionally auto-judge / refine pending proposals.
8. Archive or delete the source file per inbox mode.

## Proposal lifecycle

```text
Pending ──approve──► Approved ──apply──► Applied
   │                    │
   │                    └──fail──► Failed ──retry/repair──► Approved/Applied
   ├──reject──► Rejected
   └── (closure / assignee paths) ──► AwaitingAssigneeApproval ──► Approved/Rejected
Applied ──CLI rollback──► RolledBack   (audit marker only)
```

Change types: `Create`, `Update`, `StateTransition`, `Assign`, `Comment`.

## Web layer

- FastAPI app factory: `web/app.py` (lifespan starts/stops the scheduler).
- Routes: `web/routes.py` — HTML-first dashboard, chat, ingestion, roster, proposal actions.
- Templates under `web/templates/`.
- Settings are reloaded often via `get_settings`, so many `.env` edits apply without restart.

## Integrations

| System | Client | Auth |
|--------|--------|------|
| Azure DevOps WIT REST | `ado/client.py` (httpx) | PAT Basic auth |
| OpenAI-compatible / Azure OpenAI proxy | `llm/client.py` (LangChain) | Browser user login, Azure AD client credentials, or API key |
| Microsoft Graph / Teams | `teams/*` | Legacy MSAL — unused by inbox path |

## Persistence

SQLite (`APP_DB_PATH`) stores:

- Proposed changes and statuses
- Ingestion runs, meetings, events
- LLM / tool call logs
- Proposal judgements
- Chat sessions and messages
- Team members
- Soft-undo / audit-related fields as modeled

WAL mode is used for concurrent local access. Light migrations run on init.

## Logging

`structlog` writes rotating JSON logs under `logs/` (console logging enabled for `serve`).

## Design constraints

- **Local-first**: no multi-tenant auth; bind to localhost.
- **Human in the loop by default**: autopilot is off and never auto-creates/updates.
- **Grounded writes**: LLM proposes; apply path uses explicit payloads through the ADO client.
- **Recoverable failures**: auto-fix and agentic repair before giving up.
