# Configuration

Settings load from the project-root `.env` file and process environment variables via pydantic-settings (`src/agenticscrum/config.py`). Environment variables override values in `.env`. Unknown keys are ignored.

List-valued settings accept comma-separated strings (for example `1,2,3,5,8,13`). Relative paths for the DB and inbox resolve against the project root.

Start from `.env.example`. Never commit a filled `.env`.

## LLM

| Variable | Default / notes |
|----------|-----------------|
| `LLM_API_BASE` | **Required.** OpenAI-compatible / Azure OpenAI base URL (empty until set) |
| `LLM_API_KEY` | Required when `LLM_AUTH_MODE=api_key` |
| `LLM_AUTH_MODE` | `browser` (recommended for Entra interactive login), `azure_ad` (service principal), or `api_key` |
| `LLM_MODEL` | Deployment / model name (`gpt-4o`) |
| `LLM_API_VERSION` | Azure API version (for example `2024-10-21`) |
| `LLM_TEMPERATURE` | `0.2` |
| `LLM_MAX_TOOL_STEPS` | Max tool-loop steps per agent turn (`6`) |
| `LLM_SCHEMA_REPAIR_MAX_ATTEMPTS` | Invalid JSON repair retries (`3`) |
| `LLM_AZURE_TENANT_ID` | Entra tenant (shared by browser and SP) |
| `LLM_AZURE_USER_CLIENT_ID` | Interactive user app ID for `browser` mode |
| `LLM_AZURE_USER_AUDIENCE` | User OAuth audience for `browser` (often `api://<app-id>/.default`) |
| `LLM_AZURE_CLIENT_ID` | Service principal client ID (`azure_ad` mode) |
| `LLM_AZURE_CLIENT_SECRET` | Service principal secret (`azure_ad` mode) |
| `LLM_AZURE_AUDIENCE` | SP OAuth audience |

Browser and service principal often use **different** Entra apps/audiences. For browser mode, the portable launcher runs login automatically when no auth record exists; you can also run `python -m agenticscrum login`. The auth record is stored at `data/llm_auth_record.json` and the token cache is named `agenticscrum-llm`.

## Azure DevOps

| Variable | Default / notes |
|----------|-----------------|
| `ADO_ORG` | **Required.** Organization name |
| `ADO_PROJECT` | **Required.** Project name |
| `ADO_TEAM` | Team name |
| `ADO_AREA_PATH` | Area path (use `\` separators) |
| `ADO_PAT` | **Required** for ADO operations |
| `ADO_CATALOG_LOOKBACK_DAYS` | How far back to catalog work items (`60`) |
| `ADO_STORY_POINTS_FIELD` | WIT field ref name |
| `ADO_EFFORT_FIELD` | WIT field ref name |
| `ADO_ESTIMATION_MODE` | `auto`, `story_points`, `effort`, or `both` |
| `ADO_STORY_POINTS_SCALE` | e.g. `1,2,3,5,8,13` |
| `ADO_EFFORT_SCALE` | e.g. `1,2,3,5,8,13` |
| `ADO_EFFORT_UNIT` | Display/unit hint (`hours`) |

## Notes inbox

| Variable | Default / notes |
|----------|-----------------|
| `NOTES_INBOX_ENABLED` | `true` |
| `NOTES_INBOX_PATH` | `./data/notes_inbox` |
| `NOTES_INBOX_ARCHIVE_PATH` | `./data/notes_inbox_archive` |
| `NOTES_INBOX_ARCHIVE_MODE` | `archive`, `delete`, or `keep` |
| `NOTES_INBOX_EXTENSIONS` | `.md,.txt,.vtt,.docx` |
| `NOTES_INBOX_MIN_AGE_SECONDS` | Skip files newer than this (`30`) |

## App / scheduler

| Variable | Default / notes |
|----------|-----------------|
| `APP_HOST` | `127.0.0.1` |
| `APP_PORT` | `8765` |
| `APP_DB_PATH` | `./data/agenticscrum.db` |
| `APP_INGEST_LOOKBACK_HOURS` | Ingest window helper (`26`) |
| `APP_DAILY_RUN_TIME` | Cron time for daily ingest (`00:00`) |
| `APP_DAILY_REVIEW_TIME` | Daily board review (`08:30`) |
| `APP_DAILY_REVIEW_ENABLED` | `true` |
| `APP_DAILY_REVIEW_STALE_DAYS` | `14` |
| `APP_DAILY_REVIEW_MAX_ITEMS` | `80` |
| `APP_DAILY_REVIEW_MAX_COMMENT_ITEMS` | `25` |
| `APP_TIMEZONE` | `America/New_York` |
| `APP_APPROVAL_POLL_MINUTES` | Interval for approval poll job (`360`) |
| `APP_APPROVER_NAME` | Display name recorded on approvals (`Approver`) |
| `APP_AUTO_FIX_ERRORS` | Retry failed applies with payload revision (`true`) |
| `APP_AGENTIC_RETRY_MAX_LOOPS` | Repair loop budget (`3`) |

## Autopilot

Off by default. Enable only when you accept auto-apply of guarded change types.

| Variable | Default / notes |
|----------|-----------------|
| `APP_AUTOPILOT_ENABLED` | `false` |
| `APP_AUTOPILOT_POLL_MINUTES` | `5` |
| `APP_AUTOPILOT_INGEST_INBOX` | Ingest inbox each cycle (`true`) |
| `APP_AUTOPILOT_CONFIDENCE_THRESHOLD` | Minimum confidence (`90`) |
| `APP_AUTOPILOT_MAX_APPLY_PER_CYCLE` | `10` |
| `APP_AUTOPILOT_APPROVER_NAME` | `Autopilot` |
| `APP_AUTOPILOT_ALLOW_ASSIGN` | `true` |
| `APP_AUTOPILOT_ALLOW_STATE_TRANSITIONS` | `true` |
| `APP_AUTOPILOT_ALLOW_COMMENTS` | `true` |
| `APP_AUTOPILOT_COMMENT_MIN_CONFIDENCE` | `95` |

Autopilot never auto-applies Create or Update, and skips closures.

## Proposal judge / refine

| Variable | Default / notes |
|----------|-----------------|
| `APP_AUTO_JUDGE_ENABLED` | `true` |
| `APP_AUTO_REFINE_ENABLED` | `true` |
| `APP_AUTO_REFINE_MAX_LOOPS` | `2` |

## YAML seeds

### `config/roster.yaml`

Seeded into `team_members` when the table is empty on `init`:

```yaml
team:
  - display_name: "Jane Doe"
    email: "jane.doe@example.com"
    ado_unique_name: "jane.doe@example.com"
```

After seed, manage roster from the UI. Re-init does not overwrite existing members.

### `config/meetings.yaml`

Legacy Teams meeting chat IDs. Not used by the current notes-inbox ingestion path. Kept for historical / optional Graph workflows.

## Credential checklist

| Goal | Required |
|------|----------|
| `doctor` / ADO reads | `ADO_ORG` + `ADO_PROJECT` + `ADO_PAT` |
| Browser LLM | `LLM_API_BASE` + `LLM_AUTH_MODE=browser` + tenant / user client / audience + Portable launch (or `python -m agenticscrum login`) |
| SP LLM | `LLM_API_BASE` + `LLM_AUTH_MODE=azure_ad` + tenant / client id / secret / audience |
| API key LLM | `LLM_API_BASE` + `LLM_AUTH_MODE=api_key` + `LLM_API_KEY` |
| Ingest / chat / judge | ADO + LLM credentials for chosen `LLM_AUTH_MODE` |
| Apply to board | Same as above; write-capable PAT |
| Autopilot | Same + `APP_AUTOPILOT_ENABLED=true` |
| Teams legacy | Not documented for day-to-day; Graph settings are not in current `.env.example` |
