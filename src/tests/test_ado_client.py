"""Tests for Azure DevOps client helpers."""

from datetime import datetime, timezone

from agenticscrum.ado.client import format_wiql_datetime


def test_format_wiql_datetime() -> None:
    value = datetime(2026, 6, 22, 20, 37, 12, 123456, tzinfo=timezone.utc)
    assert format_wiql_datetime(value) == "2026-06-22"
