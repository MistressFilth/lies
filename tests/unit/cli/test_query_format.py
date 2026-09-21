"""CLI tests for the ``lies query --format`` flag (F1).

The --format flag accepts ``auto|md|table|marp``. Default is ``auto``
(the synthesizer's format_hint). Explicit values force a re-synthesis
path when they differ from the auto-route hint.

These tests pin the flag-registration + dispatch contract at the CLI
boundary; the override re-synthesis lives in the orchestrator (Task 7),
so the override path's graceful fallback to the auto-route answer is
also pinned here.

Test discipline: ``Orchestrator`` and ``resolve_wiki`` are mocked at
the ``lies.cli.__init__`` boundary (the project's existing
convention; see ``tests/unit/test_query_cli.py`` and
``tests/unit/cli/test_query_cli.py``). The query command body resolves
both names through ``lies.cli`` so ``mock.patch("lies.cli.<name>")``
intercepts the call without needing per-module indirection.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.query.models import SynthesizedAnswer
from tests.unit.cli._ansi import strip_ansi


@pytest.fixture
def cli_runner() -> CliRunner:
    return CliRunner()


def test_query_help_mentions_format(cli_runner: CliRunner) -> None:
    result = cli_runner.invoke(app, ["query", "--help"])
    assert result.exit_code == 0
    # Typer+Rich splits flag names with ANSI escapes when rendering
    # through CliRunner; strip them before substring matching.
    assert "--format" in strip_ansi(result.stdout)


def test_query_unknown_format_exits_2(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown --format values exit 2 with a clear error."""
    # Stub Orchestrator + resolve_wiki so the test doesn't touch disk.
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda name: MagicMock())
    monkeypatch.setattr("lies.cli.Orchestrator", MagicMock())

    result = cli_runner.invoke(
        app,
        ["query", "--format", "canvas", "what is a hook?"],
    )
    assert result.exit_code == 2
    # The error message is emitted to stderr; ``result.output`` joins
    # stdout + stderr so the assertion is locale-agnostic.
    combined = strip_ansi((result.output or "")).lower()
    assert "canvas" in combined or "unknown" in combined


def test_query_default_format_is_auto(cli_runner: CliRunner) -> None:
    """Default --format is auto (operator doesn't have to specify)."""
    result = cli_runner.invoke(app, ["query", "--help"])
    plain = strip_ansi(result.stdout).lower()
    assert "--format" in plain
    # Help text mentions "auto" as default.
    assert "auto" in plain


def test_query_format_md_combined_with_collection(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--format=md combines with --collection / --no-file without error."""

    orch = MagicMock()
    orch.run_query.return_value = SynthesizedAnswer(
        answer="hello\n",
        question="q",
        format="md",
    )
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda name: MagicMock())
    monkeypatch.setattr("lies.cli.Orchestrator", lambda wiki: orch)

    result = cli_runner.invoke(
        app,
        ["query", "--format", "md", "--collection", "x", "what?"],
    )
    assert result.exit_code == 0


def test_query_format_chart_combined_with_collection(
    cli_runner: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--format=chart is accepted at the CLI boundary."""

    orch = MagicMock()
    orch.run_query.return_value = SynthesizedAnswer(
        answer="```mermaid\ngraph LR\n  A --> B\n```\n",
        question="q",
        format="chart",
    )
    monkeypatch.setattr("lies.cli.resolve_wiki", lambda name: MagicMock())
    monkeypatch.setattr("lies.cli.Orchestrator", lambda wiki: orch)

    result = cli_runner.invoke(
        app,
        ["query", "--format", "chart", "--collection", "x", "what?"],
    )
    assert result.exit_code == 0
    assert "graph LR" in strip_ansi(result.stdout)


def test_query_format_chart_help_lists_chart(
    cli_runner: CliRunner,
) -> None:
    """The --format help text advertises the chart value."""
    result = cli_runner.invoke(app, ["query", "--help"])
    plain = strip_ansi(result.stdout).lower()
    assert "chart" in plain
