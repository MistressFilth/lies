"""Pin help-text honesty for the bootstrap-on-missing commands."""

from __future__ import annotations

from typer.testing import CliRunner

from lies.cli import app
from tests.unit.cli._ansi import strip_ansi

runner = CliRunner()


def test_ingest_help_advertises_bootstrap() -> None:
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "bootstrap" in strip_ansi(result.output).lower()


def test_sync_help_advertises_bootstrap() -> None:
    result = runner.invoke(app, ["sync", "--help"])
    assert result.exit_code == 0
    text = strip_ansi(result.output)
    assert "--source" in text
    assert "bootstrap" in text.lower()


def test_ingest_source_help_lists_collection_flag() -> None:
    result = runner.invoke(app, ["ingest-source", "--help"])
    assert result.exit_code == 0
    assert "--collection" in strip_ansi(result.output)
