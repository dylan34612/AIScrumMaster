"""Legacy Microsoft Graph client for Teams chats and approvals."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from agenticscrum.config import Settings
from agenticscrum.schemas import ApprovalCommand
from agenticscrum.teams.auth import GraphAuthenticator
from agenticscrum.transcript_formats import parse_vtt_to_text


GRAPH_BASE = "https://graph.microsoft.com/v1.0"
HTML_TAG_RE = re.compile(r"<[^>]+>")
APPROVAL_RE = re.compile(r"\b(?P<action>APPROVE|REJECT)\s+(?P<token>[A-Za-z0-9_-]{16,64})\b", re.I)


class TeamsClient:
    """Async Microsoft Graph client for legacy Teams data."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.auth = GraphAuthenticator(settings)
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> TeamsClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        """Return the underlying HTTP client."""

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=60)
        return self._client

    def auth_headers(self) -> dict[str, str]:
        """Return bearer auth headers for Microsoft Graph."""

        return {"Authorization": f"Bearer {self.auth.acquire_token()}"}

    @retry(
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def list_chat_messages(self, chat_id: str, since: datetime) -> list[dict[str, Any]]:
        """List Teams chat messages modified since a timestamp."""

        since_iso = (
            since.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        now_iso = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        url = f"{GRAPH_BASE}/chats/{chat_id}/messages"
        response = await self.client.get(
            url,
            headers=self.auth_headers(),
            params={
                "$top": "50",
                "$orderby": "lastModifiedDateTime desc",
                "$filter": f"lastModifiedDateTime gt {since_iso} and lastModifiedDateTime lt {now_iso}",
            },
        )
        raise_for_status(response, f"Graph list chat messages {chat_id}")
        return list(response.json().get("value", []))

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        """Fetch the chat object (includes onlineMeetingInfo for meeting chats)."""

        url = f"{GRAPH_BASE}/chats/{chat_id}"
        response = await self.client.get(
            url,
            headers=self.auth_headers(),
            params={"$select": "id,topic,chatType,onlineMeetingInfo"},
        )
        raise_for_status(response, f"Graph get chat {chat_id}")
        return dict(response.json())

    async def get_online_meeting_by_join_web_url(self, join_web_url: str) -> dict[str, Any] | None:
        """Resolve a Graph onlineMeeting by joinWebUrl (delegated /me token)."""

        join_escaped = join_web_url.replace("'", "''")
        url = f"{GRAPH_BASE}/me/onlineMeetings"
        response = await self.client.get(
            url,
            headers=self.auth_headers(),
            params={"$filter": f"joinWebUrl eq '{join_escaped}'", "$top": "1"},
        )
        raise_for_status(response, "Graph get onlineMeeting by joinWebUrl")
        value = list(response.json().get("value", []))
        if not value:
            return None
        if not isinstance(value[0], dict):
            return None
        return dict(value[0])

    async def list_online_meeting_transcripts(self, online_meeting_id: str) -> list[dict[str, Any]]:
        """List transcript artifacts for a scheduled online meeting."""

        meeting_id = quote(str(online_meeting_id), safe="")
        url = f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts"
        response = await self.client.get(
            url,
            headers=self.auth_headers(),
            params={"$top": "20"},
        )
        raise_for_status(response, "Graph list meeting transcripts")
        return list(response.json().get("value", []))

    async def get_online_meeting_transcript_content(
        self, online_meeting_id: str, transcript_id: str
    ) -> str:
        """Fetch transcript content as VTT."""

        meeting_id = quote(str(online_meeting_id), safe="")
        tid = quote(str(transcript_id), safe="")
        url = f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts/{tid}/content"
        response = await self.client.get(
            url,
            headers=self.auth_headers(),
            params={"$format": "text/vtt"},
        )
        raise_for_status(response, "Graph get transcript content")
        return response.text

    async def get_online_meeting_transcript_metadata(
        self, online_meeting_id: str, transcript_id: str
    ) -> str:
        """Fetch transcript metadataContent as VTT with JSON utterances."""

        meeting_id = quote(str(online_meeting_id), safe="")
        tid = quote(str(transcript_id), safe="")
        url = f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts/{tid}/metadataContent"
        response = await self.client.get(
            url,
            headers=self.auth_headers(),
            params={"$format": "text/vtt"},
        )
        raise_for_status(response, "Graph get transcript metadataContent")
        return response.text

    def _pick_latest_transcript(
        self, transcripts: list[dict[str, Any]], since: datetime
    ) -> dict[str, Any] | None:
        """Pick the newest transcript created after the lookback window."""

        def parse_graph_datetime(value: object) -> datetime | None:
            if not isinstance(value, str) or not value.strip():
                return None
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            # Graph can return 7-digit fractional seconds; trim to 6 for Python.
            text = re.sub(r"(\.\d{6})\d+", r"\1", text)
            try:
                return datetime.fromisoformat(text)
            except Exception:
                return None

        best: tuple[datetime, dict[str, Any]] | None = None
        for t in transcripts:
            if not isinstance(t, dict):
                continue
            created = parse_graph_datetime(t.get("createdDateTime"))
            if created is None:
                continue
            if created < since:
                continue
            if best is None or created > best[0]:
                best = (created, t)
        return best[1] if best else None

    def _parse_vtt_to_text(self, vtt: str) -> str:
        """Normalize VTT transcript (content or metadataContent) into plain text."""

        return parse_vtt_to_text(vtt)

    async def _try_read_meeting_transcript(self, chat_id: str, since: datetime) -> str:
        """Try to read a Teams meeting transcript artifact via Graph."""

        chat = await self.get_chat(chat_id)
        meeting_info = chat.get("onlineMeetingInfo") or {}
        join_url = meeting_info.get("joinWebUrl")
        if not isinstance(join_url, str) or not join_url.strip():
            return ""

        online_meeting = await self.get_online_meeting_by_join_web_url(join_url)
        meeting_id = str(online_meeting.get("id")) if isinstance(online_meeting, dict) else ""
        if not meeting_id:
            return ""

        transcripts = await self.list_online_meeting_transcripts(meeting_id)
        picked = self._pick_latest_transcript(transcripts, since)
        if not picked:
            return ""
        transcript_id = str(picked.get("id") or "").strip()
        if not transcript_id:
            return ""

        # Prefer metadataContent because it has speakerName + spokenText.
        try:
            vtt = await self.get_online_meeting_transcript_metadata(meeting_id, transcript_id)
            parsed = self._parse_vtt_to_text(vtt)
            if parsed:
                return parsed
        except Exception:
            pass

        vtt = await self.get_online_meeting_transcript_content(meeting_id, transcript_id)
        return self._parse_vtt_to_text(vtt)

    async def read_meeting_notes(
        self,
        chat_id: str,
        since: datetime,
        sender_filter: str | None,
    ) -> str:
        """Read and normalize Teams meeting notes from a chat."""

        # Prefer the meeting transcript artifact if available (much higher quality than
        # facilitator summary messages). Fall back to chat messages if transcripts
        # are unavailable or the tenant hasn't granted transcript scopes.
        try:
            transcript = await self._try_read_meeting_transcript(chat_id, since)
            if transcript.strip():
                return transcript
        except Exception:
            pass

        messages = await self.list_chat_messages(chat_id, since)
        parts: list[str] = []
        for message in messages:
            from_obj = message.get("from") or {}
            user_obj = from_obj.get("user") or {}
            app_obj = from_obj.get("application") or {}
            sender = str(
                user_obj.get("displayName")
                or app_obj.get("displayName")
                or ""
            )
            if sender_filter and sender_filter.lower() not in sender.lower():
                continue
            body_obj = message.get("body") or {}
            body = str(body_obj.get("content") or "")
            text = strip_html(body)
            if text:
                parts.append(f"{sender}: {text}" if sender else text)
        return "\n\n".join(parts)

    async def post_channel_message(self, text: str) -> dict[str, Any]:
        """Post a message to the configured Teams approval channel."""

        url = (
            f"{GRAPH_BASE}/teams/{self.settings.teams_approval_team_id}"
            f"/channels/{self.settings.teams_approval_channel_id}/messages"
        )
        response = await self.client.post(
            url,
            headers={**self.auth_headers(), "Content-Type": "application/json"},
            json={"body": {"contentType": "html", "content": html.escape(text).replace("\n", "<br>")}},
        )
        raise_for_status(response, "Graph post Teams channel message")
        return dict(response.json())

    async def read_channel_replies(self, message_id: str) -> list[dict[str, Any]]:
        """Read replies to a Teams channel message."""

        url = (
            f"{GRAPH_BASE}/teams/{self.settings.teams_approval_team_id}"
            f"/channels/{self.settings.teams_approval_channel_id}/messages/{message_id}/replies"
        )
        response = await self.client.get(url, headers=self.auth_headers())
        raise_for_status(response, "Graph read Teams channel replies")
        return list(response.json().get("value", []))

    async def read_channel_messages(self) -> list[dict[str, Any]]:
        """Read recent messages from the configured Teams approval channel."""

        url = (
            f"{GRAPH_BASE}/teams/{self.settings.teams_approval_team_id}"
            f"/channels/{self.settings.teams_approval_channel_id}/messages"
        )
        response = await self.client.get(url, headers=self.auth_headers(), params={"$top": "50"})
        raise_for_status(response, "Graph read Teams channel messages")
        return list(response.json().get("value", []))


def strip_html(content: str) -> str:
    """Strip Graph HTML message bodies to readable text."""

    return html.unescape(HTML_TAG_RE.sub(" ", content)).strip()


def parse_approval_command(body: str, message_id: str, responder: str | None = None) -> ApprovalCommand | None:
    """Parse an approval command from ADO or Teams text."""

    text = strip_html(body)
    match = APPROVAL_RE.search(text)
    if not match:
        return None
    return ApprovalCommand(
        action=match.group("action").upper(),
        token=match.group("token"),
        responder=responder,
        message_id=message_id,
        body=text,
    )


def raise_for_status(response: httpx.Response, operation: str) -> None:
    """Raise an HTTP error with a useful Graph response body."""

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = response.text[:1000]
        raise RuntimeError(
            f"{operation} failed: HTTP {response.status_code} {response.reason_phrase}; {body}"
        ) from exc
