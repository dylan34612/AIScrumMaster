"""Local notes inbox ingestion helpers.

This module lets the app ingest facilitator notes without Microsoft Graph by
polling a local folder for new `.md` / `.txt` / `.vtt` / `.docx` files.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from agenticscrum.config import PROJECT_ROOT, Settings
from agenticscrum.transcript_formats import read_transcript_text


_FRONT_MATTER_START_RE = re.compile(r"^\s*---\s*$")
_KEY_VALUE_RE = re.compile(r"^\s*(?P<key>meeting\s*title|title|meeting|meeting\s*date|date)\s*:\s*(?P<value>.+?)\s*$", re.I)
_MD_H1_RE = re.compile(r"^\s*#\s+(?P<title>.+?)\s*$")
_ISO_DATE_RE = re.compile(r"\b(?P<date>\d{4}-\d{2}-\d{2})\b")
_US_DATE_RE = re.compile(r"\b(?P<month>\d{1,2})[/-](?P<day>\d{1,2})[/-](?P<year>\d{4})\b")


@dataclass(frozen=True)
class InboxNote:
    """Parsed inbox note file."""

    path: Path
    title: str
    meeting_date: date
    notes: str
    content_hash: str


def resolve_inbox_dir(settings: Settings) -> Path:
    """Return the inbox directory as an absolute path."""

    path = Path(settings.notes_inbox_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def resolve_archive_dir(settings: Settings) -> Path:
    """Return the archive directory as an absolute path."""

    path = Path(settings.notes_inbox_archive_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def list_inbox_files(settings: Settings) -> list[Path]:
    """List candidate note files in the inbox folder."""

    inbox_dir = resolve_inbox_dir(settings)
    inbox_dir.mkdir(parents=True, exist_ok=True)

    allowed = set(settings.notes_inbox_extensions)
    candidates: list[Path] = []
    for path in inbox_dir.iterdir():
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in allowed:
            continue
        try:
            age_seconds = max(0.0, (datetime.now().timestamp() - path.stat().st_mtime))
        except OSError:
            continue
        if age_seconds < float(settings.notes_inbox_min_age_seconds):
            continue
        candidates.append(path)

    candidates.sort(key=lambda p: p.stat().st_mtime)
    return candidates


def read_inbox_notes(settings: Settings) -> list[InboxNote]:
    """Read and parse all eligible inbox files."""

    notes: list[InboxNote] = []
    for path in list_inbox_files(settings):
        notes.append(parse_inbox_file(settings, path))
    return notes


def parse_inbox_file(settings: Settings, path: Path) -> InboxNote:
    """Parse a single inbox file into meeting metadata + notes."""

    raw = read_transcript_text(path)
    title: str | None = None
    meeting_date: date | None = None
    body = raw

    # 1) YAML front matter
    fm = _extract_front_matter_yaml(raw)
    if fm is not None:
        meta, remainder = fm
        title = _coalesce_title(meta)
        meeting_date = _coalesce_date(meta)
        body = remainder

    # 2) Key-value header block
    if title is None or meeting_date is None:
        header = _extract_key_value_header(body)
        if header is not None:
            meta, remainder = header
            title = title or _coalesce_title(meta)
            meeting_date = meeting_date or _coalesce_date(meta)
            body = remainder

    # 3) Markdown H1
    if title is None:
        title_from_h1, remainder = _extract_markdown_h1(body)
        if title_from_h1:
            title = title_from_h1
            body = remainder

    # 4) Filename heuristics
    title_from_name, date_from_name = _parse_title_date_from_filename(path)
    title = title or title_from_name
    meeting_date = meeting_date or date_from_name

    # 5) Fallbacks
    if title is None:
        title = path.stem.replace("_", " ").strip() or "Meeting notes"
    if meeting_date is None:
        meeting_date = datetime.fromtimestamp(path.stat().st_mtime).date()

    notes = body.strip()
    if not notes:
        raise RuntimeError(f"Inbox note file is empty: {path}")

    content_hash = hashlib.sha256(notes.encode("utf-8")).hexdigest()
    return InboxNote(
        path=path,
        title=title.strip(),
        meeting_date=meeting_date,
        notes=notes,
        content_hash=content_hash,
    )


def finalize_processed_file(settings: Settings, note: InboxNote) -> Path | None:
    """Archive/delete/keep an inbox file after successful ingestion.

    Returns the archive destination if a move occurred.
    """

    mode = settings.notes_inbox_archive_mode
    if mode == "keep":
        return None
    if mode == "delete":
        note.path.unlink(missing_ok=True)
        return None

    archive_dir = resolve_archive_dir(settings)
    archive_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_filename(note.path.name)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = archive_dir / f"{timestamp}__{safe_name}"
    counter = 1
    while dest.exists():
        dest = archive_dir / f"{timestamp}__{counter}__{safe_name}"
        counter += 1
    shutil.move(str(note.path), str(dest))
    return dest


def write_processed_transcript_copy(
    settings: Settings,
    *,
    title: str,
    meeting_date: date,
    notes: str,
    source: str = "Manual",
    run_id: int | None = None,
    run_status: str | None = None,
) -> Path:
    """Write a plain-text transcript copy to the processed/archive folder."""

    archive_dir = resolve_archive_dir(settings)
    archive_dir.mkdir(parents=True, exist_ok=True)

    safe_title = _safe_filename(title).replace(".txt", "").replace(".md", "").strip() or "meeting"
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"{meeting_date.isoformat()} - {safe_title} ({source}).txt"
    safe_name = _safe_filename(base_name)
    dest = archive_dir / f"{timestamp}__{safe_name}"
    counter = 1
    while dest.exists():
        dest = archive_dir / f"{timestamp}__{counter}__{safe_name}"
        counter += 1

    header_lines = [
        f"Meeting Title: {title}",
        f"Meeting Date: {meeting_date.isoformat()}",
        f"Source: {source}",
        f"Captured At: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if run_id is not None:
        header_lines.append(f"Ingestion Run ID: {run_id}")
    if run_status is not None:
        header_lines.append(f"Ingestion Run Status: {run_status}")

    content = "\n".join(header_lines) + "\n\n" + notes.strip() + "\n"
    dest.write_text(content, encoding="utf-8")
    return dest


def _extract_front_matter_yaml(text: str) -> tuple[dict[str, Any], str] | None:
    lines = text.splitlines()
    if not lines:
        return None
    if not _FRONT_MATTER_START_RE.match(lines[0]):
        return None

    end_idx: int | None = None
    for i in range(1, min(len(lines), 200)):
        if _FRONT_MATTER_START_RE.match(lines[i]):
            end_idx = i
            break
    if end_idx is None:
        return None

    fm_text = "\n".join(lines[1:end_idx]).strip()
    try:
        meta = yaml.safe_load(fm_text) or {}
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None

    remainder = "\n".join(lines[end_idx + 1 :]).lstrip()
    return dict(meta), remainder


def _extract_key_value_header(text: str) -> tuple[dict[str, Any], str] | None:
    lines = text.splitlines()
    meta: dict[str, Any] = {}
    consumed = 0
    for i, line in enumerate(lines[:50]):
        if not line.strip():
            consumed = i + 1
            break
        match = _KEY_VALUE_RE.match(line)
        if not match:
            return None
        key = match.group("key").strip().lower().replace(" ", "")
        value = match.group("value").strip()
        if key in {"meetingtitle", "title", "meeting"}:
            meta["title"] = value
        elif key in {"meetingdate", "date"}:
            meta["date"] = value
    if not meta:
        return None
    remainder = "\n".join(lines[consumed:]).lstrip()
    return meta, remainder


def _extract_markdown_h1(text: str) -> tuple[str | None, str]:
    lines = text.splitlines()
    for i, line in enumerate(lines[:10]):
        if not line.strip():
            continue
        match = _MD_H1_RE.match(line)
        if not match:
            return None, text
        title = match.group("title").strip()
        remainder = "\n".join(lines[i + 1 :]).lstrip()
        return title, remainder
    return None, text


def _parse_title_date_from_filename(path: Path) -> tuple[str | None, date | None]:
    stem = path.stem.replace("_", " ").strip()

    m1 = re.match(r"^(?P<date>\d{4}-\d{2}-\d{2})[ \-–—]+(?P<title>.+)$", stem)
    if m1:
        return (
            m1.group("title").strip(),
            _parse_date(m1.group("date")),
        )
    m2 = re.match(r"^(?P<title>.+?)[ \-–—]+(?P<date>\d{4}-\d{2}-\d{2})$", stem)
    if m2:
        return (
            m2.group("title").strip(),
            _parse_date(m2.group("date")),
        )

    # If a date appears anywhere, try to use it and keep the full stem as title.
    m3 = _ISO_DATE_RE.search(stem)
    if m3:
        return stem, _parse_date(m3.group("date"))
    return stem or None, None


def _coalesce_title(meta: dict[str, Any]) -> str | None:
    for key in ("meeting_title", "meetingTitle", "title", "meeting"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _coalesce_date(meta: dict[str, Any]) -> date | None:
    for key in ("meeting_date", "meetingDate", "date"):
        value = meta.get(key)
        parsed = _parse_date(value)
        if parsed is not None:
            return parsed
    return None


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except Exception:
        pass
    m = _US_DATE_RE.search(text)
    if not m:
        return None
    try:
        return date(int(m.group("year")), int(m.group("month")), int(m.group("day")))
    except Exception:
        return None


def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "notes.txt"

