"""CLI tests for the ``lies query --format`` flag (F1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.query.models import SynthesizedAnswer
from tests.unit.cli._ansi import strip_ansi

pytestmark = pytest.mark.slow


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


# ---------------------------------------------------------------------------
# Direct ``render_answer`` chart-dispatch tests.
#
# ``test_query_format_chart_combined_with_collection`` above covers the
# happy path through the Typer runner (mermaid-bearing body → stdout
# echo). The chart dispatch has three branches inside
# ``_render_chart`` (mermaid-present, prose-only, empty-body) and a
# single stderr warning side-effect on the non-mermaid branches. Pin
# each branch directly via the public entry point so a future refactor
# of the branch order or the comparison operator surfaces as a test
# failure rather than a silent stderr regression.
# ---------------------------------------------------------------------------


def test_render_answer_chart_dispatch_with_mermaid_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Mermaid-bearing body: stdout echoes the rendered diagram; no stderr."""
    from lies.cli.query_format import render_answer

    body = "```mermaid\nflowchart LR\n  A[hook] --> B[registry]\n```\n"
    render_answer("chart", body)

    out = capsys.readouterr()
    assert "flowchart LR" in out.out
    assert "A[hook]" in out.out
    assert "B[registry]" in out.out
    assert out.err == ""


def test_render_answer_chart_dispatch_with_prose_only_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Prose-only body: stdout echoes the body unchanged; stderr warns."""
    from lies.cli.query_format import render_answer

    body = "This answer has no diagram at all, just prose.\n"
    render_answer("chart", body)

    out = capsys.readouterr()
    # ``typer.echo`` appends a trailing newline; ``body`` already ends
    # in ``\n``, so the rendered stdout carries an extra ``\n``.
    # Assert the body is the prefix rather than equality so the test
    # is robust to echo's line-break behavior.
    assert out.out.rstrip("\n") == body.rstrip("\n")
    assert "warning" in out.err.lower()
    assert "mermaid" in out.err.lower()


def test_render_answer_chart_dispatch_with_empty_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Empty body: no stdout echo (avoid blank line before stderr); stderr warns."""
    from lies.cli.query_format import render_answer

    render_answer("chart", "")

    out = capsys.readouterr()
    assert out.out == ""
    assert "warning" in out.err.lower()
    assert "mermaid" in out.err.lower()
