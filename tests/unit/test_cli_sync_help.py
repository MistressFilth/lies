"""Cheap regression test: ``--skip-reindex`` is exposed in ``lies sync --help``.

Lives in its own slow-disabled module because the historical
``tests/unit/test_cli_sync_exit_code.py`` declares
``pytestmark = pytest.mark.slow`` at module scope (the existing tests
invoke the full Typer CLI). This test only does a ``--help``
substring lookup, which is cheap and must run by default.
"""

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
