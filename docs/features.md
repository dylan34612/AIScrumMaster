# Features

Agentic Scrum turns meeting notes into grounded Azure DevOps change proposals, then applies only what you approve. This page describes the product surface area for operators and developers.

## Core workflow

1. **Ingest** notes (inbox folder, manual paste, or chat).
2. **Analyze** with an LLM that can call ADO read tools (catalog, work item details).
3. **Propose** typed changes stored in SQLite.
4. **Judge / refine** (optional second-pass reviewer).
5. **Approve, reject, or edit** in the local web UI (or autopilot for safe cases).
6. **Apply** patches to Azure DevOps; retry/fix on failure when enabled.

## Notes ingestion

### Notes inbox (recommended)

- Drop facilitator notes into `NOTES_INBOX_PATH` (default `data/notes_inbox`).
- Formats: `.md`, `.txt`, `.vtt`, `.docx`.
- Filename tip: `YYYY-MM-DD - Meeting Title.md` (date/title parsed when possible).
- Files younger than `NOTES_INBOX_MIN_AGE_SECONDS` are skipped (avoids reading mid-write).
- After success, archive mode is `archive` (move), `delete`, or `keep` (dedupe by content hash).
- Daily scheduled ingest runs when the web server is up (`APP_DAILY_RUN_TIME`).

### Manual transcript paste

- Dashboard / UI form posts to `/transcripts`.
- Useful for one-off notes without using the folder.

### Re-run

- Re-analyze a previously ingested meeting from ingestion history (`POST /ingested-meetings/{id}/rerun`).

### Ingestion observability

- Run list and detail pages under `/ingestion/runs`.
- Events, LLM calls, and tool calls are persisted for debugging.
- Clear error text or mark a stuck `Running` run as failed from the UI.

## LLM-grounded proposals

The meeting agent uses LangChain tool calling against live ADO data, then emits structured proposals.

| Change type | Typical use |
|-------------|-------------|
| **Create** | New PBI, Feature, Epic, Bug, or Task |
| **Update** | Field edits (title, description, estimation, etc.) |
| **StateTransition** | Board state moves (including closures) |
| **Assign** | Assignee changes using roster / ADO identities |
| **Comment** | Discussion capture on a work item |

Supporting behaviors:

- Schema repair retries when the model returns invalid JSON (`LLM_SCHEMA_REPAIR_MAX_ATTEMPTS`).
- Estimation normalization for story points and/or effort (`ADO_ESTIMATION_MODE`, scales).
- Confidence and rationale stored with each proposal for reviewer context.

## Approval dashboard

Primary UI at `/`:

- Pending, applied, and failed proposals
- Preview of proposed payload vs current context
- Approve / reject
- Edit proposal JSON before apply
- LLM “change request” to revise a proposal from natural language
- Retry failed applies / Fix with AI
- Per-proposal or batch judge
- Bulk reject placeholder comments
- Team roster CRUD

## Proposal judge and refine

When enabled (`APP_AUTO_JUDGE_ENABLED` / `APP_AUTO_REFINE_ENABLED`):

- A second-pass LLM reviewer scores pending proposals for safety and quality.
- Low-quality proposals can be refined iteratively (`APP_AUTO_REFINE_MAX_LOOPS`).
- Judgements are stored and reused (payload hashing) so identical proposals are not re-judged unnecessarily.
- Manual judge actions are available from the dashboard and chat.

## Apply, repair, and undo

- **Apply approved** pushes Create/Update/State/Assign/Comment to ADO via REST.
- **Auto-fix** can revise a failed payload once (`APP_AUTO_FIX_ERRORS`).
- **Agentic retry** runs diagnose → patch → retry loops (`APP_AGENTIC_RETRY_MAX_LOOPS`).
- **Soft undo** creates a compensating *pending* proposal (does not silently reverse ADO).
- **CLI rollback** marks a proposal `RolledBack` for audit only — it does **not** reverse ADO changes.

Proposal statuses: `Pending` → `Approved` / `Rejected` → `Applied` | `Failed`, plus `AwaitingAssigneeApproval` and `RolledBack`.

## Autopilot

Optional hands-off mode (`APP_AUTOPILOT_ENABLED`, default **off**):

- Periodically ingests the inbox (optional) and auto-applies **safe** pending proposals.
- Never auto-applies **Create** or **Update**.
- Closures are excluded; state transitions / assigns / comments gated by confidence and toggles.
- Comments require a higher confidence floor (`APP_AUTOPILOT_COMMENT_MIN_CONFIDENCE`).
- Cap per cycle: `APP_AUTOPILOT_MAX_APPLY_PER_CYCLE`.

Enable only after you trust the guardrails for your board.

## Scrum Master chat

Interactive UI at `/chat`:

- Ask board/process questions grounded in ADO tools.
- Request proposals from conversation (not only from meeting files).
- Clarify ambiguous asks before mutating.
- Trigger board review from chat.
- Approve / reject / retry / undo / judge proposals without leaving chat.
- Sessions and message history stored in SQLite.

## Board hygiene review

Scheduled (when `APP_DAILY_REVIEW_ENABLED`) and on-demand:

- Scans active work items for staleness and hygiene issues.
- Produces review comments / proposals within configured caps (`APP_DAILY_REVIEW_MAX_*`, `APP_DAILY_REVIEW_STALE_DAYS`).
- Runs at `APP_DAILY_REVIEW_TIME` in `APP_TIMEZONE` while `serve` is running.

## Team roster

- Seeded from `config/roster.yaml` on empty DB.
- Editable in the UI (`/roster`).
- Maps display names / emails to ADO unique names for assignment proposals.

## Scheduling (while `serve` is running)

| Job | Trigger | Purpose |
|-----|---------|---------|
| Daily ingestion | `APP_DAILY_RUN_TIME` | Process inbox notes |
| Daily board review | `APP_DAILY_REVIEW_TIME` | Hygiene pass |
| Approval poll | every `APP_APPROVAL_POLL_MINUTES` | Handle awaiting approvals / related poll work |
| Autopilot | every `APP_AUTOPILOT_POLL_MINUTES` | Optional safe auto-apply |

## Portable launcher

Primary way to run the app on Windows:

- `Agentic Scrum - Portable.cmd` — setup/repair env, browser LLM login when needed, start UI
- `Agentic Scrum - Debug.cmd` — same path under `debugpy` for Cursor attach

See [getting-started.md](./getting-started.md).

## CLI operations

Optional after Portable has created `.venv`. The portable launcher already covers setup, login, and `serve`.

| Command | Purpose |
|---------|---------|
| `init` | Create DB + seed roster |
| `login` | Browser sign-in for LLM (`LLM_AUTH_MODE=browser`) |
| `serve` | Web UI + scheduler |
| `ingest` | One ingestion cycle |
| `apply` | Apply approved + poll awaiting |
| `status` | Pending / failed / awaiting + last run |
| `doctor` | Config + ADO connectivity check |
| `rollback <id>` | Audit-only rollback marker |

See also [CLI details in development.md](./development.md#cli-reference).

## Security model

- Designed as a **trusted local workstation** tool.
- Web UI binds to `127.0.0.1` by default and has **no login**.
- Secrets live in `.env` (never commit real PATs or client secrets).
- Browser LLM mode caches a user token under `data/` after `agenticscrum login`.
- Do not expose the port beyond localhost without adding your own auth layer.

## Legacy: Teams / Microsoft Graph

`src/agenticscrum/teams/` and `config/meetings.yaml` relate to an older Teams-chat ingestion path. Day-to-day operation uses the **notes inbox**. Prefer inbox + manual paste + chat for new work.
