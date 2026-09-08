"""Tests for the ``lies ingest`` sub-app (Task 10).

The CLI is a Typer sub-app (``library_app``) that exposes a single
``ingest`` command with two modes:

- ``--source <PATH|URL>`` for one-off single-source ingestion
- ``--batch  <DIR>``    for walking a directory of sources

These tests cover the CLI wiring only (no actual ingest path is
exercised; the unit tests for ``run_source_ingest`` /
``run_batch_ingest`` live in ``test_ingest.py``).
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from lies.library.cli import library_app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def test_library_app_wires_ingest_command() -> None:
    """``library_app ingest --help`` lists both --source and --batch."""
    result = runner.invoke(library_app, ["ingest", "--help"])
    assert result.exit_code == 0, (
        f"expected exit 0; got {result.exit_code}; stderr={result.stderr!r}"
    )
    out = _strip_ansi(result.stdout)
    assert "--source" in out
    assert "--batch" in out


def test_help_text_mentions_library() -> None:
    """The ``ingest`` help body should advertise the library / wiki context."""
    result = runner.invoke(
        library_app,
        ["ingest", "--source", "p", "--help"],
    )
    out = _strip_ansi(result.stdout).lower()
    assert "library" in out or "wiki" in out
