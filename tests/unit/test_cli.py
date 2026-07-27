from __future__ import annotations

from typer.testing import CliRunner

from lies import __version__
from lies.cli import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_config_command_defaults() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "anthropic:claude-opus-4-7" in result.stdout
    assert "wiki_root" in result.stdout


def test_config_command_overrides(monkeypatch) -> None:
    monkeypatch.setenv("LIES_MODEL", "anthropic:claude-sonnet-5")
    monkeypatch.setenv("LIES_WIKI_ROOT", "/tmp/wiki")
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "anthropic:claude-sonnet-5" in result.stdout
    assert "/tmp/wiki" in result.stdout