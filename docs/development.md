# Development

Day-to-day operators should use the **portable launcher** — see [getting-started.md](./getting-started.md). This page covers CLI, tests, and contributor workflows.

## Environment

Portable launch (`Agentic Scrum - Portable.cmd`) creates `.venv` automatically. For a shell:

```powershell
.\scripts\setup_dev.ps1
.\.venv\Scripts\Activate.ps1
```

If dependencies drift after pulls:

```powershell
.\scripts\ensure_dev.ps1
```

Package metadata and tooling live in `pyproject.toml` (Hatchling, pytest, ruff, mypy).

## CLI reference

```powershell
python -m agenticscrum init
python -m agenticscrum login
python -m agenticscrum serve
python -m agenticscrum ingest
python -m agenticscrum apply
python -m agenticscrum status
python -m agenticscrum doctor
python -m agenticscrum rollback <proposal_id>
```

Console script alias (after install): `agenticscrum <command>`.

| Command | Behavior |
|---------|----------|
| `init` | Create/migrate SQLite; seed roster if empty |
| `login` | Browser sign-in for `LLM_AUTH_MODE=browser`; persist token cache |
| `serve` | Init DB, start Uvicorn + APScheduler |
| `ingest` | One inbox/analysis cycle |
| `apply` | Apply approved proposals; poll awaiting approvals |
| `status` | Counts for pending / awaiting / failed + last run |
| `doctor` | Print masked config; browser token probe; ADO WIQL smoke test |
| `rollback` | Set proposal status to `RolledBack` (audit only) |

One-shot helper: `scripts/one_shot_ingest.py`.

## Running tests

```powershell
python -m pytest
```

Or Cursor task **Agentic Scrum: Run Tests**.

- Test root: `src/tests/`
- Async mode: pytest-asyncio (`asyncio_mode = auto`)
- Helpers: `freezegun`, `respx` for time and HTTP mocking

Coverage is mostly unit/focused (inbox parsing, schemas, estimation, fields, chat models, repair guards). There is no live ADO/LLM integration suite in CI-style form — use `doctor` and a sandbox project for end-to-end checks.

## Debugging

| Method | How |
|--------|-----|
| Cursor launch | **Agentic Scrum: Serve Web UI** (sets `PYTHONPATH=src`, loads `.env`) |
| Attach | Start `Agentic Scrum - Debug.cmd`, then **Agentic Scrum: Attach To Debug Launcher** (`127.0.0.1:5678`) |
| Logs | Rotating JSON under `logs/` |
| Ingestion detail | UI `/ingestion/runs/{id}` for events, LLM, and tool traces |

## Where to change what

| Goal | Start here |
|------|------------|
| New meeting analysis behavior | `llm/agent.py`, `llm/prompt.py`, `schemas.py` |
| New ADO read tool | `llm/tools.py`, `ado/client.py` |
| Field merge / closure rules | `ado/fields.py` |
| Apply / approve path | `apply.py` |
| Failure recovery | `repair.py` |
| Autopilot eligibility | `autopilot.py` |
| Judge criteria | `llm/judge.py`, `proposal_judge.py` |
| Chat intents / replies | `chat_intent.py`, `llm/scrum_chat.py` |
| UI pages / actions | `web/routes.py`, `web/templates/` |
| Scheduler jobs | `scheduler.py` |
| Settings | `config.py`, `.env.example` |
| LLM browser login | `llm/auth.py`, `login` CLI |

## Web routes (summary)

| Area | Paths |
|------|-------|
| Dashboard | `GET /` |
| Ingest | `POST /ingest/run`, `POST /transcripts` |
| Ingestion history | `GET /ingestion/runs`, `GET /ingestion/runs/{id}`, rerun / mark-failed |
| Proposals | approve, reject, edit, change-request, retry, judge, bulk-reject |
| Roster | `POST /roster`, delete |
| Chat | `/chat`, messages, sessions, review, proposal actions, SSE demo |

## Local data

| Path | Purpose |
|------|---------|
| `data/agenticscrum.db` | Runtime SQLite |
| `data/notes_inbox` | Drop notes here |
| `data/notes_inbox_archive` | Processed files (archive mode) |
| `logs/` | Application logs |
| `.env` | Secrets (gitignored) |

Safe to delete the DB for a clean slate, then re-run `init` (you will lose proposals and history).

## Security notes for contributors

- Keep defaults bound to localhost.
- Do not log PATs, client secrets, or full bearer tokens.
- Treat sample notes in `data/` as potentially sensitive meeting content.

## Suggested smoke checklist before a PR

1. `python -m pytest`
2. `python -m agenticscrum doctor`
3. Drop a tiny `.md` into the inbox → `ingest` → approve a no-op or sandbox change → `apply` (against a non-prod project if possible)
4. Confirm UI loads at `/` and `/chat`
