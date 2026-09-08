"""Tests for the ``ingest-source`` deprecation stub.

``ingest-source`` is a one-minor-version deprecation stub retained while
operators migrate to ``lies ingest --source``. Any invocation exits
non-zero with a stderr message steering the operator to the new
subcommand; the LLM round-trip and ``--no-llm`` flag are gone.
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def test_ingest_source_stub_errors_with_deprecation_message() -> None:
    """Any ``ingest-source`` invocation errors out and steers to ``lies ingest``."""
    result = runner.invoke(
        app,
        ["ingest-source", "raw/x.md", "--collection", "foo"],
    )
    assert result.exit_code == 2, (
        f"expected exit 2; got {result.exit_code}; stderr={result.stderr!r}"
    )
    err = (result.stderr or "") + (result.stdout or "")
    assert "ingest --source" in err


def test_ingest_source_help_advertises_deprecation() -> None:
    """``ingest-source --help`` is marked deprecated and steers operators away."""
    result = runner.invoke(app, ["ingest-source", "--help"])
    assert result.exit_code == 0
    out = _strip_ansi(result.stdout).lower()
    assert "deprecated" in out
