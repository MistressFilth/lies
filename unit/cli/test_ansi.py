"""Sanity tests for the shared ANSI-strip helper."""

from __future__ import annotations

from tests.unit.cli._ansi import _ANSI_RE, strip_ansi


def test_strip_ansi_removes_sgr_codes() -> None:
    assert strip_ansi("\x1b[1;2m--source\x1b[0m") == "--source"


def test_strip_ansi_handles_plain_text() -> None:
    assert strip_ansi("plain text") == "plain text"


def test_strip_ansi_handles_empty_string() -> None:
    assert strip_ansi("") == ""


def test_ansi_re_matches_cursor_codes() -> None:
    # \x1b[2K is "erase entire line" — covered by the broader regex.
    assert _ANSI_RE.match("\x1b[2Kmore text") is not None
