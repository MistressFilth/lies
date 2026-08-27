"""Pin help-text honesty for the bootstrap-on-missing commands."""

from __future__ import annotations

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


def test_ingest_help_advertises_bootstrap() -> None:
    result = runner.invoke(app, ["ingest", "--help"])
    assert result.exit_code == 0
    assert "bootstrap" in result.stdout.lower()


def test_sync_help_advertises_bootstrap() -> None:
    result = runner.invoke(app, ["sync", "--help"])
    assert result.exit_code == 0
    assert "--source" in result.stdout
    assert "bootstrap" in result.stdout.lower()


def test_ingest_source_help_lists_collection_flag() -> None:
    result = runner.invoke(app, ["ingest-source", "--help"])
    assert result.exit_code == 0
    assert "--collection" in result.stdout
