"""Tests for approval command parsing."""

from agenticscrum.teams.client import parse_approval_command


def test_parse_ado_approval_command() -> None:
    command = parse_approval_command("Please APPROVE abcdefghijklmnop", "1", "Assignee")
    assert command is not None
    assert command.action == "APPROVE"
    assert command.token == "abcdefghijklmnop"
    assert command.responder == "Assignee"


def test_parse_reject_command_from_html() -> None:
    command = parse_approval_command("<p>REJECT abcdefghijklmnop</p>", "2")
    assert command is not None
    assert command.action == "REJECT"
