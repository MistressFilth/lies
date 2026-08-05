"""Tests for lies init <name>."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli import app


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("LIES_WIKI_NAME", raising=False)


def test_init_creates_xdg_dirs(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init", "mywiki"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "lies" / "mywiki" / "raw").exists()
    assert (tmp_path / "data" / "lies" / "mywiki" / "wiki").exists()
    assert (tmp_path / "config" / "lies" / "mywiki" / "schema.md").exists()
    # git init
    assert (tmp_path / "data" / "lies" / "mywiki" / ".git").exists()


def test_init_rejects_duplicate(tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["init", "mywiki"])
    result = runner.invoke(app, ["init", "mywiki"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_init_rejects_invalid_name(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init", "foo/bar"])
    assert result.exit_code != 0
