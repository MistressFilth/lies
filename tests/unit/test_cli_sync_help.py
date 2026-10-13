"""Cheap regression test: ``--skip-reindex`` is exposed in ``lies sync --help``."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """Strip ANSI escape sequences from ``text``."""
    return _ANSI_ESCAPE_RE.sub("", text)


def test_sync_skip_reindex_flag_exists() -> None:
    """``--skip-reindex`` flag is exposed in the sync CLI help."""
    result = runner.invoke(app, ["sync", "--help"])
    assert "--skip-reindex" in _plain(result.output)
