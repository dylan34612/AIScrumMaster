"""Tests for runtime settings behavior."""

from agenticscrum.config import Settings


def test_inbox_extensions_normalize() -> None:
    settings = Settings(notes_inbox_extensions="md, .txt")

    assert settings.notes_inbox_extensions == [".md", ".txt"]


def test_story_points_scale_parses_string() -> None:
    settings = Settings(ado_story_points_scale="1,2,3")

    assert settings.ado_story_points_scale == [1, 2, 3]
