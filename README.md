# Agentic Scrum

Local AI scrum master for team workflows. It grounds meeting notes against Azure DevOps work items, proposes changes with an LLM, and applies only what you approve.

```text
Notes inbox / paste / chat  →  LLM + ADO tools  →  proposals  →  approve  →  ADO
```

## License

MIT — see [LICENSE](LICENSE).

## Documentation

| Doc | Audience |
|-----|----------|
| [Getting started](docs/getting-started.md) | **Start here** — portable launch, login, first ingest |
| [Features](docs/features.md) | Full product capability reference |
| [Architecture](docs/architecture.md) | Layout, data flow, integrations |
| [Configuration](docs/configuration.md) | `.env` and YAML settings |
| [Development](docs/development.md) | CLI, tests, debugging, contribution map |

## Quick start (portable)

**Requirements:** Windows, Python 3.12+ (for first-time venv), an Azure DevOps PAT, and an OpenAI-compatible or Azure OpenAI endpoint (API key, Entra browser login, or service principal).

1. Open the project folder.
2. Copy `.env.example` → `.env` (the portable launcher does this on first run if missing) and set `ADO_PAT`, org/project, and your LLM endpoint + auth settings. Keep `LLM_AUTH_MODE=browser` for interactive Entra login, or use `api_key` / `azure_ad` as needed.
3. Double-click **`Agentic Scrum - Portable.cmd`**.

The launcher will:

- create/repair `.venv` and install dependencies if needed
- open a **browser login** the first time LLM auth is needed (`LLM_AUTH_MODE=browser`)
- start the app and open [http://127.0.0.1:8765/](http://127.0.0.1:8765/)

Leave the console window open while you use the app.

Full walkthrough: [docs/getting-started.md](docs/getting-started.md).

### Day-to-day use

1. Drop notes into `data/notes_inbox` (`.md`, `.txt`, `.vtt`, `.docx`; tip: `YYYY-MM-DD - Meeting Title.md`).
2. Ingest from the dashboard (or wait for the schedule).
3. Approve / edit proposals, then apply.

Chat: [http://127.0.0.1:8765/chat](http://127.0.0.1:8765/chat).

### Debug launcher

Double-click `Agentic Scrum - Debug.cmd`, then in Cursor use **Agentic Scrum: Attach To Debug Launcher** (`127.0.0.1:5678`).

## Feature highlights

- **Grounded proposals** — Create, Update, StateTransition, Assign, Comment against live ADO catalog
- **Approval dashboard** — preview, edit JSON, LLM change-requests, retries
- **Proposal judge / refine** — second-pass safety scoring
- **Scrum Master chat** — ask questions and propose changes interactively
- **Board hygiene review** — scheduled or on-demand
- **Optional autopilot** — auto-apply *safe* Assign / State / Comment only (off by default)
- **Agentic repair** — diagnose failed applies and retry

Details: [docs/features.md](docs/features.md).

## CLI (optional)

After Portable has set up `.venv`:

```powershell
.\.venv\Scripts\Activate.ps1
python -m agenticscrum login
python -m agenticscrum serve
python -m agenticscrum ingest
python -m agenticscrum apply
python -m agenticscrum status
python -m agenticscrum doctor
python -m agenticscrum rollback 123
```

Runtime state lives in SQLite under `data/`. See [docs/development.md](docs/development.md).

## Security

Designed for a **trusted local workstation**. The UI binds to `127.0.0.1` by default and has no app login. Keep PATs in `.env` only. Browser LLM mode caches a user token under `data/` after login.
