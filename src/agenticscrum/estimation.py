"""Effort and story point estimation helpers."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from agenticscrum.ado.fields import nearest_scale_value
from agenticscrum.config import Settings


def normalize_effort(raw_value: float | int, settings: Settings) -> int:
    """Normalize a raw effort estimate to the configured Fibonacci scale."""

    return nearest_scale_value(raw_value, settings.ado_effort_scale)


def normalize_story_points(raw_value: float | int, settings: Settings) -> int:
    """Normalize a raw story point estimate to the configured scale."""

    return nearest_scale_value(raw_value, settings.ado_story_points_scale)


def needs_split(raw_effort: float | int, settings: Settings) -> bool:
    """Return whether a PBI should be split rather than estimated directly."""

    return float(raw_effort) > max(settings.ado_effort_scale)


def compute_rollups(work_items: list[dict[str, Any]], effort_field: str) -> dict[int, int]:
    """Compute parent effort rollups from child work item fields and relations."""

    children_by_parent: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in work_items:
        parent_id = extract_parent_id(item)
        if parent_id is not None:
            children_by_parent[parent_id].append(item)
    rollups: dict[int, int] = {}
    for parent_id, children in children_by_parent.items():
        total = 0
        for child in children:
            effort = child.get("fields", {}).get(effort_field)
            if isinstance(effort, int | float):
                total += int(effort)
        rollups[parent_id] = total
    return rollups


def extract_parent_id(item: dict[str, Any]) -> int | None:
    """Extract a parent ID from ADO relations when available."""

    for relation in item.get("relations", []) or []:
        if relation.get("rel") != "System.LinkTypes.Hierarchy-Reverse":
            continue
        url = str(relation.get("url", ""))
        try:
            return int(url.rstrip("/").split("/")[-1])
        except ValueError:
            return None
    return None
