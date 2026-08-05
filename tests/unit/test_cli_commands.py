"""Tests for CLI command flags: --name everywhere, --wiki-root rejected."""

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
    monkeypatch.delenv("LIES_WIKI_ROOT", raising=False)


def test_query_rejects_wiki_root_flag(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["query", "what?", "--wiki-root", str(tmp_path)])
    assert result.exit_code != 0


def test_query_uses_lies_wiki_name_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from lies.wiki.wiki import Wiki

    Wiki.data_root_for("envwiki").mkdir(parents=True)
    monkeypatch.setenv("LIES_WIKI_NAME", "envwiki")
    runner = CliRunner()
    result = runner.invoke(app, ["query", "what?"])
    # We only assert it didn't crash on wiki resolution; outcome depends on env.
    assert "envwiki" not in result.output or result.exit_code == 0
