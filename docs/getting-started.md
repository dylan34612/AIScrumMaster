# Getting Started

This guide is **portable-first**: most people should run Agentic Scrum by double-clicking the Windows launcher. CLI and Cursor workflows are secondary.

## Prerequisites

| Requirement | Notes |
|-------------|--------|
| **Windows** | Portable launchers are `.cmd` + PowerShell scripts. |
| **Python 3.12+** | Needed once so the launcher can create `.venv` (prefer `py -3.12`). |
| **Azure DevOps PAT** | Work item read/write for your org/project. |
| **LLM access** | OpenAI-compatible or Azure OpenAI endpoint via `api_key`, Entra `browser` login, or `azure_ad` service principal. |
| **Network access** | Azure DevOps and your LLM endpoint. |

You do **not** need Microsoft Graph / Teams credentials for the notes-inbox workflow.

Keep the whole project folder together (`.venv`, `.env`, `data/`, `scripts/`). That folder *is* the portable install.

## Portable quick start

### 1. Get the project folder

Clone or copy the repo to a stable path, for example:

```text
c:\Users\you\source\AIScrumMaster
```

### 2. Configure `.env` once

On first launch the portable script creates `.venv` and copies `.env.example` → `.env` if needed. Before live use, edit `.env` and set at least:

```env
ADO_ORG=YourOrg
ADO_PROJECT=Your Project
ADO_TEAM=Your Team
ADO_AREA_PATH=Your Project\Your Team
ADO_PAT=xxxxxxxx

LLM_AUTH_MODE=browser
LLM_API_BASE=https://YOUR_RESOURCE.openai.azure.com/
LLM_MODEL=gpt-4o
LLM_AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
LLM_AZURE_USER_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
LLM_AZURE_USER_AUDIENCE=api://xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/.default
```

For a simpler setup with an API key instead of browser login:

```env
LLM_AUTH_MODE=api_key
LLM_API_BASE=https://YOUR_RESOURCE.openai.azure.com/
LLM_API_KEY=xxxxxxxx
LLM_MODEL=gpt-4o
```

Optional: edit `config/roster.yaml` so team names map to ADO identities (seeded on first DB init).

Full settings: [configuration.md](./configuration.md).

### 3. Launch with the portable app

Double-click from the project root:

| Launcher | Purpose |
|----------|---------|
| `Agentic Scrum - Portable.cmd` | Normal use — starts the app and opens the UI |
| `Agentic Scrum - Debug.cmd` | Starts under `debugpy` for Cursor attach |

What the portable launcher does:

1. Creates/repairs `.venv` and installs the package if needed
2. Ensures `.env` exists
3. If `LLM_AUTH_MODE=browser` and you have not signed in yet, runs interactive **browser login** (one-time; token cache saved under `data/`)
4. Starts the web UI + scheduler (`serve`)
5. Opens [http://127.0.0.1:8765/](http://127.0.0.1:8765/)

Leave the console window open while the app is running. Close it to stop the server.

Later launches reuse the LLM token cache (silent refresh). If the token expires or is revoked, the launcher will prompt browser login again when the auth record is missing, or you can force re-login:

```powershell
.\.venv\Scripts\python.exe -m agenticscrum login
```

### 4. Use the app

1. Drop facilitator notes into `data/notes_inbox` (created on use).
2. Prefer filename `YYYY-MM-DD - Meeting Title.md`.
3. Supported types: `.md`, `.txt`, `.vtt`, `.docx`.
4. Wait at least `NOTES_INBOX_MIN_AGE_SECONDS` (default 30) so partial writes are skipped.
5. Trigger ingest from the dashboard (or wait for the daily schedule).
6. Review proposals → approve / reject / edit → apply.

Processed files archive to `data/notes_inbox_archive` when `NOTES_INBOX_ARCHIVE_MODE=archive`.

Chat UI: [http://127.0.0.1:8765/chat](http://127.0.0.1:8765/chat).

### 5. Optional health check

From the project folder:

```powershell
.\.venv\Scripts\python.exe -m agenticscrum doctor
```

## Moving or sharing the portable folder

Copy the whole directory. Useful pieces to keep:

| Path | Why |
|------|-----|
| `.venv/` | Python environment |
| `.env` | Secrets (do not commit) |
| `data/` | SQLite DB, notes inbox, LLM auth record |
| `config/` | Roster / seeds |
| `scripts/` + `*.cmd` | Launchers |

On a new machine you still need Python 3.12+ available once if `.venv` must be recreated. LLM browser login is per-user; run Portable again (or `login`) after copying if the auth cache is missing or invalid.

## Alternative LLM modes

Only if you are not using browser login:

**Service principal**

```env
LLM_AUTH_MODE=azure_ad
LLM_AZURE_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
LLM_AZURE_CLIENT_ID=...
LLM_AZURE_CLIENT_SECRET=...
LLM_AZURE_AUDIENCE=api://your-app-id/.default
```

Note: SP often uses a **different** Entra audience than browser user auth.

**API key**

```env
LLM_AUTH_MODE=api_key
LLM_API_KEY=xxxxxxxx
```

## CLI workflow (optional)

For scripting or terminals, after Portable has created `.venv`:

```powershell
cd <project-root>
.\.venv\Scripts\Activate.ps1
python -m agenticscrum init
python -m agenticscrum login    # browser mode only
python -m agenticscrum doctor
python -m agenticscrum serve
```

Or run setup explicitly:

```powershell
.\scripts\setup_dev.ps1
```

See [development.md](./development.md) for the full command list.

## Cursor / VS Code workflow (optional)

Workspace config lives under `.vscode/`.

1. Task **Agentic Scrum: Setup Dev Environment** (or use Portable once).
2. Fill in `.env`.
3. Ensure LLM login (`login` or Portable first-run).
4. Debug profile **Agentic Scrum: Serve Web UI**.
5. Or start `Agentic Scrum - Debug.cmd`, then **Agentic Scrum: Attach To Debug Launcher** (`127.0.0.1:5678`).

## Common first-run issues

| Symptom | Likely fix |
|---------|------------|
| `Missing Azure DevOps settings` / `ADO_PAT` | Set `ADO_ORG`, `ADO_PROJECT`, and `ADO_PAT` in `.env`. |
| `LLM_API_BASE is required` | Set your OpenAI-compatible / Azure OpenAI base URL in `.env`. |
| Browser login window never appears | Confirm `LLM_AUTH_MODE=browser`; delete `data/llm_auth_record.json` and relaunch Portable, or run `login`. |
| No usable LLM browser token | Relaunch Portable (it will login if needed) or run `.\.venv\Scripts\python.exe -m agenticscrum login`. |
| Missing browser / Azure AD LLM settings | Fill tenant, client, and audience for your auth mode — or switch to `api_key`. |
| Python version errors | Install 3.12+; delete `.venv` and relaunch Portable. |
| Inbox file ignored | Check extension, min-age, and that inbox is enabled. |
| Ingestion stuck `Running` | Use the ingestion runs UI to mark failed, or inspect `logs/`. |
| Port in use | Close the other Portable window, or change `APP_PORT`. |

## Next reading

- [Features](./features.md) — what the product can do
- [Architecture](./architecture.md) — how pieces fit together
- [Configuration](./configuration.md) — every setting
- [Development](./development.md) — CLI, tests, contribution tips
