"""Azure DevOps field constants and transformation helpers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

ACCEPTANCE_CRITERIA = "Microsoft.VSTS.Common.AcceptanceCriteria"
AREA_PATH = "System.AreaPath"
ASSIGNED_TO = "System.AssignedTo"
DESCRIPTION = "System.Description"
EFFORT = "Microsoft.VSTS.Scheduling.Effort"
STATE = "System.State"
STORY_POINTS = "Microsoft.VSTS.Scheduling.StoryPoints"
TITLE = "System.Title"

APPEND_PATTERN = re.compile(r"^\s*<APPEND>(?P<content>.*?)</APPEND>\s*$", re.DOTALL)


def extract_append_value(value: Any) -> str | None:
    """Return the inner APPEND value if the field update is append-wrapped."""

    if not isinstance(value, str):
        return None
    match = APPEND_PATTERN.match(value)
    if not match:
        return None
    return match.group("content").strip()


def merge_append_value(existing: Any, append_value: str) -> str:
    """Append new content to an existing ADO field value."""

    current = "" if existing is None else str(existing).strip()
    addition = append_value.strip()
    if not current:
        return addition
    if not addition:
        return current
    separator = "\n\n" if "<" in current or "\n" in current else "\n"
    return f"{current}{separator}{addition}"


def nearest_scale_value(raw_value: float | int, scale: Iterable[int]) -> int:
    """Return the nearest configured estimation scale value."""

    values = list(scale)
    if not values:
        raise ValueError("Scale cannot be empty")
    return min(values, key=lambda item: (abs(item - float(raw_value)), item))


def is_closure_state(state: str | None) -> bool:
    """Return whether an ADO state represents completion."""

    return (state or "").strip().lower() in {"closed", "done"}


def json_patch_add(path: str, value: Any) -> dict[str, Any]:
    """Build an ADO JSON Patch add operation."""

    return {"op": "add", "path": path, "value": value}


def json_patch_replace(path: str, value: Any) -> dict[str, Any]:
    """Build an ADO JSON Patch replace operation."""

    return {"op": "replace", "path": path, "value": value}


def field_patch(field_name: str, value: Any) -> dict[str, Any]:
    """Build a JSON Patch operation for a work item field."""

    return json_patch_add(f"/fields/{field_name}", value)


def relation_patch(url: str, rel: str = "System.LinkTypes.Hierarchy-Reverse") -> dict[str, Any]:
    """Build a JSON Patch operation to link a work item to a parent."""

    return json_patch_add("/relations/-", {"rel": rel, "url": url})
