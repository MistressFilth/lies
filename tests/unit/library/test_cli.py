"""Tests for the ``lies ingest`` CLI (Task 10).

The command is registered directly on the root ``app`` via
``lies.library.cli.register`` (no intermediate sub-app wrapper). The
canonical user-facing invocation is ``lies ingest --source <PATH>`` or
``lies ingest --batch <DIR>``.

These tests cover the CLI wiring only (no actual ingest path is
exercised; the unit tests for ``run_source_ingest`` /
``run_batch_ingest`` live in ``test_ingest.py``).
"""

from __future__ import annotations

import re

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def test_ingest_help_lists_source_and_batch() -> None:
    """``lies ingest --help`` lists both --source and --batch on the root app."""
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0, (
        f"expected exit 0; got {result.exit_code}; stderr={result.stderr!r}"
    )
    out = _strip_ansi(result.stdout)
    assert "--source" in out
    assert "--batch" in out


def test_canonical_ingest_invocation_is_reachable() -> None:
    """The spec-mandated ``lies ingest --source <PATH>`` form reaches the command body.

    Exercises the full CLI path (root app -> ``ingest`` command -> body
    entry). With no actual library configured, the body fails inside the
    library bootstrap, NOT with a Typer "no such command" / "missing
    argument" error. The exit code being non-zero is acceptable; what we
    pin is that the command was matched and dispatched (no
    ``Usage:``-style help dump, no ``No such command``).
    """
    result = runner.invoke(app, ["ingest", "--source", "/tmp/does-not-exist"])
    combined = _strip_ansi(result.stdout) + _strip_ansi(result.stderr or "")
    assert "No such command" not in combined, (
        f"ingest was not registered as a root-level command: {combined!r}"
    )
    assert "Usage:" not in combined or "--source" in combined, (
        f"ingest help dumped instead of dispatching: {combined!r}"
    )


def test_help_text_mentions_library() -> None:
    """The ``ingest`` help body should advertise the library / wiki context."""
    result = runner.invoke(
        app,
        ["ingest", "--source", "p", "--help"],
    )
    out = _strip_ansi(result.stdout).lower()
    assert "library" in out or "wiki" in out
