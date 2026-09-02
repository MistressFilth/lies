"""Top-level --name is removed; subcommand --name is preserved."""

from __future__ import annotations

from typer.testing import CliRunner

from lies.cli import app

runner = CliRunner()


def test_top_level_name_option_errors_with_no_such_option() -> None:
    result = runner.invoke(app, ["--name", "default", "config"])
    assert result.exit_code == 2
    assert "no such option" in result.output.lower()


def test_subcommand_name_option_still_recognized() -> None:
    result = runner.invoke(app, ["config", "--name", "default"])
    assert "no such option" not in result.output.lower()
