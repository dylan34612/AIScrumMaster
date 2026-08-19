"""Tests for estimation and rollup helpers."""

from agenticscrum.config import Settings
from agenticscrum.estimation import compute_rollups, needs_split, normalize_effort


def test_effort_normalization_and_split() -> None:
    settings = Settings(ado_effort_scale=[1, 2, 3, 5, 8, 13])
    assert normalize_effort(9, settings) == 8
    assert needs_split(14, settings)


def test_rollup_computation() -> None:
    work_items = [
        {
            "id": 2,
            "fields": {"Microsoft.VSTS.Scheduling.Effort": 5},
            "relations": [{"rel": "System.LinkTypes.Hierarchy-Reverse", "url": "x/1"}],
        },
        {
            "id": 3,
            "fields": {"Microsoft.VSTS.Scheduling.Effort": 8},
            "relations": [{"rel": "System.LinkTypes.Hierarchy-Reverse", "url": "x/1"}],
        },
    ]
    assert compute_rollups(work_items, "Microsoft.VSTS.Scheduling.Effort") == {1: 13}
