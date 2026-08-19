"""Async Azure DevOps work item client."""

from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from agenticscrum.ado.fields import (
    ACCEPTANCE_CRITERIA,
    ASSIGNED_TO,
    AREA_PATH,
    DESCRIPTION,
    STATE,
    TITLE,
    extract_append_value,
    field_patch,
    merge_append_value,
    relation_patch,
)
from agenticscrum.config import Settings
from agenticscrum.models import ChangeType, ProposedChange


TRANSCRIPT_ARTIFACT_RE = re.compile(
    r"(started transcription|stopped transcription|ai-generated content may be incorrect|use arrow keys)",
    re.I,
)
SPEAKER_TIME_RE = re.compile(
    r"^\s*[A-Za-z]+,\s*[A-Za-z]+.*\b\d+\s+minutes?\s+\d+\s+seconds?\b",
    re.I,
)
TIMECODE_RE = re.compile(r"^\s*\d{1,2}:\d{2}\b")


class AdoClient:
    """Client for Azure DevOps Work Item Tracking REST APIs."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.settings.require_ado()
        token = base64.b64encode(f":{settings.ado_pat}".encode("utf-8")).decode("ascii")
        self._headers = {"Authorization": f"Basic {token}"}
        self._client = client
        self._owns_client = client is None
        self.base_url = (
            f"https://dev.azure.com/{settings.ado_org}/"
            f"{settings.ado_project_url_segment}/_apis/wit"
        )

    async def __aenter__(self) -> AdoClient:
        if self._client is None:
            self._client = httpx.AsyncClient(headers=self._headers, timeout=60)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        """Return the underlying HTTP client."""

        if self._client is None:
            self._client = httpx.AsyncClient(headers=self._headers, timeout=60)
        return self._client

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def query_active_ids(self) -> list[int]:
        """Query active work item IDs for the team area path."""

        since = datetime.now(timezone.utc) - timedelta(days=self.settings.ado_catalog_lookback_days)
        wiql = {
            "query": (
                "SELECT [System.Id] FROM WorkItems "
                f"WHERE [System.TeamProject] = '{self.settings.ado_project}' "
                f"AND [System.AreaPath] UNDER '{self.settings.ado_area_path}' "
                "AND [System.WorkItemType] IN ('Product Backlog Item','Feature','Epic','Bug','Task') "
                "AND [System.State] NOT IN ('Closed','Done','Removed') "
                f"AND [System.ChangedDate] >= '{format_wiql_datetime(since)}' "
                "ORDER BY [System.ChangedDate] DESC"
            )
        }
        response = await self.client.post(f"{self.base_url}/wiql?api-version=7.1", json=wiql)
        raise_for_status(response, "ADO WIQL query")
        return [int(item["id"]) for item in response.json().get("workItems", [])]

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def batch_get(self, ids: list[int]) -> list[dict[str, Any]]:
        """Fetch a batch of work item details."""

        if not ids:
            return []
        fields = [
            TITLE,
            "System.WorkItemType",
            STATE,
            ASSIGNED_TO,
            AREA_PATH,
            ACCEPTANCE_CRITERIA,
            self.settings.ado_story_points_field,
            self.settings.ado_effort_field,
            "System.Parent",
            "System.ChangedDate",
        ]
        payload = {"ids": ids[:200], "fields": fields}
        response = await self.client.post(
            f"{self.base_url}/workitemsbatch?api-version=7.1", json=payload
        )
        raise_for_status(response, "ADO batch get")
        return list(response.json().get("value", []))

    async def get_work_item(self, work_item_id: int, expand: str = "Relations") -> dict[str, Any]:
        """Fetch one work item."""

        response = await self.client.get(
            f"{self.base_url}/workitems/{work_item_id}?$expand={expand}&api-version=7.1"
        )
        raise_for_status(response, f"ADO get work item {work_item_id}")
        return dict(response.json())

    async def get_comments(self, work_item_id: int) -> list[dict[str, Any]]:
        """Fetch comments for a work item."""

        response = await self.client.get(
            f"{self.base_url}/workItems/{work_item_id}/comments?api-version=7.1-preview.4"
        )
        raise_for_status(response, f"ADO get comments {work_item_id}")
        return list(response.json().get("comments", []))

    async def get_work_item_type_states(self, work_item_type: str) -> list[dict[str, Any]]:
        """Return allowed states for an ADO work item type."""

        normalized_type = "Product Backlog Item" if work_item_type == "PBI" else work_item_type
        type_segment = quote(normalized_type, safe="")
        response = await self.client.get(
            f"{self.base_url}/workitemtypes/{type_segment}/states?api-version=7.1"
        )
        raise_for_status(response, f"ADO get work item type states {work_item_type}")
        return list(response.json().get("value", []))

    async def add_comment(self, work_item_id: int, text: str) -> dict[str, Any]:
        """Add a comment to a work item."""

        response = await self.client.post(
            f"{self.base_url}/workItems/{work_item_id}/comments?api-version=7.1-preview.4",
            json={"text": text},
        )
        raise_for_status(response, f"ADO add comment {work_item_id}")
        return dict(response.json())

    async def create_work_item(
        self,
        work_item_type: str,
        fields: dict[str, Any],
        parent_work_item_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a work item with optional parent relation."""

        normalized_type = "Product Backlog Item" if work_item_type == "PBI" else work_item_type
        patch = [field_patch(field, value) for field, value in fields.items()]
        if parent_work_item_id:
            parent = await self.get_work_item(parent_work_item_id)
            patch.append(relation_patch(parent["url"]))
        response = await self.client.post(
            f"{self.base_url}/workitems/${normalized_type}?api-version=7.1",
            headers={"Content-Type": "application/json-patch+json"},
            json=patch,
        )
        raise_for_status(response, f"ADO create {work_item_type}")
        return dict(response.json())

    async def patch_work_item(self, work_item_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        """Patch work item fields, handling APPEND-wrapped updates."""

        current: dict[str, Any] | None = None
        patch: list[dict[str, Any]] = []
        for field, value in fields.items():
            append_value = extract_append_value(value)
            if append_value is not None:
                if current is None:
                    current = await self.get_work_item(work_item_id)
                existing = current.get("fields", {}).get(field)
                value = merge_append_value(existing, append_value)
            patch.append(field_patch(field, value))
        response = await self.client.patch(
            f"{self.base_url}/workitems/{work_item_id}?api-version=7.1",
            headers={"Content-Type": "application/json-patch+json"},
            json=patch,
        )
        raise_for_status(response, f"ADO patch work item {work_item_id}")
        return dict(response.json())

    async def apply_payload(self, proposal: ProposedChange, approver: str) -> int:
        """Apply one approved proposal payload and return the affected work item ID."""

        payload = proposal.proposed_payload
        fields = dict(payload.get("fieldUpdates") or {})
        fields = sanitize_fields_for_ado(fields, proposal.source_quote)
        if proposal.change_type == ChangeType.CREATE:
            fields.setdefault(TITLE, sanitize_ado_text(proposal.title, proposal.source_quote))
            fields.setdefault(AREA_PATH, self.settings.ado_area_path)
            # Some processes require Description / Acceptance Criteria on create.
            desc = fields.get(DESCRIPTION)
            if isinstance(desc, str):
                unwrapped = extract_append_value(desc)
                if unwrapped is not None:
                    desc = unwrapped
            if not (isinstance(desc, str) and desc.strip()):
                derived = sanitize_ado_text(proposal.rationale, proposal.source_quote).strip()
                fields[DESCRIPTION] = derived or sanitize_ado_text(
                    proposal.title, proposal.source_quote
                ).strip() or "TBD"

            ac = fields.get(ACCEPTANCE_CRITERIA)
            if isinstance(ac, str):
                unwrapped = extract_append_value(ac)
                if unwrapped is not None:
                    ac = unwrapped
            if not (isinstance(ac, str) and ac.strip()):
                fields[ACCEPTANCE_CRITERIA] = "TBD"
            if payload.get("newAssignee"):
                fields[ASSIGNED_TO] = payload["newAssignee"]
            created = await self.create_work_item(
                proposal.work_item_type.value,
                fields,
                payload.get("parentWorkItemId"),
            )
            work_item_id = int(created["id"])
        else:
            if proposal.target_work_item_id is None:
                raise ValueError("target_work_item_id is required for non-create proposals")
            work_item_id = proposal.target_work_item_id
            if proposal.change_type == ChangeType.STATE_TRANSITION:
                fields[STATE] = payload["newState"]
            if proposal.change_type == ChangeType.ASSIGN:
                fields[ASSIGNED_TO] = payload["newAssignee"]
            if fields:
                await self.patch_work_item(work_item_id, fields)
            if proposal.change_type == ChangeType.COMMENT and payload.get("commentText"):
                await self.add_comment(
                    work_item_id,
                    professional_comment_summary(
                        proposal,
                        approver,
                        comment_text=str(payload.get("commentText") or ""),
                    ),
                )
        return work_item_id

    async def build_grounding_catalog(self) -> list[dict[str, Any]]:
        """Return active work items in compact LLM grounding form."""

        ids = await self.query_active_ids()
        items = await self.batch_get(ids)
        return [compact_work_item(item) for item in items]


def compact_work_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert ADO work item payload into compact grounding JSON."""

    fields = item.get("fields", {})
    return {
        "id": item.get("id"),
        "type": fields.get("System.WorkItemType"),
        "title": fields.get(TITLE),
        "state": fields.get(STATE),
        "assignedTo": fields.get(ASSIGNED_TO, {}).get("displayName")
        if isinstance(fields.get(ASSIGNED_TO), dict)
        else fields.get(ASSIGNED_TO),
        "areaPath": fields.get(AREA_PATH),
        "parentId": fields.get("System.Parent"),
        "storyPoints": fields.get("Microsoft.VSTS.Scheduling.StoryPoints"),
        "effort": fields.get("Microsoft.VSTS.Scheduling.Effort"),
        "changedDate": fields.get("System.ChangedDate"),
    }


def format_wiql_datetime(value: datetime) -> str:
    """Format datetimes for WIQL date-precision comparisons."""

    return value.astimezone(timezone.utc).strftime("%Y-%m-%d")


def raise_for_status(response: httpx.Response, operation: str) -> None:
    """Raise an HTTP error with a useful ADO response body."""

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = response.text[:50_000]
        raise RuntimeError(
            f"{operation} failed: HTTP {response.status_code} {response.reason_phrase}; {body}"
        ) from exc


def audit_comment(proposal: ProposedChange, approver: str) -> str:
    """Build the standard Agentic Scrum audit comment."""

    return (
        "Auto-applied by Agentic Scrum from "
        f"{proposal.source_meeting_title} on {proposal.source_meeting_date}. "
        f"Approved by {approver}."
    )


def sanitize_ado_text(text: str, source_quote: str | None = None) -> str:
    """Remove transcript artifacts and verbatim source quotes from ADO-bound text."""

    value = (text or "").replace("\r\n", "\n")
    cleaned_lines: list[str] = []
    for line in value.split("\n"):
        if TRANSCRIPT_ARTIFACT_RE.search(line):
            continue
        if TIMECODE_RE.match(line):
            continue
        if SPEAKER_TIME_RE.match(line):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)

    if source_quote:
        sq = source_quote.replace("\r\n", "\n")
        # Remove exact quote and long quote lines if present.
        if sq and sq in cleaned:
            cleaned = cleaned.replace(sq, "")
        for sq_line in (part.strip() for part in sq.split("\n")):
            if len(sq_line) >= 20 and sq_line in cleaned:
                cleaned = cleaned.replace(sq_line, "")

    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def sanitize_fields_for_ado(fields: dict[str, Any], source_quote: str | None) -> dict[str, Any]:
    """Sanitize string field updates to ensure no transcript quotes are written."""

    sanitized: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, str):
            cleaned = sanitize_ado_text(value, source_quote)
            # Avoid writing empty strings that can trip required-field rules.
            if not cleaned.strip():
                continue
            sanitized[key] = cleaned
        else:
            sanitized[key] = value
    return sanitized


def professional_comment_summary(
    proposal: ProposedChange,
    approver: str,
    *,
    comment_text: str,
) -> str:
    """Build an ADO-friendly comment with no automation watermark."""

    _unused = approver
    details = sanitize_ado_text(comment_text, proposal.source_quote).strip()
    if details:
        if len(details) > 4000:
            details = details[:4000] + "\n\n[TRUNCATED]"
        return details

    # Fallback (should be rare because we filter useless comment proposals upstream).
    summary = sanitize_ado_text(proposal.title, proposal.source_quote).strip()
    rationale = sanitize_ado_text(proposal.rationale, proposal.source_quote).strip()
    parts: list[str] = []
    if summary:
        parts.append(summary)
    if rationale and rationale.lower() != summary.lower():
        parts.append(rationale)
    return "\n\n".join(parts).strip()
