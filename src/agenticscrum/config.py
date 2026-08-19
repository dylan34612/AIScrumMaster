"""Application settings and YAML seed loaders."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class MeetingSeed:
    """Seed data for a legacy meeting source."""

    title: str
    chat_id: str
    facilitator_sender_filter: str | None
    enabled: bool


@dataclass(frozen=True)
class TeamMemberSeed:
    """Seed data for a team member and ADO identity mapping."""

    display_name: str
    email: str
    ado_unique_name: str


class Settings(BaseSettings):
    """Runtime settings loaded from `.env` and environment variables."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_base: str = ""
    llm_api_key: str = ""
    llm_auth_mode: Literal["api_key", "azure_ad", "browser"] = "browser"
    llm_model: str = "gpt-4o"
    llm_api_version: str = "2024-10-21"
    llm_temperature: float = 0.2
    llm_max_tool_steps: int = 6
    llm_schema_repair_max_attempts: int = 3
    llm_azure_tenant_id: str = ""
    llm_azure_client_id: str = ""
    llm_azure_client_secret: str = ""
    llm_azure_audience: str = ""
    # Interactive browser (user) auth — often a different Entra app/audience than SP.
    llm_azure_user_client_id: str = ""
    llm_azure_user_audience: str = ""

    ado_org: str = ""
    ado_project: str = ""
    ado_team: str = ""
    ado_area_path: str = ""
    ado_pat: str = ""
    ado_catalog_lookback_days: int = 60
    ado_story_points_field: str = "Microsoft.VSTS.Scheduling.StoryPoints"
    ado_effort_field: str = "Microsoft.VSTS.Scheduling.Effort"
    ado_estimation_mode: Literal["auto", "story_points", "effort", "both"] = "auto"
    ado_story_points_scale: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [1, 2, 3, 5, 8, 13]
    )
    ado_effort_scale: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [1, 2, 3, 5, 8, 13]
    )
    ado_effort_unit: str = "hours"

    notes_inbox_enabled: bool = True
    notes_inbox_path: str = "./data/notes_inbox"
    notes_inbox_archive_path: str = "./data/notes_inbox_archive"
    notes_inbox_archive_mode: Literal["archive", "delete", "keep"] = "archive"
    notes_inbox_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [".md", ".txt", ".vtt", ".docx"]
    )
    notes_inbox_min_age_seconds: int = 30

    app_host: str = "127.0.0.1"
    app_port: int = 8765
    app_db_path: str = "./data/agenticscrum.db"
    app_ingest_lookback_hours: int = 26
    app_daily_run_time: str = "00:00"
    app_daily_review_time: str = "08:30"
    app_daily_review_enabled: bool = True
    app_daily_review_stale_days: int = 14
    app_daily_review_max_items: int = 80
    app_daily_review_max_comment_items: int = 25
    app_timezone: str = "America/New_York"
    app_approval_poll_minutes: int = 360
    app_approver_name: str = "Approver"
    app_auto_fix_errors: bool = True
    app_agentic_retry_max_loops: int = 3

    # Autopilot: optional "hands-off" mode.
    app_autopilot_enabled: bool = False
    app_autopilot_poll_minutes: int = 5
    app_autopilot_ingest_inbox: bool = True
    app_autopilot_confidence_threshold: int = 90
    app_autopilot_max_apply_per_cycle: int = 10
    app_autopilot_approver_name: str = "Autopilot"
    app_autopilot_allow_assign: bool = True
    app_autopilot_allow_state_transitions: bool = True
    app_autopilot_allow_comments: bool = True
    app_autopilot_comment_min_confidence: int = 95

    # Automatic judge/refine of pending proposals (second-pass reviewer)
    app_auto_judge_enabled: bool = True
    app_auto_refine_enabled: bool = True
    app_auto_refine_max_loops: int = 2

    @field_validator("ado_story_points_scale", "ado_effort_scale", mode="before")
    @classmethod
    def _parse_int_list(cls, value: object) -> list[int]:
        if isinstance(value, str):
            return [int(part.strip()) for part in value.split(",") if part.strip()]
        if isinstance(value, list):
            return [int(item) for item in value]
        raise TypeError("Expected comma-separated string or list of integers")

    @field_validator("notes_inbox_extensions", mode="before")
    @classmethod
    def _parse_inbox_extensions(cls, value: object) -> list[str]:
        if isinstance(value, str):
            parts = [part.strip() for part in value.split(",") if part.strip()]
        elif isinstance(value, list):
            parts = [str(item).strip() for item in value if str(item).strip()]
        else:
            raise TypeError("Expected comma-separated string or list of file extensions")
        normalized: list[str] = []
        for ext in parts:
            if not ext:
                continue
            if not ext.startswith("."):
                ext = f".{ext}"
            normalized.append(ext.lower())
        return normalized

    @property
    def database_url(self) -> str:
        """Return the SQLAlchemy database URL."""

        db_path = Path(self.app_db_path)
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{db_path.as_posix()}"

    @property
    def ado_project_url_segment(self) -> str:
        """Return the URL-safe project segment for ADO REST calls."""

        return self.ado_project.replace(" ", "%20")

    def require_ado(self) -> None:
        """Raise if ADO credentials are missing."""

        missing = [
            name
            for name, value in {
                "ADO_ORG": self.ado_org,
                "ADO_PROJECT": self.ado_project,
                "ADO_PAT": self.ado_pat,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"Missing Azure DevOps settings: {', '.join(missing)}. "
                "Copy .env.example to .env and set your org, project, and PAT."
            )

    def require_llm(self) -> None:
        """Raise if LLM credentials are missing."""

        if not self.llm_api_base:
            raise RuntimeError(
                "LLM_API_BASE is required. Copy .env.example to .env and set your endpoint."
            )
        if self.llm_auth_mode == "api_key" and not self.llm_api_key:
            raise RuntimeError("LLM_API_KEY is required when LLM_AUTH_MODE=api_key.")
        if self.llm_auth_mode == "azure_ad":
            missing = [
                name
                for name, value in {
                    "LLM_AZURE_TENANT_ID": self.llm_azure_tenant_id,
                    "LLM_AZURE_CLIENT_ID": self.llm_azure_client_id,
                    "LLM_AZURE_CLIENT_SECRET": self.llm_azure_client_secret,
                    "LLM_AZURE_AUDIENCE": self.llm_azure_audience,
                }.items()
                if not value
            ]
            if missing:
                raise RuntimeError(f"Missing Azure AD LLM settings: {', '.join(missing)}")
        if self.llm_auth_mode == "browser":
            missing = [
                name
                for name, value in {
                    "LLM_AZURE_TENANT_ID": self.llm_azure_tenant_id,
                    "LLM_AZURE_USER_CLIENT_ID": self.llm_azure_user_client_id,
                    "LLM_AZURE_USER_AUDIENCE": self.llm_azure_user_audience,
                }.items()
                if not value
            ]
            if missing:
                raise RuntimeError(f"Missing browser LLM settings: {', '.join(missing)}")


def load_settings() -> Settings:
    """Load runtime settings."""

    return Settings()


def load_meeting_seeds(path: Path | None = None) -> list[MeetingSeed]:
    """Load meeting source seed data from YAML."""

    yaml_path = path or PROJECT_ROOT / "config" / "meetings.yaml"
    if not yaml_path.exists():
        return []
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    return [
        MeetingSeed(
            title=str(item["title"]),
            chat_id=str(item["chat_id"]),
            facilitator_sender_filter=item.get("facilitator_sender_filter"),
            enabled=bool(item.get("enabled", True)),
        )
        for item in data.get("meetings", [])
    ]


def load_roster_seeds(path: Path | None = None) -> list[TeamMemberSeed]:
    """Load team roster seed data from YAML."""

    yaml_path = path or PROJECT_ROOT / "config" / "roster.yaml"
    if not yaml_path.exists():
        return []
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    return [
        TeamMemberSeed(
            display_name=str(item["display_name"]),
            email=str(item["email"]),
            ado_unique_name=str(item["ado_unique_name"]),
        )
        for item in data.get("team", [])
    ]
