"""Tests for ADO field helpers."""

from agenticscrum.ado.fields import extract_append_value, merge_append_value, nearest_scale_value


def test_append_merge() -> None:
    assert extract_append_value("<APPEND>New AC</APPEND>") == "New AC"
    assert merge_append_value("Existing", "New AC") == "Existing\nNew AC"


def test_nearest_scale_value() -> None:
    assert nearest_scale_value(6, [1, 2, 3, 5, 8, 13]) == 5
    assert nearest_scale_value(7, [1, 2, 3, 5, 8, 13]) == 8
